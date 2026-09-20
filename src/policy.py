"""联动策略引擎。

输入：占用观测（``occupancy`` 模块）、拓扑（含临时封闭与摆渡运力）、
人工接管登记、当前时刻。输出：确定性的 ``Decision``——风险分级、
动作清单（含依据）、被抑制的自动建议、安全底线标记。

不可突破的底线（人工接管也不能覆盖）：

* 消防容量：实测可信占用量达到容量即强制停止放行，预测只能产生“建议”；
* 特殊人群通道：任何限流动作都保留 ``special_lanes`` 条通道；
* 返程保障：闭园前进入返程保障窗口，摆渡优先向出口疏运，不再放大入园。

人工接管恢复自动后，对原接管区域执行斜坡放行：
每分钟最多新增 ``ramp_release_ratio_per_minute × 容量`` 的入园量，
避免积压人群瞬间涌入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .domain import PolicyConfig, Schedule
from .occupancy import OccupancyReport, ZoneReading
from .overrides import ManualOverride, OverrideMode, OverrideRegistry
from .topology import Link, Topology


class RiskLevel(str, Enum):
    NORMAL = "normal"
    WATCH = "watch"
    WARNING = "warning"
    CRITICAL = "critical"


class ActionType(str, Enum):
    ENTRY_STOP = "entry_stop"                 # 消防硬限：区域只出不进
    RESTRICT_ADMISSION = "restrict_admission"  # 上游限流，带 max_per_minute
    DIVERT_WALK = "divert_walk"               # 步行分流至有余量区域
    DISPATCH_SHUTTLE = "dispatch_shuttle"     # 加班摆渡
    RETURN_PRIORITY = "return_priority"       # 返程优先（摆渡向出口）
    RAMP_RELEASE = "ramp_release"             # 接管恢复后的斜坡放行标记
    MANUAL = "manual"                          # 透传的人工动作


@dataclass(frozen=True)
class Risk:
    level: RiskLevel
    ratio_estimated: float
    ratio_projected: float
    fire_limit: bool
    stale_source_ratio: float


@dataclass(frozen=True)
class Action:
    action_id: str
    action_type: ActionType
    zone: str
    params: dict
    basis: str                  # hard | auto | advisory | manual
    rationale: tuple[str, ...]
    keep_special_lanes: int = 0
    return_priority: bool = False


@dataclass(frozen=True)
class Suppressed:
    action_type: str
    zone: str
    reason: str
    override_id: str | None


@dataclass(frozen=True)
class Decision:
    decision_id: str
    at: datetime
    business_date: str
    risks: dict[str, Risk]
    actions: tuple[Action, ...]
    suppressed: tuple[Suppressed, ...]
    active_overrides: tuple[str, ...]
    ramps: dict[str, float]     # zone -> 当前斜坡放行上限（人/分钟）
    notes: tuple[str, ...]


@dataclass
class _Ramp:
    started_at: datetime
    window_minutes: float


class PolicyEngine:
    def __init__(
        self,
        config: PolicyConfig,
        schedule: Schedule,
        topology: Topology,
        overrides: OverrideRegistry,
        entry_zones: tuple[str, ...] = ("gate-east",),
        exit_zones: tuple[str, ...] = ("gate-east",),
    ) -> None:
        self._config = config
        self._schedule = schedule
        self._topology = topology
        self._overrides = overrides
        self._entry_zones = entry_zones
        self._exit_zones = exit_zones
        self._capacities: dict[str, int] = {}
        self._special: dict[str, int] = {}
        self._ramps: dict[str, _Ramp] = {}
        self._was_suppressed: set[str] = set()
        self._pending_ramps: set[str] = set()
        self._decision_index = 0

    # ---------------------------------------------------------------- 主流程
    def evaluate(self, report: OccupancyReport, now: datetime) -> Decision:
        self._decision_index += 1
        did = f"D{self._decision_index:06d}@{int(now.timestamp())}"

        active = self._overrides.all_active(now)
        suppressed_zones, global_suppressed = self._suppressed_zones(active)

        risks = {code: self._risk(reading) for code, reading in report.zones.items()}
        hard_zones = {code for code, r in risks.items() if r.fire_limit}
        in_return_window = self._in_return_window(now)
        open_now = self._schedule.is_open(now)

        # 斜坡放行状态机：接管结束后，若区域仍被消防硬限/返程保障阻塞，
        # 斜坡挂起；阻塞解除的那一刻才开始计时，避免“只出不进”与
        # “恢复放行”同时下达。
        hard_blocked = set(hard_zones)
        if in_return_window:
            hard_blocked.update(z for z in self._entry_zones if z in report.zones)

        for zone in report.zones:
            is_suppressed = global_suppressed or zone in suppressed_zones
            if is_suppressed:
                self._was_suppressed.add(zone)
                self._pending_ramps.discard(zone)
                self._ramps.pop(zone, None)
                continue
            if zone in self._was_suppressed:
                self._was_suppressed.discard(zone)
                self._pending_ramps.add(zone)
            if zone in hard_blocked:
                # 放坡期间重新触限：撤销斜坡，待再次解除后重新放坡
                self._ramps.pop(zone, None)
                self._pending_ramps.add(zone)
            elif zone in self._pending_ramps:
                self._pending_ramps.discard(zone)
                self._ramps[zone] = _Ramp(
                    started_at=now,
                    window_minutes=1.0 / self._config.ramp_release_ratio_per_minute,
                )
        actions: list[Action] = []
        suppressed: list[Suppressed] = []
        notes: list[str] = []

        if not open_now:
            notes.append("当前处于闭园时段，仅执行消防硬限与返程保障类动作")
        if in_return_window and open_now:
            notes.append("已进入闭园前返程保障窗口")

        capacities = {code: self._capacity_of(code) for code in report.zones}

        # 1) 人工动作（先放，但要经过安全底线裁剪）
        manual_actions, manual_notes = self._manual_actions(active, report, risks, now, did)
        actions.extend(manual_actions)
        notes.extend(manual_notes)

        # 2) 消防硬限：只出不进，并封锁所有向该区域的上游放行
        for code in sorted(hard_zones):
            reading = report.zones[code]
            rationale = (
                f"可信占用 {reading.estimated} ≥ 消防容量 {capacities[code]}",
                f"台账 {reading.ledger}，快照 {reading.camera_snapshot or '无'}，"
                f"陈旧缓冲 {reading.stale_buffer}",
                "硬限依据为实测可信占用，不含预测",
            )
            actions.append(
                Action(f"{did}:stop:{code}", ActionType.ENTRY_STOP, code,
                       {"mode": "exit_only"}, "hard", rationale,
                       keep_special_lanes=self._special_lanes(code),
                       return_priority=in_return_window)
            )
            for link in self._links_into(code, now):
                if link.src in report.zones:
                    actions.append(
                        Action(f"{did}:stopfeed:{link.src}->{code}", ActionType.ENTRY_STOP,
                               link.src, {"mode": "hold_to", "target": code}, "hard",
                               (f"下游 {code} 触及消防容量，暂停向其放行",),
                               keep_special_lanes=self._special_lanes(link.src))
                    )

        # 3) 自动联动（闭园时段挂起普通限流；被接管区域只记录为 suppressed）
        auto, sup = self._automatic_actions(
            report, risks, now, did, suppressed_zones, global_suppressed, hard_zones,
            open_now,
        )
        actions.extend(auto)
        suppressed.extend(sup)

        # 4) 返程保障：摆渡优先疏运向出口，限制继续入园
        if in_return_window:
            self._append_return_actions(report, now, did, actions)

        # 5) 斜坡放行标记与上限（先清理已结束的斜坡，再计算当前上限）
        self._expire_ramps(now)
        ramp_caps = self._ramp_caps(now)
        for zone, cap in ramp_caps.items():
            actions.append(
                Action(f"{did}:ramp:{zone}", ActionType.RAMP_RELEASE, zone,
                       {"max_per_minute": cap}, "auto",
                       ("人工接管结束，按斜坡恢复放行，避免积压人群突然释放",))
            )
        if ramp_caps:
            for action in actions:
                if action.zone in ramp_caps and action.action_type in (
                    ActionType.RESTRICT_ADMISSION,
                ):
                    cap = min(int(action.params.get("max_per_minute", 10**9)), ramp_caps[action.zone])
                    action.params["max_per_minute"] = cap

        self._expire_ramps(now)
        return Decision(
            decision_id=did,
            at=now,
            business_date=report.business_date,
            risks=risks,
            actions=tuple(self._dedupe_actions(actions)),
            suppressed=tuple(suppressed),
            active_overrides=tuple(o.override_id for o in active),
            ramps=ramp_caps,
            notes=tuple(notes),
        )

    # ---------------------------------------------------------------- 风险
    def classify_risk(self, reading) -> Risk:
        """不产生决策的实时风险分级，供状态界面在两次 tick 之间展示。"""
        return self._risk(reading)

    def _risk(self, reading: ZoneReading):
        t = self._config.thresholds
        capacity = self._capacity_of(reading.zone)
        ratio = reading.estimated / capacity if capacity else 1.0
        projected_ratio = reading.projected / capacity if capacity else 1.0
        if reading.camera_anomaly or reading.estimated >= capacity:
            level = RiskLevel.CRITICAL
        elif ratio >= t.critical:
            level = RiskLevel.CRITICAL
        elif ratio >= t.warning:
            level = RiskLevel.WARNING
        elif projected_ratio >= t.warning or ratio >= t.watch:
            level = RiskLevel.WATCH
        else:
            level = RiskLevel.NORMAL
        return Risk(
            level=level,
            ratio_estimated=round(ratio, 4),
            ratio_projected=round(projected_ratio, 4),
            fire_limit=reading.estimated >= capacity or reading.camera_anomaly,
            stale_source_ratio=reading.stale_source_ratio,
        )

    # ---------------------------------------------------------------- 自动动作
    def _automatic_actions(
        self, report, risks, now, did, suppressed_zones, global_suppressed, hard_zones,
        open_now=True,
    ):
        actions: list[Action] = []
        suppressed: list[Suppressed] = []
        if not open_now:
            return actions, suppressed
        capacities = {code: self._capacity_of(code) for code in report.zones}

        def consider_auto(zone: str, make):
            if global_suppressed or zone in suppressed_zones:
                ov = self._overrides.active_for(zone, now)
                suppressed.append(
                    Suppressed(make[0], zone, "区域处于人工接管，自动动作挂起",
                               ov[0].override_id if ov else None)
                )
                return
            actions.append(make[1])

        for code, risk in sorted(risks.items()):
            if code in hard_zones:
                continue
            reading = report.zones[code]
            capacity = capacities[code]

            if risk.level in (RiskLevel.WARNING, RiskLevel.CRITICAL):
                headroom = max(0, capacity - reading.estimated)
                cap = max(0, headroom // 5)
                rationale = (
                    f"占用率 {risk.ratio_estimated:.0%}（实测 {reading.estimated}/{capacity}）",
                    f"预测 15 分钟内新增 {reading.predicted_add} 人（仅辅助）",
                    f"陈旧来源比例 {reading.stale_source_ratio:.0%}，缓冲 {reading.stale_buffer}",
                )
                act = Action(
                    f"{did}:restrict:{code}",
                    ActionType.RESTRICT_ADMISSION, code,
                    {"max_per_minute": int(cap), "headroom": int(headroom),
                     "keep_special_lanes": self._special_lanes(code)},
                    "auto", rationale,
                    keep_special_lanes=self._special_lanes(code),
                )
                consider_auto(code, (ActionType.RESTRICT_ADMISSION.value, act))

                # 步行分流：只向实测占用低于 watch 的区域
                for link in self._topology.outgoing(code, now, kind="walk"):
                    target = link.dst
                    if target not in report.zones:
                        continue
                    target_ratio = report.zones[target].estimated / capacities[target]
                    if target_ratio >= self._config.thresholds.watch:
                        continue
                    spare = capacities[target] - report.zones[target].estimated
                    flow = min(link.capacity_per_min, max(0, spare) // 5)
                    if flow <= 0:
                        continue
                    divert = Action(
                        f"{did}:divert:{code}:{link.code or target}",
                        ActionType.DIVERT_WALK, code,
                        {"via": link.code, "to": target, "max_per_minute": int(flow)},
                        "auto",
                        (f"通道 {link.code or code + '->' + target} 可分流至 {target}"
                         f"（余量 {spare} 人）",),
                        keep_special_lanes=self._special_lanes(code),
                    )
                    consider_auto(code, (ActionType.DIVERT_WALK.value, divert))

                # 摆渡加班：步行通道不足或封闭时，用摆渡向外疏运
                for link in self._topology.outgoing(code, now, kind="shuttle"):
                    target = link.dst
                    if target in report.zones:
                        spare = capacities[target] - report.zones[target].estimated
                        if spare <= 0 and target not in self._exit_zones:
                            continue
                    else:
                        spare = link.capacity_per_min * 5
                    need = reading.estimated - int(capacity * self._config.thresholds.warning)
                    boost = min(link.capacity_per_min, max(0, need), max(0, spare))
                    if boost <= 0:
                        continue
                    shuttle = Action(
                        f"{did}:shuttle:{code}:{link.code or target}",
                        ActionType.DISPATCH_SHUTTLE, code,
                        {"link": link.code, "to": target, "extra_per_minute": int(boost),
                         "effective_capacity_per_min": self._topology.effective_capacity(link, now)},
                        "auto",
                        (f"{code} 超预警 {need} 人，摆渡线 {link.code or target} 加班 {boost} 人/分钟",
                         f"步行通道开放 {len(self._topology.outgoing(code, now, 'walk'))} 条"),
                    )
                    consider_auto(code, (ActionType.DISPATCH_SHUTTLE.value, shuttle))

            elif risk.level is RiskLevel.WATCH and reading.predicted_add > 0:
                # 仅预测推动的建议动作
                advisory = Action(
                    f"{did}:advise:{code}",
                    ActionType.RESTRICT_ADMISSION, code,
                    {"max_per_minute": max(0, (capacity - reading.projected) // 5),
                     "advisory": True},
                    "advisory",
                    (f"实测占用率 {risk.ratio_estimated:.0%} 尚平稳，预测占用率 "
                     f"{risk.ratio_projected:.0%}，提前准备错峰（预测仅辅助）",),
                )
                consider_auto(code, ("restrict_admission_advisory", advisory))

        # 上游预限流：下游预警且上游来流通道将饱和；
        # 下游已触消防硬限时由“只出不进/暂停放行”覆盖，不再重复限流
        for dst, risk in sorted(risks.items()):
            if dst in hard_zones:
                continue
            if risk.level.value not in (RiskLevel.WARNING.value, RiskLevel.CRITICAL.value):
                continue
            for link in self._links_into(dst, now):
                src = link.src
                if src not in report.zones or src in hard_zones:
                    continue
                downstream = report.zones[dst].estimated / capacities[dst]
                if downstream < self._config.upstream_prerestrict_ratio:
                    continue
                cap = min(link.capacity_per_min, max(0, (capacities[dst] - report.zones[dst].estimated) // 5))
                pre = Action(
                    f"{did}:prerestrict:{src}:{dst}",
                    ActionType.RESTRICT_ADMISSION, src,
                    {"max_per_minute": int(cap), "target": dst,
                     "keep_special_lanes": self._special_lanes(src)},
                    "auto",
                    (f"下游 {dst} 占用率 {downstream:.0%}，提前在 {src} 限流，"
                     f"通道运力 {link.capacity_per_min} 人/分钟",
                     f"停车区在场 {report.parking_inside.get(src, 0)} 辆车为来流领先指标"),
                    keep_special_lanes=self._special_lanes(src),
                )
                consider_auto(src, ("prerestrict", pre))

        return actions, suppressed

    # ---------------------------------------------------------------- 人工
    def _manual_actions(self, active, report, risks, now, did):
        actions: list[Action] = []
        notes: list[str] = []
        capacity = {c: self._capacity_of(c) for c in report.zones}
        for override in active:
            if override.mode is OverrideMode.SUPPRESS_AUTO:
                notes.append(
                    f"接管 {override.override_id}（{override.zone}，{override.owner}，"
                    f"到期 {override.expires_at.isoformat()}）：自动动作挂起，安全底线仍生效"
                )
                continue
            for spec in override.actions:
                zone = override.zone if override.zone != "*" else spec.params.get("zone", "")
                if zone not in report.zones:
                    continue
                params = dict(spec.params)
                # 安全底线裁剪：人工动作不得使入园超过消防容量
                risk = risks[zone]
                if risk.fire_limit and spec.action_type not in (
                    ActionType.ENTRY_STOP.value, ActionType.DISPATCH_SHUTTLE.value,
                    ActionType.DIVERT_WALK.value, ActionType.RETURN_PRIORITY.value,
                ):
                    notes.append(
                        f"人工动作 {spec.action_type}@{zone} 与消防硬限冲突，已强制改为只出不进"
                    )
                    spec = type(spec)(ActionType.ENTRY_STOP.value, {"mode": "exit_only"})
                    params = {"mode": "exit_only"}
                params.setdefault("keep_special_lanes", self._special_lanes(zone))
                actions.append(
                    Action(
                        f"{did}:manual:{override.override_id}:{spec.action_type}:{zone}",
                        ActionType.MANUAL, zone,
                        {**params, "manual_action_type": spec.action_type,
                         "override_id": override.override_id,
                         "expires_at": override.expires_at.isoformat()},
                        "manual",
                        (f"人工接管 {override.override_id}：{override.reason}",
                         f"责任人 {override.owner}，期限至 {override.expires_at.isoformat()}"),
                        keep_special_lanes=self._special_lanes(zone),
                    )
                )
        return actions, notes

    # ---------------------------------------------------------------- 返程
    def _in_return_window(self, now: datetime) -> bool:
        """当日闭园前 return_guard_minutes 到闭园后 60 分钟的疏运窗口。"""
        local = now.astimezone(self._schedule.tz)
        close_today = local.replace(
            hour=self._schedule.close_hour, minute=0, second=0, microsecond=0
        )
        start = close_today - timedelta(minutes=self._config.return_guard_minutes)
        end = close_today + timedelta(minutes=60)
        return start <= now <= end

    def _append_return_actions(self, report, now, did, actions: list[Action]) -> None:
        for zone, reading in report.zones.items():
            if reading.estimated <= 0:
                continue
            # 有可达出口的摆渡线路：加班疏运
            for link in self._topology.outgoing(zone, now, kind="shuttle"):
                if link.dst in self._exit_zones or not any(
                    l.kind == "walk" for l in self._topology.outgoing(zone, now)
                ):
                    boost = min(link.capacity_per_min, max(1, reading.estimated // 10))
                    actions.append(
                        Action(
                            f"{did}:return:{zone}:{link.code or link.dst}",
                            ActionType.RETURN_PRIORITY, zone,
                            {"link": link.code, "to": link.dst, "extra_per_minute": int(boost)},
                            "hard",
                            ("闭园前返程保障：摆渡优先运送已入园游客离园",),
                            return_priority=True,
                        )
                    )
        for entry in self._entry_zones:
            if entry in report.zones:
                actions.append(
                    Action(
                        f"{did}:return-hold:{entry}",
                        ActionType.RESTRICT_ADMISSION, entry,
                        {"max_per_minute": 0, "reason": "return_guard",
                         "keep_special_lanes": self._special_lanes(entry)},
                        "hard",
                        ("返程保障窗口内不再放大入园，优先疏运已入园游客",),
                        keep_special_lanes=self._special_lanes(entry),
                        return_priority=True,
                    )
                )

    # ---------------------------------------------------------------- 斜坡
    def _ramp_caps(self, now: datetime) -> dict[str, float]:
        caps: dict[str, float] = {}
        ratio = self._config.ramp_release_ratio_per_minute
        for zone, ramp in list(self._ramps.items()):
            elapsed = (now - ramp.started_at).total_seconds() / 60.0
            cap = ratio * self._capacity_of(zone) * max(0.0, elapsed)
            caps[zone] = round(cap, 1)
        return caps

    def _expire_ramps(self, now: datetime) -> None:
        for zone, ramp in list(self._ramps.items()):
            if now - ramp.started_at >= timedelta(minutes=ramp.window_minutes):
                del self._ramps[zone]

    # ---------------------------------------------------------------- 辅助
    def _suppressed_zones(self, active: list[ManualOverride]):
        zones: set[str] = set()
        global_suppressed = False
        for override in active:
            if override.zone == "*":
                global_suppressed = True
            else:
                zones.add(override.zone)
        return zones, global_suppressed

    def _links_into(self, zone: str, now: datetime) -> list[Link]:
        return [link for link in self._topology.links() if link.dst == zone
                and not self._topology.is_closed(link.key(), now)]

    def _capacity_of(self, zone: str) -> int:
        return self._capacities.get(zone, 0)

    def _special_lanes(self, zone: str) -> int:
        return self._special.get(zone, 0)

    @property
    def capacities(self) -> dict[str, int]:
        return self._capacities

    def bind_zones(self, zones) -> None:
        self._capacities = {z.code: z.fire_capacity for z in zones}
        self._special = {z.code: z.special_lanes for z in zones}

    def _dedupe_actions(self, actions: list[Action]) -> list[Action]:
        seen: set[str] = set()
        out: list[Action] = []
        # 硬限优先，其次人工，再自动；同 id 去重
        order = {"hard": 0, "manual": 1, "auto": 2, "advisory": 3}
        for action in sorted(actions, key=lambda a: (order.get(a.basis, 9), a.action_id)):
            signature = (action.action_type, action.zone, tuple(sorted(action.params.items())))
            if signature in seen:
                continue
            seen.add(signature)
            out.append(action)
        return out

    # ------------------------------------------------------------ 恢复支持
    def restore_state(self, decision_index: int, was_suppressed: list[str],
                      ramps: list[dict], pending_ramps: list[str] | None = None) -> None:
        self._decision_index = decision_index
        self._was_suppressed = set(was_suppressed)
        self._pending_ramps = set(pending_ramps or [])
        for row in ramps:
            self._ramps[row["zone"]] = _Ramp(
                started_at=datetime.fromisoformat(row["started_at"]),
                window_minutes=float(row["window_minutes"]),
            )

    def snapshot_state(self) -> dict:
        return {
            "decision_index": self._decision_index,
            "was_suppressed": sorted(self._was_suppressed),
            "pending_ramps": sorted(self._pending_ramps),
            "ramps": [
                {"zone": z, "started_at": r.started_at.isoformat(),
                 "window_minutes": r.window_minutes}
                for z, r in self._ramps.items()
            ],
        }
