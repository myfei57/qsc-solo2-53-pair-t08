"""配置热更新：整套切换、按类别生效、回退幂等、核对与审计。"""

from __future__ import annotations

import pytest

from coke.config import default_config, config_to_mapping
from coke.configmeta import LIVE, PINNED, FIELD_CLASSES
from coke.configversion import content_hash
from coke.errors import ConfigError, InterlockError
from coke.app import build_platform
from coke.console.api import Api


def _mapping(**overrides):
    payload = config_to_mapping(default_config())
    for path, value in overrides.items():
        section, field = path.split(".")
        payload["limits"][section][field] = value
    return payload


def test_field_class_covers_every_limits_field():
    config = default_config()
    import dataclasses

    for section in (
        "pusher", "guide", "charger", "quench", "collector", "door", "heating", "dust"
    ):
        for field in dataclasses.fields(getattr(config, section)):
            assert FIELD_CLASSES[f"{section}.{field.name}"] in (LIVE, PINNED)


def test_apply_switches_whole_set_and_pins_version(platform):
    before = platform.configs.active.version_id
    result = platform.apply_config(_mapping(**{"collector.pressure_target_pa": 120.0}), reason="t")
    assert result["changed"] is True
    assert result["active"]["version_id"] != before
    assert result["previous"]["version_id"] == before
    # 整套切换后执行器立即持有新版
    assert platform.in_use_versions()["collector"] == result["active"]["version_id"]


def test_live_param_takes_effect_next_cycle(platform):
    platform.start()
    platform.apply_config(_mapping(**{"collector.pressure_target_pa": 120.0}))
    assert platform.collector.limits.pressure_target_pa == 120.0
    for _ in range(60):
        platform.cycle(dt=1.0)
    assert platform.collector.settled()


def test_pinned_param_used_by_next_operation_only(platform):
    platform.start()
    v_new = platform.apply_config(_mapping(**{"pusher.speed_max_mm_s": 42.0}))["active"]["version_id"]
    push = platform.push(oven="oven01", speed_mm_s=40.0)
    assert push["ok"]
    # 本次推焦按开始时租用的新版跑完
    assert push["config_version"] == v_new
    assert push["speed_arbitration"]["speed_mm_s"] == 40.0


def test_invalid_config_is_rejected_as_a_whole(platform):
    active_before = platform.configs.active.version_id
    bad = _mapping(**{"collector.pressure_target_pa": 0.0})
    with pytest.raises(ConfigError):
        platform.apply_config(bad)
    assert platform.configs.active.version_id == active_before
    assert platform.collector.limits.pressure_target_pa != 0.0


def test_apply_same_content_is_idempotent(platform):
    first = platform.apply_config(_mapping(**{"collector.pressure_target_pa": 120.0}))
    second = platform.apply_config(_mapping(**{"collector.pressure_target_pa": 120.0}))
    assert first["active"]["version_id"] == second["active"]["version_id"]
    assert second["changed"] is False


def test_rollback_is_idempotent_and_audited(platform):
    v1 = platform.configs.active.version_id
    v2 = platform.apply_config(_mapping(**{"collector.pressure_target_pa": 120.0}))["active"]["version_id"]
    rb1 = platform.rollback_config()
    assert rb1["switched"] is True and rb1["active"]["version_id"] == v1
    # 链首再退：状态不变（幂等）
    rb2 = platform.rollback_config()
    assert rb2["switched"] is False and rb2["active"]["version_id"] == v1
    assert v2 != v1
    actions = {entry["action"] for entry in platform.audit.entries(limit=50)}
    assert "config_rollback" in actions


def test_rollback_restores_previous_limits(platform):
    platform.start()
    platform.apply_config(_mapping(**{"collector.pressure_target_pa": 120.0}))
    assert platform.collector.limits.pressure_target_pa == 120.0
    platform.rollback_config()
    assert platform.collector.limits.pressure_target_pa == default_config().collector.pressure_target_pa


