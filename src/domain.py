"""景区区域、计数来源与策略参数的基础资料读取。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path

# 各来源默认“数据新鲜度”超时：超过该时长没有新事件即判为陈旧。
DEFAULT_FRESHNESS_SECONDS: dict[str, int] = {
    "gate": 180,
    "camera": 300,
    "shuttle": 300,
    "parking": 600,
    "ticket": 900,
}


@dataclass(frozen=True)
class Zone:
    code: str
    name: str
    fire_capacity: int
    sources: tuple[str, ...]
    # 特殊人群（无障碍、应急、妇幼）通道在任何限流动作下都必须保留的通道数；
    # 0 表示该区域无此要求。
    special_lanes: int = 0


@dataclass(frozen=True)
class Thresholds:
    """占用/消防容量的风险分级阈值。"""

    watch: float = 0.70
    warning: float = 0.85
    critical: float = 0.95


@dataclass(frozen=True)
class PolicyConfig:
    thresholds: Thresholds = field(default_factory=Thresholds)
    freshness_seconds: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_FRESHNESS_SECONDS)
    )
    # 来源全部陈旧时，按容量附加的保守占用缓冲比例
    stale_buffer_ratio: float = 0.10
    # 预测视野（把未来多久内预计到场的分时票计入风险）
    prediction_horizon_seconds: int = 900
    # 事件可接受的最大迟到时长
    max_late_seconds: int = 6 * 3600
    # 下游达到预警时，上游开始预限流的触发比例（相对下游容量）
    upstream_prerestrict_ratio: float = 0.85
    # 人工接管恢复自动后，每分钟最多释放的在场容量比例（斜坡放行）
    ramp_release_ratio_per_minute: float = 0.05
    # 闭园前多少分钟开始保障已入园游客返程（优先摆渡/只出不进）
    return_guard_minutes: int = 90
    # 预测只作辅助：预测占用超过该比例才参与动作升级，
    # 但任何动作都不得仅凭预测触达消防硬限条款之外的强制措施——
    # 消防硬限只由“可信占用量（实测下限）”触发。
    prediction_advisory_only: bool = True


@dataclass(frozen=True)
class Schedule:
    """每日运营时刻，按固定时区解释。闭园后票队列失效、进入返程保障。"""

    open_hour: int = 7
    close_hour: int = 18
    tz_offset_hours: int = 8

    @property
    def tz(self) -> timezone:
        from datetime import timedelta

        return timezone(timedelta(hours=self.tz_offset_hours))

    def is_open(self, at) -> bool:
        local = at.astimezone(self.tz)
        return self.open_hour <= local.hour < self.close_hour

    def next_close(self, at):
        """返回 at 之后（含当日尚未闭园时）最近一次闭园时刻。"""
        from datetime import datetime

        local = at.astimezone(self.tz)
        close_today = local.replace(hour=self.close_hour, minute=0, second=0, microsecond=0)
        if local < close_today:
            return close_today
        return close_today + _one_day()

    def last_close(self, at):
        """返回 at 之前最近一次闭园时刻（用于跨日队列重置判定）。"""
        return self.next_close(at) - _one_day()


def _one_day():
    from datetime import timedelta

    return timedelta(days=1)


def load_zones(path: str | Path) -> list[Zone]:
    """读取区域容量，拒绝缺少消防上限或数据来源的记录。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    zones = [
        Zone(
            item["code"],
            item["name"],
            item["fire_capacity"],
            tuple(item["sources"]),
            int(item.get("special_lanes", 0)),
        )
        for item in payload
    ]
    if any(not zone.code or zone.fire_capacity <= 0 or not zone.sources for zone in zones):
        raise ValueError("区域基础资料不完整")
    if len({z.code for z in zones}) != len(zones):
        raise ValueError("区域编码重复")
    return zones
