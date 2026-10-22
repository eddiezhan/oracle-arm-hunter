"""单轮申请流程。

每个申请时刻只跑一轮：预检 → 逐个 AD 尝试一次 → 成功则确认 RUNNING 与公网 IPv4。
不在轮内持续高频轮询；容量不足就结束本轮，等下一个申请时刻。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import client as client_mod
from . import config as cfg_mod
from . import errors as err_mod
from . import telegram as telegram_mod
from . import util

# 退出码（launchd 只记日志，人看状态时用）
EXIT_OK = 0            # 本轮正常结束（含容量不足，等下一次）
EXIT_NEEDS_HUMAN = 2   # 需人工处理，定时任务已暂停
EXIT_UNEXPECTED = 3    # 意外错误

# 实例生命周期状态
_STATE_RUNNING = "RUNNING"
_STATE_PROVISIONING = ("PROVISIONING", "STARTING")
_STATE_ALIVE = ("RUNNING", "STOPPED", "STOPPING", "PROVISIONING", "STARTING")

# 公网 IP 轮询：RUNNING 后最多再等约 110 秒
_PUBLIC_IP_TRIES = cfg_mod.PUBLIC_IP_TRIES
_PUBLIC_IP_INTERVAL = cfg_mod.PUBLIC_IP_INTERVAL


@dataclass
class RunResult:
    exit_code: int
    status: str
    summary: str
    instance_id: str = ""
    public_ip: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


@dataclass
class QuotaResult:
    verified: bool
    sufficient: bool
    summary: str
    detail: str = ""


@dataclass
class Preflight:
    ad: str
    image: client_mod.ImageInfo
    subnet: client_mod.SubnetInfo
    shape: client_mod.ShapeInfo
    quota: QuotaResult
    ads: list[str] = field(default_factory=list)  # 按优先级排序的待尝试 AD


class _StopRun(Exception):
    """内部信号：本轮到此为止，直接返回给定结果。"""

    def __init__(self, result: RunResult):
        super().__init__(result.summary)
        self.result = result


class Runner:
    """一轮申请的完整流程。api / sleep / notifier / stop_schedule 都可注入，便于测试。"""

    def __init__(
        self,
        cfg: cfg_mod.Config,
        state: cfg_mod.State,
        api,
        log,
        sleep: Callable[[float], None] = time.sleep,
        notifier: Callable[..., None] | None = None,
        stop_schedule: Callable[[], None] | None = None,
    ):
        self.cfg = cfg
        self.state = state
        self.api = api
        self.log = log
        self.sleep = sleep
        self.notifier = notifier or (lambda *a, **k: None)
        self.stop_schedule = stop_schedule or (lambda: None)
        self.ssh_public_key = ""
        self._ads: list[str] = []

    # ── 日志与状态 ─────────────────────────────────────────────────────────────
    def _log(self, msg: str, level: str = "info") -> None:
        getattr(self.log, level)(util.redact(msg))

    def _save(self) -> None:
        self.state.updated_at = util.beijing_now().isoformat()
        self.state.save()

    def _stop(self, exit_code: int, status: str, summary: str, pause: bool = False) -> None:
        """结束本轮。pause=True 时同时暂停定时任务（需人工介入的场景）。"""
        self.state.status = status
        self.state.reason = summary
        self.state.last_result = summary
        self._save()
        self._log(f"本轮结束：{summary}", "warning" if pause else "info")
        if pause:
            self._notify("Oracle ARM 申请需要处理", summary)
            try:
                self.stop_schedule()
            except Exception as exc:  # 暂停失败不应掩盖原始问题
                self._log(f"暂停定时任务失败：{exc}", "error")
        raise _StopRun(RunResult(exit_code, status, summary))

    def _notify(self, title: str, message: str) -> None:
        self.notifier(title, message, self.cfg.notification)

    def _notify_telegram(self, instance_id: str, ip: str, reason: str) -> None:
        """发一条电报消息（需求 2026-10-22）。失败只记日志，绝不影响申请结果。"""
        try:
            sent = telegram_mod.notify_success(
                self.cfg,
                log=self.log,
                reason=reason,
                instance_id=instance_id,
                public_ip=ip,
                shape=f"{cfg_mod.TARGET_OCPUS} OCPU / {cfg_mod.TARGET_MEMORY_GB}GB",
            )
            if sent:
                self._log("已发送 Telegram 电报通知")
        except Exception as exc:   # 通知模块自身有 bug 也不能影响「已成功」这个结论
            self._log(f"电报通知异常（已忽略）：{exc!r}", "warning")

    # ── 主入口 ─────────────────────────────────────────────────────────────────
    def run_once(self, dry_run: bool = False) -> RunResult:
        try:
            return self._run_once_inner(dry_run)
        except _StopRun as stop:
            return stop.result
        except Exception as exc:  # 兜底：无人值守下绝不裸崩，留痕并通知
            summary = f"未预期的错误，本轮中止：{exc!r}"
            self._log(summary, "error")
            self.state.status = cfg_mod.STATUS_ERROR
            self.state.reason = summary
            self.state.last_result = summary
            self._save()
            self._notify("Oracle ARM 申请出现未预期错误", summary)
            return RunResult(EXIT_UNEXPECTED, cfg_mod.STATUS_ERROR, summary)

    def _run_once_inner(self, dry_run: bool = False) -> RunResult:
        try:
            preflight = self._preflight()
            if dry_run:
                summary = (
                    f"预检通过（未创建实例）：AD={preflight.ad}，镜像={preflight.image.display_name}，"
                    f"子网={preflight.subnet.display_name}，额度：{preflight.quota.summary}"
                )
                # 预检只记录结果，不改写 status/reason —— 否则会把「需人工处理」
                # 悄悄抹成 idle，用户看到的状态与实际（任务仍被禁用）不符
                self.state.last_result = summary
                self._save()
                self._log(summary)
                return RunResult(EXIT_OK, cfg_mod.STATUS_IDLE, summary)
            return self._attempt_launch(preflight)
        except _StopRun as stop:
            return stop.result

    # ── 预检 ───────────────────────────────────────────────────────────────────
    def _safe_call(self, fn, what: str):
        """调用 OCI 接口并分类异常：需人工的暂停，瞬时的跳过本轮（不冒险创建）。"""
        try:
            return fn()
        except Exception as exc:
            classified = err_mod.classify(exc, self.cfg.rate_limit_default_wait)
            if classified.needs_human:
                self._stop(EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                           f"{what}失败：{classified.message}", pause=True)
            self._stop(EXIT_OK, cfg_mod.STATUS_IDLE,
                       f"{what}失败，本轮跳过：{classified.message}")

    def _preflight(self) -> Preflight:
        self._check_config()
        if self.state.pending_retry_token:
            # 上一轮在提交创建的过程中被中断：本轮必须先对账，绝不直接再发创建请求
            self._log(
                f"上一轮创建过程被中断（幂等标识 {self.state.pending_retry_token}"
                f"{'，开始于 ' + self.state.pending_since if self.state.pending_since else ''}），"
                f"本轮先查询实例状态对账"
            )
        self._check_compartment()
        self._check_existing_instance()
        plan = self._safe_plan_type()
        ad, shape, quota = self._pick_ad_and_quota()
        image = self._pick_image()
        subnet = self._pick_subnet()
        return Preflight(
            ad=ad,
            image=image,
            subnet=subnet,
            shape=shape,
            quota=quota,
            ads=self._ad_order(ad),
        )

    def _check_config(self) -> None:
        problems = self.cfg.validate()
        if problems:
            self._stop(
                EXIT_NEEDS_HUMAN,
                cfg_mod.STATUS_NEEDS_ATTENTION,
                "配置有问题：" + "；".join(problems),
                pause=True,
            )

        pub_path = Path(self.cfg.ssh_public_key).expanduser()
        if not pub_path.exists():
            self._stop(
                EXIT_NEEDS_HUMAN,
                cfg_mod.STATUS_NEEDS_ATTENTION,
                f"SSH 公钥不存在：{pub_path}。请重新运行「安装.command」生成密钥。",
                pause=True,
            )
        content = pub_path.read_text(encoding="utf-8").strip()
        if not content.startswith(("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-")):
            self._stop(
                EXIT_NEEDS_HUMAN,
                cfg_mod.STATUS_NEEDS_ATTENTION,
                f"SSH 公钥格式不对：{pub_path}（应为 .pub 公钥文件）",
                pause=True,
            )
        self.ssh_public_key = content

    def _check_compartment(self) -> None:
        """确认 Compartment 存在且 ACTIVE（需求：申请前自动检查 Compartment）。

        用户显式配置了子 Compartment 时，查询失败视为配置错误并暂停；
        用 tenancy 根 Compartment 时（默认）查询失败不阻塞——根 Compartment 必然存在，
        后面的实例/网络查询本身也会暴露权限问题，不必在这里误停。
        """
        explicit = bool(self.cfg.compartment_id)
        try:
            name, state = self.api.compartment_info()
        except Exception as exc:
            classified = err_mod.classify(exc, self.cfg.rate_limit_default_wait)
            if explicit:
                self._stop(EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                           f"检查 Compartment 失败：{classified.message}", pause=True)
            self._log(f"跳过 Compartment 检查（使用根 Compartment）：{classified.message}", "warning")
            return
        if state and state != "ACTIVE":
            self._stop(
                EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                f"Compartment {name or self.api.compartment_id} 状态是 {state}，不是 ACTIVE，已停止。",
                pause=True,
            )
        self._log(f"Compartment：{name or '(根)'}（{state or '未返回状态'}）", "debug")

    def _check_existing_instance(self) -> None:
        """检查现有实例：本工具已创建过就绝不重复创建（需求 3）。"""
        try:
            instances = self.api.list_instances()
        except Exception as exc:
            classified = err_mod.classify(exc, self.cfg.rate_limit_default_wait)
            if classified.needs_human:
                self._stop(
                    EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                    f"无法查询现有实例：{classified.message}", pause=True,
                )
            # 查询失败但属瞬时错误：本轮放弃，不冒险创建
            self._stop(EXIT_OK, cfg_mod.STATUS_IDLE, f"查询现有实例失败，本轮跳过：{classified.message}")

        # 只看 A1 规格的实例；其他规格与本目标无关
        a1 = [i for i in instances if i.shape == cfg_mod.TARGET_SHAPE]
        # 只认「本工具打的标签」：显示名不是所有权证明 —— 默认名 arm-free-1 很通用，
        # 用户手工创建的实例完全可能同名，按名字认领会把它谎报成「本工具申请成功」
        mine = [i for i in a1 if i.is_managed]
        target = (mine or a1)
        if not target:
            return

        inst = target[0]
        if inst.lifecycle_state not in _STATE_ALIVE:
            return

        if not mine:
            # 账号里已有别人的 A1 实例：绝不重复创建，也绝不把它当成「本工具申请成功」——
            # 否则用户会收到一条与实际不符的成功通知，还以为拿到的是自己那台机器。
            same_name = inst.display_name == self.cfg.display_name
            extra = (
                f"它的显示名恰好与本工具要用的名字相同（{self.cfg.display_name}），"
                f"但它没有任何 oracle-arm 标签，无法确认是同一台。"
                if same_name else ""
            )
            self._stop(
                EXIT_NEEDS_HUMAN,
                cfg_mod.STATUS_NEEDS_ATTENTION,
                f"账号里已存在 A1 实例 {inst.display_name}（{inst.lifecycle_state}，"
                f"{inst.ocpus:g} OCPU / {inst.memory_gb:g}GB，非本工具创建）。{extra}"
                f"为避免重复创建、避免产生额外费用，已停止自动申请。"
                f"如果这台就是要用的机器，什么都不用做；如果要让本工具接管，"
                f"请在 OCI 控制台给它加一个 freeform 标签 managed-by=oracle-arm，"
                f"再用「状态.command」恢复。实例 OCID：{inst.id}",
                pause=True,
            )

        self._log(f"检测到本工具创建的实例：{inst.display_name}（{inst.lifecycle_state}）")

        if inst.lifecycle_state in _STATE_PROVISIONING:
            # 上一轮已提交创建，本轮只做确认，绝不重复创建
            result = self._finalize(inst.id)
            raise _StopRun(result)

        ip = self._safe_public_ip(inst.id)
        if ip:
            self._mark_success(inst.id, ip, f"实例已存在且已分配公网 IPv4：{inst.display_name}")
        # 已存在但没有公网 IPv4：停止重复申请并提示修复（需求 6）
        self._stop(
            EXIT_NEEDS_HUMAN,
            cfg_mod.STATUS_NEEDS_ATTENTION,
            f"已存在 A1 实例 {inst.display_name}（{inst.lifecycle_state}）但没有公网 IPv4，"
            f"已停止重复申请。请在 OCI 控制台检查该实例的子网/Internet Gateway/公网 IP 配置，"
            f"修复后运行「状态.command」恢复。实例 OCID：{inst.id}",
            pause=True,
        )

    def _safe_plan_type(self) -> str | None:
        try:
            plan = self.api.plan_type()
        except Exception:
            plan = None
        if plan:
            self.state.plan_type = plan
            self._log(f"账号套餐类型：{plan}（免费账号为 FREE）", "debug")
        return plan

    def _pick_ad_and_quota(self) -> tuple[str, client_mod.ShapeInfo, QuotaResult]:
        home = self._safe_call(self.api.home_region, "查询账号 Home Region")
        if home != self.cfg.region:
            self._stop(
                EXIT_NEEDS_HUMAN,
                cfg_mod.STATUS_NEEDS_ATTENTION,
                f"账号 Home Region 是 {home or '未知'}，不是 {self.cfg.region}。"
                f"本工具只在新加坡申请，已停止。",
                pause=True,
            )
        sdk_region = getattr(self.api, "region", "")
        if sdk_region and sdk_region != self.cfg.region:
            self._stop(
                EXIT_NEEDS_HUMAN,
                cfg_mod.STATUS_NEEDS_ATTENTION,
                f"~/.oci/config 里的 region 是 {sdk_region}，不是 {self.cfg.region}，已停止。",
                pause=True,
            )

        ads = (
            [self.cfg.availability_domain]
            if self.cfg.availability_domain
            else self._safe_call(self.api.availability_domains, "查询 Availability Domain")
        )
        self._ads = [a for a in ads if a]
        if not self._ads:
            self._stop(
                EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                "账号下没有可用的 Availability Domain，已停止。", pause=True,
            )

        tried: list[str] = []
        # 仅因「查询失败」被跳过的 AD 数。查询失败是「不知道」，不是「该 AD 不可用」，
        # 不能据此永久停表（2026-10-17 18:45 实测：一次读超时就走到下面的兜底 _stop 停了任务）
        transient_failures = 0
        for ad in self._ads:
            # 单个 AD 查询失败不应放弃整轮：记下来换下一个 AD 试
            try:
                shape = self.api.shape_info(ad)
            except Exception as exc:
                classified = err_mod.classify(exc, self.cfg.rate_limit_default_wait)
                if classified.needs_human:
                    self._stop(EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                               f"查询 {ad} 的规格信息失败：{classified.message}", pause=True)
                transient_failures += 1
                tried.append(f"{ad}: 查询失败（{classified.message}）")
                continue
            if shape is None:
                tried.append(f"{ad}: 无 {cfg_mod.TARGET_SHAPE}")
                continue
            if not shape.is_free_eligible:
                # 不确定是否免费就停止（需求一）。
                # 注意 A1 的 billing_type 实测是 LIMITED_FREE（额度内免费），
                # 不能只认 ALWAYS_FREE，否则会把申请直接挡死。
                self._stop(
                    EXIT_NEEDS_HUMAN,
                    cfg_mod.STATUS_NEEDS_ATTENTION,
                    f"{cfg_mod.TARGET_SHAPE} 在 {ad} 的计费类型是 "
                    f"{shape.billing_type or '未知'}（既不是 ALWAYS_FREE 也不是 LIMITED_FREE），"
                    f"无法确认在免费范围内，已停止。请确认账号的 Always Free 资格后再试。",
                    pause=True,
                )
            quota = self._check_quota(ad, shape)
            if quota.sufficient:
                self._log(f"选定 AD：{ad}；额度：{quota.summary}", "debug")
                return ad, shape, quota
            tried.append(f"{ad}: {quota.detail or quota.summary}")

        if transient_failures == len(self._ads):
            # 每个 AD 都只是「查不到」（大概率网络抖动）→ 本轮跳过，下个申请时刻重试。
            # 这里绝不能暂停定时任务：一次网络抖动就永久停表，抢容量工具会静默停摆。
            self._stop(
                EXIT_OK, cfg_mod.STATUS_IDLE,
                "所有 Availability Domain 的规格查询都失败（可能是网络问题），"
                "本轮跳过，下一个申请时刻重试：" + "；".join(tried),
            )

        self._stop(
            EXIT_NEEDS_HUMAN,
            cfg_mod.STATUS_NEEDS_ATTENTION,
            "没有可用的 Availability Domain：" + "；".join(tried) +
            f"。需求规格为 {cfg_mod.TARGET_OCPUS} OCPU / {cfg_mod.TARGET_MEMORY_GB}GB，"
            f"不符合当前账号免费额度时本工具不会自动申请收费资源。",
            pause=True,
        )

    def _check_quota(self, ad: str, shape: client_mod.ShapeInfo) -> QuotaResult:
        """用 Limits API 实时查询额度（官方口径），不硬编码任何免费上限。

        读数若判定「额度不够」，必须复查一次再下结论：Limits API 是最终一致的，
        实测出现过瞬时假读数——2026-10-20 16:04 读到「已用 2/2、可用 0」，几秒后
        复查即恢复 0/2，OCI 审计日志证实那段时间没有任何实例创建/终止事件。
        只凭一次读数就把定时任务永久停掉，是抢容量工具最糟的失败模式（静默停摆，
        而抢容量分秒必争），所以「停止」结论一律要求**两次读数一致**——不只是「两次都
        判负」，还必须**判负的原因相同**；两次结论不一致时本轮不下结论，只跳过。
        """
        names = self._quota_names(shape)
        cap_short_reads = 0   # 判「账号额度上限不够」的读数次数
        # 正常路径只查一次；只有读数会导致停止时才复查（不影响容量不足的常规节奏）
        for attempt in (1, 2):
            core, mem, unverified = self._read_limits(ad, names)
            if core is None or mem is None:
                if attempt == 1:
                    # 第一次就读不到：沿用原有语义（allow_unverified_quota 时按配置继续）
                    return QuotaResult(False, True, "未验证（按配置继续）", unverified)
                # 复查读不到：首次读数已判负，绝不能把「不知道」当成「额度够」去发创建请求。
                # 本轮不下结论——不申请（安全），也不暂停（下个申请时刻自愈）。
                self._stop(EXIT_OK, cfg_mod.STATUS_IDLE,
                           f"额度复查失败（{unverified}），本轮跳过，下一个申请时刻重试")

            summary = (
                f"A1 核心 已用 {core.used:g}/{core.quota_value:g}（可用 {core.available:g}）；"
                f"内存 已用 {mem.used:g}/{mem.quota_value:g} GB（可用 {mem.available:g}）"
                f"{'，来源 ' + core.scope_type if core.scope_type else ''}"
            )
            self.state.quota_verified = True
            self.state.quota_summary = summary

            # 额度上限本身就不够：整个账号都不满足
            cap_short = (core.quota_value < cfg_mod.TARGET_OCPUS
                         or mem.quota_value < cfg_mod.TARGET_MEMORY_GB)
            # 该 AD 已被占满
            ad_full = (core.available < cfg_mod.TARGET_OCPUS
                       or mem.available < cfg_mod.TARGET_MEMORY_GB)
            if not cap_short and not ad_full:
                return QuotaResult(True, True, summary)

            cap_short_reads += 1 if cap_short else 0
            if attempt == 1:  # 读数会导致停止 → 等几秒让 Limits API 收敛后复查
                self._log(f"额度读数显示额度不足（{summary}），"
                          f"{cfg_mod.QUOTA_RECHECK_WAIT} 秒后复查确认", "warning")
                self.sleep(cfg_mod.QUOTA_RECHECK_WAIT)

        # 只有两次读数**都**判「上限不够」才认定账号额度真的不够。单次读数不足以
        # 下这种结论（2026-10-20 就是被一次假读数停掉的），两次结论不一致时同样不下。
        if cap_short_reads == 2:
            self._stop(
                EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                f"账号的 A1 免费额度不足以创建 {cfg_mod.TARGET_OCPUS} OCPU / "
                f"{cfg_mod.TARGET_MEMORY_GB}GB（{summary}）。本工具不会自动申请收费资源，已停止。",
                pause=True,
            )
        if cap_short_reads:
            # 一次说「上限不够」、一次说「该 AD 占满」——说的不是一回事，不构成结论。
            # 本轮跳过：既不申请（安全），也不停表（可自愈），下个申请时刻重新判。
            self._stop(EXIT_OK, cfg_mod.STATUS_IDLE,
                       f"额度两次读数结论不一致（{summary}），本轮跳过，下一个申请时刻重试")
        return QuotaResult(True, False, summary, f"该 AD 剩余额度不足（{summary}）")

    def _read_limits(
        self, ad: str, names: list[str]
    ) -> tuple[client_mod.LimitInfo | None, client_mod.LimitInfo | None, str]:
        """查一次 A1 额度，返回 (核心额度, 内存额度, 未验证原因)。

        读不到时：allow_unverified_quota=true 则返回 (None, None, 原因) 交给调用方按
        「未验证」处理；否则按错误类型处置（都会抛 _StopRun 结束本轮）——需要人工介入的
        （如 403 无 limits 读权限）暂停定时任务，网络/瞬时错误只跳过本轮、下轮重试。
        """
        try:
            limits = self.api.limit_availability(ad, names)
        except Exception as exc:
            classified = err_mod.classify(exc, self.cfg.rate_limit_default_wait)
            if self.cfg.allow_unverified_quota:
                self._log(f"额度查询失败（{classified.message}），按配置继续", "warning")
                return None, None, classified.message
            if not classified.needs_human:
                # 网络/瞬时错误只是「这次没查到」，不是「账号有问题」。据此永久停表
                # 会让一次网络抖动停掉整个抢容量任务，所以本轮跳过、下个申请时刻重试。
                self._stop(EXIT_OK, cfg_mod.STATUS_IDLE,
                           f"额度查询失败，本轮跳过：{classified.message}")
            self._stop(
                EXIT_NEEDS_HUMAN,
                cfg_mod.STATUS_NEEDS_ATTENTION,
                f"无法查询账号免费额度（{classified.message}）。无法确认 {cfg_mod.TARGET_OCPUS} OCPU / "
                f"{cfg_mod.TARGET_MEMORY_GB}GB 在免费范围内，已停止。"
                f"请确认 API Key 所属用户有 limits 读权限，或在配置里设置 allow_unverified_quota=true 自行承担风险。",
                pause=True,
            )

        by_name = {lim.name: lim for lim in limits}
        core = self._match_limit(by_name, "core")
        mem = self._match_limit(by_name, "memory")
        if core is None or mem is None:
            detail = f"额度名称未找到（已查：{', '.join(by_name) or '无'}）"
            if self.cfg.allow_unverified_quota:
                self._log(f"{detail}，按配置继续", "warning")
                return None, None, detail
            self._stop(
                EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                f"无法从 Limits API 确认 A1 的免费额度：{detail}，已停止。", pause=True,
            )
        return core, mem, ""

    @staticmethod
    def _quota_names(shape: client_mod.ShapeInfo) -> list[str]:
        """从 shape.quota_names 里挑出 A1 的核心/内存额度名称。

        名称由 OCI 返回，不硬编码数值；仅在 OCI 未返回时退回标准名称做尽力查询。
        """
        names = [n for n in (shape.quota_names or []) if "a1" in n.lower()]
        picked = [n for n in names if n.lower().endswith(("-core-count", "-memory-count"))]
        if picked:
            return picked
        return ["standard-a1-core-count", "standard-a1-memory-count"]

    @staticmethod
    def _match_limit(by_name: dict, keyword: str) -> client_mod.LimitInfo | None:
        for name, lim in by_name.items():
            low = name.lower()
            if "a1" in low and low.endswith(f"{keyword}-count"):
                return lim
        return None

    def _pick_image(self) -> client_mod.ImageInfo:
        if self.cfg.image_id:
            return client_mod.ImageInfo(id=self.cfg.image_id, display_name="(配置指定)")
        image = self._safe_call(self.api.find_image, "查找 Ubuntu ARM64 镜像")
        if image is None:
            self._stop(
                EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                f"没有找到 {cfg_mod.UBUNTU_OS} 的 ARM64 镜像，已停止。"
                f"请确认账号在 {self.cfg.region} 可用，或在配置里指定 image_id。",
                pause=True,
            )
        self._log(f"使用镜像：{image.display_name}", "debug")
        return image

    def _pick_subnet(self) -> client_mod.SubnetInfo:
        subnet = self._safe_call(self.api.find_public_subnet, "查找 Public Subnet")
        if subnet is None:
            self._stop(
                EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                "没有找到可分配公网 IPv4 的 Public Subnet（需要 VCN 里有 Internet Gateway，"
                "且子网路由指向该网关、未禁止公网 IP）。本工具不会自动创建网络资源，"
                "请在 OCI 控制台用「Create VCN with Internet Connectivity」向导创建后重试。",
                pause=True,
            )
        if not subnet.can_assign_public_ip:
            self._stop(
                EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                f"子网 {subnet.display_name} 不能分配公网 IPv4，已停止。",
                pause=True,
            )
        if self.cfg.enable_ipv6 and not subnet.has_ipv6:
            self._log("配置要求 IPv6，但该子网没有 IPv6 CIDR，本次只申请 IPv4", "warning")
        self._log(f"使用子网：{subnet.display_name}（{subnet.id}）", "debug")
        return subnet

    def _ad_order(self, first: str) -> list[str]:
        """把选定的 AD 排在最前，其余 AD 作为容量不足时的备选。"""
        return [first] + [a for a in getattr(self, "_ads", []) if a != first]

    # ── 申请 ───────────────────────────────────────────────────────────────────
    def _attempt_launch(self, preflight: Preflight) -> RunResult:
        self.state.attempts += 1
        self.state.last_attempt_at = util.beijing_now().isoformat()
        self._save()

        max_retries = max(0, self.cfg.rate_limit_max_retries)
        # 容量不足时单条失败记 debug（高频模式下是常态，全记 info 会把日志刷满），
        # 但汇总行必须带上官方原文：否则 launchd 跑 INFO 级别时，日志里只剩
        # 「容量不足」的结论，看不到 OCI 到底返回了什么（2026-10-20 排查时踩过）
        last_capacity_message = ""
        for ad in preflight.ads:
            # 每个 AD 一次逻辑请求：同一 token 的重试具备幂等性
            token = uuid.uuid4().hex
            self.state.pending_retry_token = token
            self.state.pending_since = util.beijing_now().isoformat()
            self._save()
            self._log(f"尝试创建实例：AD={ad}，规格 {cfg_mod.TARGET_OCPUS} OCPU / "
                      f"{cfg_mod.TARGET_MEMORY_GB}GB / {cfg_mod.TARGET_BOOT_GB}GB，幂等标识={token}",
                      "debug")

            # 429 与超时是两类不同的失败，各自独立计数，互不吃掉对方的预算
            rate_attempt = 0
            transient_attempt = 0
            while True:
                try:
                    instance_id = self.api.launch(
                        availability_domain=ad,
                        image_id=preflight.image.id,
                        subnet_id=preflight.subnet.id,
                        ssh_public_key=self.ssh_public_key,
                        retry_token=token,
                        enable_ipv6=self.cfg.enable_ipv6 and preflight.subnet.has_ipv6,
                        ipv6_subnet_cidr=preflight.subnet.ipv6_cidr,
                    )
                except Exception as exc:
                    classified = err_mod.classify(exc, self.cfg.rate_limit_default_wait)
                    # 容量不足在高频模式下是常态，记 debug 免得把日志刷满；
                    # 其他失败才值得 warning
                    self._log(
                        f"创建失败（{classified.kind}）：{classified.message}",
                        "debug" if classified.kind == err_mod.CAPACITY else "warning",
                    )

                    if classified.kind == err_mod.CAPACITY:
                        last_capacity_message = classified.message
                        break  # 换下一个 AD 试一次
                    if classified.kind == err_mod.RATE_LIMITED:
                        if rate_attempt < max_retries:
                            # 退避时长设上限：Retry-After 偶尔给出很大的值（如 7200），
                            # 原样照睡会把这一轮拖到几小时，错过后续申请时刻
                            wait = min(
                                classified.retry_after or self.cfg.rate_limit_default_wait,
                                cfg_mod.RATE_LIMIT_MAX_WAIT,
                            )
                            self._log(f"触发限流，按官方要求退避 {wait} 秒后重试（{rate_attempt + 1}/{max_retries}）")
                            self.sleep(wait)
                            rate_attempt += 1
                            continue
                        self.state.pending_retry_token = ""
                        self._stop(EXIT_OK, cfg_mod.STATUS_IDLE,
                                   "持续触发限流（429），本轮放弃，等待下一个申请时刻")
                    if classified.kind == err_mod.TRANSIENT:
                        # 超时/网络错误：先查询实例状态对账，禁止盲目重试（需求 4）
                        found = self._find_created_instance()
                        if found is not None:
                            # 对账已确认实例存在：无论 _finalize 是正常返回（还在创建中/
                            # 查询失败）还是抛 _StopRun，都绝不能落到下面的重试分支
                            self._log(f"对账发现实例已创建：{found.id}，不再重试创建")
                            result = self._finalize(found.id)
                            raise _StopRun(result)
                        if transient_attempt < 1:
                            self._log("对账未查到实例，用同一幂等标识重试一次")
                            transient_attempt += 1
                            continue
                        self.state.pending_retry_token = ""
                        self._stop(EXIT_OK, cfg_mod.STATUS_IDLE,
                                   f"创建请求超时且未确认结果（{classified.message}），本轮放弃，"
                                   f"下个申请时刻会先对账再决定")
                    # 认证/配额/配置等需人工处理
                    self.state.pending_retry_token = ""
                    self._stop(EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                               f"{classified.message}（已暂停定时任务，处理后可用「状态.command」恢复）",
                               pause=True)
                else:
                    return self._finalize(instance_id)

        # 所有 AD 都容量不足
        self.state.pending_retry_token = ""
        self.state.capacity_misses += 1
        detail = f"；最近一次：{last_capacity_message}" if last_capacity_message else ""
        # 汇总行带上申请规格（2026-10-22 需求）：规格只在 DEBUG 行出现过，而后台任务
        # 按 INFO 级别跑，日志里看不到到底申请的是几核几 G，排查时得手工加 --verbose。
        # 「N 个 AD」是 Availability Domain 的个数（新加坡区域只有 1 个），与规格无关。
        summary = (f"{len(preflight.ads)} 个 AD 均容量不足（累计 {self.state.capacity_misses} 次{detail}），"
                   f"申请规格 {cfg_mod.TARGET_OCPUS} OCPU / {cfg_mod.TARGET_MEMORY_GB}GB，"
                   f"等待下一个申请时刻")
        self.state.status = cfg_mod.STATUS_IDLE
        self.state.last_result = summary
        self.state.reason = ""
        self._save()
        self._log(summary)
        return RunResult(EXIT_OK, cfg_mod.STATUS_IDLE, summary)

    def _find_created_instance(self) -> client_mod.InstanceInfo | None:
        """超时后按显示名/标签对账，判断实例是否其实已经创建成功。"""
        try:
            for inst in self.api.list_instances():
                if inst.lifecycle_state in _STATE_ALIVE and (
                    inst.is_managed or inst.display_name == self.cfg.display_name
                ):
                    return inst
        except Exception as exc:
            self._log(f"对账查询失败：{exc}", "warning")
        return None

    def _finalize(self, instance_id: str) -> RunResult:
        """确认实例 RUNNING 并拿到公网 IPv4，然后停止定时任务。"""
        self.state.instance_id = instance_id
        self.state.pending_retry_token = ""
        self.state.pending_since = ""
        self._save()
        self._log(f"实例已提交创建：{instance_id}，等待进入 RUNNING")

        try:
            inst = self.api.wait_for_state(
                instance_id, _STATE_RUNNING, self.cfg.launch_timeout_seconds, sleep=self.sleep
            )
        except TimeoutError as exc:
            summary = f"实例仍在创建中（{exc}），下个申请时刻会继续确认，不会重复创建"
            self.state.status = cfg_mod.STATUS_IDLE
            self.state.last_result = summary
            self._save()
            self._log(summary, "warning")
            return RunResult(EXIT_OK, cfg_mod.STATUS_IDLE, summary, instance_id=instance_id)
        except Exception as exc:
            if err_mod.is_not_found(exc):
                # 实例查不到了（被删除或不可见）：当作本轮没创建成功，下轮重新开始
                summary = f"实例 {instance_id} 已不存在（{exc}），本轮结束，下个申请时刻重新开始"
                self.state.status = cfg_mod.STATUS_IDLE
                self.state.instance_id = ""
                self.state.last_result = summary
                self._save()
                self._log(summary, "warning")
                return RunResult(EXIT_OK, cfg_mod.STATUS_IDLE, summary)
            classified = err_mod.classify(exc, self.cfg.rate_limit_default_wait)
            if classified.needs_human:
                self._stop(EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                           f"实例 {instance_id} 状态异常：{classified.message}", pause=True)
            summary = f"等待实例状态失败：{classified.message}，下个申请时刻会继续确认"
            self.state.status = cfg_mod.STATUS_IDLE
            self.state.last_result = summary
            self._save()
            return RunResult(EXIT_OK, cfg_mod.STATUS_IDLE, summary, instance_id=instance_id)

        ip, query_failed = self._wait_public_ip(instance_id)
        if not ip and query_failed:
            # 「查不到」不等于「没分配」：本轮不下结论，下个申请时刻继续确认。
            # 实例已经在账号里，预检会发现它，绝不会重复创建。
            summary = (f"实例 {instance_id} 已 RUNNING，但连续 {_PUBLIC_IP_TRIES} 次查询公网 IPv4 "
                       f"都失败，本轮不下结论，下个申请时刻会继续确认")
            self.state.status = cfg_mod.STATUS_IDLE
            self.state.last_result = summary
            self._save()
            self._log(summary, "warning")
            return RunResult(EXIT_OK, cfg_mod.STATUS_IDLE, summary, instance_id=instance_id)
        if not ip:
            # 已创建但没有公网 IPv4：停止重复申请并提示修复（需求 6）
            self._stop(
                EXIT_NEEDS_HUMAN, cfg_mod.STATUS_NEEDS_ATTENTION,
                f"实例 {instance_id} 已 RUNNING 但没有分配到公网 IPv4，已停止重复申请。"
                f"请在 OCI 控制台为该实例的 VNIC 分配公网 IP（或改用可分配公网 IP 的 Public Subnet）"
                f"后，用「状态.command」恢复。",
                pause=True,
            )
        self._mark_success(instance_id, ip, f"实例已就绪：{inst.display_name}")
        return RunResult(EXIT_OK, cfg_mod.STATUS_SUCCESS, self.state.last_result,
                         instance_id=instance_id, public_ip=ip)

    def _safe_public_ip(self, instance_id: str) -> str | None:
        try:
            return self.api.public_ip(instance_id)
        except Exception as exc:
            self._log(f"查询公网 IP 失败：{exc}", "warning")
            return None

    def _wait_public_ip(self, instance_id: str) -> tuple[str | None, bool]:
        """轮询公网 IPv4。

        返回 (IP, 是否全部查询失败)。必须区分两种「没有 IP」：
        - 查询成功但没有 IP → 实例真的没分配，需要用户修复（需求 6）；
        - 查询本身全部失败 → 不能下结论，否则会把「限流/超时」误报成「没分配公网 IP」，
          让用户去做一件没必要做的事，还永久暂停了定时任务。
        """
        failures = 0
        for i in range(_PUBLIC_IP_TRIES):
            try:
                ip = self.api.public_ip(instance_id)
            except Exception as exc:
                failures += 1
                self._log(f"查询公网 IP 失败（{i + 1}/{_PUBLIC_IP_TRIES}）：{exc}", "warning")
                ip = None
            if ip:
                return ip, False
            if i < _PUBLIC_IP_TRIES - 1:
                self._log(f"公网 IPv4 尚未分配，{_PUBLIC_IP_INTERVAL} 秒后重查（{i + 1}/{_PUBLIC_IP_TRIES}）",
                      "debug")
                self.sleep(_PUBLIC_IP_INTERVAL)
        return None, failures >= _PUBLIC_IP_TRIES

    def _mark_success(self, instance_id: str, ip: str, reason: str) -> None:
        """成功：记录并停止定时任务（需求 5）。"""
        # 这条路径有两个调用方：①本轮真的创建成功；②预检发现实例早已存在（例如用户
        # 手动 resume 后再跑一轮）。只有前者才算「刚抢到」，后者重复发电报会变骚扰，
        # 所以电报只在状态由「非成功」变为「成功」时发送。
        is_new_success = self.state.status != cfg_mod.STATUS_SUCCESS
        self.state.status = cfg_mod.STATUS_SUCCESS
        self.state.instance_id = instance_id
        self.state.public_ip = ip
        self.state.reason = ""
        self.state.last_result = f"{reason}，公网 IPv4：{ip}"
        self._save()
        self._log(f"申请成功：{reason}；实例 OCID={instance_id}；公网 IPv4={ip}")
        self._notify(
            "Oracle 免费 ARM 实例已就绪",
            f"{reason}；公网 IPv4：{ip}；OCID：{instance_id}。定时任务已停止。",
        )
        if is_new_success:
            self._notify_telegram(instance_id, ip, reason)
        try:
            self.stop_schedule()
        except Exception as exc:
            self._log(f"停止定时任务失败：{exc}", "error")
        raise _StopRun(
            RunResult(EXIT_OK, cfg_mod.STATUS_SUCCESS, self.state.last_result,
                      instance_id=instance_id, public_ip=ip)
        )
