"""OCI SDK 薄封装。

只做三件事：把 SDK 调用收拢到一处、把返回值规整成简单数据结构、
把异常原样抛给 errors.classify() 分类。所有业务判断都在 runner.py，
这样测试里可以用假对象替换本模块，不需要真的调用 Oracle。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import oci

from . import config as cfg_mod

# 给实例打的标签，用于识别「本工具创建的实例」，避免重复申请
TAG_MANAGED_BY = "oracle-arm"
TAG_KEY = "managed-by"

# (连接超时, 读取超时)：读取超时后由 runner 先查询实例状态对账，不盲目重试
HTTP_TIMEOUT = (10, 120)

# OCI 对 shape.billing_type 只返回这三种（见 SDK Shape 模型常量）。
#   ALWAYS_FREE  —— 完全免费（如 VM.Standard.E2.1.Micro）
#   LIMITED_FREE —— 在账号免费额度内免费、超出额度才计费（A1.Flex 实测就是这种）
#   PAID         —— 计费
# 因此 A1 的判定不能只认 ALWAYS_FREE，否则会把申请直接挡死（实测踩过）。
FREE_BILLING_TYPES = frozenset({"ALWAYS_FREE", "LIMITED_FREE"})


@dataclass
class InstanceInfo:
    id: str
    display_name: str
    lifecycle_state: str
    shape: str
    ocpus: float
    memory_gb: float
    availability_domain: str = ""
    freeform_tags: dict = field(default_factory=dict)

    @property
    def is_managed(self) -> bool:
        """是否本工具创建的实例。"""
        return self.freeform_tags.get(TAG_KEY) == TAG_MANAGED_BY


@dataclass
class ShapeInfo:
    name: str
    billing_type: str
    quota_names: list[str]
    ocpus: float
    memory_gb: float

    @property
    def is_free_eligible(self) -> bool:
        """OCI 标注的计费类型是否属于免费范围（ALWAYS_FREE / LIMITED_FREE）。

        LIMITED_FREE 表示「在账号免费额度内免费、超出才计费」——A1.Flex 实测就是
        这个值。真正是否越界由 Limits API 的额度判定（见 runner._check_quota）。
        """
        return (self.billing_type or "").upper() in FREE_BILLING_TYPES


@dataclass
class LimitInfo:
    name: str
    used: float
    available: float
    quota_value: float
    scope_type: str = ""      # AD / REGION


@dataclass
class ImageInfo:
    id: str
    display_name: str
    time_created: str = ""


@dataclass
class SubnetInfo:
    id: str
    vcn_id: str
    display_name: str
    has_ipv6: bool = False
    ipv6_cidr: str = ""            # 子网的第一个 IPv6 CIDR（申请 IPv6 时需要）
    internet_gateway_id: str = ""
    prohibit_public_ip: bool = False

    @property
    def can_assign_public_ip(self) -> bool:
        return bool(self.internet_gateway_id) and not self.prohibit_public_ip


def _iso(dt) -> str:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat()
    return str(dt or "")


def _hostname_label(name: str) -> str:
    """把显示名转成合法 hostname：小写、字母开头、仅字母数字和连字符。"""
    label = re.sub(r"[^a-z0-9-]", "-", (name or "arm").lower()).strip("-")
    if not label or not label[0].isalpha():
        label = f"arm-{label}" if label else "arm-free"
    return label[:63]


class OciArmClient:
    """真实 OCI 客户端。构造时即完成 API Key 认证（失败会抛 oci 异常）。"""

    def __init__(self, cfg: cfg_mod.Config, sdk_config: dict | None = None):
        self.cfg = cfg
        # 自己控制重试节奏（429 退避），因此关闭 SDK 内置重试策略
        retry = oci.retry.NoneRetryStrategy()
        self.sdk_config = sdk_config or oci.config.from_file(
            cfg.oci_config_file, cfg.oci_profile
        )
        oci.config.validate_config(self.sdk_config)
        self.tenancy_id: str = self.sdk_config["tenancy"]
        self.region: str = self.sdk_config.get("region", "")
        # 未显式指定 compartment 时用 tenancy 根 compartment
        self.compartment_id: str = cfg.compartment_id or self.tenancy_id

        # 超时后由 runner 先查询实例状态对账，禁止盲目重试
        timeout = HTTP_TIMEOUT
        self.identity = oci.identity.IdentityClient(
            self.sdk_config, retry_strategy=retry, timeout=timeout
        )
        self.compute = oci.core.ComputeClient(
            self.sdk_config, retry_strategy=retry, timeout=timeout
        )
        self.vnet = oci.core.VirtualNetworkClient(
            self.sdk_config, retry_strategy=retry, timeout=timeout
        )
        self.limits = oci.limits.LimitsClient(
            self.sdk_config, retry_strategy=retry, timeout=timeout
        )

    # ── 账号与区域 ─────────────────────────────────────────────────────────────
    def home_region(self) -> str:
        """账号 Home Region 的 region key（如 ap-singapore-1）。"""
        subs = self.identity.list_region_subscriptions(self.tenancy_id).data
        for sub in subs:
            if sub.is_home_region:
                return sub.region_name or sub.region_key
        return ""

    def compartment_info(self) -> tuple[str, str]:
        """返回 (Compartment 名称, 状态)。未显式配置时即 tenancy 根 Compartment。"""
        data = self.identity.get_compartment(self.compartment_id).data
        return data.name or "", data.lifecycle_state or ""

    def plan_type(self) -> str | None:
        """尽力查询账号套餐；查不到返回 None，不阻塞流程。

        实测：osp_home_region 必须传**账号的 Home Region**（新加坡账号传
        us-ashburn-1 会返回 Internal Server Error），所以先试账号所在区域再退回默认值。
        免费账号返回 FREE_TIER（意味着账号本身无法产生费用）。
        """
        try:
            client = oci.osp_gateway.SubscriptionServiceClient(
                self.sdk_config, retry_strategy=oci.retry.NoneRetryStrategy(), timeout=HTTP_TIMEOUT
            )
        except Exception:
            return None
        regions = [r for r in (self.region, cfg_mod.OSP_HOME_REGION) if r]
        for region in dict.fromkeys(regions):  # 去重且保序
            try:
                subs = client.list_subscriptions(
                    osp_home_region=region, compartment_id=self.tenancy_id
                ).data
                items = getattr(subs, "items", None) or []
                if items:
                    return items[0].plan_type
            except Exception:
                continue
        return None

    def availability_domains(self) -> list[str]:
        ads = self.identity.list_availability_domains(self.tenancy_id).data
        return [ad.name for ad in ads if ad.name]

    def shape_info(self, availability_domain: str) -> ShapeInfo | None:
        """查询目标规格在该 AD 的可用性与计费类型（不硬编码任何额度）。"""
        shapes = self.compute.list_shapes(
            self.compartment_id,
            availability_domain=availability_domain,
            shape=cfg_mod.TARGET_SHAPE,
        ).data
        for shape in shapes:
            if shape.shape == cfg_mod.TARGET_SHAPE:
                return ShapeInfo(
                    name=shape.shape,
                    billing_type=shape.billing_type or "",
                    quota_names=list(shape.quota_names or []),
                    ocpus=float(shape.ocpus or 0),
                    memory_gb=float(shape.memory_in_gbs or 0),
                )
        return None

    def limit_availability(self, availability_domain: str, quota_names: list[str]) -> list[LimitInfo]:
        """逐个查询额度名称的已用量与剩余量（Limits API 是官方口径）。

        两个实测坑：
        1. 必须按 scope_type 决定要不要传 availability_domain —— 区域级额度传了 AD
           会被 400 拒绝（"Parameter 'availabilityDomain' should be null for this
           limit's scope type"），而 shape.quota_names 里 AD 级和 REGION 级是混在一起的。
        2. effective_quota_value 可能返回 None（实测有账号如此），此时用 used + available
           作为该额度的实际上限；否则会把 None 当成 0，误判成「额度不足」直接停摆。
        """
        scopes = self._limit_scopes()
        out: list[LimitInfo] = []
        for name in quota_names:
            scope_type, scope_ad = scopes.get(name, (None, None))
            kwargs: dict = {}
            if scope_type == "AD":
                kwargs["availability_domain"] = scope_ad or availability_domain
            elif scope_type is None and "regional" not in name.lower():
                # 拿不到 scope 定义时按 AD 级尝试（AD 级额度名不带 regional）
                kwargs["availability_domain"] = availability_domain

            data = self.limits.get_resource_availability(
                service_name="compute",
                limit_name=name,
                compartment_id=self.tenancy_id,
                **kwargs,
            ).data
            used = float(data.used or 0)
            available = float(data.available or 0)
            quota = data.effective_quota_value
            out.append(
                LimitInfo(
                    name=name,
                    used=used,
                    available=available,
                    quota_value=float(quota) if quota is not None else used + available,
                    scope_type=scope_type or "",
                )
            )
        return out

    def _limit_scopes(self) -> dict[str, tuple[str | None, str | None]]:
        """额度名称 → (scope_type, availability_domain)。用于决定查询时要不要带 AD。"""
        try:
            values = oci.pagination.list_call_get_all_results(
                self.limits.list_limit_values, self.tenancy_id, service_name="compute"
            ).data
        except Exception:
            return {}  # 拿不到就按名称启发式判断（见 limit_availability）
        return {lv.name: (lv.scope_type, lv.availability_domain) for lv in values}

    # ── 资源发现 ───────────────────────────────────────────────────────────────
    def list_instances(self) -> list[InstanceInfo]:
        """列出 compartment 下所有未终止实例。"""
        items = oci.pagination.list_call_get_all_results(
            self.compute.list_instances, self.compartment_id
        ).data
        result = []
        for inst in items:
            if inst.lifecycle_state == "TERMINATED":
                continue
            sc = inst.shape_config
            result.append(
                InstanceInfo(
                    id=inst.id,
                    display_name=inst.display_name or "",
                    lifecycle_state=inst.lifecycle_state or "",
                    shape=inst.shape or "",
                    ocpus=float(getattr(sc, "ocpus", 0) or 0),
                    memory_gb=float(getattr(sc, "memory_in_gbs", 0) or 0),
                    availability_domain=inst.availability_domain or "",
                    freeform_tags=dict(inst.freeform_tags or {}),
                )
            )
        return result

    def get_instance(self, instance_id: str) -> InstanceInfo:
        inst = self.compute.get_instance(instance_id).data
        sc = inst.shape_config
        return InstanceInfo(
            id=inst.id,
            display_name=inst.display_name or "",
            lifecycle_state=inst.lifecycle_state or "",
            shape=inst.shape or "",
            ocpus=float(getattr(sc, "ocpus", 0) or 0),
            memory_gb=float(getattr(sc, "memory_in_gbs", 0) or 0),
            availability_domain=inst.availability_domain or "",
            freeform_tags=dict(inst.freeform_tags or {}),
        )

    def find_image(self) -> ImageInfo | None:
        """查找 Ubuntu ARM64 镜像。

        优先级：LTS 且非 Minimal > LTS > 非 Minimal > 最新。
        实测 Canonical 同时提供 "...-24.04-aarch64-..." 与 "...-24.04-Minimal-aarch64-..."，
        Minimal 版精简掉不少常用包（系统按 TIMECREATED 倒序时它会排在前面），
        这里显式降权，避免默认给用户装精简版。
        """
        images = oci.pagination.list_call_get_all_results(
            self.compute.list_images,
            self.compartment_id,
            operating_system=cfg_mod.UBUNTU_OS,
            shape=cfg_mod.TARGET_SHAPE,
            sort_by="TIMECREATED",
            sort_order="DESC",
            lifecycle_state="AVAILABLE",
        ).data
        if not images:
            return None

        def is_lts(img) -> bool:
            return bool(re.search(r"-\d{2}\.04", img.display_name or ""))

        def is_minimal(img) -> bool:
            return "minimal" in (img.display_name or "").lower()

        chosen = sorted(images, key=lambda i: (not is_lts(i), is_minimal(i)))[0]
        return ImageInfo(
            id=chosen.id,
            display_name=chosen.display_name or "",
            time_created=_iso(getattr(chosen, "time_created", None)),
        )

    def find_public_subnet(self) -> SubnetInfo | None:
        """找一个「路由能出公网且允许公网 IP」的子网。

        只读查询，不创建任何网络资源。显式配置了 subnet_id 时只查那一个，
        但**判定逻辑与自动发现完全一致**（都要查 Internet Gateway + 路由表），
        否则显式配置会被误判成「不能分配公网 IPv4」。
        """
        if self.cfg.subnet_id:
            sub = self.vnet.get_subnet(self.cfg.subnet_id).data
            return self._resolve_subnet(sub)

        vcns = oci.pagination.list_call_get_all_results(
            self.vnet.list_vcns, self.compartment_id
        ).data
        candidates: list[SubnetInfo] = []
        for vcn in vcns:
            if vcn.lifecycle_state != "AVAILABLE":
                continue
            igw_ids = self._vcn_internet_gateways(vcn.id)
            if not igw_ids:
                continue
            route_tables = {
                rt.id: rt
                for rt in oci.pagination.list_call_get_all_results(
                    self.vnet.list_route_tables, self.compartment_id, vcn_id=vcn.id
                ).data
            }
            subnets = oci.pagination.list_call_get_all_results(
                self.vnet.list_subnets, self.compartment_id, vcn_id=vcn.id
            ).data
            for sub in subnets:
                if sub.lifecycle_state != "AVAILABLE":
                    continue
                info = self._resolve_subnet(sub, igw_ids, route_tables)
                if info.can_assign_public_ip:
                    candidates.append(info)

        if not candidates:
            return None
        # 优先名字里带 public 的子网，其次第一个可用的
        candidates.sort(key=lambda s: (0 if "public" in s.display_name.lower() else 1, s.display_name))
        return candidates[0]

    def _vcn_internet_gateways(self, vcn_id: str) -> set[str]:
        """该 VCN 下所有「可用且启用」的 Internet Gateway 的 OCID。"""
        igws = oci.pagination.list_call_get_all_results(
            self.vnet.list_internet_gateways, self.compartment_id, vcn_id=vcn_id
        ).data
        return {g.id for g in igws if g.lifecycle_state == "AVAILABLE" and g.is_enabled}

    @staticmethod
    def _routes_to_igw(route_table, igw_ids: set[str]) -> bool:
        for rule in route_table.route_rules or []:
            if (
                rule.destination == "0.0.0.0/0"
                and rule.network_entity_id in igw_ids
                and rule.destination_type in (None, "CIDR_BLOCK")
            ):
                return True
        return False

    def _resolve_subnet(self, sub, igw_ids: set[str] | None = None, route_tables: dict | None = None) -> SubnetInfo:
        """把 SDK 的 Subnet 规整成 SubnetInfo，并判定能否分配公网 IPv4。

        判定依据：子网的路由表里有 0.0.0.0/0 → 已启用的 Internet Gateway 的路由规则。
        """
        if igw_ids is None:
            igw_ids = self._vcn_internet_gateways(sub.vcn_id)
        igw_id = ""
        if igw_ids:
            if route_tables is None:
                rt = self.vnet.get_route_table(sub.route_table_id).data
            else:
                rt = route_tables.get(sub.route_table_id)
            if rt is not None and self._routes_to_igw(rt, igw_ids):
                igw_id = sorted(igw_ids)[0]  # 取一个确定的值，便于复现
        ipv6_blocks = list(sub.ipv6_cidr_blocks or [])
        return SubnetInfo(
            id=sub.id,
            vcn_id=sub.vcn_id,
            display_name=sub.display_name or "",
            has_ipv6=bool(ipv6_blocks),
            ipv6_cidr=ipv6_blocks[0] if ipv6_blocks else "",
            internet_gateway_id=igw_id,
            prohibit_public_ip=bool(sub.prohibit_public_ip_on_vnic),
        )

    # ── 创建与查询 ─────────────────────────────────────────────────────────────
    def launch(
        self,
        availability_domain: str,
        image_id: str,
        subnet_id: str,
        ssh_public_key: str,
        retry_token: str,
        enable_ipv6: bool = False,
        ipv6_subnet_cidr: str = "",
    ) -> str:
        """创建实例。规格固定为 TARGET_OCPUS / TARGET_MEMORY_GB / 50GB，不接受外部放大。

        opc_retry_token 提供幂等保证：同一次逻辑请求重复提交不会创建两个实例。
        """
        vnic_kwargs = {
            "subnet_id": subnet_id,
            "assign_public_ip": True,  # 必须分配公网 IPv4（需求）
            "assign_private_dns_record": True,
            "hostname_label": _hostname_label(self.cfg.display_name),
            "display_name": f"{self.cfg.display_name}-vnic",
        }
        if enable_ipv6 and ipv6_subnet_cidr:
            vnic_kwargs["assign_ipv6_ip"] = True
            vnic_kwargs["ipv6_address_ipv6_subnet_cidr_pair_details"] = (
                oci.core.models.Ipv6AddressIpv6SubnetCidrPairDetails(
                    ipv6_subnet_cidr=ipv6_subnet_cidr
                )
            )

        details = oci.core.models.LaunchInstanceDetails(
            availability_domain=availability_domain,
            compartment_id=self.compartment_id,
            display_name=self.cfg.display_name,
            shape=cfg_mod.TARGET_SHAPE,
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=cfg_mod.TARGET_OCPUS,
                memory_in_gbs=cfg_mod.TARGET_MEMORY_GB,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                source_type="image",
                image_id=image_id,
                boot_volume_size_in_gbs=cfg_mod.TARGET_BOOT_GB,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(**vnic_kwargs),
            metadata={"ssh_authorized_keys": ssh_public_key},
            freeform_tags={TAG_KEY: TAG_MANAGED_BY, "purpose": "always-free-arm"},
        )
        resp = self.compute.launch_instance(details, opc_retry_token=retry_token)
        return resp.data.id

    def public_ip(self, instance_id: str) -> str | None:
        """读取实例主 VNIC 的公网 IPv4；未分配时返回 None。"""
        attachments = oci.pagination.list_call_get_all_results(
            self.compute.list_vnic_attachments, self.compartment_id, instance_id=instance_id
        ).data
        for att in sorted(attachments, key=lambda a: a.nic_index or 0):
            if att.lifecycle_state != "ATTACHED" or not att.vnic_id:
                continue
            vnic = self.vnet.get_vnic(att.vnic_id).data
            if vnic.public_ip:
                return vnic.public_ip
        return None

    def wait_for_state(
        self, instance_id: str, target: str, timeout: int, interval: int = 15, sleep=time.sleep
    ) -> InstanceInfo:
        """轮询直到实例进入目标状态；超时抛 TimeoutError（由 runner 处理）。"""
        deadline = time.monotonic() + timeout
        last = None
        while True:
            last = self.get_instance(instance_id)
            if last.lifecycle_state == target:
                return last
            if last.lifecycle_state in ("TERMINATING", "TERMINATED"):
                raise RuntimeError(f"实例进入 {last.lifecycle_state} 状态，无法继续")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"等待实例 {instance_id} 进入 {target} 超时（最后状态 {last.lifecycle_state}）"
                )
            sleep(interval)
