"""Oracle Cloud 免费 ARM 实例自动申请工具（macOS）。

目标：在新加坡 ap-singapore-1 申请 Always Free 的 VM.Standard.A1.Flex
（2 OCPU / 12GB / 50GB 启动盘 / 公网 IPv4），默认每 5 分钟抢一轮，
成功拿到公网 IPv4 后自动停止定时任务。
"""

__version__ = "1.0.0"
APP_NAME = "oracle-arm"
