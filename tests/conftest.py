"""测试公共夹具：全部使用假 OCI 对象，不会调用任何真实的 Oracle 接口。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oracle_arm import client as client_mod  # noqa: E402
from oracle_arm import config as cfg_mod  # noqa: E402
from oracle_arm import runner as runner_mod  # noqa: E402
from oracle_arm import util as util_mod  # noqa: E402

# 测试期间被「发出」的通知（由 autouse 夹具替换 util.notify 后收集）
FAKE_NOTIFICATIONS: list[tuple[str, str]] = []


class FakeLogger:
    """把日志记在内存里，方便断言「有没有提示到位」。"""

    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def _add(self, level, msg):
        self.lines.append((level, str(msg)))

    def debug(self, msg): self._add("debug", msg)
    def info(self, msg): self._add("info", msg)
    def warning(self, msg): self._add("warning", msg)
    def error(self, msg): self._add("error", msg)

    @property
    def text(self) -> str:
        return "\n".join(m for _, m in self.lines)


class FakeApi:
    """OCI 客户端的假实现。行为由构造参数控制，默认一切正常。"""

    def __init__(
        self,
        ads=("AD-1", "AD-2", "AD-3"),
        home_region=cfg_mod.TARGET_REGION,
        region=cfg_mod.TARGET_REGION,
        shape=None,
        shape_by_ad=None,
        limits=None,
        limits_error=None,
        image=None,
        subnet=None,
        existing=(),
        created=(),
        launch_behaviors=(),
        public_ip="203.0.113.10",
        plan_type="FREE",
        running_state="RUNNING",
        wait_error=None,
    ):
        self.region = region
        self._home_region = home_region
        self._ads = list(ads)
        self._shape = shape if shape is not None else client_mod.ShapeInfo(
            name=cfg_mod.TARGET_SHAPE,
            billing_type="ALWAYS_FREE",
            quota_names=["standard-a1-core-count", "standard-a1-memory-count"],
            ocpus=4,
            memory_gb=24,
        )
        self._shape_by_ad = shape_by_ad or {}
        self._limits = limits if limits is not None else [
            client_mod.LimitInfo("standard-a1-core-count", used=0, available=4, quota_value=4),
            client_mod.LimitInfo("standard-a1-memory-count", used=0, available=24, quota_value=24),
        ]
        self._limits_error = limits_error
        self._image = image if image is not None else client_mod.ImageInfo(
            id="ocid1.image.oc1.ap-singapore-1.ubuntu2404", display_name="Canonical-Ubuntu-24.04-aarch64-2025.09.15-0"
        )
        self._subnet = subnet if subnet is not None else client_mod.SubnetInfo(
            id="ocid1.subnet.oc1.ap-singapore-1.public",
            vcn_id="ocid1.vcn.oc1.ap-singapore-1.x",
            display_name="public-subnet",
            internet_gateway_id="ocid1.internetgateway.oc1.ap-singapore-1.x",
        )
        self.existing = list(existing)
        self.created = list(created)
        self.launch_behaviors = list(launch_behaviors)
        self.public_ip_value = public_ip
        self.plan_type_value = plan_type
        self.running_state = running_state
        self.wait_error = wait_error

        self.launch_calls: list[dict] = []
        self.list_instances_calls = 0
        self.public_ip_calls = 0
        self.limit_calls = 0      # 额度查询次数（用于钉住「确实复查了」）

    # ── 账号与区域 ─────────────────────────────────────────────────────────────
    def home_region(self): return self._home_region
    def compartment_info(self): return ("root-compartment", "ACTIVE")
    def plan_type(self): return self.plan_type_value
    def availability_domains(self): return list(self._ads)

    def shape_info(self, ad):
        if ad in self._shape_by_ad:
            return self._shape_by_ad[ad]
        return self._shape

    def limit_availability(self, ad, quota_names):
        self.limit_calls += 1
        if self._limits_error is not None:
            raise self._limits_error
        return list(self._limits)

    # ── 资源 ───────────────────────────────────────────────────────────────────
    def list_instances(self):
        self.list_instances_calls += 1
        return list(self.existing) + list(self.created)

    def find_image(self): return self._image
    def find_public_subnet(self): return self._subnet

    # ── 创建 ───────────────────────────────────────────────────────────────────
    def launch(self, availability_domain, image_id, subnet_id, ssh_public_key,
               retry_token, enable_ipv6=False, ipv6_subnet_cidr=""):
        self.launch_calls.append({
            "ad": availability_domain, "image_id": image_id, "subnet_id": subnet_id,
            "ssh_public_key": ssh_public_key, "retry_token": retry_token,
            "enable_ipv6": enable_ipv6, "ipv6_subnet_cidr": ipv6_subnet_cidr,
        })
        behavior = self.launch_behaviors.pop(0) if self.launch_behaviors else "ocid1.instance.new"
        if callable(behavior):  # 自定义行为：可以先把实例塞进 created 再抛错
            behavior = behavior(self)
        if isinstance(behavior, Exception):
            raise behavior
        self.created.append(make_instance(behavior, state="PROVISIONING"))
        return behavior

    def get_instance(self, instance_id):
        for inst in self.existing + self.created:
            if inst.id == instance_id:
                return inst
        raise AssertionError(f"未知实例 {instance_id}")

    def wait_for_state(self, instance_id, target, timeout, interval=15, sleep=None):
        if self.wait_error is not None:
            raise self.wait_error
        inst = self.get_instance(instance_id)
        inst.lifecycle_state = self.running_state
        return inst

    def public_ip(self, instance_id):
        self.public_ip_calls += 1
        return self.public_ip_value


def make_instance(instance_id, state="RUNNING", name="arm-free-1", managed=True, ip=None):
    tags = {"managed-by": client_mod.TAG_MANAGED_BY} if managed else {}
    return client_mod.InstanceInfo(
        id=instance_id, display_name=name, lifecycle_state=state,
        shape=cfg_mod.TARGET_SHAPE, ocpus=cfg_mod.TARGET_OCPUS,
        memory_gb=cfg_mod.TARGET_MEMORY_GB,
        availability_domain="AD-1", freeform_tags=tags,
    )


def oci_error(status, code, message, headers=None):
    import oci
    return oci.exceptions.ServiceError(status, code, headers or {}, message)


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    """所有测试一律把落盘路径重定向到临时目录。

    兜底性质：任何测试（含以后新加的）都不允许写用户真实家目录
    ~/.oracle-arm 或 ~/Library/LaunchAgents，否则会污染用户的首次安装。
    """
    base = tmp_path / "home"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg_mod, "BASE_DIR", base)
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", base / "config.json")
    monkeypatch.setattr(cfg_mod, "STATE_PATH", base / "state.json")
    monkeypatch.setattr(cfg_mod, "LOG_DIR", base / "logs")
    monkeypatch.setattr(cfg_mod, "LOG_PATH", base / "logs" / "oracle-arm.log")
    monkeypatch.setattr(cfg_mod, "LOCK_PATH", base / "run.lock")
    # 凭据路径也必须隔离：机器上存在真实 ~/.oci/config 时，若不隔离，
    # 测试会构造真实客户端并发起真实 API 调用（实测把测试从 1.7s 拖到 21s）
    monkeypatch.setattr(cfg_mod, "DEFAULT_OCI_CONFIG", str(base / "no-such-oci-config"))
    monkeypatch.setattr(cfg_mod, "DEFAULT_SSH_KEY", base / "no-such-ssh-key")

    # 系统通知也必须隔离：cli 用的是真实 util.notify，测试里跑 cli.main(["install"])
    # 会真的弹出 macOS 通知（实测发生过：一条「配置有问题：申请时刻格式错误 '25:00'」）
    FAKE_NOTIFICATIONS.clear()
    monkeypatch.setattr(
        util_mod, "notify",
        lambda title, message, enabled=True: FAKE_NOTIFICATIONS.append((title, message)),
    )
    monkeypatch.setattr(cfg_mod, "APP_DIR", base / "app")
    monkeypatch.setattr(cfg_mod, "VENV_DIR", base / "venv")
    monkeypatch.setattr(
        cfg_mod, "LAUNCHD_PLIST",
        base / "LaunchAgents" / f"{cfg_mod.LAUNCHD_LABEL}.plist",
    )
    return base


@pytest.fixture
def ssh_key(tmp_path):
    """伪造一个 SSH 公钥文件（内容是假公钥，不是私钥）。"""
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE test@local\n")
    return pub


@pytest.fixture
def cfg(ssh_key, tmp_path):
    c = cfg_mod.Config(ssh_public_key=str(ssh_key), schedule_beijing=["02:05", "04:55", "08:05", "20:45"])
    c.normalize()
    return c


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg_mod, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cfg_mod, "BASE_DIR", tmp_path)
    return cfg_mod.State()


@pytest.fixture
def log():
    return FakeLogger()


class Recorder:
    def __init__(self):
        self.calls: list = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args if not kwargs else (args, kwargs))


@pytest.fixture
def sleeps():
    return Recorder()


@pytest.fixture
def notifications():
    return Recorder()


@pytest.fixture
def stopped():
    return Recorder()


@pytest.fixture
def make_runner(cfg, state, log, sleeps, notifications, stopped):
    def _make(api, **overrides):
        kwargs = dict(
            cfg=overrides.pop("cfg", cfg),
            state=overrides.pop("state", state),
            api=api,
            log=log,
            sleep=sleeps,
            notifier=notifications,
            stop_schedule=stopped,
        )
        kwargs.update(overrides)
        return runner_mod.Runner(**kwargs)

    return _make
