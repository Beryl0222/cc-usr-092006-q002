import unittest
from datetime import datetime, timezone

from src.domain import load_site, load_zones

from helpers import SITE, ZONES, at


class ZoneSeedTest(unittest.TestCase):
    def test_load_zones(self) -> None:
        zones = load_zones(ZONES)
        self.assertEqual(
            ["gate-east", "waterfall", "shuttle-north", "exit-plaza", "parking-lot"],
            [z.code for z in zones],
        )
        self.assertEqual(1800, zones[1].fire_capacity)

    def test_connectivity_and_special_passages(self) -> None:
        site = load_site(ZONES, SITE)
        self.assertEqual(["p-ge-wf"], site.route("gate-east", "waterfall"))
        self.assertIsNone(site.route("gate-east", "waterfall", frozenset({"p-ge-wf"})))
        # 常规寻路只走普通通道：到广场需经核心区、摆渡站绕行（3 跳）
        self.assertEqual(
            ["p-ge-wf", "p-wf-sn", "p-sn-ex"], site.route("gate-east", "exit-plaza")
        )
        # 疏散时允许特殊通道，走 1 跳捷径
        self.assertEqual(["p-ge-ex"], site.route("gate-east", "exit-plaza", allow_special=True))

    def test_operating_phases(self) -> None:
        site = load_site(ZONES, SITE)
        # 本地 06:00（UTC 前一天 22:00）开园前
        self.assertEqual(site.PHASE_CLOSED, site.phase(datetime(2026, 9, 19, 22, 0, tzinfo=timezone.utc)))
        # 本地 18:00（UTC 10:00）运营中
        self.assertEqual(site.PHASE_OPEN, site.phase(at(0)))
        # 本地 20:10（UTC 12:10）闭园返程保障期
        self.assertEqual(site.PHASE_CLOSING, site.phase(datetime(2026, 9, 20, 12, 10, tzinfo=timezone.utc)))
        # 本地 21:10（UTC 13:10）保障期后
        self.assertEqual(site.PHASE_AFTER, site.phase(datetime(2026, 9, 20, 13, 10, tzinfo=timezone.utc)))

    def test_invalid_site_rejected(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        good = json.loads(Path(SITE).read_text(encoding="utf-8"))

        def with_site(mutator):
            bad = json.loads(json.dumps(good))
            mutator(bad)
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
                json.dump(bad, f)
                tmp = f.name
            return load_site(ZONES, tmp)

        with self.assertRaises(ValueError):
            with_site(lambda b: b["passages"].append(
                {"code": "x", "from": "ghost", "to": "waterfall"}))
        with self.assertRaises(ValueError):
            with_site(lambda b: b.update(thresholds={"yellow": 0.9, "orange": 0.8, "red": 0.95}))
        with self.assertRaises(ValueError):
            with_site(lambda b: b["shuttle_lines"][0].update(boost_per_interval=10))


if __name__ == "__main__":
    unittest.main()
