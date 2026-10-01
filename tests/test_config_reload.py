"""配置热更新：整套切换、按类别生效、版本回退与审计核对。"""

from __future__ import annotations

import json

import pytest

from coke.app import build_platform
from coke.config import default_config, config_to_mapping
from coke.configmgr import classify, content_hash
from coke.console.api import Api
from coke.errors import ConfigError
from coke.store.jsonfile import JsonStore


def _current_mapping(platform):
    return config_to_mapping(platform.configs.current().config)


def _tweak(platform, changes):
    """以当前生效版为底，改若干 limits.<section>.<field>。"""

    mapping = _current_mapping(platform)
    for (section, field), value in changes.items():
        mapping["limits"][section][field] = value
    return mapping


def test_seed_config_persisted_as_version_one(platform, tmp_path):
    status = platform.configs.config_status()
    assert status["effective"]["seq"] == 1
    assert status["effective"]["kind"] == "seed"
    assert status["state"] == "aligned"
    assert status["consistent"] is True
    doc = json.loads((tmp_path / "state" / "config_versions.json").read_text(encoding="utf-8"))
    assert doc["current_seq"] == 1
    assert doc["versions"][0]["digest"] == content_hash(platform.configs.current().config)


def test_migrate_is_whole_set_and_applies_by_category(platform):
    platform.start()
    result = platform.migrate_config(
        _tweak(
            platform,
            {
                ("dust", "damper_open_deg"): 80.0,
                ("dust", "damper_rate_deg_per_s"): 20.0,
                ("quench", "spray_duration_s"): 120.0,
                ("pusher", "speed_max_mm_s"): 45.0,
            },
        )
    )
    assert result["changed"] is True
    assert result["seq"] == 2
    assert result["parent_seq"] == 1
    # LIVE：立即下发到控制循环组件。
    assert platform.damper.open_deg == 80.0
    # BOUND：空闲时立即下发，下一炉就是新版配方。
    assert platform.quench_car.spray.duration_s == 120.0
    assert platform.pusher.limits.speed_max_mm_s == 45.0
    overview = platform.config_overview()
    assert overview["effective"]["seq"] == 2
    assert [v["seq"] for v in overview["versions"][:2]] == [2, 1]


def test_idempotent_migrate_creates_no_version(platform):
    platform.start()
    mapping = _tweak(platform, {("dust", "damper_open_deg"): 80.0})
    first = platform.migrate_config(mapping)
    again = platform.migrate_config(_current_mapping(platform))
    assert first["seq"] == 2
    assert again["idempotent"] is True
    assert again["changed"] is False
    assert again["seq"] == 2
    assert len(platform.configs.history(limit=50)) == 2


def test_invalid_config_is_rejected_atomically(platform):
    platform.start()
    mapping = _tweak(platform, {("pusher", "speed_min_mm_s"): 999.0})
    with pytest.raises(ConfigError):
        platform.migrate_config(mapping)
    # 整套拒绝：当前版不动，组件参数也不动。
    assert platform.configs.current_seq == 1
    assert platform.pusher.limits.speed_min_mm_s == default_config().pusher.speed_min_mm_s


def test_immutable_fields_require_restart(platform):
    platform.start()
    mapping = _current_mapping(platform)
    mapping["platform"]["name"] = "renamed-without-restart"
    with pytest.raises(ConfigError):
        platform.migrate_config(mapping)
    assert platform.config.name == default_config().name


def test_in_flight_operation_runs_start_version(platform):
    platform.start()
    # 先在空闲时迁一版（喷 120s），再模拟一次在跑操作期间的改版（喷 200s）。
    platform.migrate_config(_tweak(platform, {("quench", "spray_duration_s"): 120.0}))
    platform.configs.begin_operation("quench")
    result = platform.migrate_config(_tweak(platform, {("quench", "spray_duration_s"): 200.0}))
    status = result["in_use"]
    assert status["state"] == "draining"
    assert status["consistent"] is False
    assert status["pinned"] == {"quench": 2}
    # BOUND 配方在操作期间保持旧版，按开始那一版跑完。
    assert platform.quench_car.spray.duration_s == 120.0
    ended = platform.configs.end_operation("quench")
    assert ended["caught_up"] is True
    assert ended["pinned_seq"] == 2
    assert ended["effective_seq"] == 3
    # 操作安全结束后才切到新版，重新核对一致。
    assert platform.quench_car.spray.duration_s == 200.0
    assert platform.configs.config_status()["state"] == "aligned"


