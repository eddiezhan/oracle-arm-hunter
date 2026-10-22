"""异常分类的回归测试。"""

import oci
import pytest

from oracle_arm import errors as err_mod


def svc(status, code, message, headers=None):
    return oci.exceptions.ServiceError(status, code, headers or {}, message)


@pytest.mark.parametrize(
    "exc,expected",
    [
        # 容量不足：本轮结束，等下一次
        (svc(500, "InternalError", "Out of host capacity."), err_mod.CAPACITY),
        (svc(500, "InternalError", "Out of capacity for shape VM.Standard.A1.Flex"), err_mod.CAPACITY),
        # 429：退避重试
        (svc(429, "TooManyRequests", "Too many requests"), err_mod.RATE_LIMITED),
        (oci.exceptions.TransientServiceError(429, "TooManyRequests", {}, "x"), err_mod.RATE_LIMITED),
        # 认证/权限
        (svc(401, "NotAuthenticated", "bad key"), err_mod.AUTH),
        (svc(403, "NotAuthorized", "denied"), err_mod.AUTH),
        (oci.exceptions.InvalidPrivateKey("bad pem"), err_mod.AUTH),
        (oci.exceptions.InvalidKeyFilePath("no file"), err_mod.AUTH),
        # 配置问题
        (oci.exceptions.ConfigFileNotFound("missing"), err_mod.CONFIG),
        (oci.exceptions.ProfileNotFound("nope"), err_mod.CONFIG),
        (svc(400, "InvalidParameter", "bad param"), err_mod.CONFIG),
        (svc(404, "NotAuthorizedOrNotFound", "gone"), err_mod.CONFIG),
        # 额度
        (svc(500, "LimitExceeded", "quota exceeded"), err_mod.QUOTA),
        # 瞬时
        (oci.exceptions.RequestException("timed out"), err_mod.TRANSIENT),
        (svc(503, "ServiceUnavailable", "down"), err_mod.TRANSIENT),
        (oci.exceptions.MaximumWaitTimeExceeded("wait"), err_mod.TRANSIENT),
        # 其他
        (ValueError("boom"), err_mod.FATAL),
    ],
)
def test_classify(exc, expected):
    assert err_mod.classify(exc).kind == expected


def test_retry_after_header_is_used():
    c = err_mod.classify(svc(429, "TooManyRequests", "slow down", {"retry-after": "42"}))
    assert c.retry_after == 42


def test_retry_after_defaults_when_header_missing():
    c = err_mod.classify(svc(429, "TooManyRequests", "slow down"), default_wait=17)
    assert c.retry_after == 17


def test_needs_human_and_retry_next_round_flags():
    assert err_mod.classify(svc(401, "NotAuthenticated", "x")).needs_human
    assert err_mod.classify(svc(500, "LimitExceeded", "x")).needs_human
    assert not err_mod.classify(svc(500, "InternalError", "Out of host capacity")).needs_human
    assert err_mod.classify(svc(500, "InternalError", "Out of host capacity")).retry_next_round
    assert not err_mod.classify(svc(403, "NotAuthorized", "x")).retry_next_round


def test_capacity_message_detection():
    assert err_mod.is_capacity_message("Out of host capacity.")
    assert err_mod.is_capacity_message("OUT OF CAPACITY")
    assert not err_mod.is_capacity_message("NotAuthenticated")


def test_is_not_found():
    assert err_mod.is_not_found(svc(404, "NotAuthorizedOrNotFound", "x"))
    assert not err_mod.is_not_found(svc(500, "InternalError", "Out of host capacity"))


def test_capacity_regex_does_not_swallow_parameter_errors():
    """报文里带 capacity 字样的参数错误不能判成「容量不足」——否则会静默跳过、
    永远不通知用户，工具每天 4 次撞同一面墙。"""
    c = err_mod.classify(svc(400, "InvalidParameter", "Invalid capacityReservationId value"))
    assert c.kind == err_mod.CONFIG
    assert c.needs_human          # 必须让用户看到

    c2 = err_mod.classify(svc(400, "InvalidParameter", "capacity is not a valid field"))
    assert c2.kind == err_mod.CONFIG


def test_real_capacity_messages_still_detected():
    for msg in (
        "Out of host capacity.",
        "Out of capacity for shape VM.Standard.A1.Flex in availability domain AD-1",
        "Insufficient capacity in the availability domain",
    ):
        assert err_mod.classify(svc(500, "InternalError", msg)).kind == err_mod.CAPACITY, msg


def test_capacity_message_wins_over_limit_exceeded_code():
    """容量语义比错误码更特异：code=LimitExceeded 但报文是容量不足时，
    必须判容量（下轮重试），不能判配额（永久暂停）。"""
    c = err_mod.classify(svc(500, "LimitExceeded", "Out of host capacity."))
    assert c.kind == err_mod.CAPACITY
    assert not c.needs_human


def test_capacity_message_detected_even_on_400():
    """容量不足若以 400 形式返回，也不能被参数错误分支吃掉。"""
    assert err_mod.classify(svc(400, "InvalidParameter", "Out of host capacity")).kind == err_mod.CAPACITY


def test_is_not_found_requires_404():
    """403 NotAuthorizedOrNotFound 是权限问题，实例其实还在，不能判成「已删除」。"""
    assert err_mod.is_not_found(svc(404, "NotAuthorizedOrNotFound", "x")) is True
    assert err_mod.is_not_found(svc(403, "NotAuthorizedOrNotFound", "x")) is False
    assert err_mod.is_not_found(svc(500, "InternalError", "x")) is False
