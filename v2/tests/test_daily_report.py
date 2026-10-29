import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from v2 import store
from v2.daily_report import export_report, translate_event
from v2.tests.test_store import FX, rec, snap


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "test.sqlite"
        for item in (
            patch("v2.store.usdt_twd", return_value=FX),
            patch("v2.store.BACKUP_DIR", Path(self.temp.name) / "backups"),
        ):
            item.start()
            self.addCleanup(item.stop)

    def test_event_names_match_the_dashboard(self):
        self.assertEqual(translate_event("withdraw"), "提取")
        self.assertEqual(translate_event("reduce"), "減倉")
        self.assertEqual(translate_event("reinvest+withdraw"), "再投資＋提取")
        self.assertEqual(translate_event("something_new"), "something_new")
        self.assertEqual(translate_event(None), "")

    def test_workbook_sheet_count_and_dates(self):
        store.ingest(snap("2026-09-18", [rec(GridProfit=1500)]), self.db)
        store.ingest(snap("2026-09-19", [rec(GridProfit=1505)]), self.db)
        output = Path(self.temp.name) / "report.xlsx"
        result = export_report(self.db, output, date(2026, 9, 19), False, False)
        self.assertEqual(result["sheet_count"], 3)
        self.assertEqual(result["first_date"], "2026-09-18")
        self.assertEqual(result["latest_date"], "2026-09-19")
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with zipfile.ZipFile(output) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        names = [node.attrib["name"] for node in workbook.findall(".//m:sheet", ns)]
        self.assertEqual(names, ["每日總表", "2026-09-18", "2026-09-19"])
