"""人工接管（覆盖）登记。

每次人工覆盖必须具备：期限（``expires_at``）、理由、责任人。
覆盖到期自动失效并回到自动策略；回到自动策略后的斜坡放行由策略引擎负责，
本模块只回答“某区域此刻是否处于人工接管、人工要求做什么”。

即使在人工接管期间，消防容量硬限、特殊人群通道与返程保障三类安全底线
仍由策略引擎强制叠加，人工覆盖不能突破。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class OverrideMode(str, Enum):
    # 以人工给出的动作为准，抑制该区域的自动建议动作
    MANUAL_ACTIONS = "manual_actions"
    # 仅抑制自动升级（人工在线下处置），安全底线仍强制
    SUPPRESS_AUTO = "suppress_auto"


@dataclass(frozen=True)
class ManualActionSpec:
    action_type: str
    params: dict


@dataclass(frozen=True)
class ManualOverride:
    override_id: str
    zone: str                       # 区域码，或 "*" 表示全园
    mode: OverrideMode
    reason: str
    owner: str
    created_at: datetime
    expires_at: datetime
    actions: tuple[ManualActionSpec, ...] = ()

    def active(self, now: datetime) -> bool:
        return self.created_at <= now < self.expires_at


MAX_OVERRIDE_HOURS = 12


class OverrideRegistry:
    def __init__(self, max_duration: timedelta = timedelta(hours=MAX_OVERRIDE_HOURS)) -> None:
        self._max = max_duration
        self._items: dict[str, ManualOverride] = {}

    def add(self, override: ManualOverride, now: datetime) -> None:
        if not override.reason.strip():
            raise ValueError("人工接管必须填写理由")
        if not override.owner.strip():
            raise ValueError("人工接管必须填写责任人")
        if override.expires_at <= now:
            raise ValueError("接管期限必须晚于当前时间")
        if override.expires_at - now > self._max:
            raise ValueError(f"单次接管期限不得超过 {self._max}")
        if override.override_id in self._items:
            raise ValueError("接管编号重复")
        if override.mode is OverrideMode.MANUAL_ACTIONS and not override.actions:
            raise ValueError("人工动作模式至少需要一条动作")
        self._items[override.override_id] = override

    def cancel(self, override_id: str) -> ManualOverride | None:
        return self._items.pop(override_id, None)

    def expire(self, now: datetime) -> list[ManualOverride]:
        """到期接管自动移除，返回被移除的记录（供日志与放行斜坡使用）。"""
        expired = [o for o in self._items.values() if o.expires_at <= now]
        for o in expired:
            del self._items[o.override_id]
        return expired

    def active_for(self, zone: str, now: datetime) -> list[ManualOverride]:
        result = [o for o in self._items.values() if o.active(now) and (o.zone == zone or o.zone == "*")]
        return sorted(result, key=lambda o: (o.zone != "*", o.created_at))

    def all_active(self, now: datetime) -> list[ManualOverride]:
        return sorted((o for o in self._items.values() if o.active(now)), key=lambda o: o.override_id)

    def snapshot(self) -> list[dict]:
        return [
            {
                "override_id": o.override_id,
                "zone": o.zone,
                "mode": o.mode.value,
                "reason": o.reason,
                "owner": o.owner,
                "created_at": o.created_at.isoformat(),
                "expires_at": o.expires_at.isoformat(),
                "actions": [{"action_type": a.action_type, "params": a.params} for a in o.actions],
            }
            for o in self._items.values()
        ]

    def restore(self, rows: list[dict], now: datetime) -> list[ManualOverride]:
        expired: list[ManualOverride] = []
        for row in rows:
            override = ManualOverride(
                override_id=row["override_id"],
                zone=row["zone"],
                mode=OverrideMode(row["mode"]),
                reason=row["reason"],
                owner=row["owner"],
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=datetime.fromisoformat(row["expires_at"]),
                actions=tuple(
                    ManualActionSpec(a["action_type"], dict(a.get("params", {})))
                    for a in row.get("actions", [])
                ),
            )
            if override.expires_at <= now:
                expired.append(override)
            else:
                self._items[override.override_id] = override
        return expired