def test_reversal_interval_waits_until_next_reversal(platform, clock):
    platform.start()
    old_interval = default_config().heating.reversal_interval_s
    platform.apply_config(_mapping(**{"heating.reversal_interval_s": 2400.0}))
    # 正在跑的这轮换向周期仍按旧版间隔
    assert platform.reversal.limits.reversal_interval_s == old_interval
    status = platform.config_status()
    assert status["reconcile"]["reversal_pending"] is True
    clock.advance(old_interval)
    result = platform.reverse(oven="oven01")
    assert result["ok"]
    assert platform.reversal.limits.reversal_interval_s == 2400.0
    assert platform.config_status()["reconcile"]["reversal_pending"] is False


def test_reconcile_reports_active_and_in_use(platform):
    platform.start()
    report = platform.reconcile_config()
    assert report["consistent"] is True
    assert report["all_on_active"] is True
    assert report["active_version"] in report["in_use_versions"]


def test_config_changes_are_audited_as_migration(platform):
    platform.apply_config(_mapping(**{"collector.pressure_target_pa": 120.0}), reason="调压")
    platform.rollback_config(reason="退回")
    actions = {}
    for entry in platform.audit.entries(limit=100):
        actions.setdefault(entry["action"], entry)
    for needed in ("config_proposed", "config_activated", "config_rollback", "config_reconciled"):
        assert needed in actions
    activated = actions["config_activated"]
    assert activated["payload"]["from_hash"] != activated["payload"]["to_hash"]
    assert activated["payload"]["reason"] == "调压"


def test_operation_records_config_version(platform):
    platform.start()
    platform.charge(oven="oven01")
    entries = [e for e in platform.audit.entries(limit=20) if e["action"] == "charge_complete"]
    assert entries
    assert entries[0]["payload"]["config_version"] == platform.configs.active.version_id


def test_versions_persist_across_restart(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=storage)
    v1 = first.configs.active.version_id
    v2 = first.apply_config(_mapping(**{"collector.pressure_target_pa": 120.0}))["active"]["version_id"]
    second = build_platform(default_config(), clock=clock, storage_root=storage)
    assert second.configs.active.version_id == v2
    assert second.collector.limits.pressure_target_pa == 120.0
    assert second.configs.previous().version_id == v1
    assert {v["version_id"] for v in second.configs.history()} == {v1, v2}


def test_console_exposes_config_view_and_actions(platform):
    api = Api(platform)
    status, body = api.handle("GET", "/api/config", {}, {})
    assert status == 200
    assert body["switch_mode"] == "atomic_whole_set"
    assert body["field_classes"]["collector"]["pressure_target_pa"] == LIVE
    assert body["field_classes"]["pusher"]["speed_max_mm_s"] == PINNED

    apply_body = {"config": _mapping(**{"collector.pressure_target_pa": 120.0}), "reason": "api"}
    status, body = api.handle("POST", "/api/config/apply", {}, apply_body)
    assert status == 200 and body["changed"] is True

    status, body = api.handle("POST", "/api/config/rollback", {}, {})
    assert status == 200 and body["switched"] is True

    bad = {"config": _mapping(**{"pusher.speed_min_mm_s": 999.0})}
    status, body = api.handle("POST", "/api/config/apply", {}, bad)
    assert status == 409


def test_status_carries_config_version(platform):
    status = platform.status()
    assert status["config_version"] == platform.configs.active.version_id
    assert status["config_reconcile"]["consistent"] is True


def test_pusher_rebind_rejected_mid_stroke(platform):
    # 直接构造在途状态：推焦机伸出中禁止换版（正常热更新路径不会撞上锁内操作）
    platform.pusher.drive_to(1000.0)
    with pytest.raises(InterlockError):
        platform.pusher.rebind(platform.config.pusher)


def test_content_hash_ignores_platform_section():
    import dataclasses as dc

    cfg = default_config()
    moved = dc.replace(cfg, storage_root="somewhere/else")
    assert content_hash(cfg) == content_hash(moved)
