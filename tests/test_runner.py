"""单轮申请流程的核心场景测试（全部使用假 OCI 对象，不会真实创建实例）。"""

import oci
import pytest

from oracle_arm import client as client_mod
from oracle_arm import config as cfg_mod
from oracle_arm import errors as err_mod
from oracle_arm import runner as runner_mod
from tests.conftest import FakeApi, make_instance, oci_error

TARGET = cfg_mod.TARGET_SHAPE


# ── 容量不足 ───────────────────────────────────────────────────────────────────
def test_capacity_shortage_waits_for_next_round(make_runner, stopped, notifications):
    api = FakeApi(launch_behaviors=[
        oci_error(500, "InternalError", "Out of host capacity."),
        oci_error(500, "InternalError", "Out of host capacity."),
        oci_error(500, "InternalError", "Out of host capacity."),
    ])
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK          # 不算失败，等下一个时间点
    assert result.status == cfg_mod.STATUS_IDLE
    assert len(api.launch_calls) == 3                       # 每个 AD 只试一次，不轮内轮询
    assert not stopped.calls                                # 不暂停定时任务
    assert not notifications.calls                          # 容量不足不打扰用户


def test_capacity_shortage_on_first_ad_falls_through_to_next(make_runner):
    api = FakeApi(launch_behaviors=[
        oci_error(500, "InternalError", "Out of host capacity."),
        "ocid1.instance.ok",                                # 第二个 AD 有容量
    ])
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert result.status == cfg_mod.STATUS_SUCCESS
    assert [c["ad"] for c in api.launch_calls] == ["AD-1", "AD-2"]


# ── 429 退避 ───────────────────────────────────────────────────────────────────
def test_rate_limit_backs_off_then_succeeds(make_runner, sleeps, cfg):
    api = FakeApi(launch_behaviors=[
        oci_error(429, "TooManyRequests", "slow down", {"retry-after": "7"}),
        "ocid1.instance.ok",
    ])
    result = make_runner(api).run_once()

    assert result.status == cfg_mod.STATUS_SUCCESS
    assert sleeps.calls[0] == (7,)                          # 按 Retry-After 退避
    assert len(api.launch_calls) == 2
    # 同一个 AD 内的重试必须复用同一幂等标识
    assert api.launch_calls[0]["retry_token"] == api.launch_calls[1]["retry_token"]


def test_rate_limit_exhausted_gives_up_this_round(make_runner, sleeps, stopped, cfg):
    """持续 429：按官方要求退避重试，重试用尽后本轮放弃（不再换 AD 硬撞）。"""
    cfg.rate_limit_max_retries = 2
    api = FakeApi(ads=["AD-1", "AD-2"], launch_behaviors=[
        oci_error(429, "TooManyRequests", "slow"),
        oci_error(429, "TooManyRequests", "slow"),
        oci_error(429, "TooManyRequests", "slow"),
    ])
    result = make_runner(api, cfg=cfg).run_once()

    assert result.exit_code == runner_mod.EXIT_OK            # 本轮放弃，等下一次
    assert result.status == cfg_mod.STATUS_IDLE
    assert not stopped.calls
    assert len(api.launch_calls) == 3                        # 1 次 + 2 次退避重试
    assert len(sleeps.calls) == 2                            # 每次退避都用 Retry-After 默认值
    assert all(c == (cfg.rate_limit_default_wait,) for c in sleeps.calls)


# ── 认证/配额/配置错误暂停 ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "exc,keyword",
    [
        (oci_error(401, "NotAuthenticated", "bad key"), "认证失败"),
        (oci_error(403, "NotAuthorized", "denied"), "权限不足"),
        (oci_error(500, "LimitExceeded", "quota"), "额度/配额不足"),
        (oci_error(400, "InvalidParameter", "bad"), "请求参数无效"),
    ],
)
def test_human_errors_pause_and_notify(make_runner, stopped, notifications, state, exc, keyword):
    api = FakeApi(launch_behaviors=[exc])
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert state.status == cfg_mod.STATUS_NEEDS_ATTENTION
    assert keyword in state.reason
    assert stopped.calls                                     # 暂停定时任务
    assert notifications.calls                               # 通知用户


def test_home_region_mismatch_stops(make_runner, stopped, state):
    api = FakeApi(home_region="us-ashburn-1")
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "Home Region" in state.reason
    assert not api.launch_calls
    assert stopped.calls


def test_oci_config_region_mismatch_stops(make_runner, state):
    api = FakeApi(region="eu-frankfurt-1")
    result = make_runner(api).run_once()
    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "region" in state.reason


def test_paid_shape_stops(make_runner, state, stopped):
    api = FakeApi(shape=client_mod.ShapeInfo(
        name=TARGET, billing_type="PAID", quota_names=[], ocpus=4, memory_gb=24))
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "ALWAYS_FREE" in state.reason
    assert not api.launch_calls


def test_limited_free_shape_is_accepted(make_runner):
    """A1.Flex 的 billing_type 实测是 LIMITED_FREE（额度内免费），
    只认 ALWAYS_FREE 会把申请直接挡死。"""
    api = FakeApi(shape=client_mod.ShapeInfo(
        name=TARGET, billing_type="LIMITED_FREE",
        quota_names=["standard-a1-core-count", "standard-a1-memory-count"],
        ocpus=cfg_mod.TARGET_OCPUS, memory_gb=cfg_mod.TARGET_MEMORY_GB))
    result = make_runner(api).run_once()
    assert result.status == cfg_mod.STATUS_SUCCESS
    assert len(api.launch_calls) == 1


