"""测试公共构造：固定时钟、临时日志、示例区域与通道。"""

from __future__ import annotations

import tempfile
import os
from datetime import datetime, timezone, timedelta

from src.clock import FixedClock
from src.domain import PolicyConfig, Schedule, Zone, load_zones
from src.topology import Link, load_links
from src.service import FlowService

TZ = timezone(timedelta(hours=8))
DAY1_10 = datetime(2026, 9, 20, 10, 0, tzinfo=TZ)


def zones() -> list[Zone]:
    return load_zones("fixtures/zones.json")


def links():
    return load_links("fixtures/links.json")


def clock(moment: datetime | None = None) -> FixedClock:
    return FixedClock(moment or DAY1_10)


def new_service(cfg: PolicyConfig | None = None, sched: Schedule | None = None,
                moment: datetime | None = None) -> tuple[FlowService, FixedClock, str]:
    d = tempfile.mkdtemp()
    path = os.path.join(d, "journal.jsonl")
    clk = clock(moment)
    svc = FlowService(
        zones(), links(), path, clock=clk,
        config=cfg or PolicyConfig(), schedule=sched or Schedule(),
    )
    return svc, clk, path
