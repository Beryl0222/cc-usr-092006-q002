import unittest
from datetime import timedelta

from src.occupancy import compute_occupancy
from src.policy import (
    GREEN, ORANGE, RED, PolicyState, decide,
)

from helpers import DAY, at, event, make_site


def decide_with(events, now=None, **kw):
    site = make_site()
    now = now or at(0)
    view = compute_occupancy(site, events, DAY, now)
    state = kw.pop("state", PolicyState())
    return site, decide(site, view, state, now, **kw)


def codes(decision):
    return {a.code: a for a in decision.actions}


class RiskAndCapacityTest(unittest.TestCase):
    def test_red_downstream_couples_upstream(self):
        # 核心区红色、入口绿色：入口仍必须限流（上游联动）
        events = [
            event("c", "camera", "snapshot", "waterfall", 1760, -2),
            event("g", "gate", "flow", "gate-east", 100, -1),
        ]
        site, d = decide_with(events)
        a = codes(d)["throttle:gate-east"]
        self.assertEqual(RED, d.overall)
        self.assertTrue(a.safety)
        # 放行额度由瀑布缺口倒推：1800-1760=40
        self.assertEqual(40, a.params["admit_per_interval"])
        self.assertEqual("waterfall", a.params["driven_by"])

    def test_fire_capacity_never_exceeded_budget_nonnegative(self):
        events = [
            event("c", "camera", "snapshot", "waterfall", 1850, -2),  # 已超容
            event("g", "gate", "flow", "gate-east", 50, -1),
        ]
        _, d = decide_with(events)
        a = codes(d)["hold:gate-east"]
        self.assertEqual(0, a.params["admit_per_interval"])  # 零缺口→硬截留
        self.assertTrue(a.safety)

    def test_orange_throttles_but_does_not_hold(self):
        events = [
            event("c", "camera", "snapshot", "waterfall", 1600, -2),
            event("g", "gate", "flow", "gate-east", 100, -1),
        ]
        _, d = decide_with(events)
        a = codes(d)["throttle:gate-east"]
        self.assertEqual(ORANGE, d.overall)
        self.assertGreater(a.params["admit_per_interval"], 0)
        self.assertFalse(a.safety)  # 橙色非硬约束，可被人工接管调整


class BlockedPassageTest(unittest.TestCase):
    def test_detour_given_when_alternative_exists(self):
        events = [
            event("c", "camera", "snapshot", "waterfall", 1760, -2),
            event("g", "gate", "flow", "gate-east", 100, -1),
        ]
        # 没有替代步行路径时（示例图中入口到瀑布只有 p-ge-wf）→ 硬截留
        _, d = decide_with(events, blocked=frozenset({"p-ge-wf"}))
        c = codes(d)
        self.assertIn("holdroute:gate-east:waterfall", c)
        self.assertEqual(0, c["hold:gate-east"].params["admit_per_interval"])
        self.assertTrue(c["holdroute:gate-east:waterfall"].safety)


class RampRecoveryTest(unittest.TestCase):
    def _congested_events(self):
        return [
            event("c", "camera", "snapshot", "waterfall", 1760, -2),
            event("g", "gate", "flow", "gate-east", 100, -1),
        ]

    def _recovered_events(self):
        return [
            event("c2", "camera", "snapshot", "waterfall", 300, 0),
            event("g2", "gate", "flow", "gate-east", 100, 0),
        ]

    def test_no_sudden_release_after_relief(self):
        site = make_site()
        state = PolicyState()
        t0 = at(0)
        # 红色一轮，建立 last_budget=40
        d0 = decide(site, compute_occupancy(site, self._congested_events(), DAY, t0), state, t0)
        self.assertEqual(40, codes(d0)["throttle:gate-east"].params["admit_per_interval"])

        # 缓解后立刻评估：仍应受斜坡约束，额度不可跳到满
        budgets = []
        for k in range(0, 5):
            now = t0 + timedelta(minutes=5 * k)
            d = decide(site, compute_occupancy(site, self._recovered_events(), DAY, now), state, now)
            a = codes(d).get("throttle:gate-east")
            budgets.append(a.params["admit_per_interval"] if a else None)
        # 逐级抬升（非递减），且第一步不等于容量
        self.assertEqual(40, budgets[0])
        self.assertTrue(all(budgets[i] is None or budgets[i] <= 900 for i in range(len(budgets))))
        self.assertLess(budgets[0], 900)
        # 最后限流解除
        self.assertIsNone(budgets[-1])
        # 严格递增直到 None
        numeric = [b for b in budgets if b is not None]
        self.assertEqual(numeric, sorted(numeric))

    def test_tightening_is_immediate(self):
        site = make_site()
        state = PolicyState()
        t0 = at(0)
        # 橙色：额度较高
        orange = [event("c", "camera", "snapshot", "waterfall", 1560, -2),
                  event("g", "gate", "flow", "gate-east", 100, -1)]
        d1 = decide(site, compute_occupancy(site, orange, DAY, t0), state, t0)
        b_orange = codes(d1)["throttle:gate-east"].params["admit_per_interval"]
        # 立即转红色：额度必须立即收紧，不等斜坡
        d2 = decide(site, compute_occupancy(site, self._congested_events(), DAY, t0), state, t0)
        b_red = codes(d2)["throttle:gate-east"].params["admit_per_interval"]
        self.assertLess(b_red, b_orange)

    def test_long_outage_does_not_jump_release(self):
        # 运营时段内宕机 30 分钟后才恢复评估：先维持原额度，之后每轮最多升 1 级
        site = make_site()
        state = PolicyState()
        t0 = at(0)  # 本地 18:00，运营中
        d0 = decide(site, compute_occupancy(site, self._congested_events(), DAY, t0), state, t0)
        self.assertEqual(40, codes(d0)["throttle:gate-east"].params["admit_per_interval"])

        restart = t0 + timedelta(minutes=30)
        # 恢复事件贴近重启时刻（快照在 2 分钟前），核心区已缓解
        recovered = [
            event("c2", "camera", "snapshot", "waterfall", 300, 28),
            event("g2", "gate", "flow", "gate-east", 100, 29),
        ]
        d = decide(site, compute_occupancy(site, recovered, DAY, restart), state, restart)
        a = codes(d).get("throttle:gate-east")
        self.assertIsNotNone(a)
        self.assertTrue(a.params.get("recovering"))
        self.assertEqual(40, a.params["admit_per_interval"])  # 宕机再久也先维持
        # 再过一个完整间隔，也只升 1 级：40 -> 255，而非直接 900
        later = restart + timedelta(minutes=5)
        recovered2 = [
            event("c3", "camera", "snapshot", "waterfall", 300, 33),
            event("g3", "gate", "flow", "gate-east", 100, 34),
        ]
        d2 = decide(site, compute_occupancy(site, recovered2, DAY, later), state, later)
        self.assertEqual(255, codes(d2)["throttle:gate-east"].params["admit_per_interval"])

    def test_repeated_evaluate_at_same_time_does_not_chain_release(self):
        # 同一时刻反复评估（定时器与手动评估叠加）不得连环升级
        site = make_site()
        state = PolicyState()
        t0 = at(0)
        decide(site, compute_occupancy(site, self._congested_events(), DAY, t0), state, t0)
        t1 = t0 + timedelta(minutes=5)
        view = compute_occupancy(site, self._recovered_events(), DAY, t1)
        budgets = set()
        for _ in range(5):
            d = decide(site, view, state, t1)
            budgets.add(codes(d)["throttle:gate-east"].params["admit_per_interval"])
        self.assertEqual({40}, budgets)  # 检测到缓解的当轮先维持，全部一致