def test_unknown_billing_type_stops(make_runner, state):
    """billing_type 是未知枚举值时不能猜，直接停。"""
    api = FakeApi(shape=client_mod.ShapeInfo(
        name=TARGET, billing_type="SOMETHING_NEW", quota_names=[], ocpus=1, memory_gb=6))
    assert make_runner(api).run_once().exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert not api.launch_calls


def test_quota_accepts_exactly_the_account_limit(make_runner):
    """剩余额度正好等于目标规格时必须放行（判定是 available < 需求 才拒绝，等于不算越界）。
    用 TARGET_* 常量而不是写死数字，规格再调整时这条用例仍然测的是边界。"""
    api = FakeApi(limits=[
        client_mod.LimitInfo("standard-a1-core-count", used=0,
                             available=cfg_mod.TARGET_OCPUS,
                             quota_value=cfg_mod.TARGET_OCPUS, scope_type="AD"),
        client_mod.LimitInfo("standard-a1-memory-count", used=0,
                             available=cfg_mod.TARGET_MEMORY_GB,
                             quota_value=cfg_mod.TARGET_MEMORY_GB, scope_type="AD"),
    ])
    result = make_runner(api).run_once()
    assert result.status == cfg_mod.STATUS_SUCCESS
    assert len(api.launch_calls) == 1


def test_quota_summary_records_scope(make_runner, state):
    api = FakeApi(limits=[
        client_mod.LimitInfo("standard-a1-core-count", used=0, available=2, quota_value=2,
                             scope_type="AD"),
        client_mod.LimitInfo("standard-a1-memory-count", used=0, available=12, quota_value=12,
                             scope_type="AD"),
    ])
    make_runner(api).run_once()
    assert "已用 0/2" in state.quota_summary
    assert "来源 AD" in state.quota_summary


# ── 免费额度（不硬编码，实时查询） ──────────────────────────────────────────────
def test_quota_limit_below_request_stops(make_runner, state, stopped):
    """账号 A1 上限本身就不够目标规格 → 停止，不申请收费资源。"""
    api = FakeApi(limits=[
        client_mod.LimitInfo("standard-a1-core-count", used=0, available=0, quota_value=0),
        client_mod.LimitInfo("standard-a1-memory-count", used=0, available=0, quota_value=0),
    ])
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "不足以创建" in state.reason
    assert not api.launch_calls
    assert stopped.calls


def test_quota_used_up_in_one_ad_tries_another(make_runner):
    """AD-1 额度已占满、AD-2 有余量 → 换 AD 继续，不误判为额度不足。"""
    api = FakeApi(ads=["AD-1", "AD-2"], launch_behaviors=["ocid1.instance.ok"])
    full = [
        client_mod.LimitInfo("standard-a1-core-count", used=4, available=0, quota_value=4),
        client_mod.LimitInfo("standard-a1-memory-count", used=24, available=0, quota_value=24),
    ]
    free = [
        client_mod.LimitInfo("standard-a1-core-count", used=0, available=4, quota_value=4),
        client_mod.LimitInfo("standard-a1-memory-count", used=0, available=24, quota_value=24),
    ]
    api.limit_availability = lambda ad, names: full if ad == "AD-1" else free
    result = make_runner(api).run_once()

    assert result.status == cfg_mod.STATUS_SUCCESS
    assert api.launch_calls[0]["ad"] == "AD-2"


def test_unverified_quota_stops_by_default(make_runner, state, stopped):
    """查不到额度就不确定是否免费 → 默认停止（需求：不确定就停止）。"""
    api = FakeApi(limits_error=oci_error(403, "NotAuthorized", "no limits read"))
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "无法查询账号免费额度" in state.reason
    assert not api.launch_calls
    assert stopped.calls


def test_transient_limits_query_failure_skips_round_without_pausing(make_runner, stopped, state):
    """同类路径：额度查询本身因网络抖动失败（不是权限问题）→ 本轮跳过，不永久停表。
    只有真正需要人工的错误（如 403 没有 limits 读权限）才该停表 —— 见下一个用例。"""
    api = FakeApi(limits_error=oci.exceptions.RequestException("Read timed out"))
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert state.status == cfg_mod.STATUS_IDLE
    assert not api.launch_calls
    assert not stopped.calls


def test_unverified_quota_can_be_allowed_by_config(make_runner, cfg):
    cfg.allow_unverified_quota = True
    api = FakeApi(limits_error=oci_error(403, "NotAuthorized", "no limits read"))
    result = make_runner(api, cfg=cfg).run_once()

    assert result.status == cfg_mod.STATUS_SUCCESS
    assert len(api.launch_calls) == 1


def test_quota_summary_is_recorded(make_runner, state):
    api = FakeApi(limits=[
        client_mod.LimitInfo("standard-a1-core-count", used=1, available=3, quota_value=4),
        client_mod.LimitInfo("standard-a1-memory-count", used=6, available=18, quota_value=24),
    ])
    make_runner(api).run_once()
    assert state.quota_verified is True
    assert "已用 1/4" in state.quota_summary


