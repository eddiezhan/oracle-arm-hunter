"""时间换算、脱敏与日志测试。"""

import os
import time
from datetime import datetime, timedelta, timezone

from oracle_arm import util


def test_beijing_times_unchanged_for_utc8():
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    assert util.beijing_to_local_times(["02:05", "04:55", "08:05", "20:45"], now) == [
        (2, 5), (4, 55), (8, 5), (20, 45),
    ]


def test_beijing_times_shifted_for_other_timezones():
    # 本机 UTC-5（纽约冬令时）：北京 20:45 → 当天 07:45；北京 02:05 → 前一天 13:05
    # 返回值按本机时刻排序，便于展示
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert util.beijing_to_local_times(["02:05", "20:45"], now) == [(7, 45), (13, 5)]


def test_beijing_times_dedupe_on_collision():
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    assert util.beijing_to_local_times(["02:05", "02:05"], now) == [(2, 5)]


def test_next_run_time_same_day():
    now = datetime(2026, 10, 17, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    nxt = util.next_run_time(["02:05", "04:55", "08:05", "20:45"], now)
    assert (nxt.hour, nxt.minute, nxt.day) == (20, 45, 17)


def test_next_run_time_rolls_to_next_day():
    now = datetime(2026, 10, 17, 23, 30, tzinfo=timezone(timedelta(hours=8)))
    nxt = util.next_run_time(["02:05", "04:55", "08:05", "20:45"], now)
    assert (nxt.hour, nxt.minute, nxt.day) == (2, 5, 18)


def test_redact_removes_private_key_blocks():
    text = "key:\n-----BEGIN PRIVATE KEY-----\nAAAABBBB\n-----END PRIVATE KEY-----\ndone"
    out = util.redact(text)
    assert "AAAABBBB" not in out
    assert "[REDACTED]" in out


def test_redact_removes_password_assignments():
    assert "hunter2" not in util.redact("password: hunter2")
    assert "s3cr3t" not in util.redact("api_secret=s3cr3t")
    assert "abc123" not in util.redact("opc_retry_token: abc123")


def test_setup_logger_writes_file(tmp_path):
    log = util.setup_logger(tmp_path / "logs" / "x.log")
    log.info("hello 中文")
    assert "hello 中文" in (tmp_path / "logs" / "x.log").read_text(encoding="utf-8")


# ── 运行互斥锁 ─────────────────────────────────────────────────────────────────
def test_run_lock_is_exclusive(tmp_path):
    path = tmp_path / "run.lock"
    a, b = util.RunLock(path), util.RunLock(path)
    assert a.acquire() is True
    assert b.acquire() is False          # 第二轮不能同时跑，避免重复创建实例
    a.release()
    assert b.acquire() is True
    b.release()


def test_run_lock_released_after_context(tmp_path):
    path = tmp_path / "run.lock"
    with util.RunLock(path):
        assert path.exists()
    assert not path.exists()
    assert util.RunLock(path).acquire() is True


def test_run_lock_takes_over_stale_lock(tmp_path):
    """上一轮异常退出留下的锁超过 30 分钟 → 自动接管，不会永久卡死。"""
    path = tmp_path / "run.lock"
    path.write_text("99999 old", encoding="utf-8")
    old = time.time() - 3600
    os.utime(path, (old, old))
    lock = util.RunLock(path, stale_after=1800)
    assert lock.acquire() is True


def test_run_lock_respects_fresh_lock(tmp_path):
    path = tmp_path / "run.lock"
    path.write_text("99999 fresh", encoding="utf-8")
    assert util.RunLock(path, stale_after=1800).acquire() is False


def test_run_lock_file_is_private(tmp_path):
    path = tmp_path / "run.lock"
    lock = util.RunLock(path)
    lock.acquire()
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_beijing_times_skips_invalid_entries():
    """坏时刻不能让状态页崩掉（配置校验会单独报错）。"""
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    assert util.beijing_to_local_times(["25:00", "abc", "08:05", ""], now) == [(8, 5)]


def test_next_run_time_returns_none_when_all_invalid():
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    assert util.next_run_time(["25:00", "abc"], now) is None
    assert util.next_run_time(["25:00", "08:05"], now).hour == 8


def test_run_lock_touch_keeps_it_fresh(tmp_path):
    """一轮睡很久时必须靠心跳保鲜，否则会被误判成残留锁而并发接管。"""
    path = tmp_path / "run.lock"
    lock = util.RunLock(path, stale_after=100)
    lock.acquire()
    old = time.time() - 200
    os.utime(path, (old, old))
    lock.touch()
    assert util.RunLock(path, stale_after=100).acquire() is False


def test_run_lock_touch_is_noop_when_not_acquired(tmp_path):
    path = tmp_path / "run.lock"
    util.RunLock(path).touch()          # 不应创建文件
    assert not path.exists()
