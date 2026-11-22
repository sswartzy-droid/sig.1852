"""Regression test for /health's clock reading an hour behind.

2026-10-26: server_time was an hour off. main.py used a fixed GMT-6 labelled
"CST", but Kansas is on CDT (GMT-5) from March to November. These tests pin
down: daylight time reads CDT, winter reads CST, and both are the right hour.

Stdlib only (main.py needs aiohttp installed to import):

    python3 -m unittest test_health_time -v
"""
import unittest
from datetime import datetime, timezone

from main import _format_timestamp


def _utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


class HealthTimeTests(unittest.TestCase):
    def test_summer_is_cdt(self):
        self.assertEqual(_format_timestamp(_utc(2026, 10, 27, 15, 0)), "2026-10-27 10:00:00 CDT")

    def test_winter_is_cst(self):
        self.assertEqual(_format_timestamp(_utc(2026, 12, 1, 15, 0)), "2026-12-01 09:00:00 CST")

    def test_fall_back_day(self):
        # 2026-11-01: 1:59 CDT, then the clock falls back to 1:00 CST.
        self.assertEqual(_format_timestamp(_utc(2026, 11, 1, 6, 59)), "2026-11-01 01:59:00 CDT")
        self.assertEqual(_format_timestamp(_utc(2026, 11, 1, 7, 0)), "2026-11-01 01:00:00 CST")


if __name__ == "__main__":
    unittest.main()
