"""人工接管（暴雨/雷电/设备故障时）。

每条接管记录都必须具备：
- 期限 expires_at（不允许“永久接管”，到期自动失效，恢复自动策略）；
- 理由 reason；责任人 owner；
- 作用范围 kind：
    suppress_action  压制某类自动动作（给出 code 前缀），
    throttle         手动设定入口放行额度，
    block_passage    临时封闭/重新开放步行通道。

安全硬约束动作（Action.safety=True：消防容量、闭园返程、全通道封闭截留）
在策略引擎中永远不会被 suppress 覆盖；这里仍记录请求，便于审计“有人试图覆盖安全动作”。

记录为追加式（create/revoke 各一条），当前状态由日志折叠得到，便于回放与重启恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from .events import parse_time

SUPPRESS_ACTION = "suppress_action"
THROTTLE = "throttle"
BLOCK_PASSAGE = "block_passage"
VALID_KINDS = frozenset({SUPPRESS_ACTION, THROTTLE, BLOCK_PASSAGE})

ACTIVE = "active"
EXPIRED = "expired"
REVOKED = "revoked"

MAX_TTL_SECONDS = 24 * 3600  # 单次接管最长 24h，防止漏填超长期限


class OverrideError(ValueError):
    pass


class OverrideKind(str, Enum):
    SUPPRESS_ACTION = SUPPRESS_ACTION
    THROTTLE = THROTTLE
    BLOCK_PASSAGE = BLOCK_PASSAGE


@dataclass(frozen=True)
class Override:
    override_id: str
    kind: str
    created_at: datetime
    expires_at: datetime
    owner: str
    reason: str
    # kind 相关参数：
    # suppress_action -> {"action_prefix": str}
    # throttle        -> {"zone": str, "admit_per_interval": int}
    # block_passage   -> {"passage": str, "blocked": bool}
    params: dict
    status: str = ACTIVE
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revoke_reason: str | None = None

    def is_effective(self, now: datetime) -> bool:
        if self.status != ACTIVE:
            return False
        return now < self.expires_at

    def effective_status(self, now: datetime) -> str:
        if self.status == REVOKED:
            return REVOKED
        return ACTIVE if now < self.expires_at else EXPIRED

    def to_dict(self) -> dict:
        return {
            "override_id": self.override_id,
            "kind": self.kind,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "owner": self.owner,
            "reason": self.reason,
            "params": self.params,
            "status": self.status,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoked_by": self.revoked_by,
            "revoke_reason": self.revoke_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Override":
        return cls(
            override_id=d["override_id"],
            kind=d["kind"],
            created_at=parse_time(d["created_at"]),
            expires_at=parse_time(d["expires_at"]),
            owner=d["owner"],
            reason=d["reason"],
            params=dict(d["params"]),
            status=d.get("status", ACTIVE),
            revoked_at=parse_time(d["revoked_at"]) if d.get("revoked_at") else None,
            revoked_by=d.get("revoked_by"),
            revoke_reason=d.get("revoke_reason"),
        )


def create_override(override_id: str, kind: str, now: datetime, ttl_seconds: int,
                    owner: str, reason: str, params: dict,
                    known_zones: frozenset[str] = frozenset(),
                    known_passages: frozenset[str] = frozenset(),
                    known_action_prefixes: frozenset[str] = frozenset()) -> Override:
    """校验并构造一条接管；任何要素缺失/非法都拒绝。"""
    if kind not in VALID_KINDS:
        raise OverrideError(f"未知接管类型: {kind}")
    owner = (owner or "").strip()
    reason = (reason or "").strip()
    if not owner:
        raise OverrideError("接管必须填写责任人")
    if not reason:
        raise OverrideError("接管必须填写理由")
    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise OverrideError("接管期限必须为正整数秒")
    if ttl_seconds > MAX_TTL_SECONDS:
        raise OverrideError(f"接管期限超过上限 {MAX_TTL_SECONDS}s，请到期后续期")

    clean: dict
    if kind == SUPPRESS_ACTION:
        prefix = str(params.get("action_prefix", "")).strip()
        if not prefix:
            raise OverrideError("suppress_action 需要 action_prefix")
        if known_action_prefixes and prefix not in known_action_prefixes:
            raise OverrideError(f"未知动作前缀: {prefix}")
        clean = {"action_prefix": prefix}
    elif kind == THROTTLE:
        zone = str(params.get("zone", ""))
        budget = params.get("admit_per_interval")
        if zone not in known_zones:
            raise OverrideError(f"未知区域: {zone}")
        if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
            raise OverrideError("放行额度必须为非负整数")
        clean = {"zone": zone, "admit_per_interval": budget}
    else:
        passage = str(params.get("passage", ""))
        blocked = params.get("blocked")
        if passage not in known_passages:
            raise OverrideError(f"未知通道: {passage}")
        if not isinstance(blocked, bool):
            raise OverrideError("block_passage 需要布尔 blocked")
        clean = {"passage": passage, "blocked": blocked}

    from datetime import timedelta
    return Override(
        override_id=override_id,
        kind=kind,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        owner=owner,
        reason=reason,
        params=clean,
    )


def revoke(ov: Override, now: datetime, by: str, reason: str) -> Override:
    if not (by or "").strip() or not (reason or "").strip():
        raise OverrideError("撤销接管必须填写撤销人和理由")
    if ov.status != ACTIVE:
        raise OverrideError(f"接管 {ov.override_id} 已 {ov.status}，无需撤销")
    return replace(ov, status=REVOKED, revoked_at=now, revoked_by=by.strip(),
                   revoke_reason=reason.strip())


def fold(overrides: list[Override], now: datetime) -> "OverrideView":
    """把追加式接管记录折叠为当前生效视图。"""
    suppressed: set[str] = set()
    manual_throttles: dict[str, int] = {}
    blocked: set[str] = set()
    active: list[Override] = []
    expired_or_revoked: list[Override] = []

    for ov in overrides:
        status = ov.effective_status(now)
        if status != ACTIVE:
            expired_or_revoked.append(ov)
            continue
        active.append(ov)
        if ov.kind == SUPPRESS_ACTION:
            suppressed.add(ov.params["action_prefix"])
        elif ov.kind == THROTTLE:
            # 同一区域多条有效接管：取更严（更小）额度，且后创建者在相等时生效
            z = ov.params["zone"]
            if z not in manual_throttles or ov.params["admit_per_interval"] < manual_throttles[z]:
                manual_throttles[z] = ov.params["admit_per_interval"]
        elif ov.kind == BLOCK_PASSAGE:
            if ov.params["blocked"]:
                blocked.add(ov.params["passage"])
            else:
                blocked.discard(ov.params["passage"])

    return OverrideView(
        suppressed=frozenset(suppressed),
        manual_throttles=manual_throttles,
        blocked=frozenset(blocked),
        active=tuple(active),
        inactive=tuple(expired_or_revoked),
    )


@dataclass(frozen=True)
class OverrideView:
    suppressed: frozenset[str]
    manual_throttles: dict[str, int]
    blocked: frozenset[str]
    active: tuple[Override, ...]
    inactive: tuple[Override, ...]
