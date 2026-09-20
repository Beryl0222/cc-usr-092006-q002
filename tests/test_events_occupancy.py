import unittest
from datetime import datetime, timezone

from src.events import InvalidEvent, normalize
from src.occupancy import (
    FRESH, HIGH, NONE, OFFLINE, compute_occupancy,
)

from helpers import DAY, at, event, make_site


def occ(events, now=None):
    site = make_site()
    return site, compute_occupancy(site, events, DAY, now or at(0))


class NormalizeTest(unittest.TestCase):
    def setUp(self):
        site = make_site()
        self.zones = frozenset(z.code for z in site.zones)
        self.sources = frozenset(site.sources)

    def test_valid(self):
        e = normalize({"event_id": "a", "source": "gate", "type": "flow",
                       "zone": "waterfall", "value": 5,
                       "occurred_at": "2026-09-20T09:59:00+00:00"}, self.zones, self.sources)
        self.assertEqual(5, e.value)
        self.assertEqual("2026-09-20", e.operating_day)

    def test_naive_time_assumes_local_tz(self):
        e = normalize({"event_id": "a", "source": "gate", "type": "flow",
                       "zone": "waterfall", "value": 5,
                       "occurred_at": "2026-09-20T18:00:00"}, self.zones, self.sources)
        self.assertEqual(datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc), e.occurred_at)

    def test_rejects_unknown_fields(self):
        for patch in (
            {"source": "ghost"}, {"zone": "ghost"}, {"type": "snapshotx"},
            {"event_id": ""}, {"value": -1, "type": "snapshot"},
            {"value": True}, {"occurred_at": "not-a-time"},
        ):
            raw = {"event_id": "a", "source": "gate", "type": "flow",
                   "zone": "waterfall", "value": 5,
                   "occurred_at": "2026-09-20T09:59:00+00:00"}
            raw.update(patch)
            with self.assertRaises(InvalidEvent):
                normalize(raw, self.zones, self.sources)

    def test_rejects_future_event(self):
        with self.assertRaises(InvalidEvent):
            normalize({"event_id": "a", "source": "gate", "type": "flow",
                       "zone": "waterfall", "value": 5,
                       "occurred_at": "2026-09-20T11:00:00+00:00",
                       "received_at": "2026-09-20T10:00:00+00:00"}, self.zones, self.sources)


class OccupancyFusionTest(unittest.TestCase):
    def test_duplicate_and_out_of_order_are_idempotent(self):
        # 同一集合无论到达顺序，融合结果一致
        e1 = event("e1", "gate", "flow", "waterfall", 1000, minute_offset=-10)
        e2 = event("e2", "gate", "flow", "waterfall", 200, minute_offset=-5)
        e2dup = event("e2", "gate", "flow", "waterfall", 200, minute_offset=-5,
                      received_offset=0)  # 重复投递
        site, v_a = occ([e2, e1, e2dup])
        _, v_b = occ([e1, e2, e2dup])
        self.assertEqual(v_a.get("waterfall").occ, v_b.get("waterfall").occ)
        self.assertEqual(1200, v_a.get("waterfall").occ)

    def test_snapshot_overrides_drifting_flow(self):
        e1 = event("e1", "gate", "flow", "waterfall", 1500, minute_offset=-8)
        snap = event("c1", "camera", "snapshot", "waterfall", 1200, minute_offset=-2)
        _, v = occ([e1, snap])
        zo = v.get("waterfall")
        self.assertEqual(1200, zo.occ)
        self.assertEqual("snapshot", zo.basis)
        self.assertEqual(HIGH, zo.confidence)

    def test_fused_after_stale_snapshot(self):
        # 快照在 9 分钟前（>camera 300s 窗口），之后有净增量
        snap = event("c1", "camera", "snapshot", "waterfall", 1000, minute_offset=-9)
        after = event("g1", "gate", "flow", "waterfall", 100, minute_offset=-1)
        _, v = occ([snap, after])
        zo = v.get("waterfall")
        self.assertEqual(1100, zo.occ)
        self.assertEqual("fused", zo.basis)

    def test_gate_offline_is_unknown_not_zero(self):
        # 闸机整日无数据、且无快照：占用量未知（不能猜成 0）
        _, v = occ([])
        zo = v.get("gate-east")
        self.assertIsNone(zo.occ)
        self.assertEqual(NONE, zo.confidence)
        self.assertTrue(all(s.status == OFFLINE for s in zo.sources))

    def test_fresh_snapshot_with_silent_gate(self):
        snap = event("c1", "camera", "snapshot", "waterfall", 1700, minute_offset=-1)
        _, v = occ([snap])
        zo = v.get("waterfall")
        self.assertEqual(1700, zo.occ)
        statuses = {s.source: s.status for s in zo.sources}
        self.assertEqual(FRESH, statuses["camera"])
        self.assertEqual(OFFLINE, statuses["gate"])

    def test_late_event_before_now_is_merged(self):
        # 迟到很久但发生在评估时刻之前的事件仍被采纳
        old = event("g1", "gate", "flow", "waterfall", 300, minute_offset=-20,
                    received_offset=-1)
        _, v = occ([old])
        self.assertEqual(300, v.get("waterfall").occ)

    def test_future_event_not_yet_counted(self):
        fut = event("g1", "gate", "flow", "waterfall", 300, minute_offset=20)
        _, v = occ([fut], now=at(10))
        self.assertIsNone(v.get("waterfall").occ)

    def test_lead_pressure_only_advisory_input(self):
        site = make_site()
        tk = event("t1", "ticket", "lead", "gate-east", 300, minute_offset=-5)
        pk = event("p1", "parking", "lead", "gate-east", 80, minute_offset=-3)
        v = compute_occupancy(site, [tk, pk], DAY, at(10))
        zo = v.get("gate-east")
        self.assertEqual(380, zo.inbound_pressure)
        self.assertNotIn(380, [zo.occ])  # 不计入当前占用

    def test_short_flow_span_no_false_divergence(self):
        # 运营刚开始、只有少量增量时不得误判设备背离
        snap = event("c1", "camera", "snapshot", "waterfall", 1745, minute_offset=-1)
        tiny = event("g1", "gate", "flow", "waterfall", 15, minute_offset=0)
        _, v = occ([snap, tiny])
        self.assertEqual(HIGH, v.get("waterfall").confidence)


if __name__ == "__main__":
    unittest.main()
