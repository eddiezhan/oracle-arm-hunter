"""Telegram 电报推送（可选功能）。

需求（2026-10-22）：抢到实例后除了 macOS 系统通知，还要发一条电报消息，
这样人不在电脑前也能第一时间知道。

设计要点：
1. **凭据不进代码、不进 git**：bot_token / chat_id 从
   `~/.oracle-arm/config.json` 的 `telegram_*` 字段读，缺失时本模块整体关闭。
   用自己的机器人即可：在 @BotFather 建一个 bot 拿 token，
   chat_id / message_thread_id 填成要收消息的会话。
2. **只用标准库**：本项目的依赖只有 OCI SDK（见 requirements.txt），
   为一条通知引入 requests/httpx 不值得，用 urllib 直接 POST 官方 sendMessage。
3. **代理可选**：直连 api.telegram.org 不通时，在配置里填 telegram_proxy
   （例如 http://127.0.0.1:7890）即可走代理；留空则直连。
4. **失败绝不影响主流程**：通知发不出去最多丢一条消息，
   但绝不能因此让「已经申请成功」这一结论丢失或让进程报错。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config as cfg_mod

# Telegram 官方 sendMessage 接口（bot_token 拼在路径里）
_API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"

# 单次请求超时（秒）。抢到实例时进程还要写状态、停表，不能在这里长时间阻塞。
_TIMEOUT = 15


class TelegramNotifier:
    """把消息发到 Telegram；未配置或发送失败都静默降级。"""

    def __init__(self, cfg: cfg_mod.Config, log=None):
        self.token = (cfg.telegram_bot_token or "").strip()
        self.chat_id = (cfg.telegram_chat_id or "").strip()
        self.thread_id = cfg.telegram_message_thread_id
        self.proxy = (cfg.telegram_proxy or "").strip()
        self.log = log
        # 三个必填项缺一不可；thread_id 是可选的话题 ID（群里的话题消息）
        self.enabled = bool(self.token and self.chat_id)

    def _log(self, msg: str, level: str = "warning") -> None:
        if self.log is not None:
            getattr(self.log, level)(msg)

    def send(self, text: str) -> bool:
        """发送一条纯文本消息。返回是否真的发成功（失败不抛异常）。"""
        if not self.enabled:
            self._log("未配置 Telegram（telegram_bot_token / telegram_chat_id），跳过电报通知", "debug")
            return False

        body = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
            # 不用 Markdown/HTML：实例名与 IP 里可能出现 _ - . 等字符，
            # 解析失败会被 Telegram 直接拒收（400 can't parse entities），得不偿失
        }
        if self.thread_id:
            body["message_thread_id"] = int(self.thread_id)
        data = json.dumps(body).encode("utf-8")

        # 代理只影响 Telegram，不能污染 OCI SDK 的请求，所以用只装代理的 opener
        handlers = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))   # 显式不用环境变量里的代理
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(
            _API_TEMPLATE.format(token=self.token),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(req, timeout=_TIMEOUT) as resp:
                ok = 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 时把 Telegram 的说明带进日志，但**绝不打印 URL**（含 token）
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            self._log(f"电报发送失败（HTTP {exc.code}）：{detail}", "warning")
            return False
        except Exception as exc:
            # 超时/代理未开/DNS 失败等都走这里：只记异常类型与文字，不记 URL
            self._log(f"电报发送失败：{exc!r}", "warning")
            return False
        if not ok:
            self._log(f"电报发送失败（HTTP {resp.status}）", "warning")
        return ok


def notify_success(cfg: cfg_mod.Config, log, reason: str, instance_id: str,
                   public_ip: str, shape: str) -> bool:
    """申请成功后的电报正文。集中在这里，便于测试断言内容。"""
    text = (
        "🎉 Oracle 免费 ARM 实例已抢到\n"
        f"规格：{shape}\n"
        f"公网 IPv4：{public_ip}\n"
        f"实例 OCID：{instance_id}\n"
        f"{reason}\n"
        "定时任务已自动停止，不会继续申请。"
    )
    return TelegramNotifier(cfg, log=log).send(text)