def test_live_dust_params_take_effect_during_operation(platform):
    platform.start()
    platform.configs.begin_operation("push")
    platform.migrate_config(
        _tweak(
            platform,
            {
                ("dust", "damper_open_deg"): 66.0,
                ("heating", "reversal_interval_s"): 2400.0,
            },
        )
    )
    # LIVE：不等操作结束，下一个控制周期就读新值；在跑联动起点不被改动。
    assert platform.damper.open_deg == 66.0
    assert platform.reversal.limits.reversal_interval_s == 2400.0
    platform.configs.end_operation("push")


def test_collector_target_is_deferred_until_charge_finishes(platform):
    platform.start()
    platform.configs.begin_operation("charge")
    platform.migrate_config(_tweak(platform, {("collector", "pressure_target_pa"): 140.0}))
    # 装煤序列按操作开始时的目标带判 settled，不能中途换带。
    assert platform.collector.limits.pressure_target_pa == 90.0
    platform.configs.end_operation("charge")
    assert platform.collector.limits.pressure_target_pa == 140.0


def test_new_operation_after_idle_migrate_uses_new_recipe(platform):
    platform.start()
    platform.migrate_config(_tweak(platform, {("quench", "travel_speed_m_s"): 2.0}))
    report = platform.quench(oven="oven01")
    assert report["ok"] is True
    # 42m / 2.0 m/s = 21s，新版走行速度在下一炉整段操作里生效。
    assert report["travel_eta_s"] == 21.0
    assert report["config_seq"] == 2


def test_running_operation_records_pinned_version(platform):
    platform.start()
    platform.migrate_config(_tweak(platform, {("door", "clean_duration_s"): 30.0}))
    report = platform.clean(oven="oven01")
    assert report["ok"] is True
    assert report["config_seq"] == 2
    entries = platform.audit.entries(limit=50)
    complete = next(item for item in entries if item["action"] == "clean_complete")
    assert complete["payload"]["config_seq"] == 2


def test_rollback_restores_previous_version(platform):
    platform.start()
    platform.migrate_config(
        _tweak(platform, {("dust", "damper_open_deg"): 80.0, ("quench", "spray_duration_s"): 120.0})
    )
    result = platform.rollback_config()
    assert result["changed"] is True
    assert result["kind"] == "rollback"
    assert result["seq"] == 3
    assert result["parent_seq"] == 2
    assert platform.damper.open_deg == default_config().dust.damper_open_deg
    assert platform.quench_car.spray.duration_s == default_config().quench.spray_duration_s


def test_rollback_is_idempotent(platform):
    platform.start()
    platform.migrate_config(_tweak(platform, {("dust", "damper_open_deg"): 80.0}))
    first = platform.rollback_config()
    second = platform.rollback_config()
    assert first["seq"] == 3
    assert second["idempotent"] is True
    assert second["changed"] is False
    assert second["seq"] == 3
    kinds = [v["kind"] for v in platform.configs.history(limit=50)]
    assert kinds == ["rollback", "migrate", "seed"]


def test_new_migrate_after_rollback_can_be_rolled_back_again(platform):
    platform.start()
    platform.migrate_config(_tweak(platform, {("dust", "damper_open_deg"): 80.0}))
    platform.rollback_config()
    platform.migrate_config(_tweak(platform, {("dust", "damper_open_deg"): 50.0}))
    again = platform.rollback_config()
    assert again["idempotent"] is False
    assert again["kind"] == "rollback"
    assert platform.damper.open_deg == default_config().dust.damper_open_deg


