"""配置参数的热更新语义分类。

热更新的生效时机只允许两类，分类写死在这里，不随某次改版浮动：

``live``   连续调节类参数：控制循环每个周期重新读取，没有"跑到某端点才算
           完"的契约（压力目标带、炉温确认带、除尘挡板开度与速率、联动滞
           后）。新版在控制循环的下一个周期立即生效。
``pinned`` 离散操作类参数：定义一炉操作的几何、时序或安全包络（推焦行程
           与速度、导焦栅、装煤、熄焦、炉门清扫、换向间隔与废气提前量）。
           操作开始时租用一版配置，整炉按该版跑完，操作结束后才允许换新
           版；正在跑的操作绝不中途换参。

热更新按"整套一起切"执行：一次改版产出一个不可变全量版本，不会逐参数半
生效。这张表只描述每个字段在整套切换之后**何时**被执行器采用。
"""

from __future__ import annotations

import dataclasses

LIVE = "live"
PINNED = "pinned"

# key 为 "<段落>.<字段>"，与 config/coke.json 里的路径一一对应。
FIELD_CLASSES: dict[str, str] = {
    # 推焦：一炉推焦的行程与速度包络，开始后不得中途改。
    "pusher.stroke_baseline_mm": PINNED,
    "pusher.stroke_tolerance_mm": PINNED,
    "pusher.max_stroke_mm": PINNED,
    "pusher.speed_min_mm_s": PINNED,
    "pusher.speed_max_mm_s": PINNED,
    "pusher.speed_default_mm_s": PINNED,
    # 拦焦车：对位公差与锁定行程决定本炉锁定动作。
    "guide.align_tolerance_mm": PINNED,
    "guide.apron_lock_travel_mm": PINNED,
    "guide.apron_lock_timeout_s": PINNED,
    # 装煤车：目标煤量与给煤速率定义本炉给煤过程。
    "charger.align_tolerance_mm": PINNED,
    "charger.coal_target_t": PINNED,
    "charger.feed_rate_t_per_min": PINNED,
    # 熄焦车：走行距离/速度与喷淋时长/水量定义本炉熄焦过程。
    "quench.travel_distance_m": PINNED,
    "quench.travel_speed_m_s": PINNED,
    "quench.spray_duration_s": PINNED,
    "quench.water_flow_m3_h": PINNED,
    # 炉门：闩锁行程与清扫时长定义本炉清扫过程。
    "door.latch_travel_mm": PINNED,
    "door.clean_duration_s": PINNED,
    # 加热换向：间隔与废气阀提前量是换向排程契约，钉到下一次换向完成。
    "heating.reversal_interval_s": PINNED,
    "heating.waste_close_lead_s": PINNED,
    # 炉温确认带：每个控制周期重新判定，立即跟随。
    "heating.temp_target_c": LIVE,
    "heating.temp_band_c": LIVE,
    # 集气管：连续调节，每周期重算，立即跟随。
    "collector.pressure_target_pa": LIVE,
    "collector.pressure_band_pa": LIVE,
    "collector.regulate_rate_pa_per_s": LIVE,
    # 除尘挡板：开度与驱动速率每周期驱动，立即跟随。
    "dust.damper_open_deg": LIVE,
    "dust.damper_rate_deg_per_s": LIVE,
    # 联动滞后：下一次联动排程读取，属连续循环侧参数。
    "dust.linkage_lag_s": LIVE,
}


def field_class(section: str, field: str) -> str:
    """返回某个配置字段的热更新类别。"""

    return FIELD_CLASSES[f"{section}.{field}"]


def classes_for(config: object) -> dict[str, dict[str, str]]:
    """把一份配置的全部限值字段按段落展开成分类映射。"""

    sections = {
        "pusher": config.pusher,
        "guide": config.guide,
        "charger": config.charger,
        "quench": config.quench,
        "collector": config.collector,
        "door": config.door,
        "heating": config.heating,
        "dust": config.dust,
    }
    result: dict[str, dict[str, str]] = {}
    for section, value in sections.items():
        result[section] = {
            field.name: field_class(section, field.name) for field in dataclasses.fields(value)
        }
    return result
