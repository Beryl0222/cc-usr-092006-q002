import unittest

from src.domain import load_zones


class ZoneSeedTest(unittest.TestCase):
    def test_load_zones(self) -> None:
        zones = load_zones("fixtures/zones.json")
        self.assertEqual(["gate-east", "waterfall", "shuttle-north"], [zone.code for zone in zones])
        self.assertEqual(1800, zones[1].fire_capacity)


if __name__ == "__main__":
    unittest.main()
