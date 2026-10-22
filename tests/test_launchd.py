"""launchd 定时任务的生成与安装逻辑测试（不真的注册任务）。"""

import plistlib
from pathlib import Path

import pytest

from oracle_arm import config as cfg_mod
from oracle_arm import launchd as launchd_mod


@pytest.fixture
def ld_cfg(tmp_path):
    return launchd_mod.LaunchdConfig(
        label="com.oracle-arm.hunter",
        plist_path=tmp_path / "com.oracle-arm.hunter.plist",
        python="/Users/x/.oracle-arm/venv/bin/python",
        workdir="/Users/x/code/arm-hunter",
        schedule=[(2, 5), (4, 55), (8, 5), (20, 45)],
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
    )


def test_plist_has_four_schedule_entries(ld_cfg):
    data = plistlib.loads(launchd_mod.build_plist(ld_cfg))
    assert data["StartCalendarInterval"] == [
        {"Hour": 2, "Minute": 5},
        {"Hour": 4, "Minute": 55},
        {"Hour": 8, "Minute": 5},
        {"Hour": 20, "Minute": 45},
    ]


def test_plist_runs_module_without_loading_at_login(ld_cfg):
    data = plistlib.loads(launchd_mod.build_plist(ld_cfg))
    assert data["ProgramArguments"][1:] == ["-m", "oracle_arm", "run"]
    assert data["RunAtLoad"] is False          # 只在指定时刻运行
    assert data["Label"] == "com.oracle-arm.hunter"
    assert data["WorkingDirectory"] == "/Users/x/code/arm-hunter"
    assert data["EnvironmentVariables"]["PYTHONPATH"] == "/Users/x/code/arm-hunter"


def test_plist_paths_are_absolute(ld_cfg):
    data = plistlib.loads(launchd_mod.build_plist(ld_cfg))
    assert Path(data["StandardOutPath"]).is_absolute()
    assert Path(data["StandardErrorPath"]).is_absolute()


def test_install_writes_plist_and_bootstraps(ld_cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(launchd_mod, "_run", lambda args: calls.append(args) or _ok())
    path = launchd_mod.install(ld_cfg)
    assert path.exists()
    assert plistlib.loads(path.read_bytes())["Label"] == ld_cfg.label
    assert any("bootstrap" in c for c in calls)
    assert any("bootout" in c for c in calls)   # 先卸载旧任务，避免重复
    assert any("enable" in c for c in calls)    # 清除可能残留的禁用状态


def test_install_is_idempotent(ld_cfg, monkeypatch):
    """重复安装不应产生多个定时任务：每次都是 bootout 再 bootstrap。"""
    calls = []
    monkeypatch.setattr(launchd_mod, "_run", lambda args: calls.append(args) or _ok())
    launchd_mod.install(ld_cfg)
    launchd_mod.install(ld_cfg)
    assert sum(1 for c in calls if "bootstrap" in c) == 2
    assert sum(1 for c in calls if "bootout" in c) == 2
    plists = list(ld_cfg.plist_path.parent.glob("*.plist"))
    assert len(plists) == 1


def test_install_raises_when_bootstrap_fails(ld_cfg, monkeypatch):
    def fake_run(args):
        return _ok(returncode=1, stderr="Bootstrap failed: 5: Input/output error") if "bootstrap" in args else _ok()

    monkeypatch.setattr(launchd_mod, "_run", fake_run)
    with pytest.raises(RuntimeError, match="bootstrap"):
        launchd_mod.install(ld_cfg)


def test_uninstall_removes_plist_and_boots_out(ld_cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(launchd_mod, "_run", lambda args: calls.append(args) or _ok())
    ld_cfg.plist_path.write_bytes(launchd_mod.build_plist(ld_cfg))
    assert launchd_mod.uninstall(ld_cfg.label, ld_cfg.plist_path) is True
    assert not ld_cfg.plist_path.exists()
    assert any("bootout" in c for c in calls)


def test_uninstall_without_plist_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd_mod, "_run", lambda args: _ok())
    assert launchd_mod.uninstall("com.x", tmp_path / "none.plist") is False


def test_pause_boots_out_and_persistently_disables(ld_cfg, monkeypatch):
    """暂停必须同时 disable：只 bootout 的话下次登录 macOS 会把任务加载回来。"""
    calls = []
    monkeypatch.setattr(launchd_mod, "_run", lambda args: calls.append(args) or _ok())
    ld_cfg.plist_path.write_bytes(launchd_mod.build_plist(ld_cfg))
    launchd_mod.pause(ld_cfg.label)
    assert any("bootout" in c for c in calls)
    assert any("disable" in c for c in calls)
    assert not any("bootstrap" in c for c in calls)
    assert ld_cfg.plist_path.exists()          # 暂停不删除，恢复时还能用


def test_resume_enables_before_bootstrap(ld_cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(launchd_mod, "_run", lambda args: calls.append(args) or _ok())
    ld_cfg.plist_path.write_bytes(launchd_mod.build_plist(ld_cfg))
    launchd_mod.resume(ld_cfg)
    kinds = [c[1] for c in calls]
    assert "enable" in kinds and "bootstrap" in kinds
    assert kinds.index("enable") < kinds.index("bootstrap")


def test_resume_requires_installed_plist(tmp_path):
    cfg = launchd_mod.LaunchdConfig(label="x", plist_path=tmp_path / "nope.plist",
                                    python="/p", workdir="/w")
    with pytest.raises(RuntimeError, match="未安装"):
        launchd_mod.resume(cfg)


def test_is_loaded_uses_launchctl_print(monkeypatch):
    seen = []

    def fake_run(args):
        seen.append(args)
        return _ok(returncode=0 if "print" in args else 1)

    monkeypatch.setattr(launchd_mod, "_run", fake_run)
    assert launchd_mod.is_loaded("com.oracle-arm.hunter") is True
    assert seen[0][:2] == ["launchctl", "print"]
    assert seen[0][2].startswith("gui/")


def _ok(returncode=0, stderr="", stdout=""):
    import subprocess
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_pause_disables_before_bootout(ld_cfg, monkeypatch):
    """必须先 disable 再 bootout：bootout 会让 launchd 终止当前进程，
    写在后面的 disable 可能永远执行不到，任务就会在下次登录被复活。"""
    calls = []
    monkeypatch.setattr(launchd_mod, "_run", lambda args: calls.append(args) or _ok())
    launchd_mod.pause(ld_cfg.label)
    kinds = [c[1] for c in calls]
    assert kinds.index("disable") < kinds.index("bootout")


def test_plist_uses_start_interval_in_interval_mode(tmp_path):
    """持续抢模式用 StartInterval（每 N 秒一轮），不是每天固定时刻。"""
    cfg = launchd_mod.LaunchdConfig(
        label="com.oracle-arm.hunter", plist_path=tmp_path / "x.plist",
        python="/p", workdir="/w", schedule=[], interval_seconds=300,
    )
    data = plistlib.loads(launchd_mod.build_plist(cfg))
    assert data["StartInterval"] == 300
    assert "StartCalendarInterval" not in data
    assert data["RunAtLoad"] is False


def test_plist_prefers_interval_when_both_set(tmp_path):
    cfg = launchd_mod.LaunchdConfig(
        label="x", plist_path=tmp_path / "x.plist", python="/p", workdir="/w",
        schedule=[(2, 5)], interval_seconds=120,
    )
    data = plistlib.loads(launchd_mod.build_plist(cfg))
    assert data["StartInterval"] == 120
    assert "StartCalendarInterval" not in data
