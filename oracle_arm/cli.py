"""命令行入口。

子命令：
  install    安装/更新定时任务（可重复执行），并做一次预检
  run        单轮申请（launchd 调用；也可手动执行）
  try        立刻在终端前台尝试一次（等同于 run --console --force）
  status     显示状态、日志与下次申请时间
  pause      暂停定时任务
  resume     恢复定时任务
  uninstall  卸载后台任务（不删除云端实例）
  doctor     环境与配置体检
  logs       查看日志
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import APP_NAME, __version__
from . import client as client_mod
from . import config as cfg_mod
from . import errors as err_mod
from . import launchd as launchd_mod
from . import runner as runner_mod
from . import telegram as telegram_mod
from . import util


# ── 公共 ───────────────────────────────────────────────────────────────────────
def load_config() -> cfg_mod.Config:
    return cfg_mod.Config.load()


def load_config_or_report(log=None, echo: bool = False) -> tuple[cfg_mod.Config, str]:
    """读取配置；损坏时报告并退回默认值（调用方决定是否继续）。

    配置文件可能因为手工编辑或断电被截断，此时 Config.load 会抛 ValueError。
    绝不能让所有命令（连 status 都算）一起崩掉——那样用户连诊断入口都没有。
    """
    try:
        return cfg_mod.Config.load(), ""
    except (ValueError, OSError) as exc:
        # OSError 也要抓：权限/TCC 问题会让所有命令一起崩，用户连诊断入口都没有
        message = str(exc)
        if log:
            log.error(message)
        if echo:
            print(f"✗ {message}")
            print(f"  修复方法：修正或删除 {cfg_mod.CONFIG_PATH}，然后重新运行「安装.command」。")
        return cfg_mod.Config(), message


def sync_app_dir() -> Path:
    """把源码复制到 ~/.oracle-arm/app。

    后台任务从副本加载代码，避免依赖项目目录：macOS 对 ~/Downloads 有 TCC 限制，
    launchd 里的进程读那里可能被拒绝；同时项目目录被移动/删除也不影响定时任务。
    """
    src = Path(__file__).resolve().parent
    dst = cfg_mod.APP_DIR / "oracle_arm"
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.glob("*.py")):
        shutil.copy2(item, dst / item.name)
    return cfg_mod.APP_DIR


def build_launchd_config(cfg: cfg_mod.Config) -> launchd_mod.LaunchdConfig:
    """构造 plist 配置。已同步过源码副本时用副本目录，否则退回项目目录。"""
    project_dir = Path(__file__).resolve().parent.parent
    app_dir = cfg_mod.APP_DIR
    workdir = app_dir if (app_dir / "oracle_arm").is_dir() else project_dir
    if cfg.schedule_mode == cfg_mod.SCHEDULE_MODE_INTERVAL:
        schedule, interval = [], int(cfg.interval_seconds)
    else:
        schedule, interval = util.beijing_to_local_times(cfg.schedule_beijing), None
    return launchd_mod.LaunchdConfig(
        label=cfg_mod.LAUNCHD_LABEL,
        plist_path=cfg_mod.LAUNCHD_PLIST,
        python=sys.executable,
        workdir=str(workdir),
        schedule=schedule,
        interval_seconds=interval,
    )


def cadence_text(cfg: cfg_mod.Config) -> str:
    """人话描述当前申请节奏（安装、状态、体检共用）。"""
    if cfg.schedule_mode == cfg_mod.SCHEDULE_MODE_INTERVAL:
        return f"每 {cfg.interval_seconds} 秒（约 {cfg.interval_seconds / 60:g} 分钟）一轮，持续抢直到成功"
    return "每天固定时刻：" + "、".join(cfg.schedule_beijing) + "（北京时间）"


def ensure_ssh_key(cfg: cfg_mod.Config, log=None) -> Path | None:
    """确保存在 SSH 密钥对；缺失时生成（私钥不外泄、不进日志）。"""
    priv = Path(cfg.ssh_public_key).expanduser()
    if priv.suffix == ".pub":
        priv = priv.with_suffix("")
    pub = Path(str(priv) + ".pub")
    if pub.exists():
        return pub
    priv.parent.mkdir(parents=True, exist_ok=True)
    if priv.exists():  # 只有私钥没有公钥：由私钥导出公钥
        result = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(priv)], capture_output=True, text=True, check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            pub.write_text(result.stdout.strip() + "\n", encoding="utf-8")
            return pub
    result = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "oracle-arm", "-f", str(priv)],
        capture_output=True, text=True, check=False,
    )
    if not pub.exists():
        # 不能假装成功：否则安装会打印「SSH 公钥：…」，直到预检才以「公钥不存在」暂停
        if log:
            log.error(f"生成 SSH 密钥对失败：{result.stderr.strip() or result.stdout.strip()}")
        return None
    if log:
        log.info(f"已生成 SSH 密钥对：{priv}（公钥 {pub}）")
    return pub


def make_api(cfg: cfg_mod.Config, log):
    """构造 OCI 客户端；认证失败时给出可操作的提示。"""
    try:
        return client_mod.OciArmClient(cfg)
    except Exception as exc:
        classified = err_mod.classify(exc, cfg.rate_limit_default_wait)
        hint = ""
        if classified.kind in (err_mod.AUTH, err_mod.CONFIG):
            hint = (
                f"\n请在 OCI 控制台重新生成 API Key，并把私钥与配置写入 "
                f"{Path(cfg.oci_config_file).expanduser()}（参考 README「首次授权」）。"
            )
        log.error(f"OCI 认证/配置失败：{classified.message}{hint}")
        return None


def _load_state() -> cfg_mod.State:
    return cfg_mod.State.load()


# ── run ────────────────────────────────────────────────────────────────────────
def cmd_run(args) -> int:
    state = _load_state()
    log = util.setup_logger(cfg_mod.LOG_PATH, verbose=args.verbose, to_console=args.console)
    cfg, config_error = load_config_or_report(log, echo=args.console)
    if config_error:
        # 配置坏了绝不能带着默认值继续跑：可能用错区域/规格去创建资源
        state.status = cfg_mod.STATUS_ERROR
        state.reason = config_error
        state.save()
        util.notify("Oracle ARM 配置有问题", config_error, cfg.notification)
        return runner_mod.EXIT_NEEDS_HUMAN

    if state.status == cfg_mod.STATUS_SUCCESS and not args.force:
        log.info("实例已申请成功，定时任务应已停止；本次跳过（如需强制重试请加 --force）")
        return runner_mod.EXIT_OK

    # 互斥锁：防止两轮申请重叠导致重复创建实例
    lock = util.RunLock(cfg_mod.LOCK_PATH)
    if not lock.acquire():
        log.info("上一轮申请仍在进行中，本轮跳过（避免重复创建实例）")
        return runner_mod.EXIT_OK
    try:
        return _run_locked(cfg, state, log, args, lock)
    finally:
        lock.release()


def _run_locked(cfg: cfg_mod.Config, state: cfg_mod.State, log, args,
                lock: util.RunLock) -> int:
    log.info(f"=== 开始一轮申请（{util.beijing_now().isoformat()} 北京时间）===")
    api = make_api(cfg, log)
    if api is None:
        state.status = cfg_mod.STATUS_NEEDS_ATTENTION
        state.reason = "OCI 认证或配置失败，请检查 ~/.oci/config 与 API Key"
        state.save()
        util.notify("Oracle ARM 申请需要处理", state.reason, cfg.notification)
        return runner_mod.EXIT_NEEDS_HUMAN

    def heartbeat_sleep(seconds: float) -> None:
        """睡觉前刷新运行锁：一轮可能睡很久（退避/等 RUNNING/轮询 IP），
        不刷新的话下一轮会把还活着的一轮误判成残留锁并接管。"""
        lock.touch()
        time.sleep(seconds)

    def stop_schedule() -> None:
        """停止定时任务。

        必须先释放运行锁：launchd 的 bootout 会终止当前进程，
        若把释放留给 finally，锁文件会残留，导致恢复后的轮次被静默跳过。
        """
        lock.release()
        launchd_mod.pause(cfg_mod.LAUNCHD_LABEL)

    run = runner_mod.Runner(
        cfg=cfg,
        state=state,
        api=api,
        log=log,
        notifier=util.notify,
        stop_schedule=stop_schedule,
        sleep=heartbeat_sleep,
    )
    result = run.run_once(dry_run=args.dry_run)
    log.info(f"=== 本轮结束（exit={result.exit_code}）：{result.summary} ===")
    if args.console:
        print(result.summary)
    return result.exit_code


# ── install / uninstall ────────────────────────────────────────────────────────
def cmd_install(args) -> int:
    log = util.setup_logger(cfg_mod.LOG_PATH, verbose=args.verbose, to_console=True)
    cfg_mod.BASE_DIR.mkdir(parents=True, exist_ok=True)
    cfg_mod.LOG_DIR.mkdir(parents=True, exist_ok=True)

    cfg, config_error = load_config_or_report(echo=True)
    if config_error:
        # 绝不能用默认配置继续：会把用户自定义的显示名/时刻/子网静默丢掉，
        # 装出一个「看起来成功、实际按默认配置跑」的定时任务
        print("✗ 配置文件有问题，已放弃安装。请先修正它，再重新运行安装。")
        return 1
    if not cfg_mod.CONFIG_PATH.exists():
        cfg.save()
        print(f"已生成默认配置：{cfg_mod.CONFIG_PATH}")

    interactive_setup(cfg)

    pub = ensure_ssh_key(cfg, log)
    if pub is None:
        print("✗ 生成 SSH 密钥对失败，已放弃安装。请检查 ~/.ssh 目录权限后重试。")
        return 1
    print(f"SSH 公钥：{pub}")

    app_dir = sync_app_dir()
    print(f"源码副本：{app_dir}（后台任务从这里运行，不依赖项目目录）")

    ld_cfg = build_launchd_config(cfg)
    if not ld_cfg.schedule and not ld_cfg.interval_seconds:
        # 两种模式都没有有效触发条件时不能注册：plist 里没有任何触发器等于永远不跑
        print("✗ 申请节奏配置无效（固定时刻里没有合法的 HH:MM，间隔也不合法），已放弃注册定时任务。")
        print(f"  请修正 {cfg_mod.CONFIG_PATH} 后重新运行安装。")
        return 1
    try:
        path = launchd_mod.install(ld_cfg)
    except Exception as exc:
        print(f"✗ 注册定时任务失败：{exc}")
        return 1
    print(f"✓ 定时任务已注册：{path}")
    print(f"  申请节奏：{cadence_text(cfg)}")
    if ld_cfg.interval_seconds:
        print(f"  说明：每轮会依次尝试该区域的每个 AD 各一次；"
              f"容量不足就等下一个 {ld_cfg.interval_seconds} 秒")
    else:
        rejected = [t for t in cfg.schedule_beijing
                    if not any(f"{h:02d}:{m:02d}" == t for h, m in
                               util.beijing_to_local_times([t]))]
        if rejected:
            # 部分时刻非法时不能默默少装几条：用户会以为它们生效了
            print(f"  ⚠ 以下时刻格式非法、已忽略：{'、'.join(rejected)}（应为 HH:MM）")
        local = "、".join(f"{h:02d}:{m:02d}" for h, m in ld_cfg.schedule)
        print(f"  本机时区对应时刻：{local}")
    if not launchd_mod.is_loaded(cfg_mod.LAUNCHD_LABEL):
        print("✗ 定时任务未处于加载状态，请运行「状态.command」查看原因")
        return 1

    print("\n执行一次预检（不会创建实例）…")
    api = make_api(cfg, log)
    if api is None:
        print("✗ OCI 认证失败，请先完成 API Key 配置，然后重新运行本安装脚本。")
        return 1
    state = _load_state()
    run = runner_mod.Runner(cfg=cfg, state=state, api=api, log=log, notifier=util.notify,
                            stop_schedule=lambda: launchd_mod.pause(cfg_mod.LAUNCHD_LABEL))
    result = run.run_once(dry_run=True)
    print(("✓ " if result.ok else "✗ ") + result.summary)
    if not result.ok:
        print("  定时任务已注册，但预检未通过；按上面提示修复后运行「状态.command」恢复。")
        launchd_mod.pause(cfg_mod.LAUNCHD_LABEL)
        return result.exit_code
    print("\n安装完成。之后无需人工干预；拿到公网 IPv4 后定时任务会自动停止。")
    return 0


def print_oci_setup_guide(oci_config: Path) -> None:
    """首次授权引导：告诉用户怎么把 API Key 配好（不保存网页登录密码）。"""
    print(f"\n⚠ 未找到 OCI 配置文件：{oci_config}")
    print("  首次授权步骤（只需做一次，之后无需再登录 Oracle 网页）：")
    print("   1) 登录 OCI 控制台 → 右上角头像 → My profile → API keys → Add API key")
    print("   2) 选择 Generate API key pair，下载私钥（.pem），把公钥内容粘贴进控制台")
    print("   3) 控制台会显示 Configuration file preview，把它保存成下面的文件：")
    print(f"      {oci_config}")
    print("      内容形如（region 必须是 ap-singapore-1）：")
    print("        [DEFAULT]")
    print("        user=ocid1.user.oc1..xxxx")
    print("        fingerprint=xx:xx:xx:...")
    print("        tenancy=ocid1.tenancy.oc1..xxxx")
    print("        region=ap-singapore-1")
    print("        key_file=~/.oci/oci_api_key.pem")
    print("   4) 私钥必须是 PEM 格式（文件里有 BEGIN PRIVATE KEY 字样），权限设为 600")
    print(f"   5) 完成后重新运行安装脚本。\n")


def interactive_setup(cfg: cfg_mod.Config) -> None:
    """首次配置引导：检查 OCI 配置与 compartment，缺失时在终端询问。"""
    oci_config = Path(cfg.oci_config_file).expanduser()
    if not oci_config.exists():
        print_oci_setup_guide(oci_config)
        return

    if cfg.compartment_id:
        return
    if not sys.stdin.isatty():
        return
    print(f"\n当前将使用 tenancy 根 Compartment 创建实例。")
    answer = input("如需指定子 Compartment，请粘贴其 OCID（直接回车用根 Compartment）：").strip()
    if answer:
        cfg.compartment_id = answer
        cfg.save()
        print(f"✓ 已保存 compartment_id")


def cmd_uninstall(args) -> int:
    log = util.setup_logger(cfg_mod.LOG_PATH, verbose=args.verbose, to_console=True)
    removed = launchd_mod.uninstall()
    print("✓ 已卸载后台定时任务" if removed else "· 未发现已安装的定时任务（已清理）")
    print("  云端实例与配置、日志均已保留，未做任何删除。")
    print(f"  配置：{cfg_mod.CONFIG_PATH}\n  日志：{cfg_mod.LOG_DIR}")
    print("  如需彻底清理：rm -rf " + str(cfg_mod.BASE_DIR))
    log.info("已卸载定时任务（云端实例未受影响）")
    return 0


# ── pause / resume ─────────────────────────────────────────────────────────────
def cmd_pause(args) -> int:
    cfg, _ = load_config_or_report(echo=True)
    state = _load_state()
    launchd_mod.pause(cfg_mod.LAUNCHD_LABEL)
    state.status = cfg_mod.STATUS_PAUSED
    state.reason = "用户手动暂停"
    state.save()
    print("✓ 已暂停自动申请（云端实例未受影响）")
    return 0


def cmd_resume(args) -> int:
    cfg, config_error = load_config_or_report(echo=True)
    if config_error:
        print("✗ 配置文件有问题，已放弃恢复。请先修正它，再重新运行安装。")
        return 1
    state = _load_state()
    if state.status == cfg_mod.STATUS_SUCCESS:
        # 允许恢复：预检会发现实例仍然存在并直接确认，不会重复创建；
        # 若用户已删除实例，恢复后就能重新申请。
        print("· 上次已申请成功；恢复后预检会先确认实例是否仍存在，不会重复创建。")
    ld_cfg = build_launchd_config(cfg)
    try:
        launchd_mod.resume(ld_cfg)
    except Exception as exc:
        print(f"✗ 恢复失败：{exc}")
        return 1
    state.status = cfg_mod.STATUS_IDLE
    state.reason = ""
    state.save()
    print("✓ 已恢复自动申请，将在下一个申请时刻运行。")
    return 0


# ── status ─────────────────────────────────────────────────────────────────────
def _fmt_delta(target) -> str:
    delta = target - util.beijing_now()
    total = int(delta.total_seconds())
    if total < 0:
        return "已过"
    hours, rem = divmod(total, 3600)
    return f"{hours} 小时 {rem // 60} 分钟"


def cmd_status(args) -> int:
    cfg, config_error = load_config_or_report(echo=False)
    state = _load_state()
    ld_cfg = build_launchd_config(cfg)
    loaded = launchd_mod.is_loaded(cfg_mod.LAUNCHD_LABEL)

    print(f"=== {APP_NAME} v{__version__} 状态 ===")
    if config_error:
        print(f"✗ 配置文件损坏：{config_error}")
        print(f"  修复方法：修正或删除 {cfg_mod.CONFIG_PATH}，然后重新运行「安装.command」。")
    print(f"目标：{cfg_mod.TARGET_REGION} / {cfg_mod.TARGET_SHAPE} / "
          f"{cfg_mod.TARGET_OCPUS} OCPU / {cfg_mod.TARGET_MEMORY_GB}GB / "
          f"{cfg_mod.TARGET_BOOT_GB}GB 启动盘 / 公网 IPv4 必需")
    print(f"IPv6：{'开启' if cfg.enable_ipv6 else '关闭'}")
    # 电报只显示「配没配」，绝不打印 token 本身
    tg_on = bool(cfg.telegram_bot_token and cfg.telegram_chat_id)
    print(f"电报通知：{'已配置（抢到后发消息）' if tg_on else '未配置（抢到后只发 macOS 通知）'}")
    print(f"配置文件：{cfg_mod.CONFIG_PATH}")
    print(f"OCI 配置：{Path(cfg.oci_config_file).expanduser()}（profile {cfg.oci_profile}）")

    if loaded:
        print("定时任务：已加载（运行中）")
    elif cfg_mod.LAUNCHD_PLIST.exists():
        print("定时任务：已安装但未加载（暂停中）")
    else:
        print("定时任务：未安装")
    if cfg.schedule_mode == cfg_mod.SCHEDULE_MODE_INTERVAL:
        print(f"申请节奏：{cadence_text(cfg)}")
        if loaded and state.status not in (cfg_mod.STATUS_SUCCESS,):
            print(f"下次申请：约 {cfg.interval_seconds} 秒内（由 launchd 的 StartInterval 触发）")
    else:
        print("申请时刻（北京时间）：" + "、".join(cfg.schedule_beijing))
        local = "、".join(f"{h:02d}:{m:02d}" for h, m in ld_cfg.schedule)
        print(f"申请时刻（本机时区）：{local}")
        if loaded and state.status not in (cfg_mod.STATUS_SUCCESS,):
            nxt = util.next_run_time(cfg.schedule_beijing)
            if nxt is None:
                print("下次申请：无法计算（申请时刻配置全部非法，请修正后重装）")
            else:
                print(f"下次申请：{nxt.strftime('%Y-%m-%d %H:%M')}（北京时间，还有 {_fmt_delta(nxt)}）")

    print(f"\n状态：{state.status}")
    if state.reason:
        print(f"原因：{state.reason}")
    if state.last_result:
        print(f"上次结果：{state.last_result}")
    if state.last_attempt_at:
        print(f"上次尝试：{state.last_attempt_at}（北京时间）")
    print(f"累计尝试：{state.attempts} 轮；容量不足：{state.capacity_misses} 次")
    if state.quota_summary:
        print(f"额度（Limits API）：{state.quota_summary}")
    if state.plan_type:
        print(f"账号套餐：{state.plan_type}")
    if state.instance_id:
        print(f"实例 OCID：{state.instance_id}")
    if state.public_ip:
        print(f"公网 IPv4：{state.public_ip}")

    print("\n--- 最近日志 ---")
    for line in tail_log(8):
        print(line)
    return 0


def tail_log(n: int) -> list[str]:
    if not cfg_mod.LOG_PATH.exists():
        return ["（暂无日志）"]
    lines = cfg_mod.LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-n:] or ["（暂无日志）"]


# ── doctor / logs ──────────────────────────────────────────────────────────────
def cmd_doctor(args) -> int:
    cfg, config_error = load_config_or_report(echo=False)
    log = util.setup_logger(cfg_mod.LOG_PATH, verbose=True, to_console=True)
    print(f"=== {APP_NAME} 体检 ===")
    print(f"Python：{sys.executable}")
    print(f"本机时区：{util.local_now().strftime('%Z %z')}")
    print(f"北京时间现在：{util.beijing_now().strftime('%Y-%m-%d %H:%M:%S')}")

    problems = cfg.validate()
    if config_error:
        problems.insert(0, f"配置文件损坏：{config_error}")
    for p in problems:
        print(f"✗ {p}")
    pub = Path(cfg.ssh_public_key).expanduser()
    print(("✓" if pub.exists() else "✗") + f" SSH 公钥：{pub}")
    oci_config = Path(cfg.oci_config_file).expanduser()
    print(("✓" if oci_config.exists() else "✗") + f" OCI 配置：{oci_config}")
    if not oci_config.exists():
        print_oci_setup_guide(oci_config)

    api = make_api(cfg, log)
    if api is None:
        return runner_mod.EXIT_NEEDS_HUMAN
    print("✓ OCI 认证通过")

    state = _load_state()
    run = runner_mod.Runner(cfg=cfg, state=state, api=api, log=log, notifier=util.notify,
                            stop_schedule=lambda: None)
    result = run.run_once(dry_run=True)
    print(("✓ " if result.ok else "✗ ") + result.summary)
    return result.exit_code


def cmd_logs(args) -> int:
    for line in tail_log(args.lines):
        print(line)
    return 0


def cmd_telegram_test(args) -> int:
    """发一条测试消息，确认 token / chat_id / 代理都对。

    存在的意义：抢到实例是「只发生一次」的事件，等到那一刻才发现电报发不出去就晚了。
    必须在真正抢到之前先验证通路。
    """
    log = util.setup_logger(cfg_mod.LOG_PATH, verbose=True, to_console=False)
    cfg, config_error = load_config_or_report(echo=True)
    if config_error:
        return 1
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        print("✗ 未配置电报：请先在配置里填 telegram_bot_token 与 telegram_chat_id")
        print(f"  配置文件：{cfg_mod.CONFIG_PATH}")
        return 1
    text = (
        "✅ Oracle ARM 电报通知测试\n"
        f"目标规格：{cfg_mod.TARGET_OCPUS} OCPU / {cfg_mod.TARGET_MEMORY_GB}GB\n"
        "收到这条说明抢到实例后你也会收到通知。"
    )
    ok = telegram_mod.TelegramNotifier(cfg, log=log).send(text)
    if ok:
        print("✓ 电报发送成功，请查看手机")
        return 0
    print("✗ 电报发送失败，原因见日志：")
    print(f"  {cfg_mod.LOG_PATH}")
    print("  常见原因：需要代理但 telegram_proxy 没填或代理没开、token/chat_id 不对、"
          "机器人没被拉进该群或没被 /start 过")
    return 1


# ── 入口 ───────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oracle_arm", description="Oracle 免费 ARM 实例自动申请")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="单轮申请（定时任务调用）")
    p_run.add_argument("--dry-run", action="store_true", help="只做预检，不创建实例")
    p_run.add_argument("--force", action="store_true", help="已成功时也强制尝试")
    p_run.add_argument("--verbose", action="store_true")
    p_run.add_argument("--console", action="store_true", help="同时输出到终端")
    p_run.set_defaults(func=cmd_run)

    p_try = sub.add_parser("try", help="立刻在终端前台尝试一次")
    p_try.add_argument("--dry-run", action="store_true")
    p_try.add_argument("--verbose", action="store_true", default=True)
    p_try.set_defaults(func=cmd_run, force=True, console=True)

    p_install = sub.add_parser("install", help="注册/更新定时任务")
    p_install.add_argument("--verbose", action="store_true")
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="卸载定时任务")
    p_uninstall.add_argument("--verbose", action="store_true")
    p_uninstall.set_defaults(func=cmd_uninstall)

    sub.add_parser("pause", help="暂停自动申请").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="恢复自动申请").set_defaults(func=cmd_resume)
    sub.add_parser("status", help="显示状态").set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="环境与配置体检")
    p_doctor.set_defaults(func=cmd_doctor)

    p_logs = sub.add_parser("logs", help="查看日志")
    p_logs.add_argument("-n", "--lines", type=int, default=30)
    p_logs.set_defaults(func=cmd_logs)

    sub.add_parser("telegram-test", help="发一条电报测试消息").set_defaults(
        func=cmd_telegram_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    # 统一补齐可选属性，便于子命令共享
    for attr, default in (("dry_run", False), ("force", False), ("verbose", False),
                          ("console", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    return args.func(args)