def test_versions_persist_across_restart(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=storage)
    first.start()
    first.migrate_config(
        _tweak(first, {("dust", "damper_open_deg"): 80.0, ("quench", "spray_duration_s"): 120.0})
    )
    first.rollback_config()
    second = build_platform(default_config(), clock=clock, storage_root=storage)
    assert second.configs.current_seq == 3
    assert [v["seq"] for v in second.configs.history(limit=10)] == [3, 2, 1]
    assert second.damper.open_deg == default_config().dust.damper_open_deg
    assert second.quench_car.spray.duration_s == default_config().quench.spray_duration_s


def test_effective_digest_matches_stored_content(platform):
    platform.start()
    platform.migrate_config(_tweak(platform, {("dust", "damper_open_deg"): 80.0}))
    effective = platform.config_overview()["effective"]
    assert effective["digest"] == content_hash(platform.configs.current().config)
    raw = JsonStore(platform.store.root).read("config_versions")
    stored = next(item for item in raw["versions"] if item["seq"] == effective["seq"])
    assert stored["digest"] == effective["digest"]


def test_migrations_and_rollbacks_are_audited(platform):
    platform.start()
    platform.migrate_config(_tweak(platform, {("dust", "damper_open_deg"): 80.0}), reason="audit-check")
    platform.rollback_config()
    actions = [entry["action"] for entry in platform.audit.entries(limit=50)]
    assert "config_migrated" in actions
    assert "config_rollback" in actions
    migrated = next(entry for entry in platform.audit.entries(limit=50) if entry["action"] == "config_migrated")
    assert migrated["payload"]["reason"] == "audit-check"
    assert migrated["payload"]["changed_paths"] == ["limits.dust.damper_open_deg"]
    assert migrated["payload"]["parent_seq"] == 1
    rolled = next(entry for entry in platform.audit.entries(limit=50) if entry["action"] == "config_rollback")
    assert rolled["payload"]["restored_from_seq"] == 1


def test_idempotent_calls_write_no_audit(platform):
    platform.start()
    before = platform.audit.summary()["journal_count"]
    platform.migrate_config(_current_mapping(platform))
    platform.rollback_config()  # 种子版没有父版本可回退，幂等无操作
    assert platform.audit.summary()["journal_count"] == before


def test_classification_table_is_fixed():
    assert classify("platform.name") == "immutable"
    assert classify("platform.storage_root") == "immutable"
    assert classify("limits.dust.damper_open_deg") == "live"
    assert classify("limits.heating.reversal_interval_s") == "live"
    assert classify("limits.collector.pressure_target_pa") == "deferred_live"
    assert classify("limits.pusher.speed_max_mm_s") == "bound"
    assert classify("limits.quench.spray_duration_s") == "bound"
    assert classify("limits.heating.temp_target_c") == "bound"


def test_console_exposes_versions_and_accepts_migration(platform):
    platform.start()
    api = Api(platform)
    status, payload = api.handle("GET", "/api/config", {}, {})
    assert status == 200
    assert payload["effective"]["seq"] == 1
    assert "config" in payload and "versions" in payload

    mapping = _current_mapping(platform)
    mapping["limits"]["dust"]["damper_open_deg"] = 80.0
    status, migrated = api.handle("POST", "/api/config/migrate", {}, mapping)
    assert status == 200
    assert migrated["seq"] == 2

    status, rolled = api.handle("POST", "/api/config/rollback", {}, {})
    assert status == 200
    assert rolled["kind"] == "rollback"

    status, bad = api.handle("POST", "/api/config/migrate", {}, {"nope": True})
    assert status == 409
    assert bad["component"] == "config"

    status, empty = api.handle("POST", "/api/config/migrate", {}, {})
    assert status == 409


def test_status_carries_effective_and_in_use_versions(platform):
    platform.start()
    status = platform.status()["config"]
    assert status["effective"]["seq"] == 1
    assert status["in_use_seqs"] == [1]
    assert status["operations_in_flight"] == 0
