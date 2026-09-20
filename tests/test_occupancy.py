"""占用归并：重复、乱序、迟到、闸机离线、快照融合、分时票。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from tests._support import DAY1_10, clock, links, new_service, zones


class OccupancyMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clk, _ = new_service()

    def tearDown(self) -> None:
        self.svc.close()

    def _enter(self, eid, zone, n=1, at=None):
        return self.svc.ingest(
            eid, "gate_enter", "gate", zone, at or self.clk.now(), quantity=n
        )

    def test_duplicate_event_applied_once(self) -> None:
        r1 = self._enter("g-1", "waterfall", 50)
        r2 = self._enter("g-1", "waterfall", 50)  # 同 source+event_id 重推
        self.assertTrue(r1["accepted"])
        self.assertFalse(r2["accepted"])
        self.assertEqual("duplicate", r2["reason"])
        self.svc.ingest("c1", "camera_count", "camera", "waterfall",
                        self.clk.now(), quantity=50)
        status = self.svc.status()
        self.assertEqual(50, status["zones"]["waterfall"]["estimated"])

    def test_out_of_order_events_merge_by_occurred_time(self) -> None:
        t0 = self.clk.now()
        # 先收到较晚的，再收到较早的（乱序迟到），结果应与顺序到达一致
        self._enter("late-2", "waterfall", 30, t0 - timedelta(minutes=5))
        self._enter("late-1", "waterfall", 20, t0 - timedelta(minutes=9))
        status = self.svc.status()
        self.assertEqual(50, status["zones"]["waterfall"]["ledger"])

    def test_too_late_event_rejected(self) -> None:
        old = self.clk.now() - timedelta(hours=7)
        r = self._enter("ancient", "waterfall", 10, old)
        self.assertFalse(r["accepted"])
        self.assertEqual("too_late", r["reason"])
        rejected = self.svc.status()["rejected_events"]
        self.assertEqual("too_late", rejected[-1]["reason"])

    def test_negative_ledger_clamped(self) -> None:
        self.svc.ingest("x1", "gate_exit", "gate", "waterfall", self.clk.now(), quantity=30)
        status = self.svc.status()
        self.assertEqual(0, status["zones"]["waterfall"]["ledger"])

    def test_camera_snapshot_fused_conservatively(self) -> None:
        self._enter("g1", "shuttle-north", 100)
        # 图像计数高于台账（有漏计），取高者
        self.svc.ingest("c1", "camera_count", "camera", "shuttle-north",
                        self.clk.now(), quantity=260)
        reading = self.svc.status()["zones"]["shuttle-north"]
        self.assertEqual(260, reading["estimated"])
        self.assertEqual(100, reading["ledger"])
        # 更旧的快照不能覆盖更新的
        self.svc.ingest("c0", "camera_count", "camera", "shuttle-north",
                        self.clk.now() - timedelta(minutes=2), quantity=400)
        self.assertEqual(260, self.svc.status()["zones"]["shuttle-north"]["estimated"])

    def test_gate_offline_adds_stale_buffer_and_freshness(self) -> None:
        self._enter("g1", "waterfall", 100)
        self.svc.ingest("cam1", "camera_count", "camera", "waterfall",
                        self.clk.now(), quantity=100)
        # 闸机 5 分钟无数据（超 180s 阈值），camera 仍新鲜
        self.clk.advance(300)
        status = self.svc.status()
        wf = status["zones"]["waterfall"]
        states = {f["source"]: f["state"] for f in wf["freshness"]}
        self.assertEqual("stale", states["gate"])
        self.assertEqual("fresh", states["camera"])
        self.assertGreater(wf["stale_buffer"], 0)
        self.assertEqual(100 + wf["stale_buffer"], wf["estimated"])

    def test_all_sources_silent_before_open_still_readable(self) -> None:
        # 开园后尚未有任何上报：数值可读（0），来源标 silent
        status = self.svc.status()
        wf = status["zones"]["waterfall"]
        self.assertTrue(all(f["state"] == "silent" for f in wf["freshness"]))
        self.assertEqual(0, wf["estimated"])


class TicketTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clk, _ = new_service()

    def tearDown(self) -> None:
        self.svc.close()

    def test_sold_ticket_predicted_then_used_occupies(self) -> None:
        slot = self.clk.now() + timedelta(minutes=10)
        self.svc.ingest("t1", "ticket_sold", "ticket", "waterfall", self.clk.now(),
                        quantity=40, slot_start=slot, correlation_id="C1")
        self.svc.tick()
        wf = self.svc.status()["zones"]["waterfall"]
        self.assertEqual(40, wf["predicted_add"])
        self.assertEqual(0, wf["estimated"])  # 预测不进实测

        self.svc.ingest("u1", "ticket_used", "ticket", "waterfall", self.clk.now(),
                        quantity=40, correlation_id="C1")
        wf = self.svc.status()["zones"]["waterfall"]
        self.assertEqual(40, wf["estimated"])
        self.assertEqual(0, wf["predicted_add"])


class CrossDayTest(unittest.TestCase):
    def test_rollover_resets_ledger_and_voids_unused_tickets(self) -> None:
        svc, clk, _ = new_service()
        svc.ingest("d1g", "gate_enter", "gate", "waterfall", clk.now(), quantity=500)
        # 昨日时段的未核销票
        svc.ingest("d1t", "ticket_sold", "ticket", "waterfall", clk.now(),
                   quantity=99, slot_start=clk.now() + timedelta(minutes=5),
                   correlation_id="OLD")
        self.assertEqual(500, svc.status()["zones"]["waterfall"]["estimated"])
        self.assertEqual(1, svc.status()["queued_tickets"])

        # 跨到次日开园后
        clk.set(DAY1_10 + timedelta(days=1))
        status = svc.status()
        self.assertEqual("2026-09-21", status["business_date"])
        self.assertEqual(0, status["zones"]["waterfall"]["estimated"])
        self.assertEqual(0, status["queued_tickets"])

    def test_rollover_keeps_future_day_reservations(self) -> None:
        svc, clk, _ = new_service()
        future_slot = DAY1_10 + timedelta(days=2)
        svc.ingest("future", "ticket_sold", "ticket", "waterfall", clk.now(),
                   quantity=2, slot_start=future_slot, correlation_id="FUT")
        clk.set(DAY1_10 + timedelta(days=1, hours=1))
        self.assertEqual(1, svc.status()["queued_tickets"])


if __name__ == "__main__":
    unittest.main()
