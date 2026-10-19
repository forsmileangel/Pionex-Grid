import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from v2.pipeline import main
from v2.store import CaptureError


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / "ledger.sqlite"
        self.legacy_root = root / "legacy"
        self.workbook = self.legacy_root / "data" / "pionex-grid-history.xlsx"
        self.report = root / "report.xlsx"
        self.log = root / "pipeline.log"
        self.state = root / "pipeline-state.json"
        self.preview_state = root / "preview" / "pipeline-state.json"
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE daily_summary (capture_date TEXT)")
        connection.execute("INSERT INTO daily_summary(capture_date) VALUES ('2026-10-19')")
        connection.commit()
        connection.close()
        self.patches = [
            patch("v2.pipeline.LOG_PATH", self.log),
            patch("v2.pipeline.STATE_PATH", self.state),
            patch("v2.pipeline.PREVIEW_STATE_PATH", self.preview_state),
            patch("v2.pipeline.capture", side_effect=AssertionError("API capture must not run")),
            patch("v2.pipeline.subprocess.run"),
            patch("v2.pipeline.export_report"),
        ]
        started = [item.start() for item in self.patches]
        self.addCleanup(lambda: [item.stop() for item in self.patches])
        self.capture = started[3]
        self.run_excel = started[4]
        self.export_report = started[5]
        self.run_excel.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def args(self, *extra):
        return [
            "--db", str(self.db),
            "--legacy-root", str(self.legacy_root),
            "--legacy-workbook", str(self.workbook),
            "--report-output", str(self.report),
            *extra,
        ]

    def test_skipped_capture_still_exports(self):
        self.preview_state.parent.mkdir(parents=True)
        self.preview_state.write_text(
            json.dumps({"legacy_excel": "2026-10-18", "daily_report": "2026-10-18"}),
            encoding="utf-8",
        )
        with patch("v2.pipeline.daily_capture_due", return_value=(False, "today's daily record already exists")):
            code = main(self.args("--if-missing"))
        self.assertEqual(code, 0)
        self.capture.assert_not_called()
        self.run_excel.assert_called_once()
        command = self.run_excel.call_args.args[0]
        self.assertIn("pionex-grid-daily.ps1", " ".join(command))
        self.assertIn("-V2Root", command)
        self.assertIn("-DatabasePath", command)
        self.assertIn(str(self.db), command)
        self.assertIn("-RootDir", command)
        self.assertIn(str(self.legacy_root), command)
        self.assertIn("-WorkbookPath", command)
        self.assertIn(str(self.workbook), command)
        self.export_report.assert_called_once()
        self.assertEqual(self.export_report.call_args.args[0], self.db)
        self.assertEqual(self.export_report.call_args.args[1], self.report)
        self.assertFalse(self.export_report.call_args.kwargs["allow_stale"])
        self.assertFalse(self.export_report.call_args.kwargs["archive"])
        saved = json.loads(self.preview_state.read_text(encoding="utf-8"))
        self.assertEqual(saved["legacy_excel"], "2026-10-19")
        self.assertEqual(saved["daily_report"], "2026-10-19")
        self.assertFalse(self.state.exists())

    def test_current_state_does_not_export_again(self):
        self.preview_state.parent.mkdir(parents=True)
        self.preview_state.write_text(
            json.dumps({"legacy_excel": "2026-10-19", "daily_report": "2026-10-19"}),
            encoding="utf-8",
        )
        code = main(self.args("--skip-capture"))
        self.assertEqual(code, 0)
        self.capture.assert_not_called()
        self.run_excel.assert_not_called()
        self.export_report.assert_not_called()
        self.assertIn("已是最新，略過", self.log.read_text(encoding="utf-8"))

    def test_excel_failure_exits_zero_and_is_logged(self):
        self.run_excel.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="excel locked")
        self.export_report.side_effect = RuntimeError("report failed")
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = main(self.args("--skip-capture"))
        self.assertEqual(code, 0)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("legacy_excel fail", text)
        self.assertIn("excel locked", text)
        self.assertIn("daily_report fail", text)
        self.assertIn("report failed", text)
        self.assertIn("匯出失敗", errors.getvalue())
        self.assertFalse(self.preview_state.exists())

    def test_capture_failure_does_not_export(self):
        self.capture.side_effect = CaptureError("down")
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = main(self.args())
        self.assertEqual(code, 1)
        self.run_excel.assert_not_called()
        self.export_report.assert_not_called()
        self.assertIn("capture fail", self.log.read_text(encoding="utf-8"))
        self.assertIn("擷取失敗", errors.getvalue())

    def test_dev_tree_refuses_production_workbooks(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = main(["--skip-capture", "--db", str(self.db)])
        self.assertEqual(code, 0)
        self.run_excel.assert_not_called()
        self.export_report.assert_not_called()
        self.assertIn("拒絕寫入正式 Excel", self.log.read_text(encoding="utf-8"))
        self.assertIn("拒絕寫入正式 Excel", errors.getvalue())