# ── Limits API 瞬时假读数不得永久停表 ───────────────────────────────────────────
def _a1_limits(core_used, core_avail, core_quota, mem_used, mem_avail, mem_quota):
    """构造一份 A1 额度读数（核心 + 内存）。"""
    return [
        client_mod.LimitInfo("standard-a1-core-count", used=core_used,
                             available=core_avail, quota_value=core_quota),
        client_mod.LimitInfo("standard-a1-memory-count", used=mem_used,
                             available=mem_avail, quota_value=mem_quota),
    ]


# 2026-10-20 16:04 实测到的假读数：显示已用 2/2、可用 0，复查立刻恢复 0/2
FAKE_FULL = _a1_limits(2, 0, 2, 12, 0, 12)
# 同一时刻复查得到的真实读数
REAL_FREE = _a1_limits(0, 2, 2, 0, 12, 12)


def test_transient_quota_reading_does_not_pause(make_runner, stopped, notifications, sleeps):
    """Limits API 是最终一致的：2026-10-20 16:04 实测读到「已用 2/2、可用 0」，
    几秒后复查即恢复 0/2，OCI 审计日志证实那段时间没有任何实例创建/终止事件。
    单次假读数就把定时任务永久停掉，是抢容量工具最糟的失败模式（静默停摆、分秒必争），
    所以「额度不足」的结论必须复查确认。"""
    # 用「容量不足」收尾而不是成功：成功路径本来就会按设计停表，会掩盖这里要验的误停
    api = FakeApi(ads=["AD-1"], launch_behaviors=[
        oci_error(500, "InternalError", "Out of host capacity.")])
    reads = [FAKE_FULL, REAL_FREE]
    api.limit_availability = lambda ad, names: reads.pop(0) if reads else REAL_FREE

    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert result.status == cfg_mod.STATUS_IDLE
    assert len(api.launch_calls) == 1   # 复查恢复后确实去申请了（假读数没有把整轮掐掉）
    assert not stopped.calls            # 没有因为一次假读数暂停定时任务
    assert not notifications.calls      # 也不该打扰用户
    assert reads == []                  # 两次读数都用掉了（确实复查了，不是只读一次）
    assert sleeps.calls == [(cfg_mod.QUOTA_RECHECK_WAIT,)]   # 复查前等 Limits API 收敛


def test_quota_full_confirmed_by_recheck_still_pauses(make_runner, stopped, state, sleeps):
    """复查仍然读满 → 认定该 AD 真的占满，保持原有安全语义（停表并提示人工）。
    同时钉住「确实复查过」：读两次额度、睡一次 QUOTA_RECHECK_WAIT，
    否则这条用例在修复前的代码上也会通过，等于没测到复查。"""
    api = FakeApi(ads=["AD-1"], limits=FAKE_FULL)
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "剩余额度不足" in state.reason
    assert not api.launch_calls
    assert stopped.calls
    assert api.limit_calls == 2                              # 读了两次（首次 + 复查）
    assert sleeps.calls == [(cfg_mod.QUOTA_RECHECK_WAIT,)]   # 中间等了一次收敛


def test_transient_zero_quota_cap_does_not_pause(make_runner, stopped):
    """同类路径：瞬时读数把额度「上限」也算成 0（quota_value 走 used+available 兜底）
    时，同样不能只凭一次读数就永久停表。"""
    api = FakeApi(ads=["AD-1"], launch_behaviors=[
        oci_error(500, "InternalError", "Out of host capacity.")])
    reads = [_a1_limits(0, 0, 0, 0, 0, 0), REAL_FREE]
    api.limit_availability = lambda ad, names: reads.pop(0) if reads else REAL_FREE

    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert len(api.launch_calls) == 1
    assert not stopped.calls


def test_disagreeing_quota_readings_do_not_pause(make_runner, stopped, state):
    """两次读数对「为什么不够」结论不一致时不下结论。
    第一次读到「该 AD 占满、但上限够用」，第二次读到「上限本身就不够」—— 两次说的
    不是一回事，既不能据此断定账号额度不够（会误停），也不能当成有额度（会误申请）。
    正确处理是本轮跳过：不申请、不停表，下个申请时刻重新判。"""
    api = FakeApi(ads=["AD-1"], launch_behaviors=["ocid1.instance.ok"])
    reads = [FAKE_FULL, _a1_limits(0, 0, 0, 0, 0, 0)]   # 上限 0 < 目标 → 判「上限不够」
    api.limit_availability = lambda ad, names: reads.pop(0) if reads else REAL_FREE

    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert result.status == cfg_mod.STATUS_IDLE
    assert not api.launch_calls      # 不申请（安全）
    assert not stopped.calls         # 也不永久停表（可自愈）


