import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.engine import ACK, EXECUTED, FlowEngine
from src.overrides import OverrideError
from src.store import Journal

from helpers import DAY, at, make_site


def new_engine(path):
    return FlowEngine(make_site(), Journal(path))


def flow(eid, zone, value, minute, source="gate"):
    return {"event_id": eid, "source": source, "type": "flow", "zone": zone,
            "value": value, "occurred_at": at(minute).isoformat()}


def snap(eid, zone, value, minute):
    return {"event_id": eid, "source": "camera", "type": "snapshot", "zone": zone,
            "value": value, "occurred_at": at(minute).isoformat()}


class EngineTemp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "run.jsonl")
        self._engines = []

    def tearDown(self):
        for eng in self._engines:
            eng.journal.close()
        self._tmp.cleanup()

    def engine(self):
        eng = new_engine(self.path)
        self._engines.append(eng)
        return eng


class IngestDedupTest(EngineTemp):
    def test_duplicate_late_out_of_order(self):
        eng = self.engine()
        raws = [
            snap("c1", "waterfall", 1700, -2),
            flow("g1", "gate-east", 100, -1),
            snap("c1", "waterfall", 1700, -2),       # 重复
            {"event_id": "bad", "source": "gate", "type": "flow",
             "zone": "ghost", "value": 1, "occurred_at": at(0).isoformat()},  # 非法
        ]
        summary = eng.ingest_many(raws, at(0))
        self.assertEqual({"accepted": 2, "duplicates": 1, "quarantined": 1}, summary)

    def test_gate_offline_still_trustworthy_from_camera(self):
        eng = self.engine()
        # 闸机完全无数据，仅图像快照
        eng.ingest(snap("c1", "waterfall", 1700, -1), at(0))
        ev = eng.evaluate(at(0))
        zo = ev.view.get("waterfall")
        self.assertEqual(1700, zo.occ)
        codes = {m["code"] for m in eng.measure_status(ev)}
        self.assertIn("data-alert:waterfall", codes)  # 闸机离线有告警


class OverrideLifecycleTest(EngineTemp):
    def test_requires_owner_reason_ttl(self):
        eng = self.engine()
        with self.assertRaises(OverrideError):
            eng.add_override("a", "throttle", 600, "  ", "雷电",
                             {"zone": "gate-east", "admit_per_interval": 10}, now=at(0))
        with self.assertRaises(OverrideError):
            eng.add_override("b", "throttle", 600, "赵", "",
                             {"zone": "gate-east", "admit_per_interval": 10}, now=at(0))
        with self.assertRaises(OverrideError):
            eng.add_override("c", "throttle", 0, "赵", "雷电",
                             {"zone": "gate-east", "admit_per_interval": 10}, now=at(0))
        with self.assertRaises(OverrideError):
            eng.add_override("d", "throttle", -5, "赵", "雷电",
                             {"zone": "gate-east", "admit_per_interval": 10}, now=at(0))

    def test_rejects_duplicate_id_and_unknown_refs(self):
        eng = self.engine()
        eng.add_override("o1", "throttle", 600, "赵", "雷电",
                         {"zone": "gate-east", "admit_per_interval": 10}, now=at(0))
        with self.assertRaises(OverrideError):
            eng.add_override("o1", "throttle", 600, "赵", "雷电",
                             {"zone": "gate-east", "admit_per_interval": 10}, now=at(0))
        with self.assertRaises(OverrideError):
            eng.add_override("o2", "throttle", 600, "赵", "雷电",
                             {"zone": "nope", "admit_per_interval": 10}, now=at(0))
        with self.assertRaises(OverrideError):
            eng.add_override("o3", "throttle", 30 * 86400, "赵", "雷电",
                             {"zone": "gate-east", "admit_per_interval": 10}, now=at(0))

    def test_expires_at_ttl_and_releases_via_ramp(self):
        eng = self.engine()
        # 先制造橙色拥堵建立限流
        eng.ingest_many([snap("c", "waterfall", 1600, 0), flow("g", "gate-east", 100, 0)], at(1))
        eng.evaluate(at(1))
        # 人工收紧到 10，TTL 10 分钟
        eng.add_override("o1", "throttle", 600, "赵", "雷电",
                         {"zone": "gate-east", "admit_per_interval": 10}, now=at(2))
        ev = eng.evaluate(at(2))
        self.assertEqual(10, _budget(eng, ev, "throttle:gate-east"))
        # 缓解 + 接管仍有效：保持人工额度
        eng.ingest_many([snap("c2", "waterfall", 300, 3)], at(3))
        ev = eng.evaluate(at(3))
        self.assertEqual(10, _budget(eng, ev, "throttle:gate-east"))
        # TTL 过后自动失效：恢复自动，但必须走斜坡，第一跳不得放开
        ev = eng.evaluate(at(13))
        b = _budget(eng, ev, "throttle:gate-east")
        self.assertIsNotNone(b)
        self.assertLess(b, 900)

    def test_revoke_requires_by_and_reason(self):
        eng = self.engine()
        eng.add_override("o1", "block_passage", 600, "赵", "落石",
                         {"passage": "p-ge-wf", "blocked": True}, now=at(0))
        with self.assertRaises(OverrideError):
            eng.revoke_override("o1", "", "", now=at(5))
        eng.revoke_override("o1", "赵", "排除险情", now=at(6))
        with self.assertRaises(OverrideError):
            eng.revoke_override("o1", "赵", "再次", now=at(7))


