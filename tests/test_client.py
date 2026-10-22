"""用真实 OCI SDK 模型 + 假传输层测试 OciArmClient。

与 test_runner.py 的假 API 不同：这里调用的是**真实的** client 方法，
断言的是真实构造出来的 SDK 请求对象，因此能发现「参数名对但根本没传」这类
只有真跑才会暴露的问题（例如 IPv6、显式 subnet_id 分支）。
"""

import oci
import pytest

from oracle_arm import client as client_mod
from oracle_arm import config as cfg_mod


class Resp:
    def __init__(self, data):
        self.data = data
        self.headers = {}


class FakeTransport:
    """按方法名返回预置对象，并记录调用。"""

    def __init__(self, **returns):
        self.returns = returns
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            value = self.returns.get(name)
            if callable(value):
                value = value(*args, **kwargs)
            return Resp(value)

        return call

    def called(self, name):
        return [c for c in self.calls if c[0] == name]


def make_client(cfg, compute=None, vnet=None, identity=None):
    """绕过 __init__（不读 ~/.oci/config、不联网），只装好被测方法需要的客户端。"""
    c = object.__new__(client_mod.OciArmClient)
    c.cfg = cfg
    c.sdk_config = {"tenancy": "ocid1.tenancy.oc1..t", "region": cfg_mod.TARGET_REGION}
    c.tenancy_id = "ocid1.tenancy.oc1..t"
    c.region = cfg_mod.TARGET_REGION
    c.compartment_id = cfg.compartment_id or c.tenancy_id
    c.compute = compute or FakeTransport()
    c.vnet = vnet or FakeTransport()
    c.identity = identity or FakeTransport()
    c.limits = FakeTransport()
    return c


@pytest.fixture(autouse=True)
def flat_pagination(monkeypatch):
    """分页辅助函数直接透传，避免假传输层要实现整套分页协议。"""
    monkeypatch.setattr(
        client_mod.oci.pagination, "list_call_get_all_results", lambda f, *a, **k: f(*a, **k)
    )


def _subnet(**kw):
    base = dict(
        id="ocid1.subnet.1", vcn_id="ocid1.vcn.1", display_name="public-subnet",
        route_table_id="ocid1.routetable.1", lifecycle_state="AVAILABLE",
        ipv6_cidr_blocks=[], prohibit_public_ip_on_vnic=False,
    )
    base.update(kw)
    return oci.core.models.Subnet(**base)


def _igw(gw_id="ocid1.igw.1"):
    return oci.core.models.InternetGateway(
        id=gw_id, lifecycle_state="AVAILABLE", is_enabled=True
    )


def _route_table(entity_id, rt_id="ocid1.routetable.1"):
    return oci.core.models.RouteTable(
        id=rt_id,
        route_rules=[
            oci.core.models.RouteRule(
                destination="0.0.0.0/0", destination_type="CIDR_BLOCK",
                network_entity_id=entity_id,
            )
        ],
    )


# ── 显式配置 subnet_id 的分支 ──────────────────────────────────────────────────
def test_explicit_subnet_id_resolves_internet_gateway(cfg):
    """显式指定 subnet_id 时也必须查 Internet Gateway + 路由表。

    否则会被误判成「不能分配公网 IPv4」，工具一填这个配置项就永久暂停。
    """
    cfg.subnet_id = "ocid1.subnet.1"
    vnet = FakeTransport(
        get_subnet=_subnet(ipv6_cidr_blocks=["2001:db8::/64"]),
        list_internet_gateways=[_igw()],
        get_route_table=_route_table("ocid1.igw.1"),
    )
    info = make_client(cfg, vnet=vnet).find_public_subnet()

    assert info is not None
    assert info.can_assign_public_ip is True
    assert info.internet_gateway_id == "ocid1.igw.1"
    assert info.has_ipv6 is True
    assert info.ipv6_cidr == "2001:db8::/64"


