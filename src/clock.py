"""可注入时钟。

领域服务全部通过 ``Clock`` 获取当前时间，测试与回放时可替换为固定时钟，
保证“同一输入序列 + 同一时间点 => 同一结果”的确定性要求。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """真实墙钟，始终返回带时区的 UTC 时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """测试/回放用时钟，可手动推进。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = _aware(moment)

    def now(self) -> datetime:
        return self._moment

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._moment += timedelta(seconds=seconds)

    def set(self, moment: datetime) -> None:
        self._moment = _aware(moment)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("时间必须带时区")
    return moment


def ensure_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("时间必须带时区")
    return moment
