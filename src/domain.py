"""景区区域、通道与数据源的基础资料读取。"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path


@dataclass(frozen=True)
class Zone:
    code: str
    name: str
    fire_capacity: int
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Passage:
    code: str
    a: str
    b: str
    special: bool  # 特殊人群/返程保障专用通道


@dataclass(frozen=True)
class ShuttleLine:
    code: str
    name: str
    stations: tuple[str, ...]
    normal_per_interval: int
    boost_per_interval: int
    interval_seconds: int


@dataclass(frozen=True)
class SourceProfile:
    code: str
    kind: str  # flow=进出计数 occupancy=区域人数快照 lead=前瞻压力（停车/分时票）
    staleness_seconds: int
    lead_seconds: int = 0


@dataclass(frozen=True)
class Recovery:
    step_seconds: int
    min_release: int
    release_steps: int


@dataclass(frozen=True)
class Site:
    zones: tuple[Zone, ...]
    passages: tuple[Passage, ...]
    shuttles: tuple[ShuttleLine, ...]
    sources: dict[str, SourceProfile]
    thresholds: dict[str, float]
    recovery: Recovery
    entrances: frozenset[str]
    opening_time: str  # HH:MM
    closing_time: str  # HH:MM
    return_guarantee_seconds: int

    PHASE_CLOSED = "closed"          # 当日开园前
    PHASE_OPEN = "open"
    PHASE_CLOSING = "closing"        # 闭园时刻起的返程保障期
    PHASE_AFTER = "closed_after"     # 返程保障期结束

    def phase(self, now) -> str:
        """运营阶段（按本地时区的当日时刻）。"""
        from .events import LOCAL_TZ
        local = now.astimezone(LOCAL_TZ)
        oh, om = (int(x) for x in self.opening_time.split(":"))
        ch, cm = (int(x) for x in self.closing_time.split(":"))
        open_at = local.replace(hour=oh, minute=om, second=0, microsecond=0)
        close_at = local.replace(hour=ch, minute=cm, second=0, microsecond=0)
        guarantee_end = close_at + timedelta(seconds=self.return_guarantee_seconds)
        if now < open_at:
            return self.PHASE_CLOSED
        if now < close_at:
            return self.PHASE_OPEN
        if now <= guarantee_end:
            return self.PHASE_CLOSING
        return self.PHASE_AFTER

    def zone(self, code: str) -> Zone:
        for zone in self.zones:
            if zone.code == code:
                return zone
        raise KeyError(code)

    def neighbors(self, code: str) -> list[tuple[str, str]]:
        """返回 (相邻区域, 通道代码)，无向。"""
        out: list[tuple[str, str]] = []
        for p in self.passages:
            if p.a == code:
                out.append((p.b, p.code))
            elif p.b == code:
                out.append((p.a, p.code))
        return out

    def route(self, src: str, dst: str, blocked: frozenset[str] = frozenset(),
              allow_special: bool = False) -> list[str] | None:
        """步行最短路（BFS），避开已封闭通道；None 表示当前无可达步行路径。

        allow_special=False 时特殊人群/返程专用通道不参与常规分流；
        闭园返程疏散允许使用。
        """
        if src == dst:
            return []
        queue: deque[tuple[str, list[str]]] = deque([(src, [])])
        seen = {src}
        while queue:
            node, path = queue.popleft()
            for nxt, passage in self.neighbors(node):
                p = next(p for p in self.passages if p.code == passage)
                if passage in blocked or (p.special and not allow_special) or nxt in seen:
                    continue
                path2 = path + [passage]
                if nxt == dst:
                    return path2
                seen.add(nxt)
                queue.append((nxt, path2))
        return None

    def exits(self) -> list[str]:
        """返程出口：与特殊通道相连、或摆渡可达集散广场的区域视为可返程节点，此处取资料约定。"""
        special_nodes = set()
        for p in self.passages:
            if p.special:
                special_nodes.add(p.a)
                special_nodes.add(p.b)
        for line in self.shuttles:
            special_nodes.add(line.stations[-1])
        return sorted(special_nodes)


def load_zones(path: str | Path) -> list[Zone]:
    """读取区域容量，拒绝缺少消防上限或数据来源的记录。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    zones = [Zone(item["code"], item["name"], item["fire_capacity"], tuple(item["sources"])) for item in payload]
    if any(not zone.code or zone.fire_capacity <= 0 or not zone.sources for zone in zones):
        raise ValueError("区域基础资料不完整")
    return zones


def load_site(zones_path: str | Path, site_path: str | Path) -> Site:
    """读取并校验完整场地资料：引用完整性、阈值顺序、运力合理性。"""
    zones = tuple(load_zones(zones_path))
    payload = json.loads(Path(site_path).read_text(encoding="utf-8"))
    zone_codes = {z.code for z in zones}

    passages = []
    for item in payload["passages"]:
        if item["from"] not in zone_codes or item["to"] not in zone_codes:
            raise ValueError(f"通道引用了不存在的区域: {item['code']}")
        passages.append(Passage(item["code"], item["from"], item["to"], bool(item.get("special", False))))

    shuttles = []
    for item in payload["shuttle_lines"]:
        stations = tuple(item["stations"])
        if any(s not in zone_codes for s in stations) or len(stations) < 2:
            raise ValueError(f"摆渡线路站点无效: {item['code']}")
        if item["boost_per_interval"] < item["normal_per_interval"]:
            raise ValueError(f"摆渡增能运力不得低于常规运力: {item['code']}")
        shuttles.append(
            ShuttleLine(
                item["code"], item["name"], stations,
                item["normal_per_interval"], item["boost_per_interval"], item["interval_seconds"],
            )
        )

    sources: dict[str, SourceProfile] = {}
    for code, item in payload["sources"].items():
        sources[code] = SourceProfile(
            code, item["kind"], int(item["staleness_seconds"]), int(item.get("lead_seconds", 0))
        )

    known_sources = set(sources)
    for zone in zones:
        unknown = set(zone.sources) - known_sources
        if unknown:
            raise ValueError(f"区域 {zone.code} 引用了未知数据源: {sorted(unknown)}")

    thresholds = payload["thresholds"]
    if not (0 < thresholds["yellow"] < thresholds["orange"] < thresholds["red"] <= 1):
        raise ValueError("风险阈值必须满足 0 < yellow < orange < red <= 1")

    rec = payload["recovery"]
    if rec["step_seconds"] <= 0 or rec["release_steps"] < 1:
        raise ValueError("恢复斜坡参数无效")

    entrances = frozenset(payload["entrances"])
    if not entrances <= zone_codes:
        raise ValueError("入口引用了不存在的区域")

    closure = payload["closure"]
    for key in ("opening_time", "closing_time"):
        hh, mm = closure[key].split(":")
        if not (0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ValueError(f"{key} 格式无效")
    if closure["opening_time"] >= closure["closing_time"]:
        raise ValueError("开园时间必须早于闭园时间")

    return Site(
        zones=zones,
        passages=tuple(passages),
        shuttles=tuple(shuttles),
        sources=sources,
        thresholds=thresholds,
        recovery=Recovery(
            rec["step_seconds"], rec["min_release"], rec["release_steps"]
        ),
        entrances=entrances,
        opening_time=closure["opening_time"],
        closing_time=closure["closing_time"],
        return_guarantee_seconds=int(closure["return_guarantee_seconds"]),
    )