def test_unverified_recheck_after_negative_reading_does_not_launch(make_runner, stopped, cfg):
    """allow_unverified_quota=true 也不能把「复查失败」当成「额度够」。
    首次读数已经判负，复查又读不到，就只能本轮不下结论 —— 绝不据此发创建请求。
    （这是修复前会把「不知道」当「够」的唯一路径，比改动前更不安全。）"""
    cfg.allow_unverified_quota = True
    api = FakeApi(ads=["AD-1"], launch_behaviors=["ocid1.instance.ok"])
    reads = [FAKE_FULL, oci_error(429, "TooManyRequests", "slow down")]

    def flaky(ad, names):
        item = reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    api.limit_availability = flaky
    result = make_runner(api, cfg=cfg).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert not api.launch_calls      # 绝不据此发创建请求
    assert not stopped.calls         # 也不停表


def test_transient_shape_lookup_on_every_ad_skips_round_without_pausing(make_runner, stopped, state):
    """同类路径：所有 AD 的规格查询都因网络抖动失败 → 本轮跳过、下轮重试，不永久停表。
    2026-10-17 18:45 实测发生过一次：一次读超时就走到了「没有可用的 AD」并停表。"""
    api = FakeApi(ads=["AD-1", "AD-2"])
    api.shape_info = lambda ad: (_ for _ in ()).throw(
        oci.exceptions.RequestException("Read timed out"))

    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert state.status == cfg_mod.STATUS_IDLE
    assert not api.launch_calls
    assert not stopped.calls


def test_shape_transient_mixed_with_real_reason_still_pauses(make_runner, stopped, state):
    """边界：只有「每个 AD 都只是查不到」才算查不清。若已有 AD 给出确定结论
    （这里是额度确实占满），其余 AD 的网络抖动不应掩盖它 —— 仍停表提示人工。
    注意 reason 是汇总文案（含 AD-1 的额度结论与 AD-2 的查询失败），这里只断言
    「仍然停表」这件事，不去断言它由哪一句触发。"""
    api = FakeApi(ads=["AD-1", "AD-2"], limits=FAKE_FULL)
    original = api.shape_info
    api.shape_info = lambda ad: (_ for _ in ()).throw(
        oci.exceptions.RequestException("Read timed out")) if ad == "AD-2" else original(ad)

    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "剩余额度不足" in state.reason
    assert stopped.calls


def test_capacity_summary_carries_raw_oci_message(make_runner, state):
    """容量不足的官方原始错误必须出现在 INFO 汇总行里：launchd 跑的是 INFO 级别，
    单条失败记 DEBUG 的话，日志里只剩「容量不足」的结论，看不到官方到底返回了什么
    （2026-10-20 排查时因此只能手动加 --verbose 才能拿到原文）。"""
    api = FakeApi(ads=["AD-1"],
                  launch_behaviors=[oci_error(500, "InternalError", "Out of host capacity.")])
    result = make_runner(api).run_once()

    assert "Out of host capacity." in result.summary
    assert "Out of host capacity." in state.last_result


# ── 重复申请防护 ───────────────────────────────────────────────────────────────
def test_existing_managed_instance_with_ip_is_success(make_runner, stopped, notifications, state):
    api = FakeApi(existing=[make_instance("ocid1.instance.old", state="RUNNING")],
                  public_ip="198.51.100.7")
    result = make_runner(api).run_once()

    assert result.status == cfg_mod.STATUS_SUCCESS
    assert result.public_ip == "198.51.100.7"
    assert not api.launch_calls                            # 绝不重复创建
    assert stopped.calls and notifications.calls


def test_existing_instance_without_public_ip_stops(make_runner, stopped, state):
    """实例已存在但没有公网 IPv4 → 停止重复申请并提示修复（需求 6）。"""
    api = FakeApi(existing=[make_instance("ocid1.instance.old", state="RUNNING")], public_ip=None)
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "没有公网 IPv4" in state.reason or "没有分配到公网 IPv4" in state.reason
    assert not api.launch_calls
    assert stopped.calls


def test_foreign_a1_instance_is_not_claimed_as_success(
    make_runner, state, stopped, notifications
):
    """账号里已有（非本工具创建的）A1 实例 → 不重复申请，也**不能**谎报「申请成功」。

    谎报的后果：用户收到一条与实际不符的成功通知，还以为这台就是工具申请的，
    定时任务被停掉，从此再也不会去申请真正属于自己的那台。
    """
    api = FakeApi(existing=[make_instance("ocid1.instance.manual", name="my-vm", managed=False)],
                  public_ip="198.51.100.9")
    result = make_runner(api).run_once()

    assert not api.launch_calls                                  # 绝不重复创建
    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert state.status == cfg_mod.STATUS_NEEDS_ATTENTION        # 不是 success
    assert "非本工具创建" in state.reason
    assert state.public_ip == ""                                 # 不把别人的 IP 记成自己的
    assert stopped.calls
    assert all("已就绪" not in call[1] for call in notifications.calls)


def test_provisioning_instance_is_reconciled_not_recreated(make_runner, state):
    """上一轮提交后仍在创建中 → 本轮只确认，不重复创建。"""
    api = FakeApi(existing=[make_instance("ocid1.instance.pending", state="PROVISIONING")],
                  public_ip="198.51.100.11")
    result = make_runner(api).run_once()

    assert result.status == cfg_mod.STATUS_SUCCESS
    assert not api.launch_calls


