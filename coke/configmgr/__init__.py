"""配置版本管理与热更新。

热更新规则（定死，不随调用方变化）：

1. 整套切换（atomic）：一次改版提交**一份完整配置**，先整体校验、算出内容
   哈希，再整体落盘生效；不支持逐参数推送，任何字段非法都拒绝整份，运行中
   的版本不会停在"半新半旧"的中间态。
2. 生效时机按参数类别定：

   - ``IMMUTABLE``（平台名、存储目录）：只在装配时读一次，热更新不接受改动，
     要改必须重启；
   - ``LIVE``：控制循环持续读取的调节量（集气管目标带/调节速率、除尘挡板
     开度/速率/联动滞后、换向间隔）。空闲时下版立即生效；有操作在跑时，除尘
     与换向间隔仍立即生效（下一个控制周期用新值），集气管三项延迟到该操作
     结束（装煤序列在操作内按旧目标带判 settled，不能中途换带）；
   - ``BOUND``：操作配方（推焦、导焦、装煤、熄焦、炉门清扫、炉温目标/确认
     带）。操作开始时把那一版参数冻结在该操作上，整次操作按开始版跑完；操作
     结束后组件切换到当前生效版。

3. 版本与回退：每次改版是一次**迁移**，生成新版本（单调递增序号 + 父版本 +
   内容哈希），历史只追加、永不重写。回退 = 以当前版的父版本内容做一次新版
   迁移，回退链可一直追到种子版；回退幂等——当前正处于"上一版回退产物"时，
   重复回退不产生新版本、不写审计。与当前生效版内容哈希相同的重复改版同样是
   无操作，不产生版本与审计。
4. 审计与核对：改版（含回退）与拒绝都落审计；控制台展示"当前生效版本"与
   "正在用的版本（各在跑操作冻结的版本集合）"，两者一致时 ``consistent`` 为
   真，存在在跑旧版时为 ``draining``。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from collections.abc import Callable
from typing import Any, Mapping

from ..clock import Clock
from ..config import PlatformConfig, config_to_mapping
from ..errors import ConfigError
from ..eventbus import Event, EventBus

#: 启动期固定、热更新不允许改动的字段（点路径）。
IMMUTABLE_PATHS = frozenset({"platform.name", "platform.storage_root"})

#: 操作配方：操作开始时冻结到该操作，整次操作按开始版跑完。
BOUND_SECTIONS = frozenset(("pusher", "guide", "charger", "quench", "door"))

#: 炉温确认带同样绑定在"换向"这一操作上；换向间隔则属于 LIVE。
BOUND_HEATING_PATHS = frozenset(
    {"limits.heating.temp_target_c", "limits.heating.temp_band_c", "limits.heating.waste_close_lead_s"}
)

#: 装煤操作期间必须冻结、空闲时立即生效的集气管调节量。
DEFERRED_COLLECTOR_PATHS = frozenset(
    {
        "limits.collector.pressure_target_pa",
        "limits.collector.pressure_band_pa",
        "limits.collector.regulate_rate_pa_per_s",
    }
)

DOC_ID = "config_versions"
SEED_VERSION = 1
COMPONENT = "config"


def classify(path: str) -> str:
    """按定死的分类表判定一个叶子字段的生效类别（审计核对口径）。

    返回 ``"immutable"``（启动期固定）/ ``"live"``（控制循环即时项）/
    ``"deferred_live"``（装煤操作在跑时延迟到操作结束）/ ``"bound"``（操作
    开始时冻结到该操作）。
    """

    if path in IMMUTABLE_PATHS:
        return "immutable"
    if path in DEFERRED_COLLECTOR_PATHS:
        return "deferred_live"
    if path in BOUND_HEATING_PATHS:
        return "bound"
    section = path.split(".")[1] if path.startswith("limits.") else ""
    if section in BOUND_SECTIONS:
        return "bound"
    # limits.heating.reversal_interval_s 与 limits.dust.* 为即时 LIVE。
    return "live"


def content_hash(config: PlatformConfig | Mapping[str, Any]) -> str:
    """计算一份配置的内容哈希（与解析后的规范映射绑定，忽略键序与空白）。"""

    mapping = config_to_mapping(config) if isinstance(config, PlatformConfig) else dict(config)
    raw = json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _flatten(mapping: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in mapping.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def diff_configs(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """对比两份配置映射，返回变更点路径 -> {"before", "after"}（含新增/删除）。"""

    old_flat = _flatten(old)
    new_flat = _flatten(new)
    changes: dict[str, dict[str, Any]] = {}
    for path in sorted(set(old_flat) | set(new_flat)):
        before = old_flat.get(path)
        after = new_flat.get(path)
        if before != after:
            changes[path] = {"before": before, "after": after}
    return changes


@dataclasses.dataclass(frozen=True)
class ConfigVersion:
    """一版不可变配置快照。"""

    seq: int
    parent_seq: int
    digest: str
    kind: str  # "seed" | "migrate" | "rollback"
    created_ts: float
    reason: str
    config: PlatformConfig

    def as_summary(self) -> dict:
        """返回不含配置正文的版本元信息，供列表与控制台展示。"""

        return {
            "seq": self.seq,
            "parent_seq": self.parent_seq,
            "digest": self.digest,
            "kind": self.kind,
            "created_ts": round(self.created_ts, 3),
            "reason": self.reason,
        }

    def as_stored(self) -> dict:
        """落成可持久化的字典（配置正文随版本一起存档，回退不依赖外部文件）。"""

        return {**self.as_summary(), "config": config_to_mapping(self.config)}

    @classmethod
    def from_stored(cls, raw: Mapping[str, Any]) -> "ConfigVersion":
        try:
            return cls(
                seq=int(raw["seq"]),
                parent_seq=int(raw["parent_seq"]),
                digest=str(raw["digest"]),
                kind=str(raw["kind"]),
                created_ts=float(raw["created_ts"]),
                reason=str(raw.get("reason", "")),
                config=PlatformConfig.from_mapping(raw["config"]),
            )
        except (KeyError, TypeError, ValueError, ConfigError) as error:
            raise ConfigError(f"配置版本存档损坏: {error}", component=COMPONENT) from error


class ConfigManager:
    """配置版本库：校验、版本链、在跑冻结、审计与落盘。

    组件侧的参数下发由两个回调完成（由平台装配）：

    - ``apply_live(config)``：下发始终即时生效的调节量（除尘三项、换向间隔）；
    - ``apply_bound(config)``：把操作配方整体下发到各执行组件。空闲迁移立即
      调用；在跑操作结束并发现期间有改版时补调一次。
    """

    def __init__(
        self,
        config: PlatformConfig,
        *,
        clock: Clock,
        store: Any,
        bus: EventBus,
        apply_live: Callable[[PlatformConfig], None],
        apply_bound: Callable[[PlatformConfig], None],
        apply_collector: Callable[[PlatformConfig], None] | None = None,
    ) -> None:
        self._clock = clock
        self._store = store
        self._bus = bus
        self._apply_live = apply_live
        self._apply_bound = apply_bound
        # 集气管三项在装煤操作期间需要冻结，单独一路下发。
        self._apply_collector = apply_collector or apply_live
        self._lock = threading.RLock()
        self._versions: list[ConfigVersion] = []
        self._pins: dict[str, int] = {}
        self._load_or_seed(config)

    # ------------------------------------------------------------------ 装配

    def _load_or_seed(self, bootstrap: PlatformConfig) -> None:
        raw = self._store.read(DOC_ID)
        if isinstance(raw, dict) and isinstance(raw.get("versions"), list) and raw["versions"]:
            versions = [ConfigVersion.from_stored(item) for item in raw["versions"]]
            versions.sort(key=lambda item: item.seq)
            current_seq = int(raw.get("current_seq", versions[-1].seq))
            current = next((item for item in versions if item.seq == current_seq), versions[-1])
            self._versions = versions
            self._current_seq = current.seq
        else:
            seed = ConfigVersion(
                seq=SEED_VERSION,
                parent_seq=0,
                digest=content_hash(bootstrap),
                kind="seed",
                created_ts=self._clock.now(),
                reason="bootstrap",
                config=bootstrap,
            )
            self._versions = [seed]
            self._current_seq = seed.seq
            self._persist()
        # 装配期把生效版完整下发一次，各组件口径与版本库一致。
        current = self.current()
        self._apply_live(current.config)
        self._apply_collector(current.config)
        self._apply_bound(current.config)

    def _persist(self) -> None:
        self._store.write(
            DOC_ID,
            {"current_seq": self._current_seq, "versions": [item.as_stored() for item in self._versions]},
        )

    # ------------------------------------------------------------------ 查询

    def current(self) -> ConfigVersion:
        """返回当前生效版本。"""

        with self._lock:
            return next(item for item in self._versions if item.seq == self._current_seq)

    @property
    def current_seq(self) -> int:
        with self._lock:
            return self._current_seq

    def current_config(self) -> PlatformConfig:
        """返回当前生效配置。"""

        return self.current().config

    def get(self, seq: int) -> ConfigVersion:
        """按序号取版本，不存在即抛错。"""

        with self._lock:
            for version in self._versions:
                if version.seq == seq:
                    return version
        raise ConfigError(f"配置版本 {seq} 不存在", component=COMPONENT)

    def history(self, limit: int = 32) -> list[dict]:
        """返回版本历史（最新在前）。"""

        with self._lock:
            versions = list(self._versions)[-max(1, limit):]
        return [version.as_summary() for version in reversed(versions)]

    def config_status(self) -> dict:
        """控制台展示与审计核对用的版本状态。"""

        with self._lock:
            current = self.current()
            pins = dict(self._pins)
            in_use = sorted(set(pins.values()))
        active_seqs = sorted(set(in_use) | {current.seq})
        return {
            "effective": current.as_summary(),
            "in_use_seqs": active_seqs,
            "pinned": pins,
            "operations_in_flight": len(pins),
            # 所有在跑操作都冻结在当前版上才算一致；有旧版在跑即为 draining。
            "state": "aligned" if not pins or all(seq == current.seq for seq in pins.values()) else "draining",
            "consistent": all(seq == current.seq for seq in pins.values()),
        }

    # ------------------------------------------------------------------ 冻结

    def begin_operation(self, operation: str) -> int:
        """操作开始：把当前生效版冻结到该操作上，返回冻结的版本序号。

        操作期间读到的操作配方始终是这一版；操作结束前的迁移不会改动它。
        """

        with self._lock:
            if operation in self._pins:
                raise ConfigError(f"操作 {operation!r} 已在跑，不能重复冻结配置", component=COMPONENT)
            seq = self._current_seq
            self._pins[operation] = seq
            return seq

    def end_operation(self, operation: str) -> dict:
        """操作结束：解除冻结；若期间有改版，把新版操作配方补发给组件。"""

        with self._lock:
            pinned_seq = self._pins.pop(operation, None)
            current = self.current()
            caught_up = pinned_seq != current.seq and pinned_seq is not None
            deferred_collector = caught_up
            if caught_up:
                # 操作已结束：集气管新目标带与操作配方统一切到生效版。
                self._apply_collector(current.config)
                self._apply_bound(current.config)
            return {
                "operation": operation,
                "pinned_seq": pinned_seq,
                "effective_seq": current.seq,
                "caught_up": caught_up,
                "deferred_collector": deferred_collector,
            }

    # ------------------------------------------------------------------ 改版

    def _validate_proposed(
        self, mapping: Mapping[str, Any]
    ) -> tuple[PlatformConfig, str, dict[str, dict[str, Any]]]:
        if not isinstance(mapping, Mapping):
            raise ConfigError("改版配置必须是完整映射", component=COMPONENT)
        config = PlatformConfig.from_mapping(mapping)
        digest = content_hash(config)
        changes = diff_configs(config_to_mapping(self.current().config), config_to_mapping(config))
        immutable_changed = sorted(path for path in changes if path in IMMUTABLE_PATHS)
        if immutable_changed:
            raise ConfigError(
                f"启动期参数不允许热更新（需重启后生效）: {', '.join(immutable_changed)}",
                component=COMPONENT,
            )
        return config, digest, changes

    def migrate(self, mapping: Mapping[str, Any], *, reason: str = "manual") -> dict:
        """整套切换到一份新配置（迁移）。

        内容与当前版完全相同则幂等无操作：不产生新版本、不写审计。校验失败
        或改动不可热更字段时拒绝整份，当前版保持不动。
        """

        with self._lock:
            config, digest, changes = self._validate_proposed(mapping)
            current = self.current()
            if digest == current.digest:
                return {
                    "changed": False,
                    "idempotent": True,
                    "seq": current.seq,
                    "parent_seq": current.parent_seq,
                    "digest": digest,
                    "kind": current.kind,
                    "in_use": self.config_status(),
                }
            return self._commit(config, digest, kind="migrate", reason=reason, changes=changes)

    def rollback(self, *, reason: str = "rollback") -> dict:
        """回退到上一版（父版本内容）。

        语义固定为"撤销最近一次正向迁移"：以当前版父版本的内容生成新版本。
        幂等：若当前版本身就是一次回退产物（或没有可回退的父版本内容），重复
        调用不产生新版本、不写审计，返回当前版并带 ``idempotent=True``。
        """

        with self._lock:
            current = self.current()
            if current.kind == "rollback" or current.parent_seq <= 0:
                return {
                    "changed": False,
                    "idempotent": True,
                    "seq": current.seq,
                    "parent_seq": current.parent_seq,
                    "digest": current.digest,
                    "kind": current.kind,
                    "in_use": self.config_status(),
                }
            parent = self.get(current.parent_seq)
            changes = diff_configs(config_to_mapping(current.config), config_to_mapping(parent.config))
            return self._commit(
                parent.config,
                parent.digest,
                kind="rollback",
                reason=reason,
                changes=changes,
                restored_from_seq=parent.seq,
            )

    def _commit(
        self,
        config: PlatformConfig,
        digest: str,
        *,
        kind: str,
        reason: str,
        changes: dict[str, dict[str, Any]],
        restored_from_seq: int | None = None,
    ) -> dict:
        current = self.current()
        version = ConfigVersion(
            seq=current.seq + 1,
            parent_seq=current.seq,
            digest=digest,
            kind=kind,
            created_ts=self._clock.now(),
            reason=reason,
            config=config,
        )
        self._versions.append(version)
        self._current_seq = version.seq
        self._persist()

        # 即时生效：除尘三项 + 换向间隔，无论是否有操作在跑（下周期才读取）。
        self._apply_live(config)
        if not self._pins:
            # 空闲：操作配方与集气管调节量统一切到新版。
            self._apply_collector(config)
            self._apply_bound(config)
        # 有操作在跑时：BOUND 配方与集气管冻结参数不动，等 end_operation 补发；
        # 装煤操作之外的在跑操作不读集气管，其冻结只影响延迟补发时机。

        payload: dict[str, object] = {
            "severity": "notice",
            "seq": version.seq,
            "parent_seq": current.seq,
            "digest": digest,
            "kind": kind,
            "reason": reason,
            "changed_paths": sorted(changes),
            "change_classes": {path: classify(path) for path in sorted(changes)},
            "changes": changes,
            "pinned": dict(self._pins),
        }
        if restored_from_seq is not None:
            payload["restored_from_seq"] = restored_from_seq
        self._bus.publish(
            Event.of(
                COMPONENT,
                COMPONENT,
                "config_rollback" if kind == "rollback" else "config_migrated",
                **payload,
            )
        )
        return {
            "changed": True,
            "idempotent": False,
            "seq": version.seq,
            "parent_seq": current.seq,
            "digest": digest,
            "kind": kind,
            "changes": changes,
            "changed_paths": sorted(changes),
            "in_use": self.config_status(),
        }
