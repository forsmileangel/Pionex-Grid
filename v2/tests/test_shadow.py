import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from v2.capture import TAIPEI
from v2.shadow import _last_settlement, capture_shadow, compare_day

SETTLE = 1_800_000_000
EARLIER = SETTLE - 8 * 3600


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name) / "shadow"
        for item in (patch("v2.shadow.SHADOW_DIR", self.dir),
                     patch("v2.shadow.collect_snapshot", return_value={"CapturedAt": "x", "Records": []})):
            self.collect = item.start()
            self.addCleanup(item.stop)

    def test_late_start_skips_without_calling_the_api(self):
        result = capture_shadow(Path("creds.txt"), now=datetime(2026, 10, 21, 9, 0, tzinfo=TAIPEI))
        self.assertTrue(result["skipped"])
        self.collect.assert_not_called()
        self.assertFalse(self.dir.exists())

    def test_saves_one_file_named_by_date_and_time(self):
        result = capture_shadow(Path("creds.txt"), now=datetime(2026, 10, 21, 8, 15, tzinfo=TAIPEI))
        path = self.dir / "2026-10-21-0815.json"
        self.assertEqual(result["path"], str(path))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["CapturedAt"], "x")
        self.assertEqual(self.collect.call_args.kwargs, {"reuse_finished": True, "running_from_list": True})

    def test_counts_settlements_that_posted_after_the_ledger_capture(self):
        official = {"a": (EARLIER, -1.0), "b": (SETTLE, -2.0), "c": (None, 0.0)}
        shadow = {"a": (SETTLE, -1.5), "b": (SETTLE, -2.0), "c": (SETTLE, -1.0)}
        self.assertEqual(compare_day(official, shadow, SETTLE), {
            "positions": 2, "official_posted": 1, "shadow_posted": 2, "late": 1, "late_funding": -0.5})

    def test_settlement_time_reads_detail_then_list(self):
        self.assertEqual(_last_settlement({"order": {"buOrderData": {"lastFundingFeeTs": SETTLE}}}), SETTLE)
        self.assertEqual(_last_settlement(json.dumps({"list": {"buOrderData": {"lastFundingFeeTs": str(EARLIER)}}})),
                         EARLIER)
        self.assertEqual(_last_settlement({"order": {"buOrderData": {"lastFundingFeeTs": SETTLE * 1000}}}), SETTLE)
        self.assertIsNone(_last_settlement('{"backfill": "intraday"}'))


if __name__ == "__main__":
    unittest.main()
