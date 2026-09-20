"""输入事件模型。

事件来自闸机计数、线上分时票、停车区入场、摆渡车、图像计数等不同来源，
可能迟到、乱序、重复。所有事件都带：

* ``event_id``：来源方幂等键（同一来源重复推送相同 event_id 只生效一次）；
* ``occurred_at``：事件实际发生时间（用于乱序归位与跨日判定）；
* ``received_at``：服务接收时间（可缺省，由摄取时刻补齐，用于数据新鲜度）。

事件类型：

* GATE_ENTER / GATE_EXIT：闸机进/出，带数量；
* TICKET_SOLD：线上分时票，``slot_start`` 为预约时段起点，进入预测队列；
* TICKET_USED：预约票核销（从队列转占用）；票未核销在闭园后自动失效；
* PARK_ENTER / PARK_EXIT：停车区进/出（上游来流的领先指标）；
* SHUTTLE_ARRIVE / SHUTTLE_DEPART：摆渡车到达/离开某区域，带载客数；
* CAMERA_COUNT：图像计数给出的区域瞬时在场人数（快照来源）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .clock import ensure_aware


class EventType(str, Enum):
    GATE_ENTER = "gate_enter"
    GATE_EXIT = "gate_exit"
    TICKET_SOLD = "ticket_sold"
    TICKET_USED = "ticket_used"
    PARK_ENTER = "park_enter"
    PARK_EXIT = "park_exit"
    SHUTTLE_ARRIVE = "shuttle_arrive"
    SHUTTLE_DEPART = "shuttle_depart"
    CAMERA_COUNT = "camera_count"


# 对占用量做“增量加减”的事件及其符号
DELTA_TYPES: dict[EventType, int] = {
    EventType.GATE_ENTER: +1,
    EventType.GATE_EXIT: -1,
    EventType.TICKET_USED: +1,
    EventType.SHUTTLE_ARRIVE: +1,
    EventType.SHUTTLE_DEPART: -1,
    EventType.PARK_ENTER: 0,   # 停车不直接进入景区占用，只影响上游来流预测
    EventType.PARK_EXIT: 0,
}

TICKET_TYPES = frozenset({EventType.TICKET_SOLD, EventType.TICKET_USED})


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: EventType
    source: str                 # gate / ticket / parking / shuttle / camera ...
    zone: str                   # 作用区域；票券为入园区域或 "global"
    occurred_at: datetime
    quantity: int = 1
    slot_start: datetime | None = None   # 仅 TICKET_SOLD
    correlation_id: str | None = None    # 票号：sold 与 used 用同一键关联
    target_zone: str | None = None       # 摆渡离开时的去向（用于拓扑联动）
    received_at: datetime | None = None
    sequence: int = field(default=0, compare=False)  # 接收序号，持久化恢复用

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id 不能为空")
        if not self.source:
            raise ValueError("source 不能为空")
        if not self.zone:
            raise ValueError("zone 不能为空")
        ensure_aware(self.occurred_at)
        if self.received_at is not None:
            ensure_aware(self.received_at)
        if self.slot_start is not None:
            ensure_aware(self.slot_start)
        if self.quantity < 0:
            raise ValueError("quantity 不能为负")
        if self.event_type is EventType.TICKET_SOLD:
            if self.slot_start is None or self.correlation_id is None:
                raise ValueError("售票事件需要 slot_start 与 correlation_id")

    @property
    def dedup_key(self) -> tuple[str, str]:
        """(来源, 来源方事件号)。重复事件据此丢弃。"""
        return (self.source, self.event_id)


# 事件可被接受的最大迟到时长：超过此时长的历史事件拒绝进入并记入异常，
# 防止回放旧数据污染当前台账（可由配置覆盖）。
DEFAULT_MAX_LATE_SECONDS = 6 * 3600
