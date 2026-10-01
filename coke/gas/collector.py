"""集气管压力状态。

压力需要被调节到目标带内才算"调好"，装煤序列据此放行桥管翻板。外部扰动
（开盖、换向）会临时把压力拉走，由控制循环周期性重新调节。
"""

from __future__ import annotations

from ..config import CollectorLimits
from .pressure import regulate_pressure


class CollectorMain:
    """集气管压力状态机。"""

    def __init__(self, limits: CollectorLimits, initial_pa: float = 30.0) -> None:
        self._limits = limits
        self._pressure_pa = float(initial_pa)

    @property
    def limits(self) -> CollectorLimits:
        return self._limits

    @property
    def pressure_pa(self) -> float:
        return self._pressure_pa

    def regulate(self, dt: float) -> float:
        """向目标压力调节一个步长，返回新压力。"""

        self._pressure_pa = regulate_pressure(
            self._pressure_pa,
            self._limits.pressure_target_pa,
            self._limits.regulate_rate_pa_per_s,
            dt,
        )
        return self._pressure_pa

    def disturb(self, delta_pa: float) -> float:
        """施加一次外部扰动，返回扰动后的压力。"""

        self._pressure_pa += float(delta_pa)
        return self._pressure_pa

    def settled(self) -> bool:
        """压力是否已进入目标带。"""

        return self._limits.settled(self._pressure_pa)

    def rebind(self, limits: CollectorLimits) -> None:
        """换版（live）：压力目标/带宽/速率在下一调节周期立即生效。

        连续调节没有"跑到端点才算完"的契约，因此任何时刻都允许换版；当前
        压力读数保留，只换目标带与速率。
        """

        self._limits = limits
