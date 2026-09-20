"""决策链回放与确定性校验。

从追加日志重建服务：构造一套全新的领域组件，按日志时间轴顺序重放
事件、接管、封闭，并在每个历史决策点用当时记录的时刻重新评估决策，
与日志中已落盘的决策逐字段比对。

闸机离线、重复事件、乱序事件、跨日闭园等场景都应重算出与当时一致的结果。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .domain import PolicyConfig, Schedule, Zone
from .occupancy import OccupancyEngine
from .overrides import ManualActionSpec, ManualOverride, OverrideMode, OverrideRegistry
from .policy import PolicyEngine
from .serde import event_from_dict
from .service import decision_to_dict
from .store import JournalStore
from .topology import Closure, Link, Topology


class ReplayResult:
    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.mismatches: list[dict] = []
        self.events_replayed = 0
        self.duplicates_dropped = 0


def replay(
    zones: list[Zone],
    links: list[Link],
    journal_path: str | Path,
    config: PolicyConfig | None = None,
    schedule: Schedule | None = None,
    compare: bool = True,
) -> ReplayResult:
    config = config or PolicyConfig()
    schedule = schedule or Schedule()

    rows = _read(Path(journal_path))
    historical_decisions = [r["payload"] for r in rows if r["kind"] == "decision"]

    occupancy = OccupancyEngine(zones, config, schedule)
    topology = Topology(links)
    overrides = OverrideRegistry()
    policy = PolicyEngine(config, schedule, topology, overrides)
    policy.bind_zones(zones)
    result = ReplayResult()

    # 事件按接收序号排序（序号即接收顺序）
    events = sorted(
        (event_from_dict(r["payload"]) for r in rows if r["kind"] == "event"),
        key=lambda e: (e.sequence, e.occurred_at),
    )
    # 措施变动按发生时刻排序，重闭园/重接管均可正确复现
    incidents = sorted(
        (r for r in rows if r["kind"] in (
            "override_add", "override_cancel",
            "closure_add", "closure_reopen",
        )),
        key=lambda r: r["payload"]["at"]
        if r["kind"] in ("override_cancel", "closure_reopen")
        else r["payload"]["created_at"],
    )

    event_cursor = 0
    incident_cursor = 0
    for hist in historical_decisions:
        at = datetime.fromisoformat(hist["decision"]["at"])

        while incident_cursor < len(incidents):
            kind, payload = incidents[incident_cursor]["kind"], incidents[incident_cursor]["payload"]
            ts = payload.get("at") or payload.get("created_at")
            if datetime.fromisoformat(ts) > at:
                break
            if kind == "override_add":
                try:
                    overrides.add(
                        ManualOverride(
                            override_id=payload["override_id"], zone=payload["zone"],
                            mode=OverrideMode(payload["mode"]), reason=payload["reason"],
                            owner=payload["owner"],
                            created_at=datetime.fromisoformat(payload["created_at"]),
                            expires_at=datetime.fromisoformat(payload["expires_at"]),
                            actions=tuple(
                                ManualActionSpec(a["action_type"], dict(a.get("params", {})))
                                for a in payload.get("actions", [])
                            ),
                        ),
                        datetime.fromisoformat(payload["created_at"]),
                    )
                except ValueError:
                    pass
            elif kind == "override_cancel":
                overrides.cancel(payload["override_id"])
            elif kind == "closure_add":
                topology.close(
                    Closure(
                        link_key=tuple(payload["link_key"]), reason=payload["reason"],
                        owner=payload["owner"],
                        expires_at=datetime.fromisoformat(payload["expires_at"]),
                        created_at=datetime.fromisoformat(payload["created_at"]),
                    )
                )
            elif kind == "closure_reopen":
                topology.reopen(tuple(payload["link_key"]))
            incident_cursor += 1

        # 到期状态同样按当时时刻结算
        overrides.expire(at)
        topology.expire(at)

        while event_cursor < len(events):
            event = events[event_cursor]
            received = event.received_at or event.occurred_at
            if received > at:
                break
            ingest = occupancy.ingest(event, received)
            result.events_replayed += 1
            if not ingest.accepted and ingest.reason == "duplicate":
                result.duplicates_dropped += 1
            event_cursor += 1

        report = occupancy.observe(at)
        decision = policy.evaluate(report, at)
        recomputed = decision_to_dict(decision)
        result.decisions.append(recomputed)

        if compare:
            mismatch = _diff(hist["decision"], recomputed)
            if mismatch:
                result.mismatches.append(
                    {"decision_id": hist["decision"]["decision_id"], "diff": mismatch}
                )

    return result


def _read(path: Path) -> list[dict]:
    store = JournalStore(path)
    rows = list(store.read_all())
    store.close()
    return rows


def _canonical(value):
    import json

    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str))


def _diff(expected: dict, actual: dict) -> list[str]:
    """比较两份决策的实质内容（参数、分级、依据），忽略易变的展示顺序。"""
    diffs: list[str] = []
    if set(expected) != set(actual):
        diffs.append(f"键集合不同 {set(expected) ^ set(actual)}")

    if _canonical(expected.get("risks")) != _canonical(actual.get("risks")):
        diffs.append("风险分级不同")

    ea, aa = expected.get("actions", []), actual.get("actions", [])
    if len(ea) != len(aa):
        diffs.append(f"动作数量不同 {len(ea)} != {len(aa)}")
    else:
        for i, (e, a) in enumerate(zip(ea, aa)):
            if (e["type"], e["zone"], e["basis"]) != (a["type"], a["zone"], a["basis"]):
                diffs.append(f"动作[{i}]类型/区域/依据不同: "
                             f"{e['type']}/{e['zone']}/{e['basis']} != "
                             f"{a['type']}/{a['zone']}/{a['basis']}")
            if _canonical(e.get("params")) != _canonical(a.get("params")):
                diffs.append(f"动作[{i}]参数不同: {e.get('params')} != {a.get('params')}")

    if _canonical(expected.get("suppressed")) != _canonical(actual.get("suppressed")):
        diffs.append("被抑制动作不同")
    if _canonical(expected.get("ramps")) != _canonical(actual.get("ramps")):
        diffs.append("斜坡参数不同")
    if sorted(expected.get("notes", [])) != sorted(actual.get("notes", [])):
        diffs.append(f"备注不同: {expected.get('notes')} != {actual.get('notes')}")
    if expected.get("active_overrides") != actual.get("active_overrides"):
        diffs.append("生效接管不同")
    return diffs