# ── 超时对账 ───────────────────────────────────────────────────────────────────
def test_timeout_reconciles_and_finds_created_instance(make_runner, notifications):
    """创建请求超时，但对账发现实例其实已创建 → 直接走成功流程，不再重复创建。"""

    def create_then_timeout(api):
        # 模拟：OCI 侧已受理创建，但客户端读取响应超时
        api.created.append(make_instance("ocid1.instance.timeout", state="PROVISIONING"))
        raise oci.exceptions.RequestException("Read timed out")

    api = FakeApi(launch_behaviors=[create_then_timeout])
    result = make_runner(api).run_once()

    assert result.status == cfg_mod.STATUS_SUCCESS
    assert len(api.launch_calls) == 1                       # 查到实例后不再盲目重试
    assert result.instance_id == "ocid1.instance.timeout"


def test_timeout_without_instance_retries_once_with_same_token(make_runner, stopped):
    """超时且未查到实例 → 用同一幂等标识重试一次，仍失败则本轮放弃。"""
    api = FakeApi(launch_behaviors=[
        oci.exceptions.RequestException("timed out"),
        oci.exceptions.RequestException("timed out"),
    ])
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert len(api.launch_calls) == 2
    assert api.launch_calls[0]["retry_token"] == api.launch_calls[1]["retry_token"]
    assert not stopped.calls                                # 不因超时就暂停任务


def test_wait_timeout_keeps_schedule_running(make_runner, stopped, state):
    api = FakeApi(wait_error=TimeoutError("等待实例进入 RUNNING 超时"))
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert state.status == cfg_mod.STATUS_IDLE
    assert not stopped.calls                                # 下个时间点继续确认


# ── 公网 IPv4 与成功停止 ───────────────────────────────────────────────────────
def test_success_assigns_public_ip_and_stops_schedule(make_runner, stopped, notifications, state):
    api = FakeApi(launch_behaviors=["ocid1.instance.ok"], public_ip="203.0.113.55")
    result = make_runner(api).run_once()

    assert result.status == cfg_mod.STATUS_SUCCESS
    assert result.public_ip == "203.0.113.55"
    assert state.instance_id == "ocid1.instance.ok"
    assert state.public_ip == "203.0.113.55"
    assert stopped.calls                                    # 成功后停止定时任务（需求 5）
    assert notifications.calls
    assert "203.0.113.55" in notifications.calls[0][1]


def test_running_without_public_ip_stops_and_warns(make_runner, stopped, notifications, state):
    api = FakeApi(launch_behaviors=["ocid1.instance.nopip"], public_ip=None)
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert state.status == cfg_mod.STATUS_NEEDS_ATTENTION
    assert "公网 IPv4" in state.reason
    assert stopped.calls and notifications.calls


def test_public_ip_is_polled_until_assigned(make_runner, sleeps, state):
    api = FakeApi(launch_behaviors=["ocid1.instance.ok"])
    values = iter([None, None, "203.0.113.99"])
    api.public_ip = lambda instance_id: next(values, "203.0.113.99")
    result = make_runner(api).run_once()

    assert result.public_ip == "203.0.113.99"
    assert len(sleeps.calls) == 2


# ── 请求参数与预检 ─────────────────────────────────────────────────────────────
def test_launch_uses_fixed_spec_and_public_ip(make_runner):
    """规格固定为 TARGET_OCPUS / TARGET_MEMORY_GB / 50GB，且必须分配公网 IPv4。"""
    api = FakeApi()
    make_runner(api).run_once()
    call = api.launch_calls[0]
    assert call["image_id"] == "ocid1.image.oc1.ap-singapore-1.ubuntu2404"
    assert call["subnet_id"] == "ocid1.subnet.oc1.ap-singapore-1.public"
    assert call["enable_ipv6"] is False                     # IPv6 默认关闭


def test_ipv6_flag_passed_only_when_subnet_supports_it(make_runner, cfg):
    cfg.enable_ipv6 = True
    api = FakeApi()                                          # 子网没有 IPv6 CIDR
    make_runner(api, cfg=cfg).run_once()
    assert api.launch_calls[0]["enable_ipv6"] is False

    api2 = FakeApi(subnet=client_mod.SubnetInfo(
        id="s", vcn_id="v", display_name="public-subnet", has_ipv6=True,
        internet_gateway_id="igw"))
    make_runner(api2, cfg=cfg).run_once()
    assert api2.launch_calls[0]["enable_ipv6"] is True


def test_dry_run_does_not_launch(make_runner):
    api = FakeApi()
    result = make_runner(api).run_once(dry_run=True)
    assert result.exit_code == runner_mod.EXIT_OK
    assert not api.launch_calls
    assert "预检通过" in result.summary


def test_missing_public_subnet_stops_with_guidance(make_runner, state, stopped):
    api = FakeApi(subnet=None)
    api.find_public_subnet = lambda: None
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "Public Subnet" in state.reason
    assert "Internet Gateway" in state.reason
    assert not api.launch_calls


def test_missing_image_stops(make_runner, state):
    api = FakeApi()
    api.find_image = lambda: None
    result = make_runner(api).run_once()
    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "镜像" in state.reason


def test_missing_ssh_key_stops(make_runner, cfg, state):
    cfg.ssh_public_key = "/nonexistent/key.pub"
    api = FakeApi()
    result = make_runner(api, cfg=cfg).run_once()
    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "SSH 公钥" in state.reason
    assert not api.launch_calls


