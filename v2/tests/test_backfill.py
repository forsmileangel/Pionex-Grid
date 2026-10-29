import gc
import json
import sqlite3
import tempfile
import unittest
import warnings
from io import StringIO
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from v2.backfill import main, pick_intraday_run
from v2.intraday import SCHEMA
from v2.store import CaptureError


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "intraday.sqlite"
        self.ledger = Path(self._tmp.name) / "ledger.sqlite"
        with sqlite3.connect(self.db) as con:
            con.executescript(SCHEMA)

    def tearDown(self):
        # The read-only sqlite handle inside pick_intraday_run stays open until
        # it is collected. On Windows that lock blocks deleting the temp file.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
        self._tmp.cleanup()

    def add_run(self, captured_at, profit):
        con = sqlite3.connect(self.db)
        try:
            run_id = con.execute(
                "INSERT INTO intraday_runs(captured_at, source, position_count, recorded_at) VALUES (?,?,?,?)",
                (captured_at, "live", 1, captured_at),
            ).lastrowid
            con.execute(
                "INSERT INTO intraday_points(run_id, bu_order_id, captured_at, grid_profit_i) VALUES (?,?,?,?)",
                (run_id, "g1", captured_at, profit),
            )
            con.commit()
        finally:
            con.close()

    def test_pick_intraday_run_uses_the_first_snapshot_at_or_after_0815(self):
        self.add_run("2026-10-04T08:00:00+08:00", 1)
        self.add_run("2026-10-04T08:15:00+08:00", 2)
        self.add_run("2026-10-04T09:00:00+08:00", 3)
        self.add_run("2026-10-05T08:15:00+08:00", 4)
        run, points = pick_intraday_run("2026-10-04", self.db)
        self.assertEqual(run["captured_at"], "2026-10-04T08:15:00+08:00")
        self.assertEqual([point["grid_profit_i"] for point in points], [2])

    def test_pick_intraday_run_rejects_a_day_that_has_only_an_earlier_snapshot(self):
        self.add_run("2026-10-04T08:14:00+08:00", 1)
        with self.assertRaises(CaptureError) as caught:
            pick_intraday_run("2026-10-04", self.db)
        self.assertIn("08:15", str(caught.exception))

    def test_default_run_is_a_dry_run_and_does_not_write_the_ledger(self):
        self.add_run("2026-10-04T08:00:00+08:00", 1)
        self.add_run("2026-10-04T08:15:00+08:00", 2)
        argv = ["v2.backfill", "--date", "2026-10-04", "--db", str(self.ledger), "--intraday-db", str(self.db)]
        with patch("sys.argv", argv), patch("v2.backfill.insert_missing_day") as insert:
            output = StringIO()
            with redirect_stdout(output):
                code = main()
        self.assertEqual(code, 0)
        insert.assert_not_called()
        self.assertFalse(self.ledger.exists())
        preview = json.loads(output.getvalue())
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["source_record"], "2026-10-04T08:15:00+08:00")
