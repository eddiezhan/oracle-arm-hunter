"""配置与状态文件的读写、校验测试。"""

import json

import pytest

from oracle_arm import config as cfg_mod


def test_defaults_are_the_required_target():
    c = cfg_mod.Config()
    assert c.region == "ap-singapore-1"
    assert cfg_mod.TARGET_SHAPE == "VM.Standard.A1.Flex"
    # 目标规格：2 OCPU / 12GB（按常见账号的 A1 免费额度上限用满；
    # 此前 10-20 曾降到 1/6 换命中率）
    assert (cfg_mod.TARGET_OCPUS, cfg_mod.TARGET_MEMORY_GB, cfg_mod.TARGET_BOOT_GB) == (2, 12, 50)
    assert c.schedule_beijing == ["02:05", "04:55", "08:05", "20:45"]
    # 默认改成「每 5 分钟一轮，持续抢」（与参考项目 RETRY_INTERVAL=300 一致）
    assert c.schedule_mode == cfg_mod.SCHEDULE_MODE_INTERVAL
    assert c.interval_seconds == 300
    assert c.enable_ipv6 is False
    assert c.allow_unverified_quota is False


def test_no_config_option_can_enlarge_the_shape():
    """规格必须固定：配置里不允许出现放大规格的字段。"""
    fields = set(cfg_mod.Config().__dataclass_fields__)
    for forbidden in ("ocpus", "memory_gb", "memory_in_gbs", "shape", "boot_volume_size_in_gbs"):
        assert forbidden not in fields


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    c = cfg_mod.Config(compartment_id="ocid1.compartment.oc1..x", display_name="my-arm")
    c.save(path)
    loaded = cfg_mod.Config.load(path)
    assert loaded.compartment_id == "ocid1.compartment.oc1..x"
    assert loaded.display_name == "my-arm"


def test_load_fills_missing_fields_with_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"display_name": "x"}), encoding="utf-8")
    loaded = cfg_mod.Config.load(path)
    assert loaded.display_name == "x"
    assert loaded.region == cfg_mod.TARGET_REGION
    assert loaded.schedule_beijing == cfg_mod.DEFAULT_SCHEDULE


def test_load_ignores_unknown_fields(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"display_name": "x", "legacy_field": 1}), encoding="utf-8")
    assert cfg_mod.Config.load(path).display_name == "x"


def test_load_raises_on_broken_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        cfg_mod.Config.load(path)


def test_normalize_dedupes_and_sorts_schedule():
    c = cfg_mod.Config(schedule_beijing=["20:45", "02:05", "02:05", " 08:05 "])
    c.normalize()
    assert c.schedule_beijing == ["02:05", "08:05", "20:45"]


def test_normalize_falls_back_when_schedule_empty():
    c = cfg_mod.Config(schedule_beijing=[])
    c.normalize()
    assert c.schedule_beijing == cfg_mod.DEFAULT_SCHEDULE


def test_validate_rejects_other_regions():
    assert cfg_mod.Config(region="us-ashburn-1").validate()
    assert not cfg_mod.Config().validate()


def test_validate_rejects_bad_times_in_fixed_mode():
    fixed = dict(schedule_mode=cfg_mod.SCHEDULE_MODE_FIXED)
    assert cfg_mod.Config(schedule_beijing=["25:00"], **fixed).validate()
    assert cfg_mod.Config(schedule_beijing=["abc"], **fixed).validate()
    assert cfg_mod.Config(schedule_beijing=["08:5"], **fixed).validate()
    assert not cfg_mod.Config(schedule_beijing=["08:05", "20:45"], **fixed).validate()


def test_config_file_is_private(tmp_path):
    path = tmp_path / "config.json"
    cfg_mod.Config().save(path)
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_state_finished_flag():
    s = cfg_mod.State()
    assert not s.finished
    s.status = cfg_mod.STATUS_SUCCESS
    assert s.finished
    s.status = cfg_mod.STATUS_NEEDS_ATTENTION
    assert s.finished
    s.status = cfg_mod.STATUS_IDLE
    assert not s.finished


def test_state_load_survives_broken_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("!!!", encoding="utf-8")
    assert cfg_mod.State.load(path).status == cfg_mod.STATUS_IDLE


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    s = cfg_mod.State(status=cfg_mod.STATUS_SUCCESS, public_ip="203.0.113.1", attempts=3)
    s.save(path)
    loaded = cfg_mod.State.load(path)
    assert (loaded.status, loaded.public_ip, loaded.attempts) == (cfg_mod.STATUS_SUCCESS, "203.0.113.1", 3)


