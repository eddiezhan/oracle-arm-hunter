"""配置、路径与本地状态。

两条硬性约束写在这里，其他模块不得绕过：
1. 目标规格固定为 2 OCPU / 12GB / 50GB 启动盘的 VM.Standard.A1.Flex（新加坡），
   不接受任何「放大规格」的配置项，避免误申请收费资源。
   （常见账号的 A1 免费额度上限就是 2 OCPU / 12GB，这里按满额度申请——见常量处的说明。
   运行时仍会用 Limits API 复核，额度不够会直接停止而不是去申请收费资源。）
2. 免费额度不硬编码，一律实时查询 OCI Limits API（见 client.py），
   查询不到时默认停止（allow_unverified_quota=False）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

# ── 路径 ────────────────────────────────────────────────────────────────────────
# ORACLE_ARM_HOME 仅用于测试隔离，正常使用不需要设置
BASE_DIR = Path(os.environ.get("ORACLE_ARM_HOME", "~/.oracle-arm")).expanduser()
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "oracle-arm.log"
LOCK_PATH = BASE_DIR / "run.lock"   # 防止两轮申请重叠（见 util.RunLock）
VENV_DIR = BASE_DIR / "venv"
# 源码副本目录：后台任务从这里加载代码，不依赖项目所在目录
# （macOS 对 ~/Downloads 有 TCC 限制，launchd 任务读那里可能被拒绝）
APP_DIR = BASE_DIR / "app"
DEFAULT_SSH_KEY = Path("~/.ssh/oracle_arm_ed25519").expanduser()
DEFAULT_OCI_CONFIG = "~/.oci/config"

LAUNCHD_LABEL = "com.oracle-arm.hunter"   # launchd 任务标识（与作者账号无关）
LAUNCHD_PLIST = Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCHD_LABEL}.plist"

# ── 目标实例（固定，不提供配置项） ───────────────────────────────────────────────
# 2 OCPU / 12 GB：**用满账号 A1 免费额度的全部上限**（常见上限就是 2/12）。
# 历史（供排查命中率时对照）：2026-10-17 17:22 → 10-20 15:58 用 2/12 连续 796 轮全败
# （全是 Out of host capacity），10-20 20:25 降到 1/6，此后 549 轮仍然全败；
# 说明新加坡 AD-1 的 A1 容量长期紧张，命中与否主要取决于供给，而不是请求大小。
# 仍然是常量而不是配置项：不给「被误调大」留口子；运行时由 Limits API 复核，
# 若账号额度连 2/12 都不够会直接停止并提示，不会去申请收费资源。
TARGET_SHAPE = "VM.Standard.A1.Flex"
TARGET_OCPUS = 2
TARGET_MEMORY_GB = 12
TARGET_BOOT_GB = 50
TARGET_REGION = "ap-singapore-1"
UBUNTU_OS = "Canonical Ubuntu"

# 申请节奏（两种模式，二选一）：
#   interval —— 每 N 秒跑一轮，持续抢直到成功（默认；等同参考项目 oci-arm-catcher 的
#               RETRY_INTERVAL=300 行为，命中率最高，代价是持续打接口）
#   fixed    —— 每天固定几个北京时间时刻各跑一轮
SCHEDULE_MODE_INTERVAL = "interval"
SCHEDULE_MODE_FIXED = "fixed"
DEFAULT_SCHEDULE_MODE = SCHEDULE_MODE_INTERVAL
DEFAULT_INTERVAL_SECONDS = 300          # 5 分钟，与参考项目默认值一致
MIN_INTERVAL_SECONDS = 60               # 再密就不礼貌了，且容易吃 429
MAX_INTERVAL_SECONDS = 86400

# 固定模式下使用的北京时间申请时刻
DEFAULT_SCHEDULE = ["02:05", "04:55", "08:05", "20:45"]

# OCI 商业后台（订阅/套餐查询）固定区域，与账号 Home Region 无关
OSP_HOME_REGION = "us-ashburn-1"

# 单次退避等待的上限（秒）。OCI 的 Retry-After 偶尔会给出很大的值（如 7200），
# 原样照睡会把一轮拖到几小时，既错过后续申请时刻，也会让运行锁过期。
RATE_LIMIT_MAX_WAIT = 300

# Limits API 判定「额度不足」时的复查等待（秒）。Limits API 是最终一致的，实测出现过
# 瞬时假读数（2026-10-20 16:04 读到「已用 2/2、可用 0」，几秒后复查即恢复 0/2，
# 审计日志证实当时没有任何实例），所以「停止」结论必须隔几秒复查一次再下。
QUOTA_RECHECK_WAIT = 5

# RUNNING 后确认公网 IPv4 的轮询次数与间隔（≈110 秒）
PUBLIC_IP_TRIES = 12
PUBLIC_IP_INTERVAL = 10

# 状态取值（受控词表）
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_PAUSED = "paused"
STATUS_NEEDS_ATTENTION = "needs_attention"
STATUS_ERROR = "error"


@dataclass
class Config:
    """用户配置，保存于 ~/.oracle-arm/config.json。"""

    region: str = TARGET_REGION
    compartment_id: str = ""          # 空 = 使用 tenancy 根 compartment
    display_name: str = "arm-free-1"
    # default_factory：实例化时才取常量，便于测试替换（测试绝不能碰真实凭据）
    oci_config_file: str = field(default_factory=lambda: DEFAULT_OCI_CONFIG)
    oci_profile: str = "DEFAULT"
    # default_factory：实例化时才取常量，便于测试替换 DEFAULT_SSH_KEY
    ssh_public_key: str = field(default_factory=lambda: str(DEFAULT_SSH_KEY) + ".pub")
    schedule_mode: str = DEFAULT_SCHEDULE_MODE   # interval（每 N 秒）| fixed（每天固定时刻）
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS   # interval 模式的间隔
    schedule_beijing: list[str] = field(default_factory=lambda: list(DEFAULT_SCHEDULE))
    enable_ipv6: bool = False         # 默认关闭，不影响 IPv4 申请
    allow_unverified_quota: bool = False  # 查不到额度时是否放行（默认停止）
    availability_domain: str = ""     # 空 = 自动选择/轮换全部 AD
    subnet_id: str = ""               # 空 = 自动查找公网子网
    image_id: str = ""                # 空 = 自动查找最新 Ubuntu ARM64
    notification: bool = True
    # ── Telegram 电报通知（可选，2026-10-22 需求：抢到后发消息到手机）───────────
    # 凭据只存本地配置文件（权限 600），不写进代码、不进 git。
    # 用自己的机器人即可：在 @BotFather 建 bot 拿 token，把 chat_id 填成收消息的会话。
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_message_thread_id: int = 0   # 群话题 ID，0 = 普通消息
    telegram_proxy: str = ""          # 需要代理时填（如 http://127.0.0.1:7890），留空=直连
    rate_limit_max_retries: int = 2   # 429 单轮最多退避重试次数
    rate_limit_default_wait: int = 60 # 无 Retry-After 头时的默认退避秒数
    launch_timeout_seconds: int = 900 # 等待 RUNNING 的上限

    # ── 读写 ───────────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """读取配置；文件不存在或字段缺失时用默认值补齐（保证升级兼容）。"""
        path = path or CONFIG_PATH
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"配置文件损坏，无法解析 JSON：{path}（{exc}）") from exc
            # JSON 合法但不是对象（null / [] / "x" / 123）同样要报错，
            # 否则会在后面以 TypeError/AttributeError 的形式炸掉所有命令
            if not isinstance(data, dict):
                raise ValueError(
                    f"配置文件格式不对：{path} 顶层应为 JSON 对象，实际是 {type(data).__name__}"
                )
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:  # 忽略未知字段，避免旧版本写入的残留导致崩溃
            data = {k: v for k, v in data.items() if k in known}
        cfg = cls(**data)
        cfg.normalize()
        return cfg

    def save(self, path: Path | None = None) -> Path:
        """原子写：先写临时文件再 rename。直接覆盖时若进程被杀/断电会截断成坏 JSON，
        而坏 JSON 会让之后每一次定时运行都无法启动。"""
        path = path or CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tmp.chmod(0o600)  # 含 OCID 与路径信息，仅本人可读
        tmp.replace(path)
        return path

    def normalize(self) -> None:
        """规整取值：去掉空白、去重排序申请时刻、强制目标区域与节奏参数。"""
        mode = str(self.schedule_mode or "").strip().lower()
        self.schedule_mode = mode if mode in (SCHEDULE_MODE_INTERVAL, SCHEDULE_MODE_FIXED) \
            else DEFAULT_SCHEDULE_MODE
        try:
            seconds = int(self.interval_seconds)
        except (TypeError, ValueError):
            seconds = DEFAULT_INTERVAL_SECONDS
        self.interval_seconds = max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, seconds))
        self.compartment_id = (self.compartment_id or "").strip()
        self.subnet_id = (self.subnet_id or "").strip()
        self.image_id = (self.image_id or "").strip()
        self.availability_domain = (self.availability_domain or "").strip()
        self.display_name = (self.display_name or "arm-free-1").strip()
        self.region = (self.region or TARGET_REGION).strip()
        # 电报凭据去掉首尾空白（手工粘贴时常带换行）；话题 ID 非数字时退回 0
        self.telegram_bot_token = (self.telegram_bot_token or "").strip()
        self.telegram_chat_id = (self.telegram_chat_id or "").strip()
        self.telegram_proxy = (self.telegram_proxy or "").strip()
        try:
            self.telegram_message_thread_id = int(self.telegram_message_thread_id or 0)
        except (TypeError, ValueError):
            self.telegram_message_thread_id = 0
        times = []
        for t in self.schedule_beijing or []:
            t = str(t).strip()
            if t and t not in times:
                times.append(t)
        self.schedule_beijing = sorted(times) or list(DEFAULT_SCHEDULE)

    # ── 校验 ───────────────────────────────────────────────────────────────────
    def validate(self) -> list[str]:
        """返回问题列表；空列表表示配置可用。"""
        problems: list[str] = []
        if self.region != TARGET_REGION:
            problems.append(
                f"区域必须是 {TARGET_REGION}（当前 {self.region}）；本工具只在新加坡申请"
            )
        # interval 模式下不使用固定时刻，就不必因为时刻写法而停止
        if self.schedule_mode == SCHEDULE_MODE_FIXED:
            for raw in self.schedule_beijing:
                parts = raw.split(":")
                # 严格要求 HH:MM（各两位数字），避免 "8:5" 这类歧义写法
                ok = (
                    len(parts) == 2
                    and all(len(p) == 2 and p.isdigit() for p in parts)
                    and 0 <= int(parts[0]) < 24
                    and 0 <= int(parts[1]) < 60
                )
                if not ok:
                    problems.append(f"申请时刻格式错误：{raw!r}（应为 HH:MM，北京时间）")
        if self.schedule_mode == SCHEDULE_MODE_INTERVAL:
            if not (MIN_INTERVAL_SECONDS <= int(self.interval_seconds) <= MAX_INTERVAL_SECONDS):
                problems.append(
                    f"申请间隔必须在 {MIN_INTERVAL_SECONDS}~{MAX_INTERVAL_SECONDS} 秒之间"
                    f"（当前 {self.interval_seconds}）"
                )
        if not self.ssh_public_key:
            problems.append("未配置 SSH 公钥路径")
        return problems


@dataclass
class State:
    """本地运行状态，保存于 ~/.oracle-arm/state.json。"""

    status: str = STATUS_IDLE
    reason: str = ""                  # 暂停/需人工处理的原因
    instance_id: str = ""
    public_ip: str = ""
    attempts: int = 0
    capacity_misses: int = 0
    last_attempt_at: str = ""         # 北京时间 ISO 字符串
    last_result: str = ""
    quota_verified: bool = False
    quota_summary: str = ""           # 形如 "A1 核心 已用 0/4，可用 4"
    plan_type: str = ""               # FREE / PAY_AS_YOU_GO（尽力查询）
    pending_retry_token: str = ""     # 超时对账用的幂等标识
    pending_since: str = ""
    updated_at: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "State":
        path = path or STATE_PATH
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return cls()  # 状态文件损坏不致命，重新开始即可
        if not isinstance(data, dict):
            return cls()  # 同理：状态文件不是对象时也退回默认，绝不抛异常
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path | None = None) -> Path:
        """原子写：状态文件被截断会丢掉 success/needs_attention 标记，
        进而让「成功后应停止申请」这条本地判断失效。"""
        path = path or STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tmp.replace(path)
        return path

    @property
    def finished(self) -> bool:
        """已拿到可用实例，或需人工处理 —— 两种情况都不该再自动申请。"""
        return self.status in (STATUS_SUCCESS, STATUS_NEEDS_ATTENTION, STATUS_ERROR)
