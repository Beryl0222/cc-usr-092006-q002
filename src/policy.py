"""分流策略引擎（纯函数、确定性）。

硬约束（任何时候不可被人工覆盖）：
- 消防容量：预计在场人数不得超过 fire_capacity，红色时按缺口倒推放行额度；
- 特殊人群/返程专用通道不参与常规分流，只在闭园返程疏散时启用；
- 已入园游客返程保障：闭园后停止入园、摆渡增能、开放返程通道。

预测（前瞻压力 / inbound_pressure）只产生“建议”级动作；
强制动作必须由已观测占用量、容量缺口、封闭或闭园时刻触发。

恢复不突放：限流从严变宽时按 site.recovery 的步进斜坡增加放行额度，
步进所需的节流状态由调用方持久化并随重启恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .domain import Site
from .occupancy import OFFLINE, STALE, OccupancyView, ZoneOccupancy

GREEN, YELLOW, ORANGE, RED, UNKNOWN = "green", "yellow", "orange", "red", "unknown"
RISK_ORDER = {GREEN: 0, YELLOW: 1, UNKNOWN: 2, ORANGE: 3, RED: 4}

ADVISORY = "advisory"
MANDATORY = "mandatory"

# 动作类型
THROTTLE = "throttle_entry"        # 按额度放行（params: admit_per_interval）
HOLD = "hold_outside"              # 在园区外/停车区截留
DIVERT = "divert"                  # 沿指定步行通道绕行（params: passages）
BOOST_SHUTTLE = "boost_shuttle"    # 摆渡增能（params: line）
RETURN_EVAC = "return_evacuation"  # 闭园返程疏散（开放特殊通道）
DATA_ALERT = "data_alert"          # 数据源异常，通知设备岗


class Risk(str, Enum):
    GREEN = GREEN
    YELLOW = YELLOW
    ORANGE = ORANGE
    RED = RED
    UNKNOWN = UNKNOWN


@dataclass(frozen=True)
class Action:
    code: str           # 稳定标识，同一轮次同语义动作复用，便于对账
    atype: str
    zone: str
    level: str          # advisory | mandatory
    safety: bool        # 安全硬约束动作，人工接管不得覆盖
    params: dict
    reason: str
    evidence: dict

    def to_dict(self) -> dict:
        return {
            "code": self.code, "type": self.atype, "zone": self.zone,
            "level": self.level, "safety": self.safety,
            "params": self.params, "reason": self.reason, "evidence": self.evidence,
        }


@dataclass
class ThrottleState:
    """单个入口区域的限流/恢复状态，随持久化记录保存。"""
    restricted_since: datetime | None = None
    last_budget: int | None = None      # 上一轮生效的放行额度
    ramp_start: datetime | None = None  # 本轮放宽斜坡的锚点
    ramp_from: int | None = None
    ramp_to: int | None = None
    ramp_k: int = 0                     # 斜坡已推进到第几级（持久化，防重启突放）
    next_step_at: datetime | None = None  # 最早允许升到下一级的时刻
    backlog: int = 0                    # 最近一轮前瞻窗口内待放行人次（用于展示）

    def reset_ramp(self) -> None:
        self.ramp_start = self.ramp_from = self.ramp_to = None
        self.ramp_k = 0
        self.next_step_at = None

    def to_dict(self) -> dict:
        return {
            "restricted_since": self.restricted_since.isoformat() if self.restricted_since else None,
            "last_budget": self.last_budget,
            "ramp_start": self.ramp_start.isoformat() if self.ramp_start else None,
            "ramp_from": self.ramp_from,
            "ramp_to": self.ramp_to,
            "ramp_k": self.ramp_k,
            "next_step_at": self.next_step_at.isoformat() if self.next_step_at else None,
            "backlog": self.backlog,
        }

    @classmethod
    def from_dict(cls, item: dict) -> "ThrottleState":
        from .events import parse_time
        return cls(
            restricted_since=parse_time(item["restricted_since"]) if item.get("restricted_since") else None,
            last_budget=item.get("last_budget"),
            ramp_start=parse_time(item["ramp_start"]) if item.get("ramp_start") else None,
            ramp_from=item.get("ramp_from"),
            ramp_to=item.get("ramp_to"),
            ramp_k=int(item.get("ramp_k", 0)),
            next_step_at=parse_time(item["next_step_at"]) if item.get("next_step_at") else None,
            backlog=int(item.get("backlog", 0)),
        )


@dataclass
class PolicyState:
    throttles: dict[str, ThrottleState] = field(default_factory=dict)

    def get(self, zone: str) -> ThrottleState:
        return self.throttles.setdefault(zone, ThrottleState())

    def to_dict(self) -> dict:
        # 只持久化真正生效中的限流（last_budget 非空）；空壳状态不落盘
        return {z: t.to_dict() for z, t in self.throttles.items()
                if t.last_budget is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "PolicyState":
        return cls({z: ThrottleState.from_dict(item) for z, item in data.items()})


@dataclass(frozen=True)
class ZoneRisk:
    zone: str
    level: str
    ratio: float | None
    occ: int | None
    capacity: int


@dataclass(frozen=True)
class Decision:
    now: datetime
    day: str
    phase: str
    actions: tuple[Action, ...]
    risks: dict[str, ZoneRisk]
    overall: str
    park_closed: bool
    state: PolicyState
    notes: tuple[str, ...]

    def action_by_code(self, code: str) -> Action | None:
        return next((a for a in self.actions if a.code == code), None)


def risk_of(site: Site, zo: ZoneOccupancy) -> ZoneRisk:
    cap = site.zone(zo.zone).fire_capacity
    if zo.occ is None:
        return ZoneRisk(zo.zone, UNKNOWN, None, None, cap)
    ratio = zo.occ / cap
    th = site.thresholds
    if zo.occ >= cap or ratio >= th["red"]:
        level = RED
    elif ratio >= th["orange"]:
        level = ORANGE
    elif ratio >= th["yellow"]:
        level = YELLOW
    else:
        level = GREEN
    return ZoneRisk(zo.zone, level, round(ratio, 4), zo.occ, cap)


def _evidence(zo: ZoneOccupancy, zr: ZoneRisk) -> dict:
    return {
        "occ": zo.occ,
        "capacity": zr.capacity,
        "ratio": zr.ratio,
        "confidence": zo.confidence,
        "basis": zo.basis_detail,
        "inbound_pressure": zo.inbound_pressure,
        "freshness": [
            {"source": s.source, "status": s.status, "age_seconds": s.age_seconds}
            for s in zo.sources
        ],
    }



def apply_budget(site: Site, st: ThrottleState, target: int | None, now: datetime,
                 capacity: int, immediate: bool = False) -> tuple[int | None, str | None]:
    """计算本轮放行额度，纯函数式更新 st。

    返回 (budget, note)；budget 为 None 表示恢复斜坡走完、限流正式解除。
    规则：
    - 首次设限或目标收紧：立即生效（收紧不等待），并重置斜坡锚点；
    - immediate=True（人工接管指令）：立即生效到目标，责任在责任人；
      接管结束、恢复自动策略后，再从该额度沿斜坡渐进放开；
    - 目标放宽：从当前额度沿 release_steps 个等距台阶线性恢复到目标，
      每经过一个 step_seconds 间隔**最多抬升一级**（级数 ramp_k 持久化）；
      因此即使服务宕机很久后重启，单次评估也不会跳级——恢复自动策略必须
      逐级释放，绝不一次性放出积压；解除限流时恢复终点为入口容量；
    - 斜坡进行中目标再度收紧则立即收紧并重开斜坡；目标抬高则顺延终点。
    """
    rec = site.recovery
    desired = capacity if target is None else target

    if immediate:
        st.last_budget = desired
        st.reset_ramp()
        return desired, None

    if st.last_budget is None:
        st.last_budget = desired
        st.reset_ramp()
        return desired, None

    if desired <= st.last_budget:
        st.last_budget = desired
        st.reset_ramp()
        return desired, None

    # desired > last_budget：进入或延续放宽斜坡
    if st.ramp_start is None or st.ramp_to is None or st.ramp_from is None:
        st.ramp_start, st.ramp_from, st.ramp_to, st.ramp_k = now, st.last_budget, desired, 0
        st.next_step_at = now + timedelta(seconds=rec.step_seconds)
    elif desired < st.ramp_to:
        # 斜坡终点被收紧：从当前额度重开斜坡
        st.ramp_start, st.ramp_from, st.ramp_to, st.ramp_k = now, st.last_budget, desired, 0
        st.next_step_at = now + timedelta(seconds=rec.step_seconds)
    elif desired > st.ramp_to:
        st.ramp_to = desired  # 终点抬高，锚点与级数不变，插值单调上升

    # 每轮评估最多升 1 级，且升级后把下一门限锚定到“当前时刻 + 间隔”：
    # 即使同一时刻反复评估、或宕机很久后重启，每一级都必须再经过一个完整的
    # step_seconds 真实间隔，保证积压人群逐级释放而不是一次性放出。
    if st.ramp_k < rec.release_steps and st.next_step_at is not None and now >= st.next_step_at:
        st.ramp_k += 1
        st.next_step_at = now + timedelta(seconds=rec.step_seconds)
    k = st.ramp_k
    budget = round(st.ramp_from + (st.ramp_to - st.ramp_from) * k / rec.release_steps)

    if k >= rec.release_steps:
        st.reset_ramp()
        if target is None:
            st.last_budget = None
            return None, f"恢复斜坡走完（{rec.release_steps} 级、每级 {rec.step_seconds}s），限流解除"
        st.last_budget = desired
        return desired, f"恢复斜坡走完，放行额度恢复到 {desired}"

    st.last_budget = budget
    note = f"恢复斜坡 {k}/{rec.release_steps} 级：放行额度 {st.ramp_from}→{budget}（上限 {st.ramp_to}）"
    return budget, note


# 兼容旧名称（测试或外部若引用）
_ramped_budget = apply_budget


def decide(site: Site, view: OccupancyView, state: PolicyState, now: datetime,
           blocked: frozenset[str] = frozenset(),
           suppressed: frozenset[str] = frozenset(),
           manual_throttles: dict[str, int] | None = None) -> Decision:
    """生成动作。

    blocked: 临时封闭通道集合。
    suppressed: 被有效人工接管压制的自动动作 code 前缀（安全动作除外，引擎仍会再下发）。
    manual_throttles: 人工接管给定的入口放行额度（人/步进窗口），过期后同样走斜坡恢复。
    """
    manual_throttles = manual_throttles or {}
    phase = site.phase(now)
    operating = phase in (Site.PHASE_OPEN, Site.PHASE_CLOSING)
    closing = phase == Site.PHASE_CLOSING
    risks = {z.code: risk_of(site, view.get(z.code)) for z in site.zones}
    # 总体风险只取“有观测”区域的最高等级；数据缺失不抬高风险等级，
    # 由数据告警单独呈现（入口自身未知时在入口循环里按失效安全保守放行）。
    known_levels = [r.level for r in risks.values() if r.level != UNKNOWN]
    overall = GREEN if not operating or not known_levels else max(
        known_levels, key=lambda lv: RISK_ORDER[lv]
    )
    actions: list[Action] = []
    notes: list[str] = []

    congested = [code for code, r in risks.items() if operating and r.level in (ORANGE, RED)]

    # --- 1. 数据源异常告警（不阻断安全动作） ---
    # 仅在运营时段产生；开园前/保障期后全源静默属正常，不告警。
    if operating:
        for code, zo in view.zones.items():
            statuses = {s.source: s.status for s in zo.sources}
            has_live = any(v != OFFLINE for v in statuses.values())
            offline = [s for s, v in statuses.items() if v == OFFLINE] if has_live else []
            stale = [s for s, v in statuses.items() if v == STALE] if has_live else []
            if offline or stale:
                actions.append(Action(
                    code=f"data-alert:{code}", atype=DATA_ALERT, zone=code,
                    level=ADVISORY, safety=False,
                    params={"offline_sources": offline, "stale_sources": stale},
                    reason=f"区域 {code} 数据源异常（离线 {','.join(offline) or '—'}，"
                           f"迟到 {','.join(stale) or '—'}），占用量按降级口径处理",
                    evidence=_evidence(zo, risks[code]),
                ))

    # --- 2. 摆渡增能：疏运任何可达拥堵站点的线路 ---
    # 闭园返程：只要园内（非入口区域）仍有人，所有摆渡线满负荷疏运
    in_park = sum(
        zo.occ or 0 for zc, zo in view.zones.items() if zc not in site.entrances
    ) if closing else None
    for line in site.shuttles:
        hit = None
        for cz in congested:
            if cz in line.stations:
                hit = cz
                break
            via = site.route(cz, line.stations[0], blocked)
            if via is not None:
                hit = cz
                break
        if hit or (closing and (in_park or 0) > 0):
            actions.append(Action(
                code=f"boost:{line.code}", atype=BOOST_SHUTTLE,
                zone=line.stations[0], level=MANDATORY if closing else ADVISORY,
                safety=closing,
                params={"line": line.code, "per_interval": line.boost_per_interval,
                        "interval_seconds": line.interval_seconds},
                reason=("闭园返程：摆渡满负荷疏运" if closing else f"配合 {hit} 区域疏运，摆渡增能"),
                evidence=_evidence(view.get(line.stations[0]), risks[line.stations[0]]),
            ))

    # --- 3. 各入口区域的限流/截留 ---
    # 上游联动：入口放行额度不仅看入口自身，还要看从该入口沿步行通道本应可达的
    # 下游区域——核心区红/橙时入口必须同步收紧；若通往拥堵区的通道被临时封闭到
    # “无路可达”，则必须在入口外硬截留（额度 0、安全动作），绝不能因不可达反而放开。
    # downstream[e] = [(zone, 当前路径或None, 正常路径或None)]
    downstream: dict[str, list[tuple[str, list[str] | None, list[str] | None]]] = {}
    for entrance in site.entrances:
        reachable = []
        for z in site.zones:
            if z.code == entrance:
                continue
            normal_path = site.route(entrance, z.code)
            current_path = site.route(entrance, z.code, blocked)
            reachable.append((z.code, current_path, normal_path))
        downstream[entrance] = reachable

    # 开园冷启动：当日尚无任何占用观测时，不把“全源离线”误当作设备故障。
    site_cold = all(zo.occ is None for zo in view.zones.values())

    for entrance in sorted(site.entrances):
        zo = view.get(entrance)
        zr = risks[entrance]
        st = state.get(entrance)
        # 开园前 / 返程保障期结束后：不产生入园管控（跨日状态由编排层重置）
        if phase in (Site.PHASE_CLOSED, Site.PHASE_AFTER):
            continue
        # 当前积压取前瞻窗口内待放入人次（逐轮取当前值，不累加）
        st.backlog = zo.inbound_pressure

        def _budget_for(r: ZoneRisk, observed: int | None) -> int:
            if r.level == RED:
                # 观测缺失时按零余量处理（失效安全）
                return max(0, r.capacity - (observed if observed is not None else r.capacity))
            if r.level == ORANGE:
                return max(0, (r.capacity - (observed or 0)) // 2)
            return 10 ** 9

        # 目标额度与驱动区域
        driver_zone = entrance
        target: int | None
        blocked_hold_zone: str | None = None  # 因封闭而必须硬截留的拥堵区
        if closing:
            target = 0
        elif zr.level == UNKNOWN and not site_cold:
            # 运营中入口占用未知：失效安全，保守放行，不允许用“下游没事”抵消
            target = site.recovery.min_release
        else:
            driver: tuple[str, ZoneRisk, int] | None = None
            if zr.level in (ORANGE, RED):
                driver = (entrance, zr, _budget_for(zr, zo.occ))
            for dz, current_path, normal_path in downstream[entrance]:
                if normal_path is None:
                    continue  # 本来就不连通（如停车区方向），不构成约束
                dzr = risks[dz]
                if dzr.level not in (ORANGE, RED):
                    continue
                if current_path is None:
                    # 正常能到、现在被封闭到无路可达：最高优先级硬截留
                    blocked_hold_zone = dz
                    driver = (dz, dzr, 0)
                    break
                cand = (dz, dzr, _budget_for(dzr, view.get(dz).occ))
                if driver is None or cand[2] < driver[2]:
                    driver = cand
            if driver is None:
                target = None  # 自身与正常可达下游均安全（含冷启动），不设限
            else:
                driver_zone, _dr, target = driver
        driver_is_downstream = driver_zone != entrance

        # 合并人工接管额度：
        # - 通道全封闭硬截留 / 闭园：安全硬约束，人工不能放宽；
        # - 红色：人工只能更严；
        # - 其他（含自动判断“不设限”）：人工额度直接生效。
        manual_budget = manual_throttles.get(entrance)
        manual_active = False
        if manual_budget is not None and blocked_hold_zone is None:
            manual_active = True
            if closing:
                target = 0
            elif zr.level == RED or (driver_is_downstream and risks[driver_zone].level == RED):
                target = min(target if target is not None else zr.capacity, manual_budget)
            else:
                target = manual_budget
        driving = (driver_zone, risks[driver_zone], target)

        # 前瞻压力：只产生建议，不直接强制
        if target is None and zo.inbound_pressure > 0:
            projected = (zo.occ or 0) + zo.inbound_pressure
            if projected >= site.thresholds["yellow"] * zr.capacity:
                a = Action(
                    code=f"pre-throttle:{entrance}", atype=THROTTLE, zone=entrance,
                    level=ADVISORY, safety=False,
                    params={"admit_per_interval": max(0, zr.capacity - projected),
                            "projected": projected},
                    reason=f"前瞻压力 {zo.inbound_pressure} 人次，预计占用率触及黄色（预测仅辅助）",
                    evidence=_evidence(zo, zr),
                )
                if not _suppressed(a.code, a.safety, suppressed):
                    actions.append(a)

        # 黄色：错峰劝导建议（不强制限流）
        if target is None and zr.level == YELLOW:
            a = Action(
                code=f"advise-offpeak:{entrance}", atype="advise_offpeak", zone=entrance,
                level=ADVISORY, safety=False, params={},
                reason=f"占用 {zo.occ}/{zr.capacity} 达黄色，向待入园游客发送错峰提示",
                evidence=_evidence(zo, zr),
            )
            if not _suppressed(a.code, a.safety, suppressed):
                actions.append(a)

        if target is None:
            # 限制解除：沿固定斜坡恢复到容量，斜坡走完才真正解除，不突放
            if st.last_budget is not None:
                budget, note = apply_budget(site, st, None, now, zr.capacity)
                if note:
                    notes.append(f"{entrance}: {note}")
                if budget is None:
                    state.throttles.pop(entrance, None)
                else:
                    actions.append(Action(
                        code=f"throttle:{entrance}", atype=THROTTLE, zone=entrance,
                        level=MANDATORY, safety=False,
                        params={"admit_per_interval": budget, "recovering": True},
                        reason="限流解除中，按斜坡渐进放行，避免积压人群瞬时涌入",
                        evidence=_evidence(zo, zr),
                    ))
            continue

        # 人工指令立即生效；自动策略的放宽一律走斜坡
        budget, note = apply_budget(site, st, target, now, zr.capacity,
                                    immediate=manual_active)
        if note:
            notes.append(f"{entrance}: {note}")

        if st.restricted_since is None:
            st.restricted_since = now
        st.last_budget = budget

        atype = HOLD if budget == 0 else THROTTLE
        driver_level = driving[1].level if driving else None
        if blocked_hold_zone is not None:
            reason = (f"通往 {blocked_hold_zone} 的步行通道全部临时封闭，"
                      f"而该区域处于{driver_level}级拥堵，在入口外硬截留，禁止放行（安全硬约束）")
        elif closing:
            reason = "闭园：停止入园，保障已入园游客返程"
        elif driver_is_downstream and driver_level == RED:
            dr = driving[1]
            reason = (f"下游 {driver_zone} 占用 {dr.occ}/{dr.capacity} 达红色（消防容量硬边界），"
                      f"上游入口按其缺口倒推放行，防止继续灌入")
        elif driver_is_downstream and driver_level == ORANGE:
            dr = driving[1]
            reason = f"下游 {driver_zone} 占用 {dr.occ}/{dr.capacity} 达橙色，上游入口同步控流"
        elif zr.level == RED:
            reason = f"占用 {zo.occ}/{zr.capacity} 达红色（消防容量硬边界），按缺口倒推放行"
        elif zr.level == ORANGE:
            reason = f"占用 {zo.occ}/{zr.capacity} 达橙色，控制入园节奏"
        elif zr.level == UNKNOWN:
            reason = "占用量未知且运营时段有来源离线，失效安全：保守放行"
        else:
            reason = f"自动策略未要求限流，按人工接管额度 {manual_budget} 人/窗口执行"
        if manual_active and not blocked_hold_zone and not closing and zr.level in (ORANGE, RED, UNKNOWN):
            reason = f"【人工接管】按责任人设定额度放行（{manual_budget} 人/窗口）；{reason}"
        # 硬约束：闭园、任一正常可达区域红色、通道全封闭截留、或入口占用未知
        safety = (closing or blocked_hold_zone is not None
                  or (driver_is_downstream and driver_level == RED)
                  or zr.level in (RED, UNKNOWN))
        action = Action(
            code=f"{'hold' if budget == 0 else 'throttle'}:{entrance}",
            atype=atype, zone=entrance,
            level=MANDATORY, safety=safety,
            params={"admit_per_interval": budget,
                    "driven_by": driver_zone if driver_is_downstream else None,
                    "blocked_hold": blocked_hold_zone},
            reason=reason,
            evidence=_evidence(zo, zr),
        )
        # 安全动作不会被人工接管压制（_suppressed 对 safety 返回 False）；
        # 非安全动作在有效接管期被压制时直接不出自动指令。
        if _suppressed(action.code, action.safety, suppressed):
            continue
        actions.append(action)

        # 绕行/硬截留：复用预计算路径（current_path, normal_path）
        for cz, current_path, normal_path in downstream[entrance]:
            if cz not in congested or normal_path is None:
                continue
            if current_path is None:
                # 正常可达、现在无路可达：硬截留（安全动作）
                a = Action(
                    code=f"holdroute:{entrance}:{cz}", atype=HOLD, zone=entrance,
                    level=MANDATORY, safety=True,
                    params={"target": cz, "reason_passages_blocked": True},
                    reason=f"通往 {cz} 的步行通道全部封闭，在入口外截留，禁止向游客放行",
                    evidence=_evidence(view.get(cz), risks[cz]),
                )
            elif current_path != normal_path:
                a = Action(
                    code=f"divert:{entrance}:{cz}", atype=DIVERT, zone=entrance,
                    level=MANDATORY, safety=False,
                    params={"target": cz, "passages": current_path},
                    reason=f"通往 {cz} 的常规通道临时封闭，改由 {'→'.join(current_path)} 绕行",
                    evidence=_evidence(view.get(cz), risks[cz]),
                )
            else:
                continue
            if not _suppressed(a.code, a.safety, suppressed):
                actions.append(a)

    # --- 4. 闭园返程疏散：启用特殊通道 ---
    if closing:
        present = sum(zo.occ or 0 for code, zo in view.zones.items() if code not in site.entrances)
        for entrance in sorted(site.entrances):
            actions.append(Action(
                code=f"evac:{entrance}", atype=RETURN_EVAC, zone=entrance,
                level=MANDATORY, safety=True,
                params={"open_special_passages": [p.code for p in site.passages if p.special],
                        "in_park_estimate": present},
                reason=f"闭园返程保障：启用特殊/返程通道疏运约 {present} 名在园游客",
                evidence={"closing_time": site.closing_time},
            ))

    # 同 code 去重（安全动作可能被追加两次），按 code 排序保证输出确定
    dedup = {a.code: a for a in actions}
    actions_out = tuple(dedup[k] for k in sorted(dedup))
    return Decision(
        now=now, day=view.day, phase=phase, actions=actions_out, risks=risks,
        overall=overall, park_closed=closing, state=state,
        notes=tuple(notes),
    )


def _suppressed(code: str, safety: bool, suppressed: frozenset[str]) -> bool:
    if safety:
        return False
    return any(code == pref or code.startswith(pref + ":") or code.startswith(pref) for pref in suppressed)