def test_save_is_atomic(tmp_path):
    """原子写：不留 .tmp 残留，且覆盖写入后文件始终是完整 JSON。"""
    path = tmp_path / "config.json"
    cfg_mod.Config(display_name="a").save(path)
    cfg_mod.Config(display_name="b").save(path)
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(path.read_text(encoding="utf-8"))["display_name"] == "b"


def test_broken_config_is_reported_not_silently_replaced(tmp_path):
    """坏配置必须报错，不能静默用默认值顶替（否则用户以为配置生效了）。"""
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        cfg_mod.Config.load(path)


@pytest.mark.parametrize("content", ["null", "[]", '"hello"', "123", "true"])
def test_load_rejects_json_that_is_not_an_object(tmp_path, content):
    """JSON 合法但不是对象时必须报错。

    否则会在后面以 TypeError/AttributeError 的形式炸掉**所有**命令
    （含 status/doctor），用户连诊断入口都没有。
    """
    path = tmp_path / "config.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="顶层应为 JSON 对象"):
        cfg_mod.Config.load(path)


@pytest.mark.parametrize("content", ["null", "[]", '"hello"', "123"])
def test_state_load_tolerates_json_that_is_not_an_object(tmp_path, content):
    """状态文件损坏不致命，但绝不能抛异常（它是 cmd_run 读的第一个文件）。"""
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    assert cfg_mod.State.load(path).status == cfg_mod.STATUS_IDLE


def test_state_save_is_atomic(tmp_path):
    """状态文件被截断会丢掉 success/needs_attention 标记。"""
    path = tmp_path / "state.json"
    cfg_mod.State(status=cfg_mod.STATUS_IDLE).save(path)
    cfg_mod.State(status=cfg_mod.STATUS_SUCCESS, public_ip="1.2.3.4").save(path)
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == cfg_mod.STATUS_SUCCESS


def test_default_ssh_key_is_resolved_at_instantiation(monkeypatch, tmp_path):
    """默认公钥路径要能在实例化时被替换，测试才不会写到真实 ~/.ssh。"""
    monkeypatch.setattr(cfg_mod, "DEFAULT_SSH_KEY", tmp_path / "k")
    assert cfg_mod.Config().ssh_public_key == str(tmp_path / "k") + ".pub"


# ── 申请节奏（interval 持续抢 / fixed 固定时刻） ─────────────────────────────────
def test_interval_is_clamped_to_minimum():
    c = cfg_mod.Config(interval_seconds=1)
    c.normalize()
    assert c.interval_seconds == cfg_mod.MIN_INTERVAL_SECONDS


def test_interval_is_clamped_to_maximum():
    c = cfg_mod.Config(interval_seconds=10 ** 9)
    c.normalize()
    assert c.interval_seconds == cfg_mod.MAX_INTERVAL_SECONDS


@pytest.mark.parametrize("bad", ["abc", None, 3.7])
def test_interval_falls_back_on_garbage(bad):
    c = cfg_mod.Config(interval_seconds=bad)
    c.normalize()
    assert cfg_mod.MIN_INTERVAL_SECONDS <= c.interval_seconds <= cfg_mod.MAX_INTERVAL_SECONDS


def test_unknown_mode_falls_back_to_default():
    c = cfg_mod.Config(schedule_mode="every-minute")
    c.normalize()
    assert c.schedule_mode == cfg_mod.DEFAULT_SCHEDULE_MODE


def test_mode_is_case_insensitive():
    c = cfg_mod.Config(schedule_mode=" FIXED ")
    c.normalize()
    assert c.schedule_mode == cfg_mod.SCHEDULE_MODE_FIXED


def test_interval_mode_ignores_bad_fixed_times():
    """持续抢模式下不使用固定时刻，不该因为时刻写法而停止。"""
    c = cfg_mod.Config(schedule_mode=cfg_mod.SCHEDULE_MODE_INTERVAL,
                       schedule_beijing=["25:00", "abc"])
    c.normalize()
    assert c.validate() == []


def test_validate_rejects_bad_interval():
    c = cfg_mod.Config(schedule_mode=cfg_mod.SCHEDULE_MODE_INTERVAL, interval_seconds=5)
    c.normalize()          # normalize 会先夹到下限，所以这里手工造一个非法值
    c.interval_seconds = 5
    assert any("申请间隔" in p for p in c.validate())


def test_schedule_mode_survives_save_load(tmp_path):
    path = tmp_path / "config.json"
    cfg_mod.Config(schedule_mode=cfg_mod.SCHEDULE_MODE_FIXED, interval_seconds=900).save(path)
    loaded = cfg_mod.Config.load(path)
    assert (loaded.schedule_mode, loaded.interval_seconds) == ("fixed", 900)
