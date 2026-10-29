import tempfile
import unittest
import zipfile
from pathlib import Path

from v2.xlsx import _col, write_xlsx


class XlsxTests(unittest.TestCase):
    def test_column_names(self):
        self.assertEqual(_col(0), "A")
        self.assertEqual(_col(25), "Z")
        self.assertEqual(_col(26), "AA")

    def test_workbook_keeps_numbers_text_blanks_and_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.xlsx"
            write_xlsx(path, {"表": [
                ["名稱", 12, None, ""],
                ["中文", 3.5, "abc", None],
            ]})
            with zipfile.ZipFile(path) as book:
                self.assertIsNone(book.testzip())
                sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
                workbook = book.read("xl/workbook.xml").decode("utf-8")
            self.assertIn("表", workbook)
            self.assertIn("名稱", sheet)
            self.assertIn("中文", sheet)
            self.assertIn("<v>12</v>", sheet)
            self.assertIn("<v>3.5</v>", sheet)
            self.assertIn("abc", sheet)
            self.assertIn('r="A1"', sheet)
            self.assertIn('r="B1"', sheet)
            self.assertNotIn('r="C1"', sheet)
            self.assertNotIn('r="D1"', sheet)
            self.assertIn('r="A2"', sheet)
            self.assertIn('r="C2"', sheet)
            self.assertNotIn('r="D2"', sheet)