def test_explicit_subnet_id_routed_to_nat_is_rejected(cfg):
    """路由指向 NAT 网关（不是 Internet Gateway）的子网不能分配公网 IP。"""
    cfg.subnet_id = "ocid1.subnet.1"
    vnet = FakeTransport(
        get_subnet=_subnet(),
        list_internet_gateways=[_igw()],
        get_route_table=_route_table("ocid1.natgateway.1"),
    )
    info = make_client(cfg, vnet=vnet).find_public_subnet()
    assert info.can_assign_public_ip is False


def test_explicit_subnet_id_without_igw_is_rejected(cfg):
    cfg.subnet_id = "ocid1.subnet.1"
    vnet = FakeTransport(get_subnet=_subnet(), list_internet_gateways=[])
    info = make_client(cfg, vnet=vnet).find_public_subnet()
    assert info.can_assign_public_ip is False


def test_explicit_subnet_id_prohibiting_public_ip_is_rejected(cfg):
    cfg.subnet_id = "ocid1.subnet.1"
    vnet = FakeTransport(
        get_subnet=_subnet(prohibit_public_ip_on_vnic=True),
        list_internet_gateways=[_igw()],
        get_route_table=_route_table("ocid1.igw.1"),
    )
    assert make_client(cfg, vnet=vnet).find_public_subnet().can_assign_public_ip is False


def test_disabled_internet_gateway_is_not_accepted(cfg):
    cfg.subnet_id = "ocid1.subnet.1"
    disabled = oci.core.models.InternetGateway(
        id="ocid1.igw.1", lifecycle_state="AVAILABLE", is_enabled=False
    )
    vnet = FakeTransport(get_subnet=_subnet(), list_internet_gateways=[disabled])
    assert make_client(cfg, vnet=vnet).find_public_subnet().can_assign_public_ip is False


# ── 创建请求的真实内容 ─────────────────────────────────────────────────────────
def _launch(client, **kwargs):
    params = dict(
        availability_domain="AD-1", image_id="ocid1.image.1", subnet_id="ocid1.subnet.1",
        ssh_public_key="ssh-ed25519 AAAA test", retry_token="token-1",
    )
    params.update(kwargs)
    return client.launch(**params)


def test_launch_builds_fixed_spec_request(cfg):
    compute = FakeTransport(launch_instance=type("I", (), {"id": "ocid1.instance.1"})())
    client = make_client(cfg, compute=compute)
    instance_id = _launch(client)

    assert instance_id == "ocid1.instance.1"
    details = compute.called("launch_instance")[0][1][0]
    assert details.shape == "VM.Standard.A1.Flex"
    assert details.shape_config.ocpus == cfg_mod.TARGET_OCPUS
    assert details.shape_config.memory_in_gbs == cfg_mod.TARGET_MEMORY_GB
    assert details.source_details.boot_volume_size_in_gbs == 50
    assert details.create_vnic_details.assign_public_ip is True   # 公网 IPv4 必需
    assert details.freeform_tags["managed-by"] == "oracle-arm"
    assert details.compartment_id == "ocid1.tenancy.oc1..t"
    # 幂等标识必须真的传给接口
    assert compute.called("launch_instance")[0][2]["opc_retry_token"] == "token-1"


def test_launch_does_not_request_ipv6_by_default(cfg):
    compute = FakeTransport(launch_instance=type("I", (), {"id": "x"})())
    _launch(make_client(cfg, compute=compute))
    details = compute.called("launch_instance")[0][1][0]
    assert details.create_vnic_details.assign_ipv6_ip in (None, False)
    assert details.create_vnic_details.ipv6_address_ipv6_subnet_cidr_pair_details is None


def test_launch_requests_ipv6_when_enabled_and_cidr_given(cfg):
    """enable_ipv6 不能是空开关：必须真的把 assign_ipv6_ip 写进请求。"""
    compute = FakeTransport(launch_instance=type("I", (), {"id": "x"})())
    _launch(make_client(cfg, compute=compute), enable_ipv6=True,
            ipv6_subnet_cidr="2001:db8::/64")
    vnic = compute.called("launch_instance")[0][1][0].create_vnic_details
    assert vnic.assign_ipv6_ip is True
    assert vnic.ipv6_address_ipv6_subnet_cidr_pair_details.ipv6_subnet_cidr == "2001:db8::/64"


