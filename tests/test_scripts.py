"""回归测试：shell 脚本里「裸 $VAR 紧跟非 ASCII 字符」会被 bash 当成变量名的一部分。

背景（2026-10-17 重启 Mac 后暴露）：macOS 自带 bash 3.2 在 UTF-8 locale 下会把全角
「（」的首字节算作合法变量名字符，于是 `"$PYTHON（"` 被解析成变量 `PYTHON（`，
配合脚本开头的 `set -uo pipefail` 直接报 `PYTHON（: unbound variable`——
安装脚本第 39 行就这么挂掉，看起来像「重启后坏了」，其实是 locale 差异：
C locale（无 LANG/LC_ALL）下反而一切正常。
修法是给变量加花括号：`"${PYTHON}（"`。

本测试按字节扫描仓库里所有 shell 脚本（含 .app 内的 launcher），防止同类写法再混进来。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 裸 $VAR（不带花括号）后面紧跟非 ASCII 字节 → 变量名会被多吞字符
DANGEROUS = re.compile(rb"\$[A-Za-z_][A-Za-z0-9_]*[\x80-\xff]")

# 不扫虚拟环境与缓存目录（几千个文件，且不是本仓库的脚本）
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache"}


def shell_scripts() -> list[Path]:
    """按 shebang 找出仓库内所有 shell 脚本（.command 入口、.app 里的 launcher、将来的 .sh）。"""
    found = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(root) / name
            try:
                first = path.open("rb").readline()
            except OSError:
                continue
            if first.startswith(b"#!") and b"sh" in first:
                found.append(path)
    return sorted(found)


def test_shell_scripts_are_discovered():
    """防止 glob 写错导致后面的扫描静默通过（扫不到文件 = 假绿）。"""
    names = {p.name for p in shell_scripts()}
    assert {"安装.command", "状态.command", "卸载.command"} <= names
    assert len(shell_scripts()) >= 6


def test_no_bare_var_followed_by_non_ascii():
    """$VAR 紧跟中文标点（如 `$PYTHON（`）在 UTF-8 locale 下会报 unbound variable。"""
    offenders = []
    for path in shell_scripts():
        for lineno, line in enumerate(path.read_bytes().splitlines(), 1):
            if DANGEROUS.search(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.decode('utf-8', 'replace')}")
    assert not offenders, (
        "以下位置是「裸 $VAR 紧跟非 ASCII 字符」，UTF-8 locale 下会被 bash 当成变量名的一部分：\n"
        + "\n".join(offenders)
        + "\n改法：给变量加花括号，例如 $PYTHON（ → ${PYTHON}（"
    )


@pytest.mark.parametrize("punct", ["（", "）"])
def test_braced_var_before_fullwidth_punct_runs(tmp_path, punct):
    """花括号写法必须能在 UTF-8 locale 下跑通（对照上面那条：裸写法会挂）。"""
    script = tmp_path / "ok.sh"
    script.write_text(f'set -uo pipefail\nV=hi\necho "${{V}}{punct}"\n', encoding="utf-8")
    proc = subprocess.run(
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        env={"LC_ALL": "en_US.UTF-8", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"hi{punct}"