def test_invalid_schedule_stops(make_runner, cfg, state):
    cfg.schedule_mode = cfg_mod.SCHEDULE_MODE_FIXED
    cfg.schedule_beijing = ["25:00"]
    api = FakeApi()
    result = make_runner(api, cfg=cfg).run_once()
    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "申请时刻格式错误" in state.reason


def test_state_is_persisted_on_success(make_runner, state, tmp_path):
    api = FakeApi(launch_behaviors=["ocid1.instance.persist"], public_ip="203.0.113.1")
    make_runner(api).run_once()
    reloaded = cfg_mod.State.load(tmp_path / "state.json")
    assert reloaded.status == cfg_mod.STATUS_SUCCESS
    assert reloaded.public_ip == "203.0.113.1"
    assert reloaded.attempts == 1


def test_pending_retry_token_cleared_after_success(make_runner, state):
    api = FakeApi(launch_behaviors=["ocid1.instance.tok"])
    make_runner(api).run_once()
    assert state.pending_retry_token == ""


def test_secrets_are_not_logged(make_runner, cfg, log, ssh_key):
    """日志里不能出现私钥内容（公钥可以）。"""
    api = FakeApi()
    make_runner(api).run_once()
    assert "PRIVATE KEY" not in log.text
    assert "ssh-ed25519" not in log.text  # 公钥内容也不写进日志


# ── 无人值守健壮性 ─────────────────────────────────────────────────────────────
def test_transient_error_in_preflight_skips_round(make_runner, stopped, state):
    """预检时遇到瞬时错误（网络/超时）→ 本轮跳过，不冒险创建，也不永久暂停。"""
    api = FakeApi()
    api.availability_domains = lambda: (_ for _ in ()).throw(
        oci.exceptions.RequestException("connection reset"))
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert state.status == cfg_mod.STATUS_IDLE
    assert not api.launch_calls
    assert not stopped.calls


def test_auth_error_in_preflight_pauses(make_runner, stopped, state):
    api = FakeApi()
    api.home_region = lambda: (_ for _ in ()).throw(
        oci_error(401, "NotAuthenticated", "bad key"))
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "Home Region" in state.reason
    assert stopped.calls