def test_launch_ignores_ipv6_without_cidr(cfg):
    compute = FakeTransport(launch_instance=type("I", (), {"id": "x"})())
    _launch(make_client(cfg, compute=compute), enable_ipv6=True, ipv6_subnet_cidr="")
    vnic = compute.called("launch_instance")[0][1][0].create_vnic_details
    assert vnic.assign_ipv6_ip in (None, False)


def test_launch_hostname_label_is_valid(cfg):
    cfg.display_name = "My ARM Box!"
    compute = FakeTransport(launch_instance=type("I", (), {"id": "x"})())
    _launch(make_client(cfg, compute=compute))
    vnic = compute.called("launch_instance")[0][1][0].create_vnic_details
    assert vnic.hostname_label == "my-arm-box"


# ── 公网 IP 读取 ───────────────────────────────────────────────────────────────
def test_public_ip_reads_primary_vnic(cfg):
    compute = FakeTransport(
        list_vnic_attachments=[
            oci.core.models.VnicAttachment(
                vnic_id="ocid1.vnic.1", lifecycle_state="ATTACHED", nic_index=0
            )
        ]
    )
    vnet = FakeTransport(get_vnic=oci.core.models.Vnic(id="ocid1.vnic.1", public_ip="203.0.113.7"))
    assert make_client(cfg, compute=compute, vnet=vnet).public_ip("ocid1.instance.1") == "203.0.113.7"


def test_public_ip_none_when_not_assigned(cfg):
    compute = FakeTransport(
        list_vnic_attachments=[
            oci.core.models.VnicAttachment(
                vnic_id="ocid1.vnic.1", lifecycle_state="ATTACHED", nic_index=0
            )
        ]
    )
    vnet = FakeTransport(get_vnic=oci.core.models.Vnic(id="ocid1.vnic.1", public_ip=None))
    assert make_client(cfg, compute=compute, vnet=vnet).public_ip("ocid1.instance.1") is None


def test_public_ip_skips_unattached_vnics(cfg):
    compute = FakeTransport(
        list_vnic_attachments=[
            oci.core.models.VnicAttachment(
                vnic_id="ocid1.vnic.0", lifecycle_state="ATTACHING", nic_index=0
            )
        ]
    )
    vnet = FakeTransport(get_vnic=oci.core.models.Vnic(id="x", public_ip="203.0.113.9"))
    assert make_client(cfg, compute=compute, vnet=vnet).public_ip("ocid1.instance.1") is None


# ── 账号 / 区域 / 额度 / 镜像 / 实例（全部用真实 SDK 模型） ──────────────────────
def test_home_region_picks_the_home_subscription(cfg):
    identity = FakeTransport(list_region_subscriptions=[
        oci.identity.models.RegionSubscription(
            region_key="us-ashburn-1", region_name="us-ashburn-1", is_home_region=False),
        oci.identity.models.RegionSubscription(
            region_key="ap-singapore-1", region_name="ap-singapore-1", is_home_region=True),
    ])
    assert make_client(cfg, identity=identity).home_region() == "ap-singapore-1"


def test_home_region_empty_when_none_flagged(cfg):
    identity = FakeTransport(list_region_subscriptions=[
        oci.identity.models.RegionSubscription(
            region_key="us-ashburn-1", region_name="us-ashburn-1", is_home_region=False),
    ])
    assert make_client(cfg, identity=identity).home_region() == ""


def test_shape_info_reads_billing_type_and_quota_names(cfg):
    compute = FakeTransport(list_shapes=[
        oci.core.models.Shape(
            shape="VM.Standard.A1.Flex", billing_type="ALWAYS_FREE",
            quota_names=["standard-a1-core-count", "standard-a1-memory-count"],
            ocpus=4, memory_in_gbs=24),
    ])
    info = make_client(cfg, compute=compute).shape_info("AD-1")

    assert info.name == "VM.Standard.A1.Flex"
    assert info.is_free_eligible is True
    assert info.quota_names == ["standard-a1-core-count", "standard-a1-memory-count"]


