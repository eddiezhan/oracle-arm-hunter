#!/bin/bash
# Oracle 免费 ARM 实例自动申请 —— 安装入口（双击运行）
# 做三件事：建虚拟环境装依赖 → 首次配置引导 → 注册 launchd 定时任务
set -uo pipefail

cd "$(dirname "$0")" || exit 1
PROJECT_DIR="$(pwd)"
BASE_DIR="$HOME/.oracle-arm"
VENV="$BASE_DIR/venv"
# pip 直连失败时尝试的代理；可用环境变量覆盖（不同 Clash 版本端口不同，7890 是默认值）
PROXY="${ORACLE_ARM_PIP_PROXY:-http://127.0.0.1:7890}"

echo "=================================================="
echo " Oracle 免费 ARM 实例自动申请 —— 安装"
echo "=================================================="
echo "项目目录：$PROJECT_DIR"
echo "数据目录：$BASE_DIR"
echo ""

# ── 1. 选择 Python（需要 3.9+；优先用 3.11） ─────────────────────────────────
find_python() {
  for cand in "$HOME/.local/bin/python3.11" "$(command -v python3.11 2>/dev/null)" \
              "$(command -v python3.12 2>/dev/null)" "$(command -v python3.13 2>/dev/null)" \
              "$(command -v python3 2>/dev/null)"; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      echo "$cand"; return 0
    fi
  done
  return 1
}

PYTHON="$(find_python)" || {
  echo "✗ 找不到 Python 3.9 及以上版本。"
  echo "  请先安装：xcode-select --install   （或从 python.org 安装 3.11）"
  echo ""
  read -n 1 -s -r -p "按任意键关闭窗口..."
  exit 1
}
echo "使用 Python：${PYTHON}（$("$PYTHON" -V 2>&1)）"

# ── 2. 建虚拟环境并安装依赖 ───────────────────────────────────────────────────
mkdir -p "$BASE_DIR"
if [ ! -x "$VENV/bin/python" ]; then
  echo "创建虚拟环境：$VENV"
  "$PYTHON" -m venv "$VENV" || { echo "✗ 创建虚拟环境失败"; read -n 1 -s -r -p "按任意键关闭..."; exit 1; }
fi

echo "安装依赖（OCI SDK）…"
if ! "$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1; then
  echo "· 升级 pip 失败，继续尝试安装依赖"
fi
if ! "$VENV/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"; then
  echo "· 直连安装失败，尝试走本机代理 $PROXY"
  if ! "$VENV/bin/pip" install -q --proxy "$PROXY" -r "$PROJECT_DIR/requirements.txt"; then
    echo "✗ 依赖安装失败。请确认网络（必要时开启 Clash 代理后重试）。"
    read -n 1 -s -r -p "按任意键关闭窗口..."
    exit 1
  fi
fi
echo "✓ 依赖就绪"
echo ""

# ── 3. 首次配置 + 注册定时任务 ────────────────────────────────────────────────
"$VENV/bin/python" -m oracle_arm install
STATUS=$?
echo ""
if [ $STATUS -eq 0 ]; then
  echo "✓ 安装完成。可以双击「状态.command」随时查看进度。"
else
  echo "⚠ 安装已注册定时任务，但预检未通过（退出码 ${STATUS}）。"
  echo "  请按上面提示处理后，双击「状态.command」→ 恢复自动申请。"
fi
echo ""
read -n 1 -s -r -p "按任意键关闭窗口..."
exit $STATUS
