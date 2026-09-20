"""事件与领域对象的 JSON 序列化（持久化日志与接口共用）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .events import Event, EventType


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def event_to_dict(event: Event) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "source": event.source,
        "zone": event.zone,
        "occurred_at": _dt(event.occurred_at),
        "quantity": event.quantity,
        "slot_start": _dt(event.slot_start),
        "correlation_id": event.correlation_id,
        "target_zone": event.target_zone,
        "received_at": _dt(event.received_at),
        "sequence": event.sequence,
    }


def event_from_dict(row: dict[str, Any]) -> Event:
    return Event(
        event_id=row["event_id"],
        event_type=EventType(row["event_type"]),
        source=row["source"],
        zone=row["zone"],
        occurred_at=_parse(row["occurred_at"]),
        quantity=int(row.get("quantity", 1)),
        slot_start=_parse(row.get("slot_start")),
        correlation_id=row.get("correlation_id"),
        target_zone=row.get("target_zone"),
        received_at=_parse(row.get("received_at")),
        sequence=int(row.get("sequence", 0)),
    )
