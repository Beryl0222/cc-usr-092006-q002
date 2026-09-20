"""客流联动编排引擎。

把四个纯/半纯层串起来：事件归一化 → 占用量融合 → 接管折叠 → 策略决策，
并负责：
- 按 event_id 去重（重复投递、乱序到达都得到同一事件集合）；
- 每轮评估落一条 tick 快照（含动作依据/风险/限流状态）；
- 执行回执生命周期（pending → ack → executed / failed）；
- 重启时从 Journal 重放恢复正在执行的措施与限流斜坡状态；
- 跨运营日：限流状态归零，闭园后的保障动作只取决于当日剩余事件。

引擎不内置时钟：evaluate(now) 由调用方（值班接口的定时器或测试）给时刻，
因此同一份日志 + 同一时刻永远得到同一结果（确定性、可回放）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from . import overrides as ov_mod
from .domain import Site
from .events import LOCAL_TZ, Event, InvalidEvent, normalize, parse_time, to_dict
from .occupancy import OccupancyView, compute_occupancy
from .overrides import (
    Override, OverrideView, create_override, fold, revoke,
)
from .policy import PolicyState, decide
from .store import Journal

PENDING, ACK, EXECUTED, FAILED = "pending", "ack", "executed", "failed"
_FORWARD = {
    PENDING: {ACK, FAILED},
    ACK: {EXECUTED, FAILED},
    EXECUTED: {ACK},   # 措施延续到新一轮时可再次确认
    FAILED: {ACK},
}

ACTION_PREFIXES = frozenset({
    "throttle", "hold", "holdroute", "divert", "boost", "evac",
    "data-alert", "advise-offpeak", "pre-throttle",
})


@dataclass(frozen=True)
class Evaluation:
    seq: int
    day: str
    now: datetime
    view: OccupancyView
    decision: object
    receipts: dict[str, dict]


class FlowEngine:
    def __init__(self, site: Site, journal: Journal, block_passages: frozenset[str] = frozenset()):
        self.site = site
        self.journal = journal
        self.static_blocks = frozenset(block_passages)
        self._events: dict[str, Event] = {}
        self._overrides: list[Override] = []
        self._receipts: dict[str, dict] = {}
        self._state = PolicyState()
        self._tick_seq = 0
        self._last_day: str | None = None
        self._quarantined = 0

    # ---------- 恢复 ----------
    def restore(self) -> None:
        """从持久记录重放，恢复事件集合、接管、限流状态、回执。"""
        for rec in self.journal.read():
            kind = rec["kind"]
            if kind == "event":
                e = Event(
                    rec["event_id"], rec["source"], rec["type"], rec["zone"],
                    parse_time(rec["occurred_at"]),
                    rec["value"],
                    parse_time(rec["received_at"]),
                )
                self._events[e.event_id] = e
            elif kind == "quarantined":
                self._quarantined += 1
            elif kind == "override":
                o = Override.from_dict(rec["override"])
                self._overrides.append(o)
            elif kind == "receipt":
                self._receipts[f"{rec.get('day', '?')}|{rec['code']}"] = rec
            elif kind == "tick":
                self._tick_seq = max(self._tick_seq, rec["seq"])
                self._last_day = rec["day"]
                self._state = PolicyState.from_dict(rec["decision"]["state"])
        # 接管是否生效不在恢复时判定：每次 evaluate(now) 都按该时刻重新 fold，
        # 因此过期的接管自然失效，无需在这里依赖真实时钟。

    # ---------- 事件接入 ----------
    def ingest(self, raw: dict, now: datetime | None = None) -> Event:
        """校验、去重并持久化一条原始事件。重复 event_id 直接返回既有事件。"""
        now = now or datetime.now(timezone.utc)
        known_zones = frozenset(z.code for z in self.site.zones)
        known_sources = frozenset(self.site.sources)
        try:
            event = normalize(raw, known_zones, known_sources, now)
        except InvalidEvent as exc:
            self.journal.append({
                "kind": "quarantined",
                "raw": raw if _json_safe(raw) else str(raw),
                "error": str(exc),
                "received_at": now.isoformat(),
            })
            self._quarantined += 1
            raise
        if event.event_id in self._events:
            return self._events[event.event_id]  # 重复投递：幂等忽略
        self._events[event.event_id] = event
        self.journal.append({"kind": "event", **to_dict(event)})
        return event

    def ingest_many(self, raws: Iterable[dict], now: datetime | None = None) -> dict:
        ok = dup = bad = 0
        for raw in raws:
            try:
                before = len(self._events)
                self.ingest(raw, now)
                if len(self._events) == before:
                    dup += 1
                else:
                    ok += 1
            except InvalidEvent:
                bad += 1
        return {"accepted": ok, "duplicates": dup, "quarantined": bad}

    # ---------- 人工接管 ----------
    def add_override(self, override_id: str, kind: str, ttl_seconds: int, owner: str,
                     reason: str, params: dict, now: datetime | None = None) -> Override:
        now = now or datetime.now(timezone.utc)
        if any(o.override_id == override_id for o in self._overrides):
            raise ov_mod.OverrideError(f"接管编号已存在: {override_id}")
        ov = create_override(
            override_id, kind, now, ttl_seconds, owner, reason, params,
            known_zones=frozenset(z.code for z in self.site.zones),
            known_passages=frozenset(p.code for p in self.site.passages),
            known_action_prefixes=ACTION_PREFIXES,
        )
        self._overrides.append(ov)
        self.journal.append({"kind": "override", "op": "create", "override": ov.to_dict()})
        return ov

    def revoke_override(self, override_id: str, by: str, reason: str,
                        now: datetime | None = None) -> Override:
        now = now or datetime.now(timezone.utc)
        target = next((o for o in self._overrides
                       if o.override_id == override_id and o.is_effective(now)), None)
        if target is None:
            raise ov_mod.OverrideError(f"没有生效中的接管 {override_id}")
        revoked = revoke(target, now, by, reason)
        self._overrides[self._overrides.index(target)] = revoked
        self.journal.append({"kind": "override", "op": "revoke", "override": revoked.to_dict()})
        return revoked

    def override_view(self, now: datetime) -> OverrideView:
        return fold(self._overrides, now)

    # ---------- 评估 ----------
    def evaluate(self, now: datetime | None = None, persist: bool = True) -> Evaluation:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(LOCAL_TZ).date().isoformat()
        if self._last_day is not None and day != self._last_day:
            # 跨运营日：限流斜坡状态归零，新一天从自动策略起步
            self._state = PolicyState()
        self._last_day = day

        events = [e for e in self._events.values() if e.operating_day == day]
        view = compute_occupancy(self.site, events, day, now)
        ov = fold(self._overrides, now)
        blocked = self.static_blocks | ov.blocked

        decision = decide(
            self.site, view, self._state, now,
            blocked=blocked,
            suppressed=ov.suppressed,
            manual_throttles=ov.manual_throttles,
        )
        self._tick_seq += 1
        seq = self._tick_seq
        if persist:
            self.journal.append({
                "kind": "tick",
                "seq": seq,
                "day": day,
                "now": now.isoformat(),
                "decision": _decision_dict(decision),
            })
        return Evaluation(seq, day, now, view, decision, dict(self._receipts))

    # ---------- 执行回执 ----------
    def post_receipt(self, code: str, status: str, by: str, detail: str = "",
                     now: datetime | None = None, day: str | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        day = day or now.astimezone(LOCAL_TZ).date().isoformat()
        if status not in (ACK, EXECUTED, FAILED):
            raise ValueError(f"非法回执状态: {status}")
        key = f"{day}|{code}"
        prev = self._receipts.get(key)
        prev_status = prev["status"] if prev else PENDING
        allowed = _FORWARD[prev_status]
        if status not in allowed:
            raise ValueError(f"回执不能从 {prev_status} 转为 {status}（动作 {code}）")
        rec = {"kind": "receipt", "day": day, "code": code, "status": status,
               "by": by, "detail": detail, "at": now.isoformat()}
        self._receipts[key] = rec
        self.journal.append(rec)
        return rec

    def measure_status(self, evaluation: Evaluation) -> list[dict]:
        """当前轮次各动作的执行状态（沿动作 code 继承当日上一轮回执）。"""
        out = []
        for a in evaluation.decision.actions:
            r = self._receipts.get(f"{evaluation.day}|{a.code}")
            out.append({
                "code": a.code,
                "type": a.atype,
                "level": a.level,
                "safety": a.safety,
                "params": a.params,
                "reason": a.reason,
                "status": r["status"] if r else PENDING,
                "receipt_by": r["by"] if r else None,
                "receipt_detail": r.get("detail") if r else None,
            })
        return out

    # ---------- 回放 ----------
    def replay(self, day: str) -> dict:
        """重组某运营日拥堵前后的完整决策链。"""
        chain_events, ticks, receipts, overrides_h = [], [], [], []
        for rec in self.journal.read():
            if rec["kind"] == "event" and _day_of_iso(rec["occurred_at"]) == day:
                chain_events.append(rec)
            elif rec["kind"] == "tick" and rec["day"] == day:
                ticks.append(rec)
            elif rec["kind"] == "receipt":
                receipts.append(rec)
            elif rec["kind"] == "override":
                overrides_h.append(rec)
        return {
            "day": day,
            "events": sorted(chain_events, key=lambda r: (r["occurred_at"], r["event_id"])),
            "overrides": overrides_h,
            "ticks": sorted(ticks, key=lambda r: r["seq"]),
            "receipts": sorted(receipts, key=lambda r: r["at"]),
            "quarantined_total": self._quarantined,
        }

    # ---------- 值班快照 ----------
    def status_payload(self, evaluation: Evaluation) -> dict:
        d = evaluation.decision
        zones_out = {}
        for code, zo in evaluation.view.zones.items():
            zr = d.risks[code]
            zones_out[code] = {
                "name": self.site.zone(code).name,
                "risk": zr.level,
                "occ": zo.occ,
                "capacity": zr.capacity,
                "ratio": zr.ratio,
                "basis": zo.basis_detail,
                "confidence": zo.confidence,
                "inbound_pressure": zo.inbound_pressure,
                "freshness": [
                    {"source": s.source, "status": s.status,
                     "age_seconds": s.age_seconds, "limit_seconds": s.staleness_seconds}
                    for s in zo.sources
                ],
            }
        ov = fold(self._overrides, evaluation.now)
        return {
            "day": evaluation.day,
            "now": evaluation.now.isoformat(),
            "phase": d.phase,
            "overall_risk": d.overall,
            "park_closing": d.park_closed,
            "zones": zones_out,
            "blocked_passages": sorted(self.static_blocks | ov.blocked),
            "measures": self.measure_status(evaluation),
            "active_overrides": [o.to_dict() for o in ov.active],
            "notes": list(d.notes),
            "quarantined_total": self._quarantined,
        }


# ---------- 序列化辅助 ----------
def _decision_dict(d) -> dict:
    return {
        "now": d.now.isoformat(),
        "day": d.day,
        "phase": d.phase,
        "overall": d.overall,
        "park_closed": d.park_closed,
        "actions": [a.to_dict() for a in d.actions],
        "risks": {k: {"zone": v.zone, "level": v.level, "ratio": v.ratio,
                      "occ": v.occ, "capacity": v.capacity} for k, v in d.risks.items()},
        "notes": list(d.notes),
        "state": d.state.to_dict(),
    }


def _day_of_iso(iso_str: str) -> str:
    from .events import parse_time
    return parse_time(iso_str).astimezone(LOCAL_TZ).date().isoformat()


def _json_safe(raw: dict) -> bool:
    import json
    try:
        json.dumps(raw, ensure_ascii=False)
        return True
    except (TypeError, ValueError):
        return False