def test_unexpected_exception_does_not_crash(make_runner, notifications, state, monkeypatch):
    """任何未预期异常都不能让任务裸崩：留痕 + 通知 + 返回退出码 3。"""
    api = FakeApi()
    runner = make_runner(api)
    monkeypatch.setattr(runner, "_preflight", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    result = runner.run_once()
    assert result.exit_code == runner_mod.EXIT_UNEXPECTED
    assert state.status == cfg_mod.STATUS_ERROR
    assert "boom" in state.reason
    assert notifications.calls


def test_shape_lookup_failure_moves_to_next_ad(make_runner):
    api = FakeApi(ads=["AD-1", "AD-2"], launch_behaviors=["ocid1.instance.ok"])
    original = api.shape_info

    def flaky(ad):
        if ad == "AD-1":
            raise oci.exceptions.RequestException("timeout")
        return original(ad)

    api.shape_info = flaky
    result = make_runner(api).run_once()
    assert result.status == cfg_mod.STATUS_SUCCESS
    assert api.launch_calls[0]["ad"] == "AD-2"


def test_inactive_compartment_stops(make_runner, state, stopped):
    api = FakeApi()
    api.compartment_info = lambda: ("child", "DELETING")
    result = make_runner(api).run_once()
    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "DELETING" in state.reason
    assert not api.launch_calls


def test_missing_compartment_pauses_with_reason(make_runner, cfg, state, stopped):
    """显式配置了子 Compartment 但 OCID 写错 → 给出明确原因并暂停。"""
    cfg.compartment_id = "ocid1.compartment.oc1..wrong"
    api = FakeApi()
    api.compartment_info = lambda: (_ for _ in ()).throw(
        oci_error(404, "NotAuthorizedOrNotFound", "compartment not found"))
    result = make_runner(api, cfg=cfg).run_once()
    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert "Compartment" in state.reason
    assert stopped.calls


def test_compartment_check_failure_is_tolerated_for_root(make_runner, state, log):
    """用根 Compartment（默认）时，Compartment 查询失败不应误停。"""
    api = FakeApi(launch_behaviors=["ocid1.instance.ok"])
    api.compartment_info = lambda: (_ for _ in ()).throw(
        oci_error(404, "NotAuthorizedOrNotFound", "cannot get tenancy as compartment"))
    result = make_runner(api).run_once()
    assert result.status == cfg_mod.STATUS_SUCCESS
    assert "跳过 Compartment 检查" in log.text


# ── 跨轮次行为 ─────────────────────────────────────────────────────────────────
def test_second_round_after_success_does_not_create_again(make_runner, state):
    """成功后（例如用户手动「立即尝试」）再跑一轮 → 只确认，绝不重复创建。"""
    api = FakeApi(launch_behaviors=["ocid1.instance.first"], public_ip="203.0.113.5")
    make_runner(api).run_once()
    assert len(api.launch_calls) == 1

    result = make_runner(api).run_once()
    assert result.status == cfg_mod.STATUS_SUCCESS
    assert result.public_ip == "203.0.113.5"
    assert len(api.launch_calls) == 1          # 关键：没有第二次创建


def test_capacity_round_then_success_round(make_runner, state):
    """先容量不足、后拿到容量：状态从 idle 正确走到 success。"""
    api = FakeApi(ads=["AD-1"], launch_behaviors=[
        oci_error(500, "InternalError", "Out of host capacity."),
        "ocid1.instance.later",
    ])
    first = make_runner(api).run_once()
    assert first.status == cfg_mod.STATUS_IDLE
    assert state.capacity_misses == 1

    second = make_runner(api).run_once()
    assert second.status == cfg_mod.STATUS_SUCCESS
    assert state.capacity_misses == 1          # 成功后不再累计
    assert state.public_ip == "203.0.113.10"


# ── 审查发现的缺陷：逐条回归 ───────────────────────────────────────────────────
def test_reconcile_found_instance_never_retries(make_runner, state, log):
    """对账已确认实例存在时，即使 _finalize 没能立刻完成（还在创建中），
    也绝不能落到重试分支再发一次创建请求 —— 那会绕过幂等防线。"""

    def create_then_timeout(api):
        api.created.append(make_instance("ocid1.instance.recon", state="PROVISIONING"))
        raise oci.exceptions.RequestException("Read timed out")

    api = FakeApi(launch_behaviors=[create_then_timeout],
                  wait_error=TimeoutError("等待实例进入 RUNNING 超时"))
    result = make_runner(api).run_once()

    assert len(api.launch_calls) == 1                     # 关键：没有第二次创建请求
    assert result.instance_id == "ocid1.instance.recon"
    assert "不再重试创建" in log.text
    assert "未查到实例" not in log.text                    # 日志不能说谎


def test_rate_limit_and_transient_have_separate_budgets(make_runner, sleeps):
    """429 与超时是两类失败，不能共用同一个重试计数器互相吃掉预算。"""
    api = FakeApi(ads=["AD-1"], launch_behaviors=[
        oci_error(429, "TooManyRequests", "slow"),      # 用掉限流预算 1/2
        oci.exceptions.RequestException("timeout"),     # 用掉超时预算 1/1
        "ocid1.instance.ok",
    ])
    result = make_runner(api).run_once()

    assert result.status == cfg_mod.STATUS_SUCCESS
    assert len(api.launch_calls) == 3


def test_retry_after_is_capped(make_runner, sleeps):
    """Retry-After 可能给出很大的值（如 7200），原样照睡会把一轮拖到几小时。"""
    api = FakeApi(ads=["AD-1"], launch_behaviors=[
        oci_error(429, "TooManyRequests", "slow", {"retry-after": "7200"}),
        "ocid1.instance.ok",
    ])
    make_runner(api).run_once()
    assert sleeps.calls[0] == (cfg_mod.RATE_LIMIT_MAX_WAIT,)
    assert cfg_mod.RATE_LIMIT_MAX_WAIT <= 600


def test_public_ip_query_failure_does_not_pause(make_runner, stopped, state, log):
    """「查不到公网 IP」不等于「没分配」：不能因此永久暂停并让用户白跑一趟控制台。"""
    api = FakeApi(launch_behaviors=["ocid1.instance.ok"])

    def boom(instance_id):
        raise oci.exceptions.RequestException("throttled")

    api.public_ip = boom
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert state.status == cfg_mod.STATUS_IDLE
    assert not stopped.calls
    assert "查询公网 IP 失败" in log.text


def test_public_ip_missing_but_readable_still_pauses(make_runner, stopped, state):
    """查询成功但确实没有公网 IP → 仍按需求停止重复申请并提示修复。"""
    api = FakeApi(launch_behaviors=["ocid1.instance.ok"], public_ip=None)
    result = make_runner(api).run_once()
    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert stopped.calls


def test_dry_run_does_not_overwrite_needs_attention(make_runner, state):
    """预检不得把「需人工处理」悄悄抹成 idle，否则状态页与实际情况不符。"""
    state.status = cfg_mod.STATUS_NEEDS_ATTENTION
    state.reason = "已存在实例但没有公网 IPv4"
    make_runner(FakeApi()).run_once(dry_run=True)

    assert state.status == cfg_mod.STATUS_NEEDS_ATTENTION
    assert state.reason == "已存在实例但没有公网 IPv4"


def test_interrupted_previous_round_is_reported(make_runner, state, log):
    """上一轮在提交创建时被中断 → 本轮要说明先对账，而不是默默再发一次创建。"""
    state.pending_retry_token = "deadbeef"
    state.pending_since = "2026-10-17T02:05:00+08:00"
    make_runner(FakeApi()).run_once(dry_run=True)

    assert "deadbeef" in log.text
    assert "对账" in log.text


def test_ipv6_cidr_is_passed_to_launch(make_runner, cfg):
    """开启 IPv6 时要把子网的 IPv6 CIDR 传给创建请求，否则开关是空的。"""
    cfg.enable_ipv6 = True
    api = FakeApi(subnet=client_mod.SubnetInfo(
        id="s", vcn_id="v", display_name="public-subnet", has_ipv6=True,
        ipv6_cidr="2001:db8::/64", internet_gateway_id="igw"))
    make_runner(api, cfg=cfg).run_once()
    assert api.launch_calls[0]["ipv6_subnet_cidr"] == "2001:db8::/64"


def test_instance_deleted_midway_is_treated_as_not_created(make_runner, state):
    """实例在等待过程中消失（被删除/不可见）→ 按本轮未创建处理，下轮重新开始。"""
    api = FakeApi(launch_behaviors=["ocid1.instance.gone"])
    api.wait_for_state = lambda *a, **k: (_ for _ in ()).throw(
        oci_error(404, "NotAuthorizedOrNotFound", "instance not found"))
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_OK
    assert state.status == cfg_mod.STATUS_IDLE
    assert state.instance_id == ""


def test_foreign_instance_with_default_name_is_not_claimed(make_runner, state, notifications):
    """显示名不是所有权证明：默认名 arm-free-1 很通用，用户手工创建的实例可能同名。"""
    api = FakeApi(existing=[make_instance("ocid1.instance.homonym", name="arm-free-1",
                                          managed=False)],
                  public_ip="198.51.100.3")
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert state.status == cfg_mod.STATUS_NEEDS_ATTENTION
    assert state.public_ip == ""
    assert "显示名恰好" in state.reason            # 明确提示名字撞车
    assert not api.launch_calls
    assert all("已就绪" not in c[1] for c in notifications.calls)


def test_instance_403_is_not_treated_as_deleted(make_runner, state):
    """403 是权限问题，实例还在：不能清掉 OCID 记录，也不能不暂停。"""
    api = FakeApi(launch_behaviors=["ocid1.instance.perm"])
    api.wait_for_state = lambda *a, **k: (_ for _ in ()).throw(
        oci_error(403, "NotAuthorizedOrNotFound", "not authorized"))
    result = make_runner(api).run_once()

    assert result.exit_code == runner_mod.EXIT_NEEDS_HUMAN
    assert state.instance_id == "ocid1.instance.perm"   # OCID 不能被丢掉


# ── 电报通知（2026-10-22 需求：抢到后发电报） ──────────────────────────────────
def _record_telegram(monkeypatch):
    """把电报发送换成记录器，返回记录列表（绝不发真实请求）。"""
    sent = []
    def fake(cfg, log, reason, instance_id, public_ip, shape):
        sent.append(dict(reason=reason, instance_id=instance_id,
                         public_ip=public_ip, shape=shape))
        return True
    monkeypatch.setattr(runner_mod.telegram_mod, "notify_success", fake)
    return sent


def test_success_sends_telegram_once(make_runner, cfg, monkeypatch):
    """刚抢到实例时发一条电报，正文带规格与公网 IP。"""
    sent = _record_telegram(monkeypatch)
    api = FakeApi(launch_behaviors=["ocid1.instance.ok"], public_ip="203.0.113.42")
    result = make_runner(api).run_once()

    assert result.status == cfg_mod.STATUS_SUCCESS
    assert len(sent) == 1
    assert sent[0]["public_ip"] == "203.0.113.42"
    assert sent[0]["instance_id"] == "ocid1.instance.ok"
    assert sent[0]["shape"] == f"{cfg_mod.TARGET_OCPUS} OCPU / {cfg_mod.TARGET_MEMORY_GB}GB"


def test_reconfirming_existing_instance_does_not_resend_telegram(make_runner, monkeypatch):
    """成功后用户手动 resume / --force 再跑一轮：预检会重新确认实例，
    这不是「刚抢到」，不能重复发电报（否则每次手动跑都骚扰一次）。"""
    sent = _record_telegram(monkeypatch)
    api = FakeApi(launch_behaviors=["ocid1.instance.ok"], public_ip="203.0.113.42")

    first = make_runner(api).run_once()
    assert first.status == cfg_mod.STATUS_SUCCESS
    assert len(sent) == 1

    second = make_runner(api).run_once()      # 预检发现实例已存在
    assert second.status == cfg_mod.STATUS_SUCCESS
    assert len(sent) == 1                     # 关键：没有第二条电报
    assert len(api.launch_calls) == 1


def test_telegram_failure_does_not_break_success(make_runner, stopped, state, monkeypatch):
    """电报发不出去（没网/没配 token/模块自身抛错）也必须保留「已成功」结论。"""
    def boom(*a, **k):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(runner_mod.telegram_mod, "notify_success", boom)

    api = FakeApi(launch_behaviors=["ocid1.instance.ok"], public_ip="203.0.113.43")
    result = make_runner(api).run_once()

    assert result.status == cfg_mod.STATUS_SUCCESS
    assert result.public_ip == "203.0.113.43"
    assert state.status == cfg_mod.STATUS_SUCCESS
    assert stopped.calls                     # 定时任务照样停


def test_capacity_summary_includes_target_shape(make_runner, state):
    """汇总行必须带申请规格：规格原先只在 DEBUG 行，后台按 INFO 跑时日志里看不到
    到底申请的是几核几 G（2026-10-22 排查时踩过）。"""
    api = FakeApi(ads=["AD-1"], launch_behaviors=[
        oci_error(500, "InternalError", "Out of host capacity.")])
    result = make_runner(api).run_once()

    expected = f"申请规格 {cfg_mod.TARGET_OCPUS} OCPU / {cfg_mod.TARGET_MEMORY_GB}GB"
    assert expected in result.summary
    assert expected in state.last_result
