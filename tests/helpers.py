"""测试共享辅助：装载示例场地、便捷构造事件。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import load_site
from src.events import normalize

ROOT = Path(__file__).resolve().parents[1]
ZONES = ROOT / "fixtures" / "zones.json"
SITE = ROOT / "fixtures" / "site.json"

# 固定基准时刻：UTC 10:00 = 本地（东八区）18:00，处于运营时段
BASE = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
DAY = "2026-09-20"


def make_site():
    return load_site(ZONES, SITE)


def at(minute_offset: float = 0, base: datetime = BASE) -> datetime:
    return base + timedelta(minutes=minute_offset)


def iso(minute_offset: float = 0, base: datetime = BASE) -> str:
    return at(minute_offset, base).isoformat()


def event(event_id, source, etype, zone, value, minute_offset=0, received_offset=None):
    site = make_site()
    raw = {
        "event_id": event_id,
        "source": source,
        "type": etype,
        "zone": zone,
        "value": value,
        "occurred_at": iso(minute_offset),
        "received_at": iso(received_offset if received_offset is not None else minute_offset),
    }
    return normalize(raw, frozenset(z.code for z in site.zones), frozenset(site.sources))