class ManualOverrideSafetyTest(unittest.TestCase):
    def test_manual_cannot_relax_red_budget(self):
        events = [
            event("c", "camera", "snapshot", "waterfall", 1760, -2),
            event("g", "gate", "flow", "gate-east", 100, -1),
        ]
        # 人工试图在红色时放到 800：安全硬约束取更严
        _, d = decide_with(events, manual_throttles={"gate-east": 800})
        a = codes(d)["throttle:gate-east"]
        self.assertEqual(40, a.params["admit_per_interval"])
        self.assertTrue(a.safety)

    def test_manual_budget_applies_in_orange(self):
        events = [
            event("c", "camera", "snapshot", "waterfall", 1600, -2),
            event("g", "gate", "flow", "gate-east", 100, -1),
        ]
        _, d = decide_with(events, manual_throttles={"gate-east": 10})
        self.assertEqual(10, codes(d)["throttle:gate-east"].params["admit_per_interval"])

    def test_suppress_cannot_hide_safety_actions(self):
        events = [
            event("c", "camera", "snapshot", "waterfall", 1760, -2),
            event("g", "gate", "flow", "gate-east", 100, -1),
        ]
        # 即使接管要求压制 throttle/hold，红色安全动作仍然下发
        _, d = decide_with(events, suppressed=frozenset({"throttle", "hold", "holdroute"}))
        c = codes(d)
        self.assertIn("throttle:gate-east", c)
        self.assertTrue(c["throttle:gate-east"].safety)

    def test_suppress_hides_advisory_only(self):
        events = [
            event("g", "gate", "flow", "gate-east", 700, -1),  # 黄色
        ]
        _, d = decide_with(events, suppressed=frozenset({"advise-offpeak"}))
        self.assertNotIn("advise-offpeak:gate-east", codes(d))


class ClosingReturnTest(unittest.TestCase):
    def test_closing_stops_entry_and_opens_special(self):
        from datetime import datetime, timezone
        events = [
            event("c", "camera", "snapshot", "waterfall", 500, -2),
        ]
        # 本地 20:10 = UTC 12:10
        now = datetime(2026, 9, 20, 12, 10, tzinfo=timezone.utc)
        site, d = decide_with(events, now=now)
        c = codes(d)
        self.assertTrue(d.park_closed)
        self.assertEqual(0, c["hold:gate-east"].params["admit_per_interval"])
        self.assertIn("evac:gate-east", c)
        self.assertIn("p-ge-ex", c["evac:gate-east"].params["open_special_passages"])
        self.assertIn("boost:north", c)
        self.assertTrue(c["boost:north"].safety)

    def test_pre_open_no_actions(self):
        from datetime import datetime, timezone
        pre = datetime(2026, 9, 19, 22, 0, tzinfo=timezone.utc)  # 本地 06:00
        _, d = decide_with([], now=pre)
        self.assertEqual(GREEN, d.overall)
        self.assertEqual((), d.actions)


if __name__ == "__main__":
    unittest.main()
