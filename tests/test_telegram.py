"""Telegram 电报通知的测试。

全部打桩 urllib，不发出任何真实网络请求（尤其不会碰真实 bot_token）。
"""

from __future__ import annotations

import json

import pytest

from oracle_arm import config as cfg_mod
from oracle_arm import telegram as telegram_mod


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """记录发出的请求，按预设行为返回或抛错。"""

    def __init__(self, status=200, error=None):
        self.status = status
        self.error = error
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.status)


@pytest.fixture
def patched(monkeypatch):
    """把 build_opener 换成假实现，返回一个可配置的容器。"""
    state = {"opener": FakeOpener(), "proxy": None}

    def build(*handlers):
        state["proxy"] = handlers[0] if handlers else None
        return state["opener"]

    monkeypatch.setattr(telegram_mod.urllib.request, "build_opener", build)
    return state


def _cfg(**kw):
    c = cfg_mod.Config(telegram_bot_token="123:ABC", telegram_chat_id="-100999")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ── 配置门禁 ───────────────────────────────────────────────────────────────────
def test_disabled_without_credentials(patched):
    """缺 token 或 chat_id 时整体关闭，不发出任何请求。"""
    for kw in ({"telegram_bot_token": ""}, {"telegram_chat_id": ""}):
        cfg = _cfg(**kw)
        assert telegram_mod.TelegramNotifier(cfg).enabled is False
        assert telegram_mod.TelegramNotifier(cfg).send("x") is False
    assert patched["opener"].requests == []


def test_enabled_with_both_credentials():
    assert telegram_mod.TelegramNotifier(_cfg()).enabled is True


# ── 请求内容 ───────────────────────────────────────────────────────────────────
def test_sends_expected_payload(patched):
    assert telegram_mod.TelegramNotifier(_cfg()).send("hello") is True

    req = patched["opener"].requests[0]
    assert req.method == "POST"
    # token 拼在 URL 路径里（Telegram 官方约定）
    assert req.full_url == "https://api.telegram.org/bot123:ABC/sendMessage"
    body = json.loads(req.data.decode("utf-8"))
    assert body["chat_id"] == "-100999"
    assert body["text"] == "hello"
    # 不用 Markdown/HTML：实例名/IP 里的 _ - . 会让 Telegram 解析失败而拒收
    assert "parse_mode" not in body


def test_thread_id_is_included_only_when_set(patched):
    telegram_mod.TelegramNotifier(_cfg()).send("a")
    assert "message_thread_id" not in json.loads(patched["opener"].requests[0].data)

    patched["opener"].requests.clear()
    telegram_mod.TelegramNotifier(_cfg(telegram_message_thread_id=8353)).send("b")
    assert json.loads(patched["opener"].requests[0].data)["message_thread_id"] == 8353


def test_proxy_is_used(patched):
    """访问 api.telegram.org 必须走本机代理，直连会超时。"""
    telegram_mod.TelegramNotifier(_cfg(telegram_proxy="http://127.0.0.1:7890")).send("x")
    assert isinstance(patched["proxy"], telegram_mod.urllib.request.ProxyHandler)


# ── 失败绝不影响主流程 ─────────────────────────────────────────────────────────
def test_http_error_returns_false_and_logs(patched, log):
    patched["opener"] = FakeOpener(error=telegram_mod.urllib.error.HTTPError(
        "u", 400, "Bad Request", {}, None))
    assert telegram_mod.TelegramNotifier(_cfg(), log=log).send("x") is False
    assert "电报发送失败" in log.text


def test_network_error_returns_false_and_logs(patched, log):
    patched["opener"] = FakeOpener(error=OSError("Connection refused"))
    assert telegram_mod.TelegramNotifier(_cfg(), log=log).send("x") is False
    assert "电报发送失败" in log.text


def test_token_never_appears_in_log(patched, log):
    """失败日志里绝不能出现 bot_token（URL 含 token）。"""
    patched["opener"] = FakeOpener(error=OSError("boom"))
    telegram_mod.TelegramNotifier(_cfg(), log=log).send("x")
    assert "123:ABC" not in log.text


# ── 成功通知正文 ───────────────────────────────────────────────────────────────
def test_notify_success_text_contains_key_facts(patched):
    sent = telegram_mod.notify_success(
        _cfg(), log=None, reason="实例已就绪：arm-free-1",
        instance_id="ocid1.instance.oc1..abc", public_ip="203.0.113.7",
        shape="2 OCPU / 12GB")
    assert sent is True
    text = json.loads(patched["opener"].requests[0].data)["text"]
    assert "203.0.113.7" in text
    assert "ocid1.instance.oc1..abc" in text
    assert "2 OCPU / 12GB" in text
    assert "不会继续申请" in text


# ── 配置读写 ───────────────────────────────────────────────────────────────────
def test_config_roundtrip_telegram_fields(tmp_path):
    path = tmp_path / "config.json"
    cfg_mod.Config(telegram_bot_token=" 123:ABC\n", telegram_chat_id=" -1001 ",
                   telegram_message_thread_id=8353).save(path)
    loaded = cfg_mod.Config.load(path)
    # normalize 去掉粘贴带进来的空白
    assert loaded.telegram_bot_token == "123:ABC"
    assert loaded.telegram_chat_id == "-1001"
    assert loaded.telegram_message_thread_id == 8353


def test_bad_thread_id_falls_back_to_zero(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"telegram_message_thread_id": "not-a-number"}), encoding="utf-8")
    assert cfg_mod.Config.load(path).telegram_message_thread_id == 0
