#!/bin/bash
# Oracle 免费 ARM 实例自动申请 —— 状态/操作入口（双击运行）
set -uo pipefail

cd "$(dirname "$0")" || exit 1
PROJECT_DIR="$(pwd)"
VENV="$HOME/.oracle-arm/venv"
PY="$VENV/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

run() { "$PY" -m oracle_arm "$@"; }

while true; do
  clear
  run status
  echo ""
  echo "--------------------------------------------------"
  echo " 1) 立即尝试一次（前台显示过程，可能等待较久）"
  echo " 2) 只做预检（不创建实例）"
  echo " 3) 暂停自动申请"
  echo " 4) 恢复自动申请"
  echo " 5) 查看日志（最近 50 行）"
  echo " 6) 环境与配置体检"
  echo " 0) 退出"
  echo "--------------------------------------------------"
  read -r -p "请选择: " choice
  case "$choice" in
    1) echo ""; run try; echo ""; read -n 1 -s -r -p "按任意键返回菜单..." ;;
    2) echo ""; run doctor; echo ""; read -n 1 -s -r -p "按任意键返回菜单..." ;;
    3) echo ""; run pause; sleep 1 ;;
    4) echo ""; run resume; sleep 1 ;;
    5) echo ""; run logs -n 50; echo ""; read -n 1 -s -r -p "按任意键返回菜单..." ;;
    6) echo ""; run doctor; echo ""; read -n 1 -s -r -p "按任意键返回菜单..." ;;
    0) exit 0 ;;
    *) echo "无效选项"; sleep 1 ;;
  esac
done
