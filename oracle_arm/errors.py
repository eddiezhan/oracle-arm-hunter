"""OCI 异常分类。

把 SDK 抛出的各种异常收敛成有限的几种处置动作，供 runner 决策：
容量不足等下一轮、429 退避重试、认证/配置/配额问题暂停并提示、
超时先对账再决定。分类逻辑集中在这里，便于单测覆盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import oci

# ── 处置类型（受控词表） ────────────────────────────────────────────────────────
CAPACITY = "capacity"          # 容量不足：本轮结束，等下一个申请时刻
RATE_LIMITED = "rate_limited"  # 429：按官方要求退避后可在本轮内重试
AUTH = "auth"                  # 认证/授权失败：暂停，需人工
CONFIG = "config"              # 配置/资源问题：暂停，需人工
QUOTA = "quota"                # 额度/配额不足：暂停，需人工
TRANSIENT = "transient"        # 超时/网络/5xx：先对账，禁止盲目重试
FATAL = "fatal"                # 其他未知错误：暂停并提示

# 容量不足的错误文案（OCI 在不同区域/时间的措辞略有差异）。
# 注意：不能只匹配 "capacity" —— 像 "Invalid capacityReservationId value" 这类
# 参数错误会被误判成容量不足，于是静默跳过、永远不通知用户。
_CAPACITY_RE = re.compile(
    r"out of host capacity|out of capacity|host capacity|insufficient capacity", re.I
)
_NOT_FOUND_RE = re.compile(r"notauthorizedornotfound|not found", re.I)


@dataclass
class Classified:
    """一次失败的处置建议。"""

    kind: str
    message: str
    retry_after: int | None = None   # 仅 RATE_LIMITED 有意义
    http_status: int | None = None
    code: str = ""

    @property
    def needs_human(self) -> bool:
        """是否需要人工介入（需要暂停定时任务）。"""
        return self.kind in (AUTH, CONFIG, QUOTA, FATAL)

    @property
    def retry_next_round(self) -> bool:
        """是否值得下一个申请时刻再试。"""
        return self.kind in (CAPACITY, RATE_LIMITED, TRANSIENT)


def is_capacity_message(text: str) -> bool:
    return bool(text) and bool(_CAPACITY_RE.search(text))


def is_not_found(exc: BaseException) -> bool:
    """资源确实不存在（用来判断「实例已被删除」）。

    必须严格要求 404：OCI 对「无权访问」也返回 code=NotAuthorizedOrNotFound，
    但那种情况实例其实还在，误判会丢掉 OCID 记录并给出错误诊断。
    """
    status = getattr(exc, "status", None)
    if status == 404:
        return True
    if status is None:  # 少数路径不带 status，退回看 code
        return bool(_NOT_FOUND_RE.search(str(getattr(exc, "code", "") or "")))
    return False


def _retry_after_from_headers(headers) -> int | None:
    """读取 Retry-After 响应头（OCI 返回秒数）。"""
    if not headers:
        return None
    try:
        items = headers.items() if hasattr(headers, "items") else headers
        for key, value in items:
            if str(key).lower() == "retry-after":
                return max(1, int(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return None


def classify(exc: BaseException, default_wait: int = 60) -> Classified:
    """把异常映射成处置建议。"""
    # 1) 认证/配置文件类异常（SDK 在构造 client 时抛出）
    if isinstance(exc, oci.exceptions.ConfigFileNotFound):
        return Classified(CONFIG, f"找不到 OCI 配置文件：{exc}")
    if isinstance(exc, oci.exceptions.ProfileNotFound):
        return Classified(CONFIG, f"OCI 配置里没有该 profile：{exc}")
    if isinstance(exc, oci.exceptions.InvalidConfig):
        return Classified(CONFIG, f"OCI 配置无效：{exc}")
    if isinstance(
        exc,
        (
            oci.exceptions.InvalidPrivateKey,
            oci.exceptions.InvalidKeyFilePath,
            oci.exceptions.MissingPrivateKeyPassphrase,
        ),
    ):
        return Classified(AUTH, f"OCI 私钥不可用（路径/格式/口令问题）：{exc}")

    # 等待实例进入目标状态超时：属于「需要先对账」的情况
    if isinstance(exc, oci.exceptions.MaximumWaitTimeExceeded):
        return Classified(TRANSIENT, f"等待超时：{exc}")

    # 2) SDK 判定为瞬时错误（持续 429/5xx 后的结果）
    if isinstance(exc, oci.exceptions.TransientServiceError):
        status = getattr(exc, "status", None)
        if status == 429:
            return Classified(
                RATE_LIMITED,
                f"请求过于频繁（429）：{exc}",
                retry_after=_retry_after_from_headers(getattr(exc, "headers", None)) or default_wait,
                http_status=status,
                code=str(getattr(exc, "code", "") or ""),
            )
        return Classified(TRANSIENT, f"服务端瞬时错误：{exc}", http_status=status)

    # 3) 网络/超时
    if isinstance(exc, oci.exceptions.RequestException):
        return Classified(TRANSIENT, f"网络或超时错误：{exc}")

    # 4) 服务端返回的错误
    status = getattr(exc, "status", None)
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or exc)
    headers = getattr(exc, "headers", None)

    if status == 429 or code == "TooManyRequests":
        return Classified(
            RATE_LIMITED,
            f"请求过于频繁（429）：{message}",
            retry_after=_retry_after_from_headers(headers) or default_wait,
            http_status=status,
            code=code,
        )

    # 容量不足的语义比错误码更特异（正则已收紧，不会误吞 "Invalid capacityReservationId"
    # 这类参数错误），所以放在最前：无论 OCI 用 500/400/哪种 code 报，都能正确识别
    if is_capacity_message(message):
        return Classified(CAPACITY, f"容量不足：{message}", http_status=status, code=code)

    if code == "LimitExceeded" or code == "QuotaExceeded":
        return Classified(QUOTA, f"额度/配额不足：{message}", http_status=status, code=code)

    if status == 401 or code == "NotAuthenticated":
        return Classified(AUTH, f"认证失败：{message}", http_status=status, code=code)
    if status == 403 or code == "NotAuthorized":
        return Classified(AUTH, f"权限不足：{message}", http_status=status, code=code)
    # 参数/资源类错误必须先于容量判断：它们的报文里可能恰好含 capacity 字样
    if status == 400 or code in ("InvalidParameter", "MissingParameter"):
        return Classified(CONFIG, f"请求参数无效：{message}", http_status=status, code=code)
    if status == 404 or _NOT_FOUND_RE.search(code):
        return Classified(CONFIG, f"资源不存在或不可见：{message}", http_status=status, code=code)
    if status == 409 or code in ("Conflict", "IncorrectState"):
        return Classified(CONFIG, f"资源状态冲突：{message}", http_status=status, code=code)

    if status is not None and status >= 500:
        return Classified(TRANSIENT, f"服务端错误 {status}：{message}", http_status=status, code=code)

    return Classified(FATAL, f"未预期的错误：{message}", http_status=status, code=code)
