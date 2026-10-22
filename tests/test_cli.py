"""命令行端到端测试（launchctl 与 OCI 全部打桩，不会注册真实任务、不会创建实例）。"""

import subprocess

import pytest

from oracle_arm import cli
from oracle_arm import config as cfg_mod
from oracle_arm import launchd as launchd_mod
from oracle_arm import runner as runner_mod
from oracle_arm import util as util_mod
from tests.conftest import FAKE_NOTIFICATIONS, FakeApi, oci_error


def _ok(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.fixture
def env(tmp_path, monkeypatch, ssh_key):
    """把所有落盘路径指到临时目录，并拦截 launchctl。"""
    # 落盘路径已由 conftest 的 autouse 夹具隔离到临时目录
    base = cfg_mod.BASE_DIR
    oci_config = tmp_path / "oci" / "config"
    oci_config.parent.mkdir(parents=True, exist_ok=True)
    oci_config.write_text("[DEFAULT]\nregion=ap-singapore-1\n", encoding="utf-8")

    calls = []
    loaded = {"value": True}

    def fake_run(args):
        calls.append(args)
        if "print" in args:
            return _ok(0 if loaded["value"] else 1)
        return _ok()

    monkeypatch.setattr(launchd_mod, "_run", fake_run)

    cfg = cfg_mod.Config(ssh_public_key=str(ssh_key), oci_config_file=str(oci_config))
    cfg.save(cfg_mod.CONFIG_PATH)
    return {"calls": calls, "loaded": loaded, "cfg": cfg, "base": base}


def _patch_api(monkeypatch, api):
    monkeypatch.setattr(cli, "make_api", lambda cfg, log: api)


def test_install_registers_job_and_runs_dry_run(env, monkeypatch, capsys):
    _patch_api(monkeypatch, FakeApi())
    code = cli.main(["install"])

    assert code == 0
    out = capsys.readouterr().out
    assert "定时任务已注册" in out
    assert "每 300 秒" in out          # 默认持续抢模式
    assert "安装完成" in out
    assert cfg_mod.LAUNCHD_PLIST.exists()
    assert any("bootstrap" in c for c in env["calls"])


def test_install_twice_does_not_create_duplicate_jobs(env, monkeypatch, capsys):
    _patch_api(monkeypatch, FakeApi())
    cli.main(["install"])
    cli.main(["install"])
    capsys.readouterr()

    assert sum(1 for c in env["calls"] if "bootstrap" in c) == 2
    assert sum(1 for c in env["calls"] if "bootout" in c) == 2   # 每次先卸载旧的
    assert len(list(cfg_mod.LAUNCHD_PLIST.parent.glob("*.plist"))) == 1


def test_install_pauses_job_when_preflight_fails(env, monkeypatch, capsys):
    """预检不通过时不应留在「运行中」状态反复撞墙。"""
    api = FakeApi(subnet=None)
    api.find_public_subnet = lambda: None
    _patch_api(monkeypatch, api)

    code = cli.main(["install"])
    capsys.readouterr()
    assert code == runner_mod.EXIT_NEEDS_HUMAN
    assert any("bootout" in c for c in env["calls"])


def test_install_creates_private_config_file(env, monkeypatch, capsys):
    _patch_api(monkeypatch, FakeApi())
    cli.main(["install"])
    capsys.readouterr()
    assert cfg_mod.CONFIG_PATH.exists()
    assert oct(cfg_mod.CONFIG_PATH.stat().st_mode)[-3:] == "600"


def test_install_fails_when_oci_auth_fails(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_api", lambda cfg, log: None)
    assert cli.main(["install"]) == 1
    assert "OCI 认证失败" in capsys.readouterr().out


def test_uninstall_removes_job_but_keeps_config(env, monkeypatch, capsys):
    _patch_api(monkeypatch, FakeApi())
    cli.main(["install"])
    capsys.readouterr()

    assert cli.main(["uninstall"]) == 0
    out = capsys.readouterr().out
    assert "已卸载后台定时任务" in out
    assert "云端实例与配置、日志均已保留" in out
    assert not cfg_mod.LAUNCHD_PLIST.exists()
    assert cfg_mod.CONFIG_PATH.exists()          # 配置不删


def test_uninstall_is_safe_when_not_installed(env, capsys):
    assert cli.main(["uninstall"]) == 0
    assert "未发现已安装的定时任务" in capsys.readouterr().out


def test_pause_and_resume(env, monkeypatch, capsys):
    _patch_api(monkeypatch, FakeApi())
    cli.main(["install"])
    capsys.readouterr()

    assert cli.main(["pause"]) == 0
    capsys.readouterr()
    state = cfg_mod.State.load(cfg_mod.STATE_PATH)
    assert state.status == cfg_mod.STATUS_PAUSED
    assert any("bootout" in c for c in env["calls"])
    assert cfg_mod.LAUNCHD_PLIST.exists()        # 暂停不删除 plist

    assert cli.main(["resume"]) == 0
    capsys.readouterr()
    assert cfg_mod.State.load(cfg_mod.STATE_PATH).status == cfg_mod.STATUS_IDLE
    assert sum(1 for c in env["calls"] if "bootstrap" in c) >= 2


def test_resume_after_success_resets_state_but_warns(env, monkeypatch, capsys):
    """成功后仍允许恢复（例如用户删掉了实例要重新申请）；预检会防止重复创建。"""
    _patch_api(monkeypatch, FakeApi())
    cli.main(["install"])
    capsys.readouterr()
    state = cfg_mod.State(status=cfg_mod.STATUS_SUCCESS, public_ip="203.0.113.9")
    state.save(cfg_mod.STATE_PATH)

    assert cli.main(["resume"]) == 0
    out = capsys.readouterr().out
    assert "不会重复创建" in out
    assert cfg_mod.State.load(cfg_mod.STATE_PATH).status == cfg_mod.STATUS_IDLE


def test_install_copies_sources_outside_project(env, monkeypatch, capsys):
    """后台任务从 ~/.oracle-arm/app 加载代码，不依赖项目目录（避开 ~/Downloads 的 TCC 限制）。"""
    import plistlib
    _patch_api(monkeypatch, FakeApi())
    cli.main(["install"])
    capsys.readouterr()

    app_dir = cfg_mod.APP_DIR
    assert (app_dir / "oracle_arm" / "runner.py").exists()
    assert (app_dir / "oracle_arm" / "__main__.py").exists()
    data = plistlib.loads(cfg_mod.LAUNCHD_PLIST.read_bytes())
    assert data["WorkingDirectory"] == str(app_dir)
    assert data["EnvironmentVariables"]["PYTHONPATH"] == str(app_dir)


def test_status_shows_target_and_cadence(env, capsys):
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "ap-singapore-1" in out
    assert "2 OCPU / 12GB" in out
    assert "公网 IPv4 必需" in out
    assert "每 300 秒" in out and "持续抢" in out


def test_status_shows_fixed_times_in_fixed_mode(env, capsys):
    import json
    data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["schedule_mode"] = "fixed"
    cfg_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")

    cli.main(["status"])
    out = capsys.readouterr().out
    assert "02:05" in out and "20:45" in out


def test_status_shows_instance_and_next_run(env, monkeypatch, capsys):
    state = cfg_mod.State(status=cfg_mod.STATUS_IDLE, attempts=3, capacity_misses=2,
                          public_ip="203.0.113.77", instance_id="ocid1.instance.x")
    state.save(cfg_mod.STATE_PATH)
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "203.0.113.77" in out
    assert "ocid1.instance.x" in out
    assert "下次申请" in out
    assert "累计尝试：3 轮" in out


def test_run_skips_when_already_successful(env, monkeypatch, capsys):
    state = cfg_mod.State(status=cfg_mod.STATUS_SUCCESS, public_ip="203.0.113.1")
    state.save(cfg_mod.STATE_PATH)

    def boom(cfg, log):
        raise AssertionError("已成功时不应再调用 OCI")

    monkeypatch.setattr(cli, "make_api", boom)
    assert cli.main(["run"]) == runner_mod.EXIT_OK
    assert "已申请成功" in cfg_mod.LOG_PATH.read_text(encoding="utf-8")


def test_run_marks_needs_attention_when_auth_fails(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_api", lambda cfg, log: None)
    assert cli.main(["run"]) == runner_mod.EXIT_NEEDS_HUMAN
    state = cfg_mod.State.load(cfg_mod.STATE_PATH)
    assert state.status == cfg_mod.STATUS_NEEDS_ATTENTION
    assert "认证" in state.reason


def test_run_does_not_log_private_key(env, monkeypatch):
    _patch_api(monkeypatch, FakeApi())
    cli.main(["run"])
    text = cfg_mod.LOG_PATH.read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in text


def test_try_command_forces_attempt(env, monkeypatch, capsys):
    """「立即尝试」即使在已成功状态下也强制跑一次（用户主动发起）。"""
    state = cfg_mod.State(status=cfg_mod.STATUS_SUCCESS)
    state.save(cfg_mod.STATE_PATH)
    api = FakeApi()
    _patch_api(monkeypatch, api)

    cli.main(["try"])
    assert api.launch_calls or api.list_instances_calls


def test_logs_command_prints_tail(env, capsys):
    cfg_mod.LOG_DIR.mkdir(parents=True, exist_ok=True)
    cfg_mod.LOG_PATH.write_text("\n".join(f"line{i}" for i in range(100)), encoding="utf-8")
    cli.main(["logs", "-n", "3"])
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["line97", "line98", "line99"]


def test_run_skips_when_another_round_is_running(env, monkeypatch):
    """已有正在跑的一轮 → 直接跳过，避免两个实例被创建出来。"""
    cfg_mod.LOCK_PATH.write_text("12345 running", encoding="utf-8")

    def boom(cfg, log):
        raise AssertionError("有锁在时不应调用 OCI")

    monkeypatch.setattr(cli, "make_api", boom)
    assert cli.main(["run"]) == runner_mod.EXIT_OK
    assert "仍在进行中" in cfg_mod.LOG_PATH.read_text(encoding="utf-8")


def test_run_releases_lock_after_finish(env, monkeypatch):
    _patch_api(monkeypatch, FakeApi())
    cli.main(["run"])
    assert not cfg_mod.LOCK_PATH.exists()


# ── 配置文件损坏（手工编辑/断电截断都可能造成） ─────────────────────────────────
def test_broken_config_does_not_crash_status(env, capsys):
    """配置坏了也必须能看状态，否则用户连诊断入口都没有。"""
    cfg_mod.CONFIG_PATH.write_text("{broken", encoding="utf-8")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "配置文件损坏" in out
    assert "安装.command" in out


def test_broken_config_does_not_crash_doctor(env, capsys):
    cfg_mod.CONFIG_PATH.write_text("{broken", encoding="utf-8")
    cli.main(["doctor"])
    assert "配置文件损坏" in capsys.readouterr().out


def test_broken_config_does_not_crash_pause_uninstall(env, capsys):
    cfg_mod.CONFIG_PATH.write_text("{broken", encoding="utf-8")
    assert cli.main(["pause"]) == 0
    assert cli.main(["uninstall"]) == 0
    capsys.readouterr()


def test_broken_config_stops_run_without_touching_oci(env, monkeypatch, capsys):
    """配置坏了绝不能带着默认值继续跑 —— 可能用错区域/规格去创建资源。"""
    cfg_mod.CONFIG_PATH.write_text("{broken", encoding="utf-8")

    def boom(cfg, log):
        raise AssertionError("配置损坏时不应调用 OCI")

    monkeypatch.setattr(cli, "make_api", boom)
    assert cli.main(["run"]) == runner_mod.EXIT_NEEDS_HUMAN
    capsys.readouterr()
    state = cfg_mod.State.load(cfg_mod.STATE_PATH)
    assert state.status == cfg_mod.STATUS_ERROR
    assert "损坏" in state.reason


def test_broken_schedule_does_not_crash_status(env, capsys):
    """申请时刻写错时 status 仍要能打印（原来会先抛 ValueError）。"""
    import json
    data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["schedule_beijing"] = ["25:00", "abc"]
    cfg_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")

    assert cli.main(["status"]) == 0
    capsys.readouterr()


def test_lock_released_before_stopping_schedule(env, monkeypatch, capsys):
    """停止定时任务前必须先释放运行锁：bootout 会终止进程，finally 可能跑不到。"""
    _patch_api(monkeypatch, FakeApi(launch_behaviors=["ocid1.instance.ok"]))
    assert cli.main(["run"]) == runner_mod.EXIT_OK
    capsys.readouterr()

    assert not cfg_mod.LOCK_PATH.exists()
    assert any("bootout" in c for c in env["calls"])
    assert cfg_mod.State.load(cfg_mod.STATE_PATH).status == cfg_mod.STATUS_SUCCESS


def test_sleep_heartbeats_the_lock(env, monkeypatch, capsys):
    """退避睡眠时必须刷新运行锁（否则下一轮会接管并可能并发创建）。"""
    from oracle_arm import util as util_mod
    touches = []
    monkeypatch.setattr(util_mod.RunLock, "touch", lambda self: touches.append(1))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)   # 不让测试真的等 5 秒
    _patch_api(monkeypatch, FakeApi(ads=["AD-1"], launch_behaviors=[
        oci_error(429, "TooManyRequests", "slow", {"retry-after": "5"}),
        "ocid1.instance.ok",
    ]))
    cli.main(["run"])
    capsys.readouterr()
    assert touches


def test_install_refuses_when_schedule_is_all_invalid(env, monkeypatch, capsys):
    """固定模式下时刻全部非法时不能注册任务：plist 里没有触发器等于永远不跑。"""
    import json
    data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["schedule_mode"] = "fixed"
    data["schedule_beijing"] = ["25:00"]
    cfg_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")

    _patch_api(monkeypatch, FakeApi())
    assert cli.main(["install"]) == 1
    out = capsys.readouterr().out
    assert "申请节奏配置无效" in out
    assert not any("bootstrap" in c for c in env["calls"])
    assert not cfg_mod.LAUNCHD_PLIST.exists()


@pytest.mark.parametrize("content", ["null", "[]", '"x"'])
def test_non_object_config_does_not_crash_any_command(env, monkeypatch, capsys, content):
    """JSON 合法但不是对象（null/[]/"x"）同样不能让所有命令裸崩。"""
    cfg_mod.CONFIG_PATH.write_text(content, encoding="utf-8")
    monkeypatch.setattr(cli, "make_api", lambda cfg, log: None)

    assert cli.main(["status"]) == 0
    assert cli.main(["doctor"]) in (0, runner_mod.EXIT_NEEDS_HUMAN)
    assert cli.main(["pause"]) == 0
    capsys.readouterr()


@pytest.mark.parametrize("content", ["null", "[]", '"x"'])
def test_non_object_state_does_not_crash_run(env, monkeypatch, capsys, content):
    """state.json 是 cmd_run 读的第一个文件，坏了也必须能走完流程。"""
    cfg_mod.STATE_PATH.write_text(content, encoding="utf-8")
    _patch_api(monkeypatch, FakeApi(launch_behaviors=["ocid1.instance.ok"]))
    assert cli.main(["run"]) == runner_mod.EXIT_OK
    capsys.readouterr()


def test_install_refuses_when_config_broken(env, monkeypatch, capsys):
    """配置损坏时绝不能用默认值注册任务：用户自定义的显示名/时刻会被静默丢掉。"""
    import json
    data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["display_name"] = "my-special-arm"
    data["schedule_beijing"] = ["03:33"]
    cfg_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")
    # 再把它弄坏（保留内容不可解析）
    cfg_mod.CONFIG_PATH.write_text("{broken", encoding="utf-8")

    _patch_api(monkeypatch, FakeApi())
    assert cli.main(["install"]) == 1
    out = capsys.readouterr().out
    assert "已放弃安装" in out
    assert not cfg_mod.LAUNCHD_PLIST.exists()
    assert not any("bootstrap" in c for c in env["calls"])


def test_resume_refuses_when_config_broken(env, monkeypatch, capsys):
    cfg_mod.CONFIG_PATH.write_text("{broken", encoding="utf-8")
    assert cli.main(["resume"]) == 1
    assert "已放弃恢复" in capsys.readouterr().out


def test_install_reports_rejected_schedule_entries(env, monkeypatch, capsys):
    """部分时刻非法时要显式列出来，不能让用户以为它生效了。"""
    import json
    data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["schedule_mode"] = "fixed"
    data["schedule_beijing"] = ["02:05", "25:00", "08:05"]
    cfg_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")

    _patch_api(monkeypatch, FakeApi())
    cli.main(["install"])
    out = capsys.readouterr().out
    assert "已忽略" in out and "25:00" in out
    import plistlib
    entries = plistlib.loads(cfg_mod.LAUNCHD_PLIST.read_bytes())["StartCalendarInterval"]
    assert entries == [{"Hour": 2, "Minute": 5}, {"Hour": 8, "Minute": 5}]


def test_install_reports_ssh_keygen_failure(env, monkeypatch, capsys, tmp_path):
    """ssh-keygen 失败不能让安装假装成功。"""
    import subprocess as sp
    cfg = cfg_mod.Config.load(cfg_mod.CONFIG_PATH)
    cfg.ssh_public_key = str(tmp_path / "sshdir" / "k.pub")
    cfg.save(cfg_mod.CONFIG_PATH)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess([], 1, "", "boom"))

    assert cli.main(["install"]) == 1
    assert "生成 SSH 密钥对失败" in capsys.readouterr().out


def test_install_writes_start_interval_by_default(env, monkeypatch, capsys):
    """默认（持续抢）模式下 plist 必须是 StartInterval=300。"""
    import plistlib
    _patch_api(monkeypatch, FakeApi())
    assert cli.main(["install"]) == 0
    capsys.readouterr()

    data = plistlib.loads(cfg_mod.LAUNCHD_PLIST.read_bytes())
    assert data["StartInterval"] == 300
    assert "StartCalendarInterval" not in data


def test_install_honours_custom_interval(env, monkeypatch, capsys):
    import json
    import plistlib
    data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["interval_seconds"] = 600
    cfg_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")

    _patch_api(monkeypatch, FakeApi())
    cli.main(["install"])
    out = capsys.readouterr().out
    assert "每 600 秒" in out
    assert plistlib.loads(cfg_mod.LAUNCHD_PLIST.read_bytes())["StartInterval"] == 600


def test_install_writes_calendar_entries_in_fixed_mode(env, monkeypatch, capsys):
    """切回固定模式后仍按 4 个北京时间时刻注册。"""
    import json
    import plistlib
    data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["schedule_mode"] = "fixed"
    cfg_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")

    _patch_api(monkeypatch, FakeApi())
    cli.main(["install"])
    out = capsys.readouterr().out
    assert "02:05" in out and "20:45" in out

    plist = plistlib.loads(cfg_mod.LAUNCHD_PLIST.read_bytes())
    assert "StartInterval" not in plist
    assert plist["StartCalendarInterval"] == [
        {"Hour": 2, "Minute": 5}, {"Hour": 4, "Minute": 55},
        {"Hour": 8, "Minute": 5}, {"Hour": 20, "Minute": 45},
    ]


def test_reinstall_switches_mode_without_leaving_old_trigger(env, monkeypatch, capsys):
    """换模式重装不能留下旧触发器（StartInterval 与 StartCalendarInterval 互斥）。"""
    import json
    import plistlib
    _patch_api(monkeypatch, FakeApi())
    cli.main(["install"])
    capsys.readouterr()
    assert "StartInterval" in plistlib.loads(cfg_mod.LAUNCHD_PLIST.read_bytes())

    data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["schedule_mode"] = "fixed"
    cfg_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")
    cli.main(["install"])
    capsys.readouterr()

    plist = plistlib.loads(cfg_mod.LAUNCHD_PLIST.read_bytes())
    assert "StartInterval" not in plist
    assert len(plist["StartCalendarInterval"]) == 4


def test_tests_never_fire_real_notifications(env, monkeypatch, capsys):
    """回归：测试绝不能真的弹 macOS 通知。

    起因：用户收到过一条「Oracle ARM 申请需要处理 / 配置有问题：申请时刻格式错误
    '25:00'」的系统通知 —— 那是测试用故意写错的时刻跑 cli.main(["install"]) 时，
    经由真实的 util.notify 发出去的，与用户的实际配置无关。
    """
    import json
    # 部分时刻非法：安装本身能过（plist 里还有合法时刻），但随后的预检会判配置有问题
    data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["schedule_mode"] = "fixed"
    data["schedule_beijing"] = ["02:05", "25:00", "08:05"]
    cfg_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")

    _patch_api(monkeypatch, FakeApi())
    cli.main(["install"])
    capsys.readouterr()

    # 走到了通知分支，但落在假实现里（真实现会调 osascript 真的弹通知）
    assert FAKE_NOTIFICATIONS, "应捕获到通知调用"
    assert any("25:00" in msg for _, msg in FAKE_NOTIFICATIONS)


def test_notify_is_stubbed_for_whole_suite():
    """确认 autouse 夹具生效：util.notify 已被替换（真实现会起 osascript 子进程）。"""
    import inspect
    src = inspect.getsource(util_mod.notify)
    assert "osascript" not in src, "测试期间 util.notify 不应还是真实实现"


def test_main_without_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "install" in capsys.readouterr().out


# ── 电报（2026-10-22 需求） ─────────────────────────────────────────────────────
def test_status_shows_telegram_configured(env, capsys):
    import json
    data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["telegram_bot_token"] = "123:ABC"
    data["telegram_chat_id"] = "-100999"
    cfg_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")

    cli.main(["status"])
    out = capsys.readouterr().out
    assert "电报通知：已配置" in out
    assert "123:ABC" not in out          # token 绝不显示


def test_status_shows_telegram_not_configured(env, capsys):
    cli.main(["status"])
    assert "电报通知：未配置" in capsys.readouterr().out


def test_telegram_test_command_reports_success(env, monkeypatch, capsys):
    from oracle_arm import telegram as telegram_mod
    monkeypatch.setattr(telegram_mod.TelegramNotifier, "send", lambda self, text: True)
    import json
    data = json.loads(cfg_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["telegram_bot_token"] = "123:ABC"
    data["telegram_chat_id"] = "-100999"
    cfg_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")

    assert cli.main(["telegram-test"]) == 0
    assert "电报发送成功" in capsys.readouterr().out


def test_telegram_test_command_fails_when_unconfigured(env, capsys):
    assert cli.main(["telegram-test"]) == 1
    assert "未配置电报" in capsys.readouterr().out
