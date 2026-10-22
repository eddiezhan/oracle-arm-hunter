"""日志、macOS 系统通知与时间工具。

所有日志写入 ~/.oracle-arm/logs/，绝不记录私钥内容（见 redact()）。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 北京时间固定 UTC+8（需求指定），不随本机时区变化
BEIJING_TZ = timezone(timedelta(hours=8))

_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def beijing_now() -> datetime:
    """当前北京时间（带时区）。"""
    return datetime.now(tz=BEIJING_TZ)


def local_now() -> datetime:
    """当前本机时间（带本机时区）。"""
    return datetime.now().astimezone()


def beijing_to_local_times(times: list[str], now: datetime | None = None) -> list[tuple[int, int]]:
    """把北京时间 "HH:MM" 列表换算成本机时区的 (时, 分)。

    launchd 的 StartCalendarInterval 按本机时区解释，所以必须先换算。
    本机是 Asia/Shanghai 时结果与输入相同；其他时区则整体平移，
    保证实际执行时刻仍是北京时间的那 4 个点。
    """
    now = now or local_now()
    offset = now.utcoffset() or timedelta(0)
    # 本机时区相对北京时间的偏移量
    delta = offset - timedelta(hours=8)

    result: list[tuple[int, int]] = []
    for raw in times:
        try:
            hh, mm = (int(x) for x in str(raw).split(":"))
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError(raw)
        except (ValueError, TypeError):
            continue  # 坏数据跳过：配置校验会单独报错，状态页不能因此崩掉
        shifted = datetime(2000, 1, 1, hh, mm) + delta
        pair = (shifted.hour, shifted.minute)
        if pair not in result:  # 极端时区下可能撞车，去重避免 launchd 重复触发
            result.append(pair)
    return sorted(result)


def next_run_time(times: list[str], now: datetime | None = None) -> datetime | None:
    """下一次申请时刻（北京时间）；时刻全部非法时返回 None（状态页不能因此崩掉）。"""
    now = now or beijing_now()
    today = now.date()
    candidates = []
    for raw in times:
        try:
            hh, mm = (int(x) for x in str(raw).split(":"))
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError(raw)
        except (ValueError, TypeError):
            continue
        candidates.append(datetime(today.year, today.month, today.day, hh, mm, tzinfo=BEIJING_TZ))
    if not candidates:
        return None
    for c in sorted(candidates):
        if c > now:
            return c
    # 今天已过完，取明天第一个
    return sorted(candidates)[0] + timedelta(days=1)


# 私钥/口令类内容的兜底脱敏：任何日志与异常文本都先过一遍
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    # 关键字可能带前缀（api_secret、opc_retry_token），因此不加 \b
    re.compile(r"(?i)(pass[_ ]?phrase|password|secret|token)\s*[:=]\s*\S+"),
]


def redact(text: str) -> str:
    """脱敏：私钥块与 password/secret/token 赋值一律替换掉。"""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def setup_logger(log_path, verbose: bool = False, to_console: bool = False) -> logging.Logger:
    """构造带滚动切割的文件日志（同时可选输出到终端）。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("oracle-arm")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(fh)

    if to_console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        sh.setLevel(logging.DEBUG if verbose else logging.INFO)
        logger.addHandler(sh)

    return logger


def notify(title: str, message: str, enabled: bool = True) -> None:
    """发送 macOS 系统通知；失败不影响主流程。"""
    if not enabled:
        return
    safe_msg = redact(message).replace('"', "'")
    safe_title = redact(title).replace('"', "'")
    script = f'display notification "{safe_msg}" with title "{safe_title}" sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True, check=False)
    except Exception:  # 通知失败（无 GUI 会话等）不应影响申请结果
        pass


class RunLock:
    """一轮申请的互斥锁。

    防止两轮申请重叠（例如上一轮还在等实例 RUNNING，下一次定时又触发），
    否则可能用不同的幂等标识创建出两个实例 —— 这是本工具最需要避免的后果。
    锁文件里记录 pid 与开始时间；超过 stale_after 秒视为上一轮异常退出的残留，自动接管。
    """

    def __init__(self, path: Path, stale_after: int = 1800):
        self.path = Path(path)
        self.stale_after = stale_after
        self.acquired = False

    def acquire(self) -> bool:
        try:
            fd = self._create()
        except FileExistsError:
            if not self._is_stale():
                return False
            try:  # 残留锁：清理后重试一次
                self.path.unlink()
            except FileNotFoundError:
                pass
            try:
                fd = self._create()
            except FileExistsError:
                return False
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{os.getpid()} {beijing_now().isoformat()}\n")
        self.acquired = True
        return True

    def _create(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

    def touch(self) -> None:
        """刷新锁的 mtime（心跳）。

        一轮里可能睡很久（429 退避、等待实例 RUNNING、轮询公网 IP），
        而 _is_stale 只看 mtime；不刷新的话下一轮会把「还活着的一轮」误判成残留锁并接管，
        两个进程并发就可能各建一个实例。
        """
        if not self.acquired:
            return
        try:
            os.utime(self.path, None)
        except OSError:
            pass

    def _is_stale(self) -> bool:
        try:
            return (time.time() - self.path.stat().st_mtime) > self.stale_after
        except OSError:
            return True  # 文件刚被删掉，视为可接管

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.acquired = False

    def __enter__(self) -> "RunLock":
        if not self.acquire():
            raise RuntimeError("已有另一轮申请在运行中，拒绝并发执行")
        return self

    def __exit__(self, *exc_info) -> bool:
        self.release()
        return False
