"""把归一化事件集合融合为各区域可信占用量与数据新鲜度。

设计要点：
- 纯函数：给定同一运营日、同一时刻 T、同一批事件，结果唯一，与事件到达先后无关；
  重复事件在日志层按 event_id 去重后，这里看到的就是集合。
- 流量口径（闸机/摆渡计数）会漂移，快照口径（图像计数）会间歇。两者融合：
  有新鲜快照以快照为准；快照过期则用其后的流量增量修正；全无数据则流量累加；
  任何来源都缺失时占用量为 None（未知），策略层按失效安全处理，而不是猜零。
- lead（停车入场/分时票）不计入当前占用，只形成未来到达压力。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .domain import Site
from .events import Event, FLOW, LEAD, SNAPSHOT

FRESH = "fresh"
STALE = "stale"
OFFLINE = "offline"  # 本运营日内从未收到

HIGH = "high"      # 新鲜快照
MEDIUM = "medium"  # 流量口径 / 快照+流量融合
LOW = "low"        # 快照与流量严重背离，或快照过期较久
NONE = "none"      # 无任何数据

# 快照与流量口径背离超过该比例时降低置信度并在依据中提示
DIVERGENCE = 0.25
# 超过新鲜度窗口多少倍判定为低置信（仍可参考，不直接判离线）
LOW_FACTOR = 3.0


@dataclass(frozen=True)
class SourceFreshness:
    source: str
    status: str
    last_at: datetime | None
    age_seconds: int | None
    staleness_seconds: int


@dataclass(frozen=True)
class ZoneOccupancy:
    zone: str
    occ: int | None
    basis: str  # snapshot | fused | flow | none
    basis_detail: str
    confidence: str
    flow_count: int
    snapshot_value: int | None
    snapshot_at: datetime | None
    inbound_pressure: int  # 未来 lead_seconds 窗口内预计到达
    sources: tuple[SourceFreshness, ...]

    @property
    def any_source_offline(self) -> bool:
        return any(s.status == OFFLINE for s in self.sources)


@dataclass(frozen=True)
class OccupancyView:
    day: str
    now: datetime
    zones: dict[str, ZoneOccupancy]

    def get(self, zone: str) -> ZoneOccupancy:
        return self.zones[zone]


def _freshness(site: Site, source: str, events: list[Event], now: datetime) -> SourceFreshness:
    profile = site.sources[source]
    if not events:
        return SourceFreshness(source, OFFLINE, None, None, profile.staleness_seconds)
    last_at = max(e.occurred_at for e in events)
    age = int((now - last_at).total_seconds())
    if age <= profile.staleness_seconds:
        status = FRESH
    elif age <= profile.staleness_seconds * LOW_FACTOR:
        status = STALE
    else:
        status = OFFLINE
    return SourceFreshness(source, status, last_at, max(age, 0), profile.staleness_seconds)


def compute_occupancy(site: Site, events: list[Event], day: str, now: datetime) -> OccupancyView:
    """对指定运营日 day、评估时刻 now 计算占用量。仅采用 occurred_at <= now 的事件。

    入口按 event_id 防御性去重（同一 ID 取最早接收的一条），因此即使上游
    传入重复事件，融合结果也保持幂等，不依赖调用方先去重。
    """
    unique: dict[str, Event] = {}
    for e in events:
        prev = unique.get(e.event_id)
        if prev is None or e.received_at < prev.received_at:
            unique[e.event_id] = e
    events = list(unique.values())

    result: dict[str, ZoneOccupancy] = {}

    for zone in site.zones:
        by_source: dict[str, list[Event]] = {s: [] for s in zone.sources}
        for e in events:
            if e.zone != zone.code or e.operating_day != day or e.occurred_at > now:
                continue
            if e.source in by_source:
                by_source[e.source].append(e)

        flow_count = 0
        flow_span_seconds = 0  # 流量序列已覆盖的时间跨度，过短则不做背离判定
        snapshot: tuple[int, datetime, str] | None = None
        inbound_pressure = 0
        freshness: list[SourceFreshness] = []

        flow_times: list[datetime] = []
        for source, evs in by_source.items():
            profile = site.sources[source]
            freshness.append(_freshness(site, source, evs, now))

            if profile.kind == "flow":
                flow_evs = [e for e in evs if e.etype == FLOW]
                flow_count += sum(e.value for e in flow_evs)
                if flow_evs:
                    flow_times.append(min(e.occurred_at for e in flow_evs))
            elif profile.kind == "occupancy":
                snaps = [e for e in evs if e.etype == SNAPSHOT]
                if snaps:
                    latest = max(snaps, key=lambda e: e.occurred_at)
                    if snapshot is None or latest.occurred_at > snapshot[1]:
                        snapshot = (latest.value, latest.occurred_at, latest.source)
            # 前瞻压力：任何配了 lead_seconds 的来源，其 LEAD 事件按窗口计入
            if profile.lead_seconds > 0:
                horizon = timedelta(seconds=profile.lead_seconds)
                for e in evs:
                    if e.etype == LEAD and e.occurred_at <= now < e.occurred_at + horizon:
                        inbound_pressure += max(e.value, 0)

        if flow_times:
            flow_span_seconds = int((now - min(flow_times)).total_seconds())
        # 流量来源需要至少覆盖其一个新鲜度窗口，累加值才足以代表全天净在场
        flow_window_min = min(
            (site.sources[s].staleness_seconds for s in zone.sources
             if site.sources[s].kind == "flow"),
            default=0,
        )
        flow_comparable = flow_span_seconds >= flow_window_min

        flow_count = max(flow_count, 0)
        occ: int | None
        basis: str
        detail: str
        confidence: str

        if snapshot is not None:
            snap_value, snap_at, snap_source = snapshot
            age = (now - snap_at).total_seconds()
            profile_window = site.sources[snap_source].staleness_seconds
            flow_after = 0
            # 快照后的流量增量用于修正（同一区域的全部流量来源）
            for source, evs in by_source.items():
                if site.sources[source].kind == "flow":
                    flow_after += sum(
                        e.value for e in evs if e.etype == FLOW and e.occurred_at > snap_at
                    )
            flow_after = max(flow_after, 0)
            if age <= profile_window:
                occ, basis = snap_value, "snapshot"
                detail = f"图像快照@{snap_at.isoformat()} 来源={snap_source}"
                confidence = HIGH
                # 流量口径已覆盖足够长的时间跨度、且与快照背离过大时才降级，
                # 避免运营刚开始、只有少量增量时误判设备异常。
                if (flow_comparable and flow_count > 0
                        and abs(flow_count - snap_value) > DIVERGENCE * max(snap_value, 1)):
                    confidence = LOW
                    detail += f"；与流量计数 {flow_count} 背离>{int(DIVERGENCE*100)}%"
            else:
                occ, basis = max(snap_value + flow_after, 0), "fused"
                detail = (
                    f"过期快照 {snap_value}@{snap_at.isoformat()} + 其后流量增量 {flow_after}"
                )
                confidence = MEDIUM if age <= profile_window * LOW_FACTOR else LOW
        elif flow_count > 0 or any(s.status != OFFLINE for s in freshness if site.sources[s.source].kind == "flow"):
            occ, basis, confidence = flow_count, "flow", MEDIUM
            detail = "闸机/摆渡进出流量累加"
        else:
            occ, basis, confidence = None, "none", NONE
            detail = "无任何计数来源数据"

        result[zone.code] = ZoneOccupancy(
            zone=zone.code,
            occ=occ,
            basis=basis,
            basis_detail=detail,
            confidence=confidence,
            flow_count=flow_count,
            snapshot_value=snapshot[0] if snapshot else None,
            snapshot_at=snapshot[1] if snapshot else None,
            inbound_pressure=inbound_pressure,
            sources=tuple(freshness),
        )

    return OccupancyView(day=day, now=now, zones=result)
