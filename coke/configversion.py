"""配置版本与版本注册中心。

一次热更新 = 一次**整套配置迁移**：新配置先经 :meth:`PlatformConfig.from_mapping`
全量校验，通过后包成一个不可变的 :class:`ConfigVersion`（全量快照 + 内容哈希
+ 单调序号 + 父版本），再原子地切成当前生效版。逐参数半生效不允许出现。

注册中心保留全量版本链，支持：

* :meth:`ConfigRegistry.activate` 提议并激活一个新版本；
* :meth:`ConfigRegistry.rollback` 退回当前版的父版本——**回退幂等**：对同一条
  父子边重复回退是 no-op，状态结果一致；
* :meth:`ConfigRegistry.reconcile` 用执行器实际在用的版本核对当前生效版是否
  一致（所有在跑执行器都持有效版本）。

版本链与"当前生效指针"通过原子 JSON 文档落盘，重启后仍能核对生效版与上一版。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from .config import PlatformConfig, config_to_mapping
from .errors import ConfigError
from .store.jsonfile import JsonStore

_DOC_ID = "config_versions"
# 内存与落盘保留的版本上限；热更新不频繁，足够巡检回放又不无限增长。
_HISTORY_LIMIT = 32


def content_hash(config: PlatformConfig) -> str:
    """计算一份配置**可调参数**（``limits`` 段）的哈希。

    ``platform.name`` / ``platform.storage_root`` 属启动装配项，不参与热更新，
    因此不计入版本身份；否则存储目录不同但炉参相同也会被误判为改版。归一化
    键序，跨进程稳定。
    """

    payload = config_to_mapping(config)["limits"]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ConfigVersion:
    """一个不可变的整套配置版本。"""

    seq: int
    version_id: str
    config: PlatformConfig
    content_hash: str
    parent_id: str | None
    created_ts: float
    source: str
    reason: str

    def as_summary(self) -> dict:
        """返回不含全量配置的版本摘要。"""

        return {
            "seq": self.seq,
            "version_id": self.version_id,
            "content_hash": self.content_hash,
            "parent_id": self.parent_id,
            "created_ts": round(self.created_ts, 3),
            "source": self.source,
            "reason": self.reason,
        }


class ConfigRegistry:
    """配置版本链、当前生效指针与回退。"""

    def __init__(self, store: JsonStore, clock: object, *, initial: PlatformConfig, source: str = "startup") -> None:
        self._store = store
        self._clock = clock
        self._lock = threading.RLock()
        self._versions: dict[str, ConfigVersion] = {}
        self._order: list[str] = []
        self._active_id: str | None = None
        # 全局单调的版本序号：回退后再改版也要递增，保证版本标识永不复用。
        self._next_seq = 1
        self._restore(initial, source)

    # ---- 读取 -----------------------------------------------------------

    @property
    def active(self) -> ConfigVersion:
        """当前生效版本。"""

        with self._lock:
            return self._versions[self._active_id]  # type: ignore[index]

    def config(self) -> PlatformConfig:
        """当前生效的整套配置。"""

        return self.active.config

    def get(self, version_id: str) -> ConfigVersion | None:
        """按标识取版本。"""

        with self._lock:
            return self._versions.get(version_id)

    def previous(self) -> ConfigVersion | None:
        """当前生效版的父版本（没有则为 ``None``）。"""

        with self._lock:
            parent_id = self.active.parent_id
            return self._versions.get(parent_id) if parent_id else None

    def history(self) -> list[dict]:
        """按版本序号升序返回全部版本摘要。"""

        with self._lock:
            return [self._versions[vid].as_summary() for vid in self._order]

    # ---- 写入 -----------------------------------------------------------

    def propose(self, mapping: Mapping[str, Any]) -> PlatformConfig:
        """全量校验一份候选配置；非法即抛 :class:`ConfigError`，不改变任何状态。"""

        return PlatformConfig.from_mapping(mapping)

    def activate(
        self,
        config: PlatformConfig,
        *,
        source: str,
        reason: str = "",
    ) -> ConfigVersion:
        """把一份已校验的配置激活为新版本，返回新版本。

        内容与当前生效版完全一致（哈希相同）时不产生新版本，直接返回当前版，
        保证"无变化的改版"幂等。
        """

        with self._lock:
            current = self.active
            new_hash = content_hash(config)
            if new_hash == current.content_hash:
                return current
            version = self._build(config, parent_id=current.version_id, source=source, reason=reason)
            self._commit(version)
            return version

    def rollback(self, *, source: str, reason: str = "") -> tuple[ConfigVersion, bool]:
        """退回当前生效版的父版本，返回 ``(目标版本, 是否真的发生了切换)``。

        幂等：如果当前版已经没有父版本（即链首，通常是启动基线），不做任何
        状态变更，返回 ``(当前版, False)``。回退不新建版本——它只是把生效指针
        沿迁移时记录的父指针移回去，因此对同一条父子边反复调用结果一致。
        """

        with self._lock:
            current = self.active
            parent = self.previous()
            if parent is None:
                return current, False
            self._active_id = parent.version_id
            self._persist()
            return parent, True

    # ---- 核对 -----------------------------------------------------------

    def reconcile(self, in_use: Mapping[str, str | None]) -> dict:
        """用执行器实际持有的版本核对当前生效版。

        ``in_use`` 为 ``执行器 -> 版本标识``（在跑的长寿命执行器持有旧版是
        合法的"迁移中"状态；值为 ``None`` 表示该执行器无版本租约）。
        """

        with self._lock:
            active_id = self.active.version_id
            known = set(self._versions)
            holders = {name: vid for name, vid in in_use.items() if vid is not None}
            unknown = sorted(name for name, vid in holders.items() if vid not in known)
            in_use_ids = sorted(set(holders.values()))
            # 所有持有版本都是链上的有效版本即一致；持有旧版表示有操作正在
            # 按旧版跑完，属于预期内的迁移中，不报不一致。
            consistent = not unknown
            return {
                "active_version": active_id,
                "active_seq": self.active.seq,
                "active_hash": self.active.content_hash,
                "in_use": dict(sorted(holders.items())),
                "in_use_versions": in_use_ids,
                "all_on_active": all(vid == active_id for vid in holders.values()),
                "unknown_holders": unknown,
                "consistent": consistent,
            }

    # ---- 内部 -----------------------------------------------------------

    def _now(self) -> float:
        value = self._clock.now()
        return float(value)

    def _build(
        self,
        config: PlatformConfig,
        *,
        parent_id: str | None,
        source: str,
        reason: str,
    ) -> ConfigVersion:
        seq = self._next_seq
        self._next_seq += 1
        digest = content_hash(config)
        version_id = f"cfg-{seq:04d}-{digest[:8]}"
        if version_id in self._versions:
            raise ConfigError(f"配置版本标识冲突: {version_id}", component="config")
        return ConfigVersion(
            seq=seq,
            version_id=version_id,
            config=config,
            content_hash=digest,
            parent_id=parent_id,
            created_ts=self._now(),
            source=source,
            reason=reason,
        )

    def _commit(self, version: ConfigVersion) -> None:
        self._versions[version.version_id] = version
        self._order.append(version.version_id)
        if len(self._order) > _HISTORY_LIMIT:
            drop = self._order[: len(self._order) - _HISTORY_LIMIT]
            self._order = self._order[len(drop) :]
            for vid in drop:
                self._versions.pop(vid, None)
        self._active_id = version.version_id
        self._persist()

    def _persist(self) -> None:
        payload = {
            "active_id": self._active_id,
            "next_seq": self._next_seq,
            "order": list(self._order),
            "versions": {
                vid: {**self._versions[vid].as_summary(), "config": config_to_mapping(self._versions[vid].config)}
                for vid in self._order
            },
        }
        self._store.write(_DOC_ID, payload)

    def _restore(self, initial: PlatformConfig, source: str) -> None:
        raw = self._store.read(_DOC_ID)
        if isinstance(raw, dict) and isinstance(raw.get("versions"), dict):
            max_seq = 0
            for vid in raw.get("order", []):
                item = raw["versions"].get(vid)
                if not isinstance(item, dict) or "config" not in item:
                    continue
                summary = {key: item[key] for key in item if key != "config"}
                version = ConfigVersion(
                    seq=int(summary["seq"]),
                    version_id=str(summary["version_id"]),
                    config=PlatformConfig.from_mapping(item["config"]),
                    content_hash=str(summary["content_hash"]),
                    parent_id=summary.get("parent_id"),
                    created_ts=float(summary.get("created_ts", 0.0)),
                    source=str(summary.get("source", "restored")),
                    reason=str(summary.get("reason", "")),
                )
                self._versions[version.version_id] = version
                self._order.append(version.version_id)
                max_seq = max(max_seq, version.seq)
            # 已裁剪掉的旧版本序号也要延续：优先用落盘记录的 next_seq。
            self._next_seq = int(raw.get("next_seq", max_seq + 1))
            active_id = raw.get("active_id")
            if isinstance(active_id, str) and active_id in self._versions:
                self._active_id = active_id
        if self._active_id is None:
            version = self._build(initial, parent_id=None, source=source, reason="启动基线")
            self._versions[version.version_id] = version
            self._order.append(version.version_id)
            self._active_id = version.version_id
            self._persist()

    def restored_differs(self, startup: PlatformConfig) -> bool:
        """启动传入配置是否与落盘生效版内容不同（供装配审计）。"""

        return content_hash(startup) != self.active.content_hash
