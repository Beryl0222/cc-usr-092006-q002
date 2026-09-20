"""原始事件的校验与归一化。

事件来自闸机、图像、停车、分时票、摆渡等不同系统，允许迟到、乱序和重复投递。
归一化结果是不可变值对象；去重和按时间归并在 occupancy / journal 层完成，
保证同一条事件投递一次或多次、按任何顺序到达，最终状态一致（幂等）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

LOCAL_TZ = timezone(timedelta(hours=8))  # 示例资料按东八区运营日切分

FLOW = "flow"        # 进出计数增量（入园 +、出园 -、上车 -、下车 +）
SNAPSHOT = "snapshot"  # 区域在场人数快照（图像计数）
LEAD = "lead"        # 前瞻压力（分时票/停车入场，预计未来一段时间到达）
VALID_TYPES = frozenset({FLOW, SNAPSHOT, LEAD})


class InvalidEvent(ValueError):
    """事件字段不合法，进入隔离记录而不影响既有占用量。"""


class EventKind(str, Enum):
    FLOW = FLOW
    SNAPSHOT = SNAPSHOT
    LEAD = LEAD


@dataclass(frozen=True)
class Event:
    event_id: str
    source: str
    etype: str
    zone: str
    occurred_at: datetime  # 事件发生时间（带时区）
    value: int
    received_at: datetime  # 服务接收时间（带时区）

    @property
    def operating_day(self) -> str:
        return self.occurred_at.astimezone(LOCAL_TZ).date().isoformat()


def parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError as exc:
            raise InvalidEvent(f"时间格式无法解析: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):  # bool 是 int 的子类，显式拒绝
        raise InvalidEvent("布尔值不能作为计数值")
    if not isinstance(value, int):
        raise InvalidEvent(f"计数值必须是整数: {value!r}")
    return value


def normalize(raw: dict, known_zones: frozenset[str], known_sources: frozenset[str],
              now: datetime | None = None) -> Event:
    """把外部 JSON 消息校验为 Event。now 仅用于补全 received_at，便于回放测试。"""
    try:
        event_id = str(raw["event_id"])
        source = str(raw["source"])
        etype = str(raw["type"])
        zone = str(raw["zone"])
    except (KeyError, TypeError) as exc:
        raise InvalidEvent(f"缺少必填字段: {exc}") from exc

    if not event_id:
        raise InvalidEvent("event_id 为空")
    if source not in known_sources:
        raise InvalidEvent(f"未知数据源: {source}")
    if etype not in VALID_TYPES:
        raise InvalidEvent(f"未知事件类型: {etype}")
    if zone not in known_zones:
        raise InvalidEvent(f"未知区域: {zone}")

    value = _coerce_int(raw.get("value", 0))
    if etype in (SNAPSHOT, LEAD) and value < 0:
        raise InvalidEvent(f"{etype} 事件数值不能为负: {value}")

    occurred = parse_time(raw["occurred_at"])
    received = parse_time(raw["received_at"]) if raw.get("received_at") else (now or datetime.now(timezone.utc))
    if occurred > received + timedelta(minutes=1):
        raise InvalidEvent("事件发生时间晚于接收时间")
    return Event(event_id, source, etype, zone, occurred, value, received)


def to_dict(event: Event) -> dict:
    return {
        "event_id": event.event_id,
        "source": event.source,
        "type": event.etype,
        "zone": event.zone,
        "occurred_at": event.occurred_at.isoformat(),
        "value": event.value,
        "received_at": event.received_at.isoformat(),
    }
