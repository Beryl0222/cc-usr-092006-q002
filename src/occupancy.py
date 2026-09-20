"""多源事件归并：把迟到/乱序/重复事件融合为各区域可信占用量。

归并规则（全部为确定性纯计算，只依赖已接受事件序列与当前时刻）：

1. 去重：按 (source, event_id) 丢弃重复推送；
2. 增量台账：闸机进/出、票核销、摆渡到/离做有符号累加，读取时不允许为负；
3. 快照融合：新鲜的图像计数是在场人数的直接观测，取
   ``max(台账余量, 最新快照)`` 作为保守占用量（两种手段各有漏计，取高者对安全更稳妥）；
4. 新鲜度缓冲：区域内已接入来源按声明清单逐一判定陈旧，陈旧比例越高，
   按容量附加越大的保守缓冲；全部陈旧仍给出可读数值而不是“未知”；
5. 分时票：售出进预测队列、核销转占用；闭园后未核销票作废，次日开园重置台账；
6. 停车事件不直接计入占用，只作为上游来流指标暴露给值班界面与预限流判断。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .domain import PolicyConfig, Schedule, Zone
from .events import DELTA_TYPES, Event, EventType

# 超过此时钟偏移的“未来事件”视为来源时钟异常，拒绝并入
FUTURE_TOLERANCE_SECONDS = 60
# 已过时段但未核销的票，在预测中仍保留的宽限时间
TICKET_GRACE_SECONDS = 15 * 60
# 每区域保留的最近证据条数（动作依据用）
EVIDENCE_LIMIT = 20


class RejectReason(str):
    DUPLICATE = "duplicate"
    TOO_LATE = "too_late"
    FUTURE_EVENT = "future_event"
    UNKNOWN_ZONE = "unknown_zone"
    QUEUED_TICKET = "queued_ticket_used_unknown"


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    reason: str | None = None


@dataclass
class _SourceState:
    last_occurred_at: datetime | None = None
    last_received_at: datetime | None = None
    events_seen: int = 0


@dataclass
class _Ticket:
    correlation_id: str
    zone: str
    slot_start: datetime
    quantity: int
    sold_event_id: str


@dataclass
class SourceFreshness:
    source: str
    state: str            # fresh | stale | silent（从未上报）
    age_seconds: float | None


@dataclass(frozen=True)
class ZoneReading:
    zone: str
    ledger: int                  # 增量台账（去负后）
    camera_snapshot: int | None
    camera_at: datetime | None
    stale_buffer: int
    estimated: int               # 可信占用量（消防硬限只看它与 ledger）
    predicted_add: int           # 预测视野内预计新增（仅辅助）
    projected: int               # estimated + predicted_add
    stale_source_ratio: float
    freshness: list[SourceFreshness]
    camera_anomaly: bool
    evidence: list[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class OccupancyReport:
    at: datetime
    business_date: str
    zones: dict[str, ZoneReading]
    parking_inside: dict[str, int]
    queued_tickets: int
    rejected: tuple[dict, ...]


class OccupancyEngine:
    def __init__(self, zones: list[Zone], config: PolicyConfig, schedule: Schedule) -> None:
        self._zones = {z.code: z for z in zones}
        self._config = config
        self._schedule = schedule

        self._seen: set[tuple[str, str]] = set()
        self._source_state: dict[tuple[str, str], _SourceState] = {}
        self._ledger: dict[str, int] = {z.code: 0 for z in zones}
        self._camera: dict[str, tuple[int, datetime]] = {}
        self._tickets: dict[str, _Ticket] = {}
        self._parking: dict[str, int] = {z.code: 0 for z in zones}
        self._evidence: dict[str, deque[str]] = {z.code: deque(maxlen=EVIDENCE_LIMIT) for z in zones}
        self._rejected: deque[dict] = deque(maxlen=200)
        self._business_date = ""
        self._sequence = 0

    # ---------------------------------------------------------------- 摄取
    def ingest(self, event: Event, now: datetime) -> IngestResult:
        if event.sequence > self._sequence:
            self._sequence = event.sequence
        self._rollover_if_needed(now)

        key = event.dedup_key
        if key in self._seen:
            return IngestResult(False, RejectReason.DUPLICATE)

        age = (now - event.occurred_at).total_seconds()
        if age > self._config.max_late_seconds:
            self._reject(event, RejectReason.TOO_LATE)
            return IngestResult(False, RejectReason.TOO_LATE)
        if age < -FUTURE_TOLERANCE_SECONDS:
            self._reject(event, RejectReason.FUTURE_EVENT)
            return IngestResult(False, RejectReason.FUTURE_EVENT)

        if event.zone not in self._zones:
            self._reject(event, RejectReason.UNKNOWN_ZONE)
            return IngestResult(False, RejectReason.UNKNOWN_ZONE)

        self._seen.add(key)
        src = self._source_state.setdefault((event.source, event.zone), _SourceState())
        src.events_seen += 1
        if src.last_occurred_at is None or event.occurred_at >= src.last_occurred_at:
            src.last_occurred_at = event.occurred_at
        received = event.received_at or now
        if src.last_received_at is None or received >= src.last_received_at:
            src.last_received_at = received

        et = event.event_type
        if et is EventType.TICKET_SOLD:
            self._tickets[event.correlation_id] = _Ticket(
                correlation_id=event.correlation_id,
                zone=event.zone,
                slot_start=event.slot_start,
                quantity=event.quantity,
                sold_event_id=event.event_id,
            )
            self._append_evidence(event.zone, event)
        elif et is EventType.TICKET_USED:
            ticket = self._tickets.pop(event.correlation_id, None)
            qty = ticket.quantity if ticket is not None else event.quantity
            self._ledger[event.zone] += qty
            self._append_evidence(event.zone, event)
        elif et is EventType.CAMERA_COUNT:
            prev = self._camera.get(event.zone)
            if prev is None or event.occurred_at >= prev[1]:
                self._camera[event.zone] = (event.quantity, event.occurred_at)
            self._append_evidence(event.zone, event)
        elif et in (EventType.PARK_ENTER, EventType.PARK_EXIT):
            sign = +1 if et is EventType.PARK_ENTER else -1
            self._parking[event.zone] = max(0, self._parking[event.zone] + sign * event.quantity)
        else:
            delta = DELTA_TYPES.get(et, 0) * event.quantity
            if delta:
                self._ledger[event.zone] += delta
                self._append_evidence(event.zone, event)
        return IngestResult(True)

    def _append_evidence(self, zone: str, event: Event) -> None:
        self._evidence[zone].append(
            f"#{self._sequence} {event.event_type.value} x{event.quantity} "
            f"({event.source}/{event.event_id}@{event.occurred_at.isoformat()})"
        )

    def _reject(self, event: Event, reason: str) -> None:
        self._rejected.append(
            {
                "sequence": self._sequence,
                "event_id": event.event_id,
                "source": event.source,
                "event_type": event.event_type.value,
                "zone": event.zone,
                "occurred_at": event.occurred_at.isoformat(),
                "reason": reason,
            }
        )

    # ------------------------------------------------------------ 跨日重置
    def _rollover_if_needed(self, now: datetime) -> bool:
        """开园跨日：确定地重置台账与快照、作废旧票。返回是否发生重置。"""
        local = now.astimezone(self._schedule.tz)
        day = local.date().isoformat()
        if self._business_date == day:
            return False
        if local.hour < self._schedule.open_hour:
            # 开园前：保持上一运营日数据，直到新一天开园
            return False
        if not self._business_date:
            self._business_date = day
            return False
        # 新运营日第一次到达：重置当日状态
        self._business_date = day
        new_open = local.replace(hour=self._schedule.open_hour, minute=0, second=0, microsecond=0)
        for zone in self._zones:
            self._ledger[zone] = 0
            self._camera.pop(zone, None)
            self._parking[zone] = 0
            self._evidence[zone].clear()
        # 新运营日：来源在线状态重新起算（设备重新开机会重新上报）
        self._source_state.clear()
        # 未核销且时段早于新一天开园的票作废；未来日预约保留
        self._tickets = {
            cid: t for cid, t in self._tickets.items() if t.slot_start >= new_open
        }
        return True

    # ---------------------------------------------------------------- 读取
    def observe(self, now: datetime) -> OccupancyReport:
        self._rollover_if_needed(now)
        open_now = self._schedule.is_open(now)
        horizon = timedelta(seconds=self._config.prediction_horizon_seconds)
        grace = timedelta(seconds=TICKET_GRACE_SECONDS)

        readings: dict[str, ZoneReading] = {}
        for code, zone in self._zones.items():
            freshness = self._freshness(zone, now, open_now)
            # 只有“曾经上报后失联”（stale）才附加保守缓冲；
            # 从未上报（silent）说明设备未启动而非中断，不加缓冲、仅如实展示。
            stale_weight = sum(1.0 for f in freshness if f.state == "stale") / len(freshness)
            stale_sources = [f.source for f in freshness if f.state == "stale"]

            ledger = max(0, self._ledger[code])
            snapshot_row = self._camera.get(code)
            camera_value = camera_at = None
            anomaly = False
            if snapshot_row is not None:
                value, at = snapshot_row
                age = (now - at).total_seconds()
                limit = self._config.freshness_seconds.get("camera", 300)
                if age <= limit:
                    camera_value, camera_at = value, at
                    anomaly = value > zone.fire_capacity

            # 新鲜度缓冲：来源失联比例越大缓冲越大；闭园期间不加缓冲
            if open_now and stale_sources:
                buffer = round(zone.fire_capacity * self._config.stale_buffer_ratio * stale_weight)
            else:
                buffer = 0

            estimated = max(ledger, camera_value or 0) + buffer

            predicted = 0
            for ticket in self._tickets.values():
                if ticket.zone != code:
                    continue
                if now - grace <= ticket.slot_start <= now + horizon:
                    predicted += ticket.quantity

            readings[code] = ZoneReading(
                zone=code,
                ledger=ledger,
                camera_snapshot=camera_value,
                camera_at=camera_at,
                stale_buffer=buffer,
                estimated=estimated,
                predicted_add=predicted,
                projected=estimated + predicted,
                stale_source_ratio=round(stale_weight, 4),
                freshness=freshness,
                camera_anomaly=anomaly,
                evidence=tuple(self._evidence[code]),
            )

        return OccupancyReport(
            at=now,
            business_date=self._business_date,
            zones=readings,
            parking_inside=dict(self._parking),
            queued_tickets=len(self._tickets),
            rejected=tuple(self._rejected),
        )

    def _freshness(self, zone: Zone, now: datetime, open_now: bool) -> list[SourceFreshness]:
        rows: list[SourceFreshness] = []
        for source in zone.sources:
            state = self._source_state.get((source, zone.code))
            limit = self._config.freshness_seconds.get(source, 300)
            if state is None or state.last_received_at is None:
                rows.append(SourceFreshness(source, "silent", None))
                continue
            age = (now - state.last_received_at).total_seconds()
            if not open_now:
                rows.append(SourceFreshness(source, "fresh", age))
            elif age <= limit:
                rows.append(SourceFreshness(source, "fresh", age))
            else:
                rows.append(SourceFreshness(source, "stale", age))
        return rows

    # ------------------------------------------------------------ 恢复支持
    def replay_from_events(self, events: list[Event]) -> None:
        """按 occurred_at 之外的原始接收顺序重放（序号顺序即接收顺序）。"""
        for event in sorted(events, key=lambda e: (e.sequence, e.occurred_at)):
            now = event.received_at or event.occurred_at
            self.ingest(event, now)

    @property
    def business_date(self) -> str:
        return self._business_date

    @property
    def next_sequence(self) -> int:
        return self._sequence + 1
