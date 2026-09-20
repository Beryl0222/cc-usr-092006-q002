"""区域间拓扑：步行通道、摆渡运力与临时封闭。

* 步行边有方向与正常通行能力（人/分钟）；
* 摆渡边有核定运力（人/分钟，按班次折算）；
* 临时封闭针对具体边，必须带期限、理由、责任人（与人工接管同规则）。

封闭与运力变化是持久化状态，重启后恢复。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def load_links(path: str | Path) -> list[Link]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    links = [
        Link(
            src=item["src"],
            dst=item["dst"],
            kind=item["kind"],
            capacity_per_min=int(item["capacity_per_min"]),
            code=item.get("code", ""),
        )
        for item in payload
    ]
    if any(link.kind not in ("walk", "shuttle") for link in links):
        raise ValueError("通道类型只能是 walk 或 shuttle")
    return links


@dataclass(frozen=True)
class Link:
    src: str
    dst: str
    kind: str            # "walk" | "shuttle"
    capacity_per_min: int  # 单向每分钟通行/运力人数
    code: str = ""       # 通道/线路编码

    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.code)


@dataclass(frozen=True)
class Closure:
    link_key: tuple[str, str, str]
    reason: str
    owner: str
    expires_at: datetime
    created_at: datetime


class Topology:
    def __init__(self, links: list[Link]) -> None:
        self._links: dict[tuple[str, str, str], Link] = {}
        for link in links:
            if link.key() in self._links:
                raise ValueError(f"重复通道: {link.key()}")
            if link.capacity_per_min <= 0:
                raise ValueError("通行能力必须为正")
            self._links[link.key()] = link
        self._closures: dict[tuple[str, str, str], Closure] = {}

    # ---- 封闭管理 ----
    def close(self, closure: Closure) -> None:
        if not closure.reason or not closure.owner:
            raise ValueError("封闭必须给出理由与责任人")
        if closure.link_key not in self._links:
            raise ValueError(f"未知通道: {closure.link_key}")
        self._closures[closure.link_key] = closure

    def reopen(self, link_key: tuple[str, str, str]) -> None:
        self._closures.pop(link_key, None)

    def expire(self, now: datetime) -> list[Closure]:
        """到期封闭自动解除，返回被解除的封闭记录。"""
        expired = [c for c in self._closures.values() if c.expires_at <= now]
        for c in expired:
            del self._closures[c.link_key]
        return expired

    def is_closed(self, link_key: tuple[str, str, str], now: datetime) -> bool:
        closure = self._closures.get(link_key)
        return closure is not None and closure.expires_at > now

    def closures(self, now: datetime) -> list[Closure]:
        return [c for c in self._closures.values() if c.expires_at > now]

    # ---- 查询 ----
    def links(self) -> list[Link]:
        return list(self._links.values())

    def link(self, key: tuple[str, str, str]) -> Link | None:
        return self._links.get(key)

    def outgoing(self, zone: str, now: datetime, kind: str | None = None) -> list[Link]:
        result = []
        for link in self._links.values():
            if link.src != zone:
                continue
            if kind is not None and link.kind != kind:
                continue
            if self.is_closed(link.key(), now):
                continue
            result.append(link)
        return result

    def effective_capacity(self, link: Link, now: datetime) -> int:
        """封闭返回 0，否则返回核定能力（摆渡停运时上游可据此感知）。"""
        return 0 if self.is_closed(link.key(), now) else link.capacity_per_min

    # ---- 快照/恢复 ----
    def snapshot(self) -> list[dict]:
        return [
            {
                "link_key": list(c.link_key),
                "reason": c.reason,
                "owner": c.owner,
                "expires_at": c.expires_at.isoformat(),
                "created_at": c.created_at.isoformat(),
            }
            for c in self._closures.values()
        ]

    def restore(self, rows: list[dict], now: datetime) -> list[Closure]:
        """恢复未到期封闭；已到期的不恢复并返回列表供日志记录。"""
        expired: list[Closure] = []
        for row in rows:
            key = tuple(row["link_key"])
            closure = Closure(
                link_key=key,
                reason=row["reason"],
                owner=row["owner"],
                expires_at=datetime.fromisoformat(row["expires_at"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            if key not in self._links:
                continue
            if closure.expires_at <= now:
                expired.append(closure)
            else:
                self._closures[key] = closure
        return expired
