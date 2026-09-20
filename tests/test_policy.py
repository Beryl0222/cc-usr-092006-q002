"""策略引擎：风险分级、消防硬限、分流、摆渡、人工接管底线、斜坡、返程。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from tests._support import DAY1_10, new_service


def action_types(decision, zone=None):
    return sorted(
        {(a["type"], a["zone"]) for a in decision["actions"]
         if zone is None or a["zone"] == zone}
    )


def find(decision, atype, zone=None, basis=None):
    for a in decision["actions"]:
        if a["type"] == atype and (zone is None or a["zone"] == zone) \
                and (basis is None or a["basis"] == basis):
            return a
    return None


class RiskAndHardLimitTest(unittest.TestCase):
    def test_warning_triggers_restrict_and_divert(self) -> None:
        svc, clk, _ = new_service()
        # waterfall 实测 1600/1800 = 89% 预警；camera 上报避免陈旧缓冲
        for i in range(1600):
            svc.ingest(f"g{i}", "gate_enter", "gate", "waterfall", clk.now())
        svc.ingest("c1", "camera_count", "camera", "waterfall", clk.now(), quantity=1600)
        d = svc.tick()
        self.assertIsNotNone(find(d, "restrict_admission", "waterfall"))
        # waterfall 无向外步行通道（只有返回摆渡），应调度摆渡
        self.assertIsNotNone(find(d, "dispatch_shuttle", "waterfall"))

    def test_fire_capacity_forces_exit_only_even_under_override(self) -> None:
        svc, clk, _ = new_service()
        svc.add_override(
            "OV-FIRE", "waterfall", "设备故障人工处置", "李队",
            clk.now() + timedelta(hours=2),
            actions=[{"action_type": "restrict_admission", "params": {"max_per_minute": 500}}],
        )
        for i in range(1801):
            svc.ingest(f"g{i}", "gate_enter", "gate", "waterfall", clk.now())
        svc.ingest("c1", "camera_count", "camera", "waterfall", clk.now(), quantity=1801)
        d = svc.tick()
        stop = find(d, "entry_stop", "waterfall", basis="hard")
        self.assertIsNotNone(stop)
        self.assertEqual("exit_only", stop["params"]["mode"])
        # 人工要求放大入园的动作被强制改写
        manual = find(d, "manual", "waterfall")
        self.assertEqual("entry_stop", manual["params"]["manual_action_type"])
        # 特殊通道仍保留
        self.assertEqual(1, stop["keep_special_lanes"])
        # 上游同时被拦截
        self.assertIsNotNone(find(d, "entry_stop", "gate-east", basis="hard"))
        self.assertIsNotNone(find(d, "entry_stop", "shuttle-north", basis="hard"))

    def test_prediction_is_advisory_only(self) -> None:
        svc, clk, _ = new_service()
        # 实测很低，但售出大批 10 分钟后时段票
        svc.ingest("g1", "gate_enter", "gate", "waterfall", clk.now(), quantity=100)
        svc.ingest("c1", "camera_count", "camera", "waterfall", clk.now(), quantity=100)
        svc.ingest("t1", "ticket_sold", "ticket", "waterfall", clk.now(),
                   quantity=1500, slot_start=clk.now() + timedelta(minutes=10),
                   correlation_id="BIG")
        d = svc.tick()
        advisory = find(d, "restrict_admission", "waterfall", basis="advisory")
        self.assertIsNotNone(advisory)
        self.assertTrue(advisory["params"].get("advisory"))
        # 不得仅凭预测触发硬限
        self.assertIsNone(find(d, "entry_stop", "waterfall", basis="hard"))

    def test_special_lanes_preserved_on_restriction(self) -> None:
        svc, clk, _ = new_service()
        for i in range(380):  # shuttle-north 420 容量，>85%
            svc.ingest(f"s{i}", "shuttle_arrive", "shuttle", "shuttle-north", clk.now())
        svc.ingest("c1", "camera_count", "camera", "shuttle-north", clk.now(), quantity=380)
        d = svc.tick()
        act = find(d, "restrict_admission", "shuttle-north")
        self.assertEqual(1, act["keep_special_lanes"])
        self.assertEqual(1, act["params"]["keep_special_lanes"])


class UpstreamPreRestrictTest(unittest.TestCase):
    def test_upstream_held_before_downstream_hard_limit(self) -> None:
        svc, clk, _ = new_service()
        # 下游瀑布区 89%（预警未硬限），上游仍在放行
        for i in range(1600):
            svc.ingest(f"g{i}", "gate_enter", "gate", "waterfall", clk.now())
        svc.ingest("c1", "camera_count", "camera", "waterfall", clk.now(), quantity=1600)
        # 停车区仍有车辆进场（来流领先指标）
        svc.ingest("p1", "park_enter", "parking", "gate-east", clk.now(), quantity=120)
        d = svc.tick()
        pre_gate = next(
            (a for a in d["actions"]
             if a["type"] == "restrict_admission" and a["zone"] == "gate-east"
             and a["params"].get("target") == "waterfall"),
            None,
        )
        self.assertIsNotNone(pre_gate, "下游预警时上游应被提前限流")
        self.assertGreaterEqual(pre_gate["params"]["max_per_minute"], 0)
        # 下游尚未硬限，不应有 entry_stop
        self.assertIsNone(find(d, "entry_stop", "waterfall", basis="hard"))


class OverrideAndRampTest(unittest.TestCase):
    def test_override_requires_terms(self) -> None:
        svc, clk, _ = new_service()
        with self.assertRaises(ValueError):
            svc.add_override("X", "waterfall", "  ", "李队",
                             clk.now() + timedelta(hours=1))
        with self.assertRaises(ValueError):
            svc.add_override("Y", "waterfall", "暴雨", "  ",
                             clk.now() + timedelta(hours=1))
        with self.assertRaises(ValueError):
            svc.add_override("Z", "waterfall", "暴雨", "李队", clk.now())

    def test_suppress_auto_keeps_hard_limits(self) -> None:
        svc, clk, _ = new_service()
        svc.add_override("OV-S", "gate-east", "雷电预警", "王总",
                         clk.now() + timedelta(hours=1), mode="suppress_auto")
        for i in range(800):  # 89%
            svc.ingest(f"g{i}", "gate_enter", "gate", "gate-east", clk.now())
        svc.ingest("p1", "park_enter", "parking", "gate-east", clk.now(), quantity=10)
        d = svc.tick()
        # 自动限流被挂起并在 suppressed 可见
        self.assertTrue(any(s["zone"] == "gate-east" for s in d["suppressed"]))
        # 未到硬限，不产生 entry_stop
        self.assertIsNone(find(d, "entry_stop", "gate-east", basis="hard"))
        # 触及硬限时安全底线仍在
        for i in range(101):
            svc.ingest(f"h{i}", "gate_enter", "gate", "gate-east", clk.now())
        svc.ingest("c1", "camera_count", "camera", "gate-east", clk.now(), quantity=901)
        d = svc.tick()
        self.assertIsNotNone(find(d, "entry_stop", "gate-east", basis="hard"))

    def test_ramp_release_after_override_expires(self) -> None:
        svc, clk, _ = new_service()
        svc.add_override("OV-R", "gate-east", "暴雨", "值班长",
                         clk.now() + timedelta(hours=1), mode="suppress_auto")
        svc.tick()
        clk.set(clk.now() + timedelta(hours=1, minutes=1))
        d = svc.tick()  # 到期这一拍：斜坡从 0 开始
        self.assertEqual(0.0, d["ramps"].get("gate-east"))
        clk.advance(120)
        d = svc.tick()
        # 5%/分钟 × 容量900 × 2分钟 = 90
        self.assertEqual(90.0, d["ramps"]["gate-east"])
        clk.advance(60 * 20)
        d = svc.tick()  # 超出 20 分钟放坡窗口后移除
        self.assertNotIn("gate-east", d["ramps"])


    def test_ramp_waits_while_hard_limit_or_return_window(self) -> None:
        from src.domain import Schedule
        svc, clk, _ = new_service(sched=Schedule(close_hour=18))
        # 返程窗口内接管入口
        clk.set(DAY1_10.replace(hour=16, minute=40))
        svc.add_override("OV-W", "gate-east", "暴雨", "值班长",
                         clk.now() + timedelta(minutes=30), mode="suppress_auto")
        svc.tick()
        clk.advance(31 * 60)  # 接管到期，仍在返程窗口
        d = svc.tick()
        self.assertNotIn("gate-east", d["ramps"])  # 返程保障阻塞，斜坡挂起

        # 次日开园后且非返程窗口，阻塞解除，斜坡才开始
        clk.set(DAY1_10.replace(hour=10) + timedelta(days=1))
        d = svc.tick()
        self.assertEqual(0.0, d["ramps"].get("gate-east"))
        clk.advance(60)
        d = svc.tick()
        self.assertEqual(45.0, d["ramps"]["gate-east"])  # 5%×900×1分钟

    def test_ramp_restarts_if_zone_hits_hard_limit_again(self) -> None:
        svc, clk, _ = new_service()
        svc.add_override("OV-R2", "gate-east", "雷电", "值班长",
                         clk.now() + timedelta(hours=1), mode="suppress_auto")
        svc.tick()
        clk.set(clk.now() + timedelta(hours=1, minutes=1))
        svc.tick()  # 斜坡启动
        clk.advance(120)
        d = svc.tick()
        self.assertEqual(90.0, d["ramps"]["gate-east"])
        # 放坡期间区域再次触限
        for i in range(901):
            svc.ingest(f"x{i}", "gate_enter", "gate", "gate-east", clk.now())
        svc.ingest("cx", "camera_count", "camera", "gate-east", clk.now(), quantity=901)
        d = svc.tick()
        self.assertNotIn("gate-east", d["ramps"])
        self.assertTrue(any(a["type"] == "entry_stop" and a["zone"] == "gate-east"
                            for a in d["actions"]))


class ClosureTest(unittest.TestCase):
    def test_closed_link_excluded_from_diversion(self) -> None:
        svc, clk, _ = new_service()
        svc.add_closure(("waterfall", "gate-east", "shuttle-return"),
                        "栈道维修", "工程组", clk.now() + timedelta(hours=2))
        for i in range(1600):
            svc.ingest(f"g{i}", "gate_enter", "gate", "waterfall", clk.now())
        svc.ingest("c1", "camera_count", "camera", "waterfall", clk.now(), quantity=1600)
        d = svc.tick()
        self.assertIsNone(find(d, "dispatch_shuttle", "waterfall"))
        self.assertTrue(any(c["reason"] == "栈道维修" for c in svc.status()["closures"]))

    def test_closure_requires_terms(self) -> None:
        svc, clk, _ = new_service()
        with self.assertRaises(ValueError):
            svc.add_closure(("waterfall", "gate-east", "shuttle-return"),
                            "", "工程组", clk.now() + timedelta(hours=1))


class ReturnGuardTest(unittest.TestCase):
    def test_return_window_stops_new_admission_and_dispatches(self) -> None:
        from src.domain import Schedule
        svc, clk, _ = new_service(sched=Schedule(close_hour=18))
        clk.set(DAY1_10.replace(hour=16, minute=35))  # 距闭园 85 分钟
        for i in range(300):
            svc.ingest(f"g{i}", "gate_enter", "gate", "waterfall", clk.now())
        svc.ingest("c1", "camera_count", "camera", "waterfall", clk.now(), quantity=300)
        d = svc.tick()
        hold = find(d, "restrict_admission", "gate-east", basis="hard")
        self.assertIsNotNone(hold)
        self.assertEqual(0, hold["params"]["max_per_minute"])
        self.assertTrue(any(a["return_priority"] for a in d["actions"]))

    def test_after_close_only_return_actions(self) -> None:
        svc, clk, _ = new_service()
        clk.set(DAY1_10.replace(hour=18, minute=20))
        for i in range(300):
            svc.ingest(f"g{i}", "gate_enter", "gate", "waterfall",
                       DAY1_10.replace(hour=17))
        d = svc.tick()
        # 普通自动限流不产生（无 warning 区域，且闭园挂起）
        self.assertFalse(any(a["basis"] == "auto" for a in d["actions"]))
        # 仍有返程摆渡
        self.assertTrue(any(a["type"] == "return_priority" for a in d["actions"]))


if __name__ == "__main__":
    unittest.main()
