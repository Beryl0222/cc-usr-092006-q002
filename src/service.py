"""客流联动服务门面。

职责：
* 统一分配事件接收序号与接收时刻，写入追加日志；
* 驱动策略 tick：到期清理 → 占用观测 → 决策 → 落盘；
* 人工接管/临时封闭的登记（强制期限、理由、责任人）与执行回执；
* 重启后从日志恢复：占用台账由事件重放得到，措施/接管/封闭/斜坡恢复；
* 向值班室暴露当前风险、数据新鲜度、动作依据与回执。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .clock import Clock, SystemClock, ensure_aware
from .domain import PolicyConfig, Schedule, Zone
from .events import Event, EventType
from .occupancy import OccupancyEngine, OccupancyReport
from .overrides import (
    ManualActionSpec,
    ManualOverride,
    OverrideMode,
    OverrideRegistry,
)
from .policy import Action, Decision, PolicyEngine
from .serde import event_from_dict, event_to_dict
from .store import JournalStore
from .topology import Closure, Link, Topology


class FlowService:
    def __init__(
        self,
        zones: list[Zone],
        links: list[Link],
        journal_path: str | Path,
        clock: Clock | None = None,
        config: PolicyConfig | None = None,
        schedule: Schedule | None = None,
        entry_zones: tuple[str, ...] = ("gate-east",),
        exit_zones: tuple[str, ...] = ("gate-east",),
    ) -> None:
        self._zones = zones
        self._clock = clock or SystemClock()
        self._config = config or PolicyConfig()
        self._schedule = schedule or Schedule()
        self._topology = Topology(links)
        self._overrides = OverrideRegistry()
        self._occupancy = OccupancyEngine(zones, self._config, self._schedule)
        self._policy = PolicyEngine(
            self._config, self._schedule, self._topology, self._overrides,
            entry_zones=entry_zones, exit_zones=exit_zones,
        )
        self._policy.bind_zones(zones)
        self._store = JournalStore(journal_path)
        self._seq = 0
        self._receipts: dict[str, dict] = {}   # 动作签名 -> 最新回执
        self._last_decision: Decision | None = None
        self._last_report: OccupancyReport | None = None
        self._recover()

    # ============================================================ 事件摄取
    def ingest(
        self,
        event_id: str,
        event_type: str | EventType,
        source: str,
        zone: str,
        occurred_at: datetime,
        quantity: int = 1,
        slot_start: datetime | None = None,
        correlation_id: str | None = None,
        target_zone: str | None = None,
    ) -> dict:
        now = self._clock.now()
        self._seq += 1
        event = Event(
            event_id=event_id,
            event_type=EventType(event_type) if isinstance(event_type, str) else event_type,
            source=source,
            zone=zone,
            occurred_at=ensure_aware(occurred_at),
            quantity=quantity,
            slot_start=slot_start,
            correlation_id=correlation_id,
            target_zone=target_zone,
            received_at=now,
            sequence=self._seq,
        )
        result = self._occupancy.ingest(event, now)
        self._store.append("event", event_to_dict(event))
        return {"accepted": result.accepted, "reason": result.reason, "sequence": self._seq}

    # ============================================================ 人工接管
    def add_override(
        self,
        override_id: str,
        zone: str,
        reason: str,
        owner: str,
        expires_at: datetime,
        mode: str = OverrideMode.MANUAL_ACTIONS.value,
        actions: list[dict] | None = None,
    ) -> dict:
        now = self._clock.now()
        override = ManualOverride(
            override_id=override_id,
            zone=zone,
            mode=OverrideMode(mode),
            reason=reason,
            owner=owner,
            created_at=now,
            expires_at=ensure_aware(expires_at),
            actions=tuple(ManualActionSpec(a["action_type"], dict(a.get("params", {})))
                          for a in (actions or [])),
        )
        self._overrides.add(override, now)
        payload = {
            "override_id": override_id, "zone": zone, "mode": mode,
            "reason": reason, "owner": owner,
            "created_at": now.isoformat(), "expires_at": override.expires_at.isoformat(),
            "actions": actions or [],
        }
        self._store.append("override_add", payload)
        return payload

    def cancel_override(self, override_id: str) -> None:
        now = self._clock.now()
        removed = self._overrides.cancel(override_id)
        if removed is None:
            raise KeyError(f"无此接管: {override_id}")
        self._store.append("override_cancel",
                           {"override_id": override_id, "at": now.isoformat()})

    # ============================================================ 临时封闭
    def add_closure(
        self, link_key: tuple[str, str, str], reason: str, owner: str,
        expires_at: datetime,
    ) -> dict:
        now = self._clock.now()
        closure = Closure(
            link_key=tuple(link_key), reason=reason, owner=owner,
            expires_at=ensure_aware(expires_at), created_at=now,
        )
        self._topology.close(closure)
        payload = {
            "link_key": list(closure.link_key), "reason": reason, "owner": owner,
            "expires_at": closure.expires_at.isoformat(), "created_at": now.isoformat(),
        }
        self._store.append("closure_add", payload)
        return payload

    def reopen(self, link_key: tuple[str, str, str]) -> None:
        now = self._clock.now()
        self._topology.reopen(tuple(link_key))
        self._store.append("closure_reopen",
                           {"link_key": list(link_key), "at": now.isoformat()})

    # ============================================================ 执行回执
    def record_receipt(
        self, action_id: str, status: str, operator: str, note: str = ""
    ) -> dict:
        """登记动作执行回执。action_id 需来自最近一次决策。"""
        now = self._clock.now()
        if status not in ("acknowledged", "executed", "failed", "skipped"):
            raise ValueError("未知回执状态")
        action = self._find_action(action_id)
        receipt = {
            "action_id": action_id,
            "signature": _action_signature(action),
            "status": status,
            "operator": operator,
            "note": note,
            "at": now.isoformat(),
        }
        self._receipts[receipt["signature"]] = receipt
        self._store.append("receipt", receipt)
        return receipt

    def _find_action(self, action_id: str) -> Action:
        if self._last_decision is None:
            raise KeyError("尚无决策")
        for action in self._last_decision.actions:
            if action.action_id == action_id:
                return action
        raise KeyError(f"最近决策中无此动作: {action_id}")

    # ============================================================ 决策 tick
    def tick(self) -> dict:
        now = self._clock.now()
        # 到期清理（派生事件，仅用于决策链留痕）
        for override in self._overrides.expire(now):
            self._store.append("override_expired",
                               {"override_id": override.override_id, "at": now.isoformat()})
        for closure in self._topology.expire(now):
            self._store.append("closure_expired",
                               {"link_key": list(closure.link_key), "at": now.isoformat()})

        report = self._occupancy.observe(now)
        decision = self._policy.evaluate(report, now)
        self._last_report = report
        self._last_decision = decision

        record = {
            "decision": decision_to_dict(decision),
            "readings": readings_to_dict(report),
            "engine_state": self._policy.snapshot_state(),
        }
        self._store.append("decision", record)
        return self.decision_view(decision, report)

    # ============================================================ 值班视图
    def status(self) -> dict:
        now = self._clock.now()
        report = self._occupancy.observe(now)
        decision = self._last_decision
        zones_view = {}
        for code, reading in report.zones.items():
            live_risk = self._policy.classify_risk(reading)
            zones_view[code] = {
                "name": _zone_name(self._zones, code),
                "estimated": reading.estimated,
                "ledger": reading.ledger,
                "camera_snapshot": reading.camera_snapshot,
                "stale_buffer": reading.stale_buffer,
                "predicted_add": reading.predicted_add,
                "projected": reading.projected,
                "risk": live_risk.level.value,
                "ratio_estimated": live_risk.ratio_estimated,
                "freshness": [
                    {"source": f.source, "state": f.state,
                     "age_seconds": None if f.age_seconds is None else round(f.age_seconds, 1)}
                    for f in reading.freshness
                ],
            }
        return {
            "at": now.isoformat(),
            "business_date": report.business_date,
            "open": self._schedule.is_open(now),
            "return_guard_window": _in_return_window(self._schedule,
                                                     self._config.return_guard_minutes, now),
            "zones": zones_view,
            "parking_inside": report.parking_inside,
            "queued_tickets": report.queued_tickets,
            "active_overrides": [
                {
                    "override_id": o.override_id, "zone": o.zone, "mode": o.mode.value,
                    "reason": o.reason, "owner": o.owner,
                    "expires_at": o.expires_at.isoformat(),
                }
                for o in self._overrides.all_active(now)
            ],
            "closures": [
                {
                    "link_key": list(c.link_key), "reason": c.reason, "owner": c.owner,
                    "expires_at": c.expires_at.isoformat(),
                }
                for c in self._topology.closures(now)
            ],
            "latest_decision_id": decision.decision_id if decision else None,
            "actions": self.decision_view(decision, report)["actions"] if decision else [],
            "rejected_events": list(report.rejected),
        }

    def decision_view(self, decision: Decision, report: OccupancyReport | None = None) -> dict:
        report = report or self._last_report
        return {
            "decision_id": decision.decision_id,
            "at": decision.at.isoformat(),
            "business_date": decision.business_date,
            "notes": list(decision.notes),
            "ramps": decision.ramps,
            "active_overrides": list(decision.active_overrides),
            "actions": [
                {
                    "action_id": a.action_id,
                    "type": a.action_type.value,
                    "zone": a.zone,
                    "params": a.params,
                    "basis": a.basis,
                    "rationale": list(a.rationale),
                    "keep_special_lanes": a.keep_special_lanes,
                    "return_priority": a.return_priority,
                    "receipt": self._receipts.get(_action_signature(a)),
                }
                for a in decision.actions
            ],
            "suppressed": [
                {"type": s.action_type, "zone": s.zone, "reason": s.reason,
                 "override_id": s.override_id}
                for s in decision.suppressed
            ],
            "evidence": {
                code: list(report.zones[code].evidence)
                for code in (report.zones if report else [])
            },
        }

    # ============================================================ 决策链
    def chain(self, around_decision_id: str | None = None, window: int = 5) -> dict:
        """返回某次决策前后完整决策链：决策、读数证据、回执、接管/封闭变动。"""
        decisions: list[dict] = []
        events_log: list[dict] = []
        for record in self._store.read_all():
            kind, payload = record["kind"], record["payload"]
            if kind == "decision":
                decisions.append(payload)
            elif kind in ("override_add", "override_cancel", "override_expired",
                          "closure_add", "closure_reopen", "closure_expired", "receipt"):
                events_log.append({"kind": kind, "payload": payload})
        if not decisions:
            return {"decisions": [], "incidents": events_log}
        idx = len(decisions) - 1
        if around_decision_id is not None:
            for i, rec in enumerate(decisions):
                if rec["decision"]["decision_id"] == around_decision_id:
                    idx = i
                    break
        lo = max(0, idx - window)
        hi = min(len(decisions), idx + window + 1)
        return {
            "anchor": decisions[idx]["decision"]["decision_id"],
            "window": [lo, hi - 1],
            "decisions": decisions[lo:hi],
            "incidents": events_log,
        }

    @property
    def store(self) -> JournalStore:
        return self._store

    def close(self) -> None:
        self._store.close()

    # ============================================================ 恢复
    def _recover(self) -> None:
        events: list[Event] = []
        canceled_overrides: set[str] = set()
        reopened: set[tuple] = set()
        override_rows: list[dict] = []
        closure_rows: list[dict] = []
        latest_state: dict | None = None

        for record in self._store.read_all():
            kind, payload = record["kind"], record["payload"]
            if kind == "event":
                event = event_from_dict(payload)
                events.append(event)
                self._seq = max(self._seq, event.sequence)
            elif kind == "override_add":
                override_rows.append(payload)
            elif kind == "override_cancel":
                canceled_overrides.add(payload["override_id"])
            elif kind == "closure_add":
                closure_rows.append(payload)
            elif kind == "closure_reopen":
                reopened.add(tuple(payload["link_key"]))
            elif kind == "receipt":
                self._receipts[payload["signature"]] = payload
            elif kind == "decision":
                latest_state = payload.get("engine_state")

        # 占用台账：按接收顺序重放全部事件（去重/迟到规则原样生效）
        for event in sorted(events, key=lambda e: (e.sequence, e.occurred_at)):
            self._occupancy.ingest(event, event.received_at or event.occurred_at)

        now = self._clock.now()
        active_override_rows = [r for r in override_rows if r["override_id"] not in canceled_overrides]
        self._overrides.restore(active_override_rows, now)
        active_closure_rows = [r for r in closure_rows if tuple(r["link_key"]) not in reopened]
        self._topology.restore(active_closure_rows, now)
        if latest_state:
            self._policy.restore_state(
                latest_state["decision_index"],
                latest_state["was_suppressed"],
                latest_state["ramps"],
                latest_state.get("pending_ramps", []),
            )
        # 重启后立即给一版观测，但不产生新决策编号；首个 tick 会续号
        self._last_report = self._occupancy.observe(now)


# ============================================================== 序列化
def _action_signature(action: Action) -> str:
    params = {k: v for k, v in action.params.items() if k not in ("headroom",)}
    return json.dumps(
        {"t": action.action_type.value, "z": action.zone, "p": params},
        sort_keys=True, ensure_ascii=False,
    )


def _zone_name(zones: list[Zone], code: str) -> str:
    for zone in zones:
        if zone.code == code:
            return zone.name
    return code


def _in_return_window(schedule: Schedule, guard_minutes: int, now: datetime) -> bool:
    from datetime import timedelta

    local = now.astimezone(schedule.tz)
    close_today = local.replace(hour=schedule.close_hour, minute=0, second=0, microsecond=0)
    return close_today - timedelta(minutes=guard_minutes) <= now <= close_today + timedelta(minutes=60)


def decision_to_dict(decision: Decision) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "at": decision.at.isoformat(),
        "business_date": decision.business_date,
        "risks": {
            code: {
                "level": risk.level.value,
                "ratio_estimated": risk.ratio_estimated,
                "ratio_projected": risk.ratio_projected,
                "fire_limit": risk.fire_limit,
                "stale_source_ratio": risk.stale_source_ratio,
            }
            for code, risk in decision.risks.items()
        },
        "actions": [
            {
                "action_id": a.action_id,
                "type": a.action_type.value,
                "zone": a.zone,
                "params": a.params,
                "basis": a.basis,
                "rationale": list(a.rationale),
                "keep_special_lanes": a.keep_special_lanes,
                "return_priority": a.return_priority,
            }
            for a in decision.actions
        ],
        "suppressed": [
            {"type": s.action_type, "zone": s.zone, "reason": s.reason,
             "override_id": s.override_id}
            for s in decision.suppressed
        ],
        "active_overrides": list(decision.active_overrides),
        "ramps": decision.ramps,
        "notes": list(decision.notes),
    }


def readings_to_dict(report: OccupancyReport) -> dict[str, Any]:
    return {
        "at": report.at.isoformat(),
        "business_date": report.business_date,
        "zones": {
            code: {
                "estimated": r.estimated,
                "ledger": r.ledger,
                "camera_snapshot": r.camera_snapshot,
                "stale_buffer": r.stale_buffer,
                "predicted_add": r.predicted_add,
                "projected": r.projected,
                "stale_source_ratio": r.stale_source_ratio,
                "camera_anomaly": r.camera_anomaly,
                "evidence": list(r.evidence),
                "freshness": [
                    {"source": f.source, "state": f.state,
                     "age_seconds": None if f.age_seconds is None else round(f.age_seconds, 1)}
                    for f in r.freshness
                ],
            }
            for code, r in report.zones.items()
        },
        "parking_inside": report.parking_inside,
        "queued_tickets": report.queued_tickets,
        "rejected": list(report.rejected),
    }
