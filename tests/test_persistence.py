"""持久化、重启恢复与决策链回放。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from src.clock import FixedClock
from src.domain import PolicyConfig, Schedule
from src.replay import replay
from src.service import FlowService
from tests._support import DAY1_10, links, new_service, zones


def reopen_service(path: str, clk: FixedClock, cfg=None, sched=None) -> FlowService:
    return FlowService(
        zones(), links(), path, clock=clk,
        config=cfg or PolicyConfig(), schedule=sched or Schedule(),
    )


class RecoveryTest(unittest.TestCase):
    def test_occupancy_recovered_after_restart(self) -> None:
        svc, clk, path = new_service()
        svc.ingest("g1", "gate_enter", "gate", "waterfall", clk.now(), quantity=700)
        svc.ingest("c1", "camera_count", "camera", "waterfall", clk.now(), quantity=720)
        svc.tick()
        svc.close()

        svc2 = reopen_service(path, clk)
        status = svc2.status()
        self.assertEqual(720, status["zones"]["waterfall"]["estimated"])
        svc2.close()

    def test_duplicate_after_restart_still_dropped(self) -> None:
        svc, clk, path = new_service()
        svc.ingest("g1", "gate_enter", "gate", "waterfall", clk.now(), quantity=10)
        svc.close()
        svc2 = reopen_service(path, clk)
        r = svc2.ingest("g1", "gate_enter", "gate", "waterfall", clk.now(), quantity=10)
        self.assertFalse(r["accepted"])
        self.assertEqual("duplicate", r["reason"])
        svc2.close()

    def test_active_override_and_closure_survive_restart(self) -> None:
        svc, clk, path = new_service()
        svc.add_override("OV1", "gate-east", "暴雨封园准备", "赵总",
                         clk.now() + timedelta(hours=2), mode="suppress_auto")
        svc.add_closure(("gate-east", "waterfall", "walk-river"),
                        "边坡落石", "工程组", clk.now() + timedelta(hours=2))
        svc.tick()
        svc.close()

        svc2 = reopen_service(path, clk)
        status = svc2.status()
        self.assertEqual("OV1", status["active_overrides"][0]["override_id"])
        self.assertEqual("walk-river", status["closures"][0]["link_key"][2])
        svc2.close()

    def test_expired_override_not_restored(self) -> None:
        svc, clk, path = new_service()
        svc.add_override("OV-OLD", "gate-east", "短时雷雨", "赵总",
                         clk.now() + timedelta(minutes=30), mode="suppress_auto")
        svc.close()
        clk.advance(60 * 60)
        svc2 = reopen_service(path, clk)
        self.assertEqual([], svc2.status()["active_overrides"])
        svc2.close()

    def test_receipts_recovered(self) -> None:
        svc, clk, path = new_service()
        for i in range(1600):
            svc.ingest(f"g{i}", "gate_enter", "gate", "waterfall", clk.now())
        svc.ingest("c1", "camera_count", "camera", "waterfall", clk.now(), quantity=1600)
        d = svc.tick()
        restrict = next(a for a in d["actions"] if a["type"] == "restrict_admission")
        svc.record_receipt(restrict["action_id"], "executed", "检票班", "已开启备用通道")
        svc.close()

        svc2 = reopen_service(path, clk)
        d2 = svc2.tick()
        again = next(a for a in d2["actions"]
                     if a["type"] == "restrict_admission"
                     and a["zone"] == restrict["zone"])
        self.assertIsNotNone(again["receipt"])
        self.assertEqual("executed", again["receipt"]["status"])
        svc2.close()

    def test_ramp_continues_after_restart(self) -> None:
        svc, clk, path = new_service()
        svc.add_override("OV-R", "gate-east", "暴雨", "值班长",
                         clk.now() + timedelta(hours=1), mode="suppress_auto")
        svc.tick()
        clk.set(clk.now() + timedelta(hours=1, minutes=1))
        svc.tick()  # 斜坡开始
        clk.advance(120)
        svc.tick()  # 应放至 90/分钟
        svc.close()

        svc2 = reopen_service(path, clk)
        d = svc2.tick()
        self.assertEqual(90.0, d["ramps"]["gate-east"])
        svc2.close()

    def test_sequence_continues_after_restart(self) -> None:
        svc, clk, path = new_service()
        svc.ingest("g1", "gate_enter", "gate", "waterfall", clk.now())
        svc.close()
        svc2 = reopen_service(path, clk)
        r = svc2.ingest("g2", "gate_enter", "gate", "waterfall", clk.now())
        self.assertEqual(2, r["sequence"])
        svc2.close()


class ReplayDeterminismTest(unittest.TestCase):
    def test_replay_matches_historical_decisions(self) -> None:
        svc, clk, path = new_service()

        # 构造一段含预警、接管、封闭、回执的完整拥堵过程
        for i in range(1500):
            svc.ingest(f"g{i}", "gate_enter", "gate", "waterfall", clk.now())
        svc.ingest("c1", "camera_count", "camera", "waterfall", clk.now(), quantity=1500)
        svc.tick()

        clk.advance(120)
        svc.add_override("OV1", "gate-east", "雷电", "值班长",
                         clk.now() + timedelta(hours=1),
                         actions=[{"action_type": "restrict_admission",
                                   "params": {"max_per_minute": 5}}])
        svc.add_closure(("gate-east", "waterfall", "walk-river"),
                        "临时管控", "安保组", clk.now() + timedelta(hours=1))
        for i in range(200):
            svc.ingest(f"h{i}", "gate_enter", "gate", "waterfall", clk.now())
        svc.ingest("c2", "camera_count", "camera", "waterfall", clk.now(), quantity=1700)
        d2 = svc.tick()
        svc.record_receipt(d2["actions"][0]["action_id"], "acknowledged", "值班长")

        clk.advance(60 * 60 + 60)
        for i in range(10):
            svc.ingest(f"e{i}", "gate_exit", "gate", "waterfall", clk.now(), quantity=10)
        svc.tick()
        svc.close()

        result = replay(zones(), links(), path)
        self.assertGreater(result.events_replayed, 0)
        self.assertEqual([], result.mismatches, result.mismatches)

    def test_replay_with_duplicates_and_late_events(self) -> None:
        svc, clk, path = new_service()
        svc.ingest("g1", "gate_enter", "gate", "waterfall", clk.now(), quantity=100)
        svc.tick()
        clk.advance(60)
        # 重复重推
        svc.ingest("g1", "gate_enter", "gate", "waterfall",
                   clk.now() - timedelta(seconds=30), quantity=100)
        # 迟到但未超窗
        svc.ingest("late1", "gate_exit", "gate", "waterfall",
                   clk.now() - timedelta(minutes=2), quantity=20)
        svc.tick()
        svc.close()

        result = replay(zones(), links(), path)
        self.assertEqual(1, result.duplicates_dropped)
        self.assertEqual([], result.mismatches, result.mismatches)

    def test_replay_across_day_boundary(self) -> None:
        sched = Schedule(open_hour=7, close_hour=18)
        svc, clk, path = new_service(sched=sched)
        clk.set(DAY1_10.replace(hour=17, minute=50))
        for i in range(300):
            svc.ingest(f"g{i}", "gate_enter", "gate", "waterfall", clk.now())
        svc.ingest("c1", "camera_count", "camera", "waterfall", clk.now(), quantity=300)
        svc.tick()
        clk.set(DAY1_10 + timedelta(days=1, hours=2))  # 次日凌晨，未开园
        svc.tick()
        clk.set(DAY1_10 + timedelta(days=1, hours=8))  # 次日开园后
        svc.ingest("ng1", "gate_enter", "gate", "waterfall", clk.now(), quantity=10)
        svc.ingest("nc1", "camera_count", "camera", "waterfall", clk.now(), quantity=10)
        svc.tick()
        svc.close()

        result = replay(zones(), links(), path, schedule=sched)
        self.assertEqual([], result.mismatches, result.mismatches)
        # 最后一次决策的读数中昨日占用已清零
        last = result.decisions[-1]
        wf = last["risks"]["waterfall"]
        self.assertAlmostEqual(10 / 1800, wf["ratio_estimated"], places=4)


class ChainTest(unittest.TestCase):
    def test_chain_returns_window_and_incidents(self) -> None:
        svc, clk, path = new_service()
        for i in range(5):
            clk.advance(60)
            svc.ingest(f"g{i}", "gate_enter", "gate", "waterfall", clk.now(), quantity=300)
            svc.ingest(f"c{i}", "camera_count", "camera", "waterfall",
                       clk.now(), quantity=300 * (i + 1))
            svc.tick()
        svc.add_override("OVC", "gate-east", "拥堵演练", "指挥中心",
                         clk.now() + timedelta(hours=1), mode="suppress_auto")
        anchor = svc.tick()["decision_id"]

        chain = svc.chain(anchor, window=2)
        self.assertEqual(anchor, chain["anchor"])
        self.assertLessEqual(len(chain["decisions"]), 5)
        kinds = {inc["kind"] for inc in chain["incidents"]}
        self.assertIn("override_add", kinds)
        svc.close()


if __name__ == "__main__":
    unittest.main()