def test_shape_info_none_when_shape_absent(cfg):
    compute = FakeTransport(list_shapes=[
        oci.core.models.Shape(shape="VM.Standard.E4.Flex", billing_type="PAID"),
    ])
    assert make_client(cfg, compute=compute).shape_info("AD-1") is None


def _limit_values(*specs):
    """构造 list_limit_values 的返回：specs 为 (name, scope_type, ad, value)。"""
    return [
        oci.limits.models.LimitValueSummary(
            name=name, scope_type=scope, availability_domain=ad, value=value
        )
        for name, scope, ad, value in specs
    ]


def test_limit_availability_reads_quota_fields(cfg):
    limits = FakeTransport(
        list_limit_values=_limit_values(("standard-a1-core-count", "AD", "AD-1", 2)),
        get_resource_availability=lambda *a, **k:
            oci.limits.models.ResourceAvailability(used=1, available=1, effective_quota_value=2),
    )
    client = make_client(cfg)
    client.limits = limits
    out = client.limit_availability("AD-1", ["standard-a1-core-count"])

    assert out[0].name == "standard-a1-core-count"
    assert (out[0].used, out[0].available, out[0].quota_value) == (1, 1, 2)
    assert out[0].scope_type == "AD"
    _, args, kwargs = limits.called("get_resource_availability")[0]
    assert kwargs["service_name"] == "compute"
    assert kwargs["availability_domain"] == "AD-1"
    assert kwargs["compartment_id"] == "ocid1.tenancy.oc1..t"


def test_limit_availability_omits_ad_for_regional_scope(cfg):
    """区域级额度传了 availability_domain 会被 400 拒绝（实测踩过）。"""
    limits = FakeTransport(
        list_limit_values=_limit_values(
            ("standard-a1-core-regional-count", "REGION", None, 2),
            ("standard-a1-core-count", "AD", "AD-1", 2),
        ),
        get_resource_availability=lambda *a, **k:
            oci.limits.models.ResourceAvailability(used=0, available=2, effective_quota_value=2),
    )
    client = make_client(cfg)
    client.limits = limits
    client.limit_availability("AD-1", ["standard-a1-core-regional-count", "standard-a1-core-count"])

    regional, ad = limits.called("get_resource_availability")
    assert "availability_domain" not in regional[2]          # 区域级不传 AD
    assert ad[2]["availability_domain"] == "AD-1"            # AD 级要传


def test_limit_availability_falls_back_when_quota_value_is_none(cfg):
    """实测 effective_quota_value 可能是 None；当成 0 会误判成「额度不足」直接停摆。"""
    limits = FakeTransport(
        list_limit_values=_limit_values(("standard-a1-core-count", "AD", "AD-1", 2)),
        get_resource_availability=lambda *a, **k:
            oci.limits.models.ResourceAvailability(used=0, available=2, effective_quota_value=None),
    )
    client = make_client(cfg)
    client.limits = limits
    out = client.limit_availability("AD-1", ["standard-a1-core-count"])
    assert out[0].quota_value == 2          # used + available


def test_limit_availability_without_scope_info_uses_name_heuristic(cfg):
    """拿不到 scope 定义时：名字带 regional 的按区域级处理，其余按 AD 级。"""
    limits = FakeTransport(
        list_limit_values=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no permission")),
        get_resource_availability=lambda *a, **k:
            oci.limits.models.ResourceAvailability(used=0, available=2, effective_quota_value=2),
    )
    client = make_client(cfg)
    client.limits = limits
    client.limit_availability("AD-1", ["standard-a1-core-regional-count", "standard-a1-core-count"])

    regional, ad = limits.called("get_resource_availability")
    assert "availability_domain" not in regional[2]
    assert ad[2]["availability_domain"] == "AD-1"


