"""平台装配与控制循环。

``Platform`` 把各组件按真实调用链接起来，并暴露四件事：操作编排（装煤、
推焦、熄焦、换向、炉门清扫）、控制循环、并发仲裁探测和状态快照。控制循环
每个周期做三件事：把集气管压力调向目标带、按推焦联动时序驱动除尘挡板、
采集趋势；换向与推焦这类跨模块动作都由编排一次性走完并写审计。
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Any, Mapping

from .audit.log import AuditLog
from .audit.record import AuditRecord
from .charger.machine import ChargingCar
from .clock import Clock, SystemClock
from .config import PlatformConfig, config_to_mapping, default_config
from .configmeta import classes_for
from .configversion import ConfigRegistry, content_hash
from .door.latch import DoorLatch
from .door.machine import DoorCleaner
from .dust.damper import DustDamper
from .dust.schedule import DamperSchedule
from .dust.snapshot import DustSnapshot
from .dust.system import DustSystem
from .errors import CokeError
from .eventbus import Event, EventBus
from .gas.collector import CollectorMain
from .gas.flap import BridgeFlap
from .gas.latch import FlapLatch
from .guide.machine import GuideCar
from .heating.machine import HeatingReversal
from .heating.snapshot import HeatingSnapshot
from .heating.temperature import OvenTemperature
from .heating.valvetrain import ValveTrain
from .ids import IdFactory
from .ns.registry import NamespaceRegistry
from .ns.topology import Topology, build_default_topology
from .pusher.machine import PusherMachine
from .pusher.speed import SpeedArbiter
from .pusher.stroke import StrokeBaseline
from .quench.machine import QuenchCar
from .sequence import charge_oven, clean_door, push_oven, quench_oven, reverse_heating
from .store.journal import JsonlJournal
from .store.jsonfile import JsonStore
from .telemetry import TrendBuffer

DEFAULT_UNIT = "battery1"
DEFAULT_OVEN = "oven01"


def _audit_scalar(key: str, value: Any) -> Any:
    """把核对结果里的非标量字段转成可落审计的标量（审计不嵌复杂结构）。"""

    if key == "in_use_versions":
        return ",".join(value) if value else ""
    if key == "all_on_active":
        return bool(value)
    return value


class Platform:
    """焦炉推装熄顺序控制平台。"""

    def __init__(
        self,
        config: PlatformConfig | None = None,
        clock: Clock | None = None,
        unit: str = DEFAULT_UNIT,
    ) -> None:
        startup = config or default_config()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.unit = unit

        store = JsonStore(startup.storage_root)
        self.store = store
        self.audit = AuditLog(self.clock, self.bus, JsonlJournal(store.root, "audit", segment_limit=512))
        # 配置版本注册中心：从落盘版本链恢复当前生效版；首次启动则以传入配置
        # 建立基线版本。所有后续装配都以"当前生效版"为准，而不是启动参数。
        self.configs = ConfigRegistry(store, self.clock, initial=startup, source="startup")
        if self.configs.restored_differs(startup):
            self._publish_config(
                "config_restored",
                severity="warning",
                message="落盘生效配置与启动配置不一致，已以落盘版本为准",
                startup_hash=content_hash(startup),
            )
        active = self.configs.active
        self._active_version_id = active.version_id
        config = active.config

        self.namespaces = NamespaceRegistry(unit)
        self.topology: Topology = build_default_topology()
        self.trend = TrendBuffer(period_s=5.0)
        self.ids = IdFactory("op")

        # 每个执行器实际持有的配置版本：live 类换版后立即指向新版，pinned 类
        # 在各自操作安全边界换版后才更新。据此核对"生效版 vs 在跑版"。
        self._actuator_versions: dict[str, str] = {}
        self.pusher = PusherMachine(config.pusher)
        self._bind_version("pusher", active.version_id)
        self.stroke = StrokeBaseline(
            config.pusher.stroke_baseline_mm, config.pusher.stroke_tolerance_mm
        )
        self.speed = SpeedArbiter(
            config.pusher.speed_min_mm_s,
            config.pusher.speed_max_mm_s,
            config.pusher.speed_default_mm_s,
        )
        self._bind_version("pusher_speed", active.version_id)
        self.guide = GuideCar(config.guide)
        self._bind_version("guide", active.version_id)
        self.charger = ChargingCar(config.charger)
        self._bind_version("charger", active.version_id)
        self.quench_car = QuenchCar(config.quench)
        self._bind_version("quench", active.version_id)
        self.collector = CollectorMain(config.collector)
        self._bind_version("collector", active.version_id)
        self.flap = BridgeFlap(FlapLatch())
        self.door = DoorCleaner(config.door, DoorLatch(config.door.latch_travel_mm))
        self._bind_version("door", active.version_id)
        self.reversal = HeatingReversal(config.heating, self.clock)
        self._reversal_pending_version_id: str | None = None
        self._bind_version("reversal", active.version_id)
        self.valves = ValveTrain()
        self.temperature = OvenTemperature(config.heating)
        self._bind_version("temperature", active.version_id)
        self.dust = DustSystem()
        self.damper = DustDamper(config.dust.damper_open_deg, config.dust.damper_rate_deg_per_s)
        self._bind_version("damper", active.version_id)
        self.schedule = DamperSchedule(config.dust.linkage_lag_s)
        self._bind_version("damper_linkage", active.version_id)

        self._cycles = 0
        self._operations = 0
        self._lock = threading.RLock()
        self._register_namespaces()
        self._restore()

    # ---- 配置热更新 -----------------------------------------------------

    @property
    def config(self) -> PlatformConfig:
        """当前生效的整套配置。"""

        return self.configs.config()

    def _bind_version(self, actuator: str, version_id: str) -> None:
        self._actuator_versions[actuator] = version_id

    def _publish_config(self, action: str, **payload: Any) -> None:
        severity = str(payload.pop("severity", "notice"))
        message = str(payload.pop("message", ""))
        self.bus.publish(
            Event.of("config", "config", action, severity=severity, message=message, **payload)
        )

    def _adopt_config(self, cfg: PlatformConfig, version_id: str) -> None:
        """把一版已激活的配置下发给各执行器并更新它们的在跑版本。

        live 类执行器立即换版；pinned 类执行器若处于操作中途会抛联锁异常——
        但平台所有操作都在同一把锁内同步完成，调用本方法时它们必然空闲，真
        正跨操作的长寿命契约（换向间隔）由排程器自己延后到下一次换向安装。
        """

        # --- pinned：离散操作类，仅在安全空闲边界换版 ---
        self.pusher.rebind(cfg.pusher)
        self._bind_version("pusher", version_id)
        self.speed.rebind(
            cfg.pusher.speed_min_mm_s, cfg.pusher.speed_max_mm_s, cfg.pusher.speed_default_mm_s
        )
        self._bind_version("pusher_speed", version_id)
        self.guide.rebind(cfg.guide)
        self._bind_version("guide", version_id)
        self.charger.rebind(cfg.charger)
        self._bind_version("charger", version_id)
        self.quench_car.rebind(cfg.quench)
        self._bind_version("quench", version_id)
        self.door.rebind(cfg.door)
        self._bind_version("door", version_id)
        # 换向间隔/废气提前量：登记待生效，当前这轮换向周期仍按旧版跑到点。
        self.reversal.rebind(cfg.heating)
        if self.reversal.pending_version_ready():
            self._reversal_pending_version_id = version_id
        else:
            self._reversal_pending_version_id = None
            self._bind_version("reversal", version_id)
        # --- live：连续调节类，下一周期立即生效 ---
        self.collector.rebind(cfg.collector)
        self._bind_version("collector", version_id)
        self.temperature.rebind(cfg.heating)
        self._bind_version("temperature", version_id)
        self.damper.rebind(cfg.dust.damper_open_deg, cfg.dust.damper_rate_deg_per_s)
        self._bind_version("damper", version_id)
        self.schedule.rebind(cfg.dust.linkage_lag_s)
        self._bind_version("damper_linkage", version_id)

    def in_use_versions(self) -> dict[str, str]:
        """返回各执行器当前实际在用的配置版本标识。"""

        with self._lock:
            return dict(self._actuator_versions)

    def reconcile_config(self) -> dict:
        """核对当前生效版与各执行器在跑版本，返回核对结果。"""

        with self._lock:
            report = self.configs.reconcile(self._actuator_versions)
            report["reversal_pending"] = self.reversal.pending_version_ready()
            return report

    def apply_config(
        self,
        mapping: Mapping[str, Any],
        *,
        source: str = "console",
        reason: str = "",
    ) -> dict:
        """热更新：校验并整套切换到新版配置，不打断任何在跑操作。

        校验失败（缺字段、越界、跨字段矛盾）时整版拒绝，当前生效版不动；
        成功则原子激活新版本、下发执行器，并把提议与激活都落审计。
        """

        with self._lock:
            before = self.configs.active
            candidate = self.configs.propose(mapping)
            # platform 段是启动装配项，不参与热更新：保留当前生效版的身份与
            # 存储目录，只整套替换 limits 段。版本身份哈希也只覆盖 limits。
            candidate = dataclasses.replace(
                candidate, name=before.config.name, storage_root=before.config.storage_root
            )
            self._publish_config(
                "config_proposed",
                from_version=before.version_id,
                from_hash=before.content_hash,
                proposed_hash=content_hash(candidate),
                source=source,
                reason=reason,
            )
            version = self.configs.activate(candidate, source=source, reason=reason)
            changed = version.version_id != before.version_id
            if changed:
                self._adopt_config(version.config, version.version_id)
                self._active_version_id = version.version_id
            self._publish_config(
                "config_activated",
                from_version=before.version_id,
                to_version=version.version_id,
                from_hash=before.content_hash,
                to_hash=version.content_hash,
                changed=changed,
                source=source,
                reason=reason,
                severity="notice",
                message="配置已整套切换" if changed else "配置内容无变化，沿用当前版",
            )
            report = self.reconcile_config()
            self._publish_config("config_reconciled", **{k: _audit_scalar(k, v) for k, v in report.items() if k != "in_use"})
            return {
                "changed": changed,
                "active": version.as_summary(),
                "previous": before.as_summary(),
                "reconcile": report,
            }

    def rollback_config(self, *, source: str = "console", reason: str = "") -> dict:
        """退回上一版配置。回退幂等：重复调用状态结果一致，每次尝试都留痕。"""

        with self._lock:
            before = self.configs.active
            target, switched = self.configs.rollback(source=source, reason=reason)
            if switched:
                self._adopt_config(target.config, target.version_id)
                self._active_version_id = target.version_id
            self._publish_config(
                "config_rollback",
                from_version=before.version_id,
                to_version=target.version_id,
                from_hash=before.content_hash,
                to_hash=target.content_hash,
                switched=switched,
                source=source,
                reason=reason,
                severity="notice" if switched else "warning",
                message="配置已退回上一版" if switched else "已无上一版可退（或已在该版），状态不变",
            )
            if switched:
                report = self.reconcile_config()
                self._publish_config(
                    "config_reconciled",
                    **{k: _audit_scalar(k, v) for k, v in report.items() if k != "in_use"},
                )
            else:
                report = self.reconcile_config()
            return {
                "switched": switched,
                "active": target.as_summary(),
                "requested_from": before.as_summary(),
                "reconcile": report,
            }

    def config_status(self) -> dict:
        """返回配置版本视图，供控制台展示与落审计核对。"""

        with self._lock:
            active = self.configs.active
            previous = self.configs.previous()
            return {
                "switch_mode": "atomic_whole_set",
                "active": active.as_summary(),
                "previous": previous.as_summary() if previous else None,
                "rollback_available": previous is not None,
                "in_use": self.in_use_versions(),
                "reconcile": self.reconcile_config(),
                "field_classes": classes_for(active.config),
                "history": self.configs.history(),
                "config": config_to_mapping(active.config),
            }

    def _register_namespaces(self) -> None:
        """登记现场设备命名空间（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("battery", "pusher", "pusher-machine"),
            ("battery", "guide", "guide-car"),
            ("battery", "charger", "charging-car"),
            ("battery", "quench", "quench-car"),
            ("battery", "collector", "collector-main"),
            ("battery", "bridge_flap", "bridge-flap"),
            ("battery", "door", "door-cleaner"),
            ("battery", "reversal", "reversing-valve"),
            ("battery", "dust", "dust-damper"),
        ):
            self.namespaces.register(area, device, kind)

    def _restore(self) -> None:
        """从落盘审计回放最后一次换向，恢复换向间隔计时。"""

        try:
            records = self.audit.to_records(self.audit.replay_from_journal(limit=100_000))
        except CokeError:
            return
        reversals = [item for item in records if item.action == "reverse_complete"]
        # 只接受不晚于当前时钟的回放记录；可推进时钟重置后，上一轮留下的未来
        # 时间戳属于过期状态，不能拿它把换向间隔计时顶到当前时刻之前。
        if reversals and reversals[-1].ts <= self.clock.now():
            self.reversal.mark_reversed(reversals[-1].ts)
        state = self.store.read("platform_state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))

    def _persist(self) -> None:
        """落盘平台计数，供重启后对照。"""

        self.store.write(
            "platform_state",
            {"operations": self._operations, "cycles": self._cycles, "unit": self.unit},
        )

    def _record_operation(self, op_id: str, operation: str, report: dict, config_version: str | None = None) -> dict:
        """把编排步骤逐条写审计，并记一条汇总事件。"""

        oven = str(report.get("oven", ""))
        for step in report.get("steps", []):
            detail = {
                key: value
                for key, value in step.get("detail", {}).items()
                if not isinstance(value, (dict, list))
                and key not in ("oven", "op_id", "severity", "message")
            }
            self.bus.publish(
                Event.of(
                    step["component"],
                    step["component"],
                    step["action"],
                    oven=oven,
                    op_id=op_id,
                    severity="notice",
                    **detail,
                )
            )
        for violation in report.get("violations", []):
            self.bus.publish(
                Event.of(
                    "audit",
                    "audit",
                    "interlock_rejected",
                    oven=oven,
                    op_id=op_id,
                    severity="alarm",
                    message=f"{operation} 被拒绝：{violation}",
                    violation=violation,
                )
            )
        summary = {
            "op_id": op_id,
            "operation": operation,
            "oven": oven,
            "ok": bool(report.get("ok")),
            "violations": list(report.get("violations", [])),
            # 本次操作按哪一版配置跑完——操作开始时租用，整炉不变。
            "config_version": config_version or self._active_version_id,
        }
        self.bus.publish(
            Event.of(
                "audit",
                "audit",
                f"{operation}_complete",
                severity="notice" if report.get("ok") else "warning",
                message=f"{operation} 完成" if report.get("ok") else f"{operation} 未完成",
                **summary,
            )
        )
        return summary

    def start(self) -> dict:
        """启动平台：启动除尘风机并核对拓扑。"""

        with self._lock:
            self.dust.start()
            report = self.topology.verify(set())
            return {
                "fan_running": self.dust.is_running(),
                "topology_missing": report["missing"],
                "operations": self._operations,
            }

    def stop(self) -> dict:
        """停运平台：停除尘风机。"""

        with self._lock:
            self.dust.stop()
            return {"fan_running": self.dust.is_running(), "operations": self._operations}

    def charge(
        self,
        oven: str = DEFAULT_OVEN,
        hopper_target_mm: float = 0.0,
        hopper_measured_mm: float = 0.0,
        dt: float = 1.0,
    ) -> dict:
        """装煤编排：煤斗对位 → 调压 → 开翻板 → 给煤。"""

        with self._lock:
            op_id = self.ids.next()
            report = charge_oven(
                self.charger, self.collector, self.flap, oven, hopper_target_mm, hopper_measured_mm, dt
            )
            summary = self._record_operation(op_id, "charge", report, self._actuator_versions.get("charger"))
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def push(
        self,
        oven: str = DEFAULT_OVEN,
        guide_target_mm: float = 0.0,
        guide_measured_mm: float = 0.0,
        stroke_target_mm: float | None = None,
        speed_mm_s: float | None = None,
        dt: float = 1.0,
    ) -> dict:
        """推焦编排：导焦栅对位 → 锁定 → 推焦 → 退杆；同时触发除尘联动。"""

        with self._lock:
            op_id = self.ids.next()
            target = self.stroke.baseline_mm if stroke_target_mm is None else stroke_target_mm
            self.speed.request("auto_cycle", speed_mm_s or self.config.pusher.speed_default_mm_s, priority=50)
            arbitration = self.speed.resolve()
            self.schedule.start_link(self.clock.now())
            report = push_oven(
                self.pusher,
                self.guide,
                oven,
                guide_target_mm,
                guide_measured_mm,
                target,
                arbitration.speed_mm_s,
                dt,
            )
            stroke_check = self.stroke.check(self.pusher.ram_mm)
            report["speed_arbitration"] = arbitration.as_dict()
            report["stroke_check"] = stroke_check
            summary = self._record_operation(op_id, "push", report, self._actuator_versions.get("pusher"))
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def quench(self, oven: str = DEFAULT_OVEN, dt: float = 1.0) -> dict:
        """熄焦编排：接焦 → 走行 → 喷淋。"""

        with self._lock:
            op_id = self.ids.next()
            report = quench_oven(self.quench_car, oven, dt)
            report["travel_eta_s"] = self.quench_car.plan.eta_s()
            summary = self._record_operation(op_id, "quench", report, self._actuator_versions.get("quench"))
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def reverse(self, oven: str = DEFAULT_OVEN, temperature_c: float | None = None) -> dict:
        """换向编排：废气阀 → 煤气阀 → 炉温确认。"""

        with self._lock:
            op_id = self.ids.next()
            target_temp = self.config.heating.temp_target_c if temperature_c is None else temperature_c
            report = reverse_heating(self.reversal, self.valves, self.temperature, oven, target_temp)
            report["offset_c"] = self.temperature.offset_c()
            # 换向安全完成后排程器才会安装待生效的新版间隔，同步在跑版本指针。
            if report.get("ok") and self._reversal_pending_version_id is not None:
                pending_id = self._reversal_pending_version_id
                self._reversal_pending_version_id = None
                self._bind_version("reversal", pending_id)
            summary = self._record_operation(op_id, "reverse", report, self._actuator_versions.get("reversal"))
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def clean(self, oven: str = DEFAULT_OVEN, dt: float = 1.0) -> dict:
        """炉门清扫编排：松开闩锁 → 开炉门 → 清扫 → 复位闩锁。"""

        with self._lock:
            op_id = self.ids.next()
            report = clean_door(self.door, oven, dt)
            summary = self._record_operation(op_id, "clean", report, self._actuator_versions.get("door"))
            summary["op_id"] = op_id
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def cycle(self, dt: float = 1.0) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        with self._lock:
            self._cycles += 1
            moment = self.clock.now()
            before = self.collector.pressure_pa
            pressure = self.collector.regulate(dt)
            linkage = self.schedule.due(moment)
            target_deg = self.damper.open_deg if self.schedule.links() else 0.0
            angle = self.damper.drive_to(target_deg, dt)
            if self.damper.at_open():
                self.schedule.clear()
            self.trend.append("collector_pressure", moment, pressure)
            self.trend.append("damper_angle", moment, angle)
            self._persist()
            return {
                "cycle": self._cycles,
                "pressure_pa": round(pressure, 2),
                "pressure_delta": round(pressure - before, 3),
                "settled": self.collector.settled(),
                "damper_deg": round(angle, 2),
                "linkage_due": linkage,
                "linkage_elapsed_s": round(self.schedule.elapsed_s(moment), 2),
            }

    def concurrency_probe(self, rounds: int = 24) -> dict:
        """用真实并行调用探测推焦速度仲裁的收敛结果。

        两个线程分别代表自动循环与手动控制台，同时向推焦杆提交速度。探测只
        读取最终提交值，用来确认共享执行器始终收敛到唯一值。
        """

        if rounds < 1:
            raise ValueError("探测轮数必须为正")

        def worker(writer: str, speed: float, priority: int) -> None:
            for _ in range(rounds):
                self.speed.request(writer, speed, priority)

        threads = [
            threading.Thread(target=worker, args=("auto_cycle", self.config.pusher.speed_max_mm_s, 50)),
            threading.Thread(target=worker, args=("manual_console", self.config.pusher.speed_min_mm_s, 80)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        outcome = self.speed.resolve()
        self.pusher.set_speed(outcome.speed_mm_s)
        return {
            "rounds": rounds,
            "winner": outcome.winner,
            "committed_mm_s": round(outcome.speed_mm_s, 2),
            "contenders": outcome.contenders,
            "writers": self.speed.writers(),
            "committed_on_machine": round(self.pusher.committed_speed(), 2),
        }

    def calibrate_stroke(self, value_mm: float) -> dict:
        """重新标定推焦行程基线，返回标定前后的差值。"""

        if value_mm <= 0:
            raise CokeError("标定值必须为正", component="pusher")
        before = self.stroke.baseline_mm
        after = self.stroke.calibrate(value_mm)
        self.bus.publish(
            Event.of(
                "pusher",
                "pusher",
                "stroke_recalibrated",
                severity="notice",
                baseline_before_mm=before,
                baseline_after_mm=after,
                epoch=self.stroke.epoch,
            )
        )
        return {"before_mm": before, "after_mm": after, "epoch": self.stroke.epoch}

    def disturb_collector(self, delta_pa: float) -> dict:
        """模拟一次集气管扰动，返回扰动后压力。"""

        before = self.collector.pressure_pa
        after = self.collector.disturb(delta_pa)
        return {"before_pa": round(before, 2), "after_pa": round(after, 2), "settled": self.collector.settled()}

    def note_error(self, error: CokeError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        payload = error.as_record()
        record = self.audit.record(
            AuditRecord.create(
                self.clock.now(),
                "",
                payload["component"],
                "error",
                severity="warning",
                message=payload["message"],
            )
        )
        return record

    def reset_state(self) -> bool:
        """清掉落盘的平台计数（交付前重新标定用）。"""

        if not self.store.exists("platform_state"):
            return False
        return self.store.remove("platform_state")

    def heating_snapshot(self) -> HeatingSnapshot:
        """返回加热换向状态快照。"""

        return HeatingSnapshot(
            waste_gas_open=self.valves.waste_open,
            gas_open=self.valves.gas_open,
            temperature_c=self.temperature.value_c,
            confirmed=self.temperature.confirmed(),
            reversals=self.reversal.count(),
            seconds_to_due=self.reversal.seconds_to_due(),
        )

    def dust_snapshot(self) -> DustSnapshot:
        """返回除尘状态快照。"""

        return DustSnapshot(
            fan_running=self.dust.is_running(),
            damper_deg=self.damper.angle_deg,
            damper_open=self.damper.at_open(),
            linkage_pending=self.schedule.links() > 0,
            links=self.schedule.links(),
        )

    def topology_summary(self) -> dict:
        """返回工艺链拓扑摘要。"""

        return self.topology.summary()

    def namespaces_describe(self) -> dict:
        """返回命名空间摘要。"""

        return self.namespaces.describe()

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        return {
            "platform": self.config.name,
            "unit": self.unit,
            "operations": self._operations,
            "cycles": self._cycles,
            "config_version": self.configs.active.version_id,
            "config_reconcile": self.reconcile_config(),
            "now": round(self.clock.now(), 3),
            "utc": self.clock.utc_iso(),
            "pusher": self.pusher.snapshot().as_dict(),
            "speed": {"committed_mm_s": round(self.speed.committed(), 2)},
            "stroke": {
                "baseline_mm": self.stroke.baseline_mm,
                "tolerance_mm": self.stroke.tolerance_mm,
                "epoch": self.stroke.epoch,
            },
            "guide": self.guide.snapshot().as_dict(),
            "charger": self.charger.snapshot().as_dict(),
            "quench": self.quench_car.snapshot().as_dict(),
            "gas": {
                "pressure_pa": round(self.collector.pressure_pa, 2),
                "target_pa": self.collector.limits.pressure_target_pa,
                "settled": self.collector.settled(),
                "flap_deg": round(self.flap.angle_deg, 2),
                "flap_open": self.flap.is_open(),
                "latch_engaged": self.flap.latch.is_engaged(),
                "latch_cycles": self.flap.latch.cycles(),
                "latch_resets": self.flap.latch.resets(),
            },
            "door": {**self.door.snapshot().as_dict(), "latch_cycles": self.door.latch.cycles()},
            "heating": self.heating_snapshot().as_dict(),
            "dust": self.dust_snapshot().as_dict(),
            "dust_starts": self.dust.starts(),
            "audit": self.audit.summary(),
            "ids_issued": self.ids.issued(),
        }

    def snapshot(self) -> dict:
        """返回只读的整体快照（状态 + 拓扑 + 命名空间 + 趋势）。"""

        return {
            "status": self.status(),
            "topology": self.topology_summary(),
            "namespaces": self.namespaces_describe(),
            "trend": self.trend.snapshot(),
        }


def build_platform(
    config: PlatformConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str = DEFAULT_UNIT,
) -> Platform:
    """构造一个平台；``storage_root`` 可覆盖配置里的存储目录。"""

    base = config or default_config()
    if storage_root is not None:
        base = dataclasses.replace(base, storage_root=storage_root)
    return Platform(base, clock=clock, unit=unit)
