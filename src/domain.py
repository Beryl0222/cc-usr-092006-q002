"""景区区域和传感来源的基础资料读取。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Zone:
    code: str
    name: str
    fire_capacity: int
    sources: tuple[str, ...]


def load_zones(path: str | Path) -> list[Zone]:
    """读取区域容量，拒绝缺少消防上限或数据来源的记录。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    zones = [Zone(item["code"], item["name"], item["fire_capacity"], tuple(item["sources"])) for item in payload]
    if any(not zone.code or zone.fire_capacity <= 0 or not zone.sources for zone in zones):
        raise ValueError("区域基础资料不完整")
    return zones