def test_plan_type_tries_home_region_first(cfg):
    """实测：osp_home_region 必须传账号 Home Region，传 us-ashburn-1 会 500。"""
    seen = []

    class FakeSubs:
        def __init__(self, *a, **k):
            pass

        def list_subscriptions(self, osp_home_region, compartment_id):
            seen.append(osp_home_region)
            if osp_home_region == cfg_mod.OSP_HOME_REGION:
                raise RuntimeError("Internal Server Error")
            return type("R", (), {"data": type("D", (), {
                "items": [oci.osp_gateway.models.SubscriptionSummary(plan_type="FREE_TIER")]})()})()

    import oracle_arm.client as cm
    original = cm.oci.osp_gateway.SubscriptionServiceClient
    cm.oci.osp_gateway.SubscriptionServiceClient = FakeSubs
    try:
        assert make_client(cfg).plan_type() == "FREE_TIER"
    finally:
        cm.oci.osp_gateway.SubscriptionServiceClient = original
    assert seen[0] == cfg_mod.TARGET_REGION


def test_find_image_prefers_lts_over_newer_non_lts(cfg):
    compute = FakeTransport(list_images=[
        oci.core.models.Image(id="new", display_name="Canonical-Ubuntu-25.10-aarch64-2026.01.01-0"),
        oci.core.models.Image(id="lts", display_name="Canonical-Ubuntu-24.04-aarch64-2025.09.15-0"),
    ])
    assert make_client(cfg, compute=compute).find_image().id == "lts"


def test_find_image_prefers_full_over_minimal(cfg):
    """Canonical 同时提供 Minimal 版，且按时间倒序时它排在前面；
    默认不能给用户装精简版。"""
    compute = FakeTransport(list_images=[
        oci.core.models.Image(
            id="min", display_name="Canonical-Ubuntu-24.04-Minimal-aarch64-2026.09.18-0"),
        oci.core.models.Image(
            id="full", display_name="Canonical-Ubuntu-24.04-aarch64-2026.09.18-0"),
    ])
    assert make_client(cfg, compute=compute).find_image().id == "full"


def test_find_image_falls_back_to_minimal_if_only_option(cfg):
    compute = FakeTransport(list_images=[
        oci.core.models.Image(
            id="min", display_name="Canonical-Ubuntu-24.04-Minimal-aarch64-2026.09.18-0"),
    ])
    assert make_client(cfg, compute=compute).find_image().id == "min"


def test_find_image_falls_back_to_newest(cfg):
    compute = FakeTransport(list_images=[
        oci.core.models.Image(id="new", display_name="Canonical-Ubuntu-25.10-aarch64-2026.01.01-0"),
    ])
    assert make_client(cfg, compute=compute).find_image().id == "new"


def test_find_image_none_when_empty(cfg):
    assert make_client(cfg, compute=FakeTransport(list_images=[])).find_image() is None


def test_list_instances_skips_terminated_and_reads_spec(cfg):
    compute = FakeTransport(list_instances=[
        oci.core.models.Instance(
            id="alive", display_name="arm-free-1", lifecycle_state="RUNNING",
            shape="VM.Standard.A1.Flex", availability_domain="AD-1",
            shape_config=oci.core.models.InstanceShapeConfig(ocpus=1, memory_in_gbs=6),
            freeform_tags={"managed-by": "oracle-arm"}),
        oci.core.models.Instance(
            id="dead", display_name="old", lifecycle_state="TERMINATED",
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.InstanceShapeConfig(ocpus=1, memory_in_gbs=6)),
    ])
    instances = make_client(cfg, compute=compute).list_instances()

    assert [i.id for i in instances] == ["alive"]
    assert instances[0].is_managed is True
    assert (instances[0].ocpus, instances[0].memory_gb) == (1, 6)


def test_plan_type_returns_none_when_unavailable(cfg, monkeypatch):
    """套餐查询是尽力而为：失败不能影响主流程。"""
    def boom(*a, **k):
        raise RuntimeError("no permission")

    monkeypatch.setattr(client_mod.oci.osp_gateway, "SubscriptionServiceClient", boom)
    assert make_client(cfg).plan_type() is None