class ReceiptTest(EngineTemp):
    def test_valid_and_invalid_transitions(self):
        eng = self.engine()
        eng.ingest_many([snap("c", "waterfall", 1760, 0), flow("g", "gate-east", 100, 0)], at(1))
        ev = eng.evaluate(at(1))
        code = "throttle:gate-east"
        # 不能直接 pending -> executed
        with self.assertRaises(ValueError):
            eng.post_receipt(code, EXECUTED, "岗", now=at(2))
        eng.post_receipt(code, ACK, "岗", "收到", now=at(2))
        eng.post_receipt(code, EXECUTED, "岗", "执行", now=at(3))
        ev2 = eng.evaluate(at(4))
        status = {m["code"]: m["status"] for m in eng.measure_status(ev2)}
        self.assertEqual(EXECUTED, status[code])


class RestartRecoveryTest(EngineTemp):
    def test_recovers_measures_and_is_deterministic(self):
        eng = self.engine()
        eng.ingest_many([snap("c", "waterfall", 1760, 0), flow("g", "gate-east", 100, 0)], at(1))
        eng.add_override("o1", "throttle", 1800, "赵", "雷电",
                         {"zone": "gate-east", "admit_per_interval": 10}, now=at(2))
        ev1 = eng.evaluate(at(2))
        eng.post_receipt("throttle:gate-east", ACK, "岗", "控流", now=at(3))

        # 重启：新引擎从同一日志恢复
        eng2 = self.engine()
        eng2.restore()
        self.assertEqual(2, len(eng2._events))
        self.assertEqual(1, len(eng2._overrides))

        ev2 = eng2.evaluate(at(5), persist=False)
        # 在同一时刻的动作、额度、状态与崩溃前语义一致
        b1 = _budget(eng, ev1, "throttle:gate-east")
        b2 = _budget(eng2, ev2, "throttle:gate-east")
        self.assertEqual(b1, b2)
        self.assertEqual(eng._state.to_dict(), eng2._state.to_dict())
        status = {m["code"]: m["status"] for m in eng2.measure_status(ev2)}
        self.assertEqual(ACK, status["throttle:gate-east"])

        # 确定性：再次用同一日志恢复，同一时刻评估结果仍相同
        eng3 = self.engine()
        eng3.restore()
        ev3 = eng3.evaluate(at(5), persist=False)
        self.assertEqual(
            [a.to_dict() for a in ev2.decision.actions],
            [a.to_dict() for a in ev3.decision.actions],
        )

    def test_cross_day_resets_throttle_and_closing_actions(self):
        eng = self.engine()
        eng.ingest_many([snap("c", "waterfall", 1760, 0), flow("g", "gate-east", 100, 0)], at(1))
        eng.evaluate(at(1))
        self.assertTrue(eng._state.to_dict())  # 有限流状态

        # 次日运营时段（UTC 02:00 = 本地 10:00）
        next_day = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc)
        ev = eng.evaluate(next_day)
        self.assertEqual("2026-09-21", ev.day)
        self.assertEqual({}, eng._state.to_dict())  # 跨日归零

        # 当日闭园（本地 20:10 = UTC 12:10）
        closing = datetime(2026, 9, 21, 12, 10, tzinfo=timezone.utc)
        eng.ingest(
            {"event_id": "c2", "source": "camera", "type": "snapshot", "zone": "waterfall",
             "value": 400, "occurred_at": "2026-09-21T12:08:00+00:00"}, closing)
        evc = eng.evaluate(closing)
        codes = {a.code for a in evc.decision.actions}
        self.assertIn("evac:gate-east", codes)
        self.assertIn("boost:north", codes)
        self.assertEqual(0, _budget(eng, evc, "hold:gate-east"))


class ReplayTest(EngineTemp):
    def test_replay_contains_full_chain(self):
        eng = self.engine()
        eng.ingest_many([snap("c", "waterfall", 1760, 0), flow("g", "gate-east", 100, 0)], at(1))
        eng.add_override("o1", "throttle", 600, "赵", "雷电",
                         {"zone": "gate-east", "admit_per_interval": 10}, now=at(2))
        ev = eng.evaluate(at(2))
        eng.post_receipt("throttle:gate-east", ACK, "岗", now=at(3))
        chain = eng.replay(DAY)
        self.assertEqual(2, len(chain["events"]))
        self.assertGreaterEqual(len(chain["ticks"]), 1)
        self.assertEqual(1, len(chain["overrides"]))
        self.assertEqual(1, len(chain["receipts"]))
        # tick 中含动作依据，可还原决策链
        actions = chain["ticks"][-1]["decision"]["actions"]
        self.assertTrue(any(a["reason"] for a in actions))


def _budget(eng, ev, code):
    for m in eng.measure_status(ev):
        if m["code"] == code:
            return m["params"]["admit_per_interval"]
    raise KeyError(code)


if __name__ == "__main__":
    unittest.main()
