"""macOS launchd 定时任务管理。

定时时刻按本机时区写入 StartCalendarInterval，换算自北京时间（见 util.beijing_to_local_times），
所以本机时区不是 UTC+8 时也能在正确的北京时间触发。
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod


@dataclass
class LaunchdConfig:
    label: str
    plist_path: Path
    python: str            # venv 里的 python 绝对路径
    workdir: str           # 项目目录（保证能找到 oracle_arm 包）
    schedule: list[tuple[int, int]] = field(default_factory=list)  # 本机时区的 (时, 分)
    # 持续抢模式：每 N 秒跑一轮（StartInterval）。设置后忽略 schedule。
    interval_seconds: int | None = None
    # 用 default_factory 在实例化时才取值，便于测试替换 LOG_DIR
    stdout_path: Path = field(default_factory=lambda: cfg_mod.LOG_DIR / "launchd.out.log")
    stderr_path: Path = field(default_factory=lambda: cfg_mod.LOG_DIR / "launchd.err.log")


def build_plist(cfg: LaunchdConfig) -> bytes:
    """生成 plist 内容。RunAtLoad=False：不在登录时立刻跑。

    - interval 模式用 StartInterval（每 N 秒一轮，持续抢）
    - fixed 模式用 StartCalendarInterval（每天固定时刻）
    """
    trigger: dict = (
        {"StartInterval": int(cfg.interval_seconds)}
        if cfg.interval_seconds
        else {"StartCalendarInterval": [
            {"Hour": hour, "Minute": minute} for hour, minute in cfg.schedule
        ]}
    )
    payload = {
        "Label": cfg.label,
        "ProgramArguments": [cfg.python, "-m", "oracle_arm", "run"],
        "WorkingDirectory": cfg.workdir,
        "EnvironmentVariables": {
            "PYTHONPATH": cfg.workdir,
            "PYTHONUNBUFFERED": "1",
            "LC_ALL": "en_US.UTF-8",
        },
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(cfg.stdout_path),
        "StandardErrorPath": str(cfg.stderr_path),
    }
    payload.update(trigger)
    return plistlib.dumps(payload)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def is_loaded(label: str = cfg_mod.LAUNCHD_LABEL) -> bool:
    """定时任务当前是否已加载（暂停后返回 False，但 plist 文件仍在）。"""
    return _run(["launchctl", "print", f"{_domain()}/{label}"]).returncode == 0


def bootout(label: str = cfg_mod.LAUNCHD_LABEL) -> None:
    """卸载已加载的任务；未加载时静默返回（保证安装可重复执行）。"""
    _run(["launchctl", "bootout", f"{_domain()}/{label}"])


def disable(label: str = cfg_mod.LAUNCHD_LABEL) -> None:
    """持久禁用：仅 bootout 是不够的——plist 还留在 ~/Library/LaunchAgents，
    下次登录 macOS 会自动把它加载回来（成功后会被「复活」）。disable 状态由
    launchd 单独记录，跨登录/重启保持，必须配 enable 才能恢复。"""
    _run(["launchctl", "disable", f"{_domain()}/{label}"])


def enable(label: str = cfg_mod.LAUNCHD_LABEL) -> None:
    _run(["launchctl", "enable", f"{_domain()}/{label}"])


def bootstrap(plist_path: Path, label: str = cfg_mod.LAUNCHD_LABEL) -> None:
    """加载任务；先清除禁用状态并卸载旧的，避免产生多个任务或加载不上。"""
    enable(label)
    bootout(label)
    result = _run(["launchctl", "bootstrap", _domain(), str(plist_path)])
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap 失败：{result.stderr.strip() or result.stdout.strip()}")


def install(cfg: LaunchdConfig) -> Path:
    """写入 plist 并加载。可重复执行：先卸载旧任务，不会产生多个定时任务。"""
    cfg.plist_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.stdout_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.plist_path.write_bytes(build_plist(cfg))
    bootstrap(cfg.plist_path, cfg.label)
    return cfg.plist_path


def uninstall(label: str = cfg_mod.LAUNCHD_LABEL, plist_path: Path | None = None) -> bool:
    """卸载后台任务。只动本机定时任务，不触碰任何云端实例。"""
    plist_path = plist_path or cfg_mod.LAUNCHD_PLIST
    bootout(label)
    if plist_path.exists():
        plist_path.unlink()
        return True
    return False


def pause(label: str = cfg_mod.LAUNCHD_LABEL) -> None:
    """暂停并持久禁用（跨登录/重启有效），避免下次登录被自动加载回来。

    顺序很关键：必须先 disable 再 bootout。本函数常在定时任务自己的进程里被调用，
    bootout 会让 launchd 终止该进程，写在后面的 disable 就可能永远执行不到。
    """
    disable(label)
    bootout(label)


def resume(cfg: LaunchdConfig) -> None:
    if not cfg.plist_path.exists():
        raise RuntimeError("定时任务未安装，请先运行「安装.command」")
    bootstrap(cfg.plist_path, cfg.label)


def kickstart(label: str = cfg_mod.LAUNCHD_LABEL) -> None:
    """立刻触发一次（不等待定时时刻）。"""
    _run(["launchctl", "kickstart", "-k", f"{_domain()}/{label}"])
