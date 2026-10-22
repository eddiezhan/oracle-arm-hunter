#!/bin/bash
# Oracle 免费 ARM 实例自动申请 —— 卸载入口（双击运行）
# 只卸载本机后台定时任务；不删除、不停机任何云端实例
set -uo pipefail

cd "$(dirname "$0")" || exit 1
VENV="$HOME/.oracle-arm/venv"
PY="$VENV/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

echo "=================================================="
echo " 卸载 Oracle ARM 自动申请"
echo "=================================================="
echo "将卸载本机 launchd 定时任务。"
echo "云端实例、配置文件和日志都会保留，不会被删除。"
echo ""
read -r -p "确认卸载？(y/N): " ans
case "$ans" in
  y|Y) ;;
  *) echo "已取消。"; sleep 1; exit 0 ;;
esac

"$PY" -m oracle_arm uninstall
echo ""
read -n 1 -s -r -p "按任意键关闭窗口..."
