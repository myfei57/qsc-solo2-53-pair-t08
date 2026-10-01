"""换向排程。

按固定间隔判定是否到期；每次成功换向记录时间并累加计数。到期末打标记不会
自己换向，仍由编排在确认安全后调用，符合"申请 → 执行 → 确认"的真实节奏。
"""

from __future__ import annotations

from ..config import HeatingLimits
from ..clock import Clock


class HeatingReversal:
    """加热换向排程器。"""

    def __init__(self, limits: HeatingLimits, clock: Clock) -> None:
        self._limits = limits
        self._clock = clock
        self._last_ts = clock.now()
        self._count = 0
        self._pending_limits: HeatingLimits | None = None

    @property
    def limits(self) -> HeatingLimits:
        return self._limits

    def due(self, now: float | None = None) -> bool:
        """当前时刻是否已到换向间隔。"""

        moment = self._clock.now() if now is None else now
        return (moment - self._last_ts) >= self._limits.reversal_interval_s

    def mark_reversed(self, now: float | None = None) -> int:
        """记录一次换向，返回累计换向次数；同时安装待生效的新版限值。"""

        self._last_ts = self._clock.now() if now is None else now
        self._adopt_pending()
        self._count += 1
        return self._count

    def count(self) -> int:
        """返回累计换向次数。"""

        return self._count

    def seconds_to_due(self, now: float | None = None) -> float:
        """返回距下次换向还有多少秒（已到期返回 0）。"""

        moment = self._clock.now() if now is None else now
        remaining = self._limits.reversal_interval_s - (moment - self._last_ts)
        return max(0.0, remaining)

    def rebind(self, limits: HeatingLimits) -> None:
        """换版（pinned）：登记一版待生效的换向限值，但当前间隔不立即采用。

        换向间隔是跨操作的长寿命契约：正在跑的这一轮换向周期必须按开始时那
        一版的间隔跑到点。新限值在 :meth:`mark_reversed`（一次换向安全完成）
        时才安装，从而"按旧版跑完再换"。
        """

        self._pending_limits = limits

    def _adopt_pending(self) -> bool:
        pending = getattr(self, "_pending_limits", None)
        if pending is None or pending == self._limits:
            return False
        self._limits = pending
        self._pending_limits = None
        return True

    def pending_version_ready(self) -> bool:
        """是否有一版新限值在等下一次换向后安装。"""

        pending = getattr(self, "_pending_limits", None)
        return pending is not None and pending != self._limits
