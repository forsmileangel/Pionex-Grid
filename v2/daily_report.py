"""Export the v2 Pionex SQLite ledger to the daily Excel report.

The ledger is the source of truth. This module never calls Pionex and opens the
SQLite database read-only, so the spreadsheet cannot drift from v2's profit
classification logic.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr


SCALE = Decimal("100000000")
DEFAULT_DB = Path(r"D:\My-project\pionex grid record-v2\v2-data\pionex-grid.sqlite")
# Kept at the original 08:10 workbook. __file__ now lives under v2/.
DEFAULT_OUTPUT = Path(r"D:\My-project\pionex grid record\pionex grid daily report\data\pionex-grid-daily-report.xlsx")
REPORT_TITLE = "派網網格每日利潤紀錄"


class ReportError(RuntimeError):
    pass


def fixed(value: int | None) -> float | None:
    return None if value is None else float(Decimal(value) / SCALE)


def number(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ReportError(f"無效的擷取時間：{value}") from exc


def excel_serial(value: date | datetime) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        base = datetime(1899, 12, 30)
        return (value - base).total_seconds() / 86400
    return (value - date(1899, 12, 30)).days


def col_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def translate_status(value: str | None) -> str:
    return {
        "baseline": "基準",
        "new": "新增",
        "continue": "持續",
        "closed": "關倉",
        "closed_unresolved": "關倉待確認",
    }.get(value or "", value or "")


def translate_event(value: str | None) -> str:
    return {
        "baseline": "基準",
        "new": "新增",
        "continue": "無調整",
        "add": "加倉",
        "withdraw": "減倉",
        "reduce": "減倉",
        "reinvest": "再投資",
        "review": "待確認",
        "closed": "關倉",
        "closed_unresolved": "關倉待確認",
    }.get(value or "", value or "")


def product_label(product: str | None) -> str:
    if product == "coin_margined_contract_grid":
        return "幣本位合約網格"
    return "合約網格"


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise ReportError(f"找不到 v2 SQLite：{path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise ReportError(f"無法以唯讀模式開啟 v2 SQLite：{exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


REQUIRED_COLUMNS = {
    "capture_runs": {"id", "captured_at", "capture_date", "status", "error", "wallet_total_usdt", "fx_usdt_twd"},
    "grid_positions": {"bu_order_id", "symbol", "created_at", "product"},
    "grid_snapshots": {"run_id", "bu_order_id", "mark_price_s", "mark_price_i", "liquidation_price_i", "trend", "leverage"},
    "daily_grid_profit": {
        "run_id", "capture_date", "bu_order_id", "status", "grid_profit_i", "daily_profit_i",
        "investment_i", "event", "gap_days", "grid_profit_coin_i", "daily_profit_coin_i",
        "conversion_price_i", "fx_gap_i",
    },
    "daily_summary": {
        "capture_date", "run_id", "daily_profit_i", "cumulative_i", "true_profit_i", "investment_i",
        "active_count", "new_count", "closed_count", "unresolved_count", "gap_days", "fx_gap_i",
    },
}


def validate_schema(connection: sqlite3.Connection) -> None:
    for table, required in REQUIRED_COLUMNS.items():
        found = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        missing = sorted(required - found)
        if missing:
            raise ReportError(f"v2 SQLite 結構不相容，{table} 缺少：{', '.join(missing)}")


def load_report(connection: sqlite3.Connection, today: date, allow_stale: bool) -> tuple[list[dict], dict[str, list[dict]]]:
    validate_schema(connection)
    summaries = [dict(row) for row in connection.execute(
        """SELECT d.*, r.captured_at, r.status AS run_status, r.error AS run_error,
                  r.wallet_total_usdt, r.fx_usdt_twd
           FROM daily_summary d
           JOIN capture_runs r ON r.id=d.run_id
           ORDER BY d.capture_date"""
    )]
    if not summaries:
        raise ReportError("v2 SQLite 尚無每日摘要資料。")
    latest = summaries[-1]
    if latest["run_status"] != "success":
        raise ReportError(f"v2 最新擷取未成功：{latest['run_error'] or latest['run_status']}")
    latest_date = date.fromisoformat(latest["capture_date"])
    if latest_date > today:
        raise ReportError(f"v2 最新日期 {latest_date} 晚於本機日期 {today}。")
    if not allow_stale and latest_date != today:
        raise ReportError(f"v2 今日資料尚未完成（最新為 {latest_date}）；保留既有 Excel，不覆寫。")

    rows_by_date: dict[str, list[dict]] = {}
    query = """SELECT d.*, p.symbol, p.created_at, p.product,
                      s.mark_price_s, s.mark_price_i, s.liquidation_price_i,
                      s.trend, s.leverage, s.list_status
               FROM daily_grid_profit d
               JOIN grid_positions p ON p.bu_order_id=d.bu_order_id
               LEFT JOIN grid_snapshots s
                 ON s.run_id=d.run_id AND s.bu_order_id=d.bu_order_id
               ORDER BY d.capture_date, p.symbol, p.created_at, d.bu_order_id"""
    for row in connection.execute(query):
        item = dict(row)
        rows_by_date.setdefault(item["capture_date"], []).append(item)
    for summary in summaries:
        rows_by_date.setdefault(summary["capture_date"], [])
    return summaries, rows_by_date


@dataclass(frozen=True)
class Cell:
    value: object = None
    style: int = 0
    formula: str | None = None
    cached: float | None = None


def c(value=None, style: int = 0, formula: str | None = None, cached: float | None = None) -> Cell:
    return Cell(value=value, style=style, formula=formula, cached=cached)


def cell_xml(ref: str, cell: Cell) -> str:
    style = f' s="{cell.style}"' if cell.style else ""
    if cell.formula is not None:
        cached = "" if cell.cached is None else f"<v>{cell.cached:.12g}</v>"
        return f'<c r="{ref}"{style}><f>{escape(cell.formula)}</f>{cached}</c>'
    value = cell.value
    if value is None or value == "":
        return f'<c r="{ref}"{style}/>' if cell.style else ""
    if isinstance(value, bool):
        return f'<c r="{ref}"{style} t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float, Decimal)):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def row_xml(number_: int, values: list[Cell], height: int | None = None) -> str:
    attrs = f' r="{number_}"'
    if height:
        attrs += f' ht="{height}" customHeight="1"'
    cells = "".join(cell_xml(f"{col_name(index)}{number_}", value) for index, value in enumerate(values, 1))
    return f"<row{attrs}>{cells}</row>"


def sheet_xml(
    rows: list[list[Cell]], widths: list[float], merges: list[str], auto_filter: str | None,
    frozen_rows: int = 4, hidden_columns: set[int] | None = None,
) -> str:
    hidden_columns = hidden_columns or set()
    max_columns = max((len(row) for row in rows), default=1)
    max_rows = max(len(rows), 1)
    columns = []
    for index, width in enumerate(widths, 1):
        hidden = ' hidden="1"' if index in hidden_columns else ""
        columns.append(f'<col min="{index}" max="{index}" width="{width}" customWidth="1"{hidden}/>')
    data = []
    for index, values in enumerate(rows, 1):
        height = 28 if index == 1 else 34 if index == 2 else 24 if index == 4 else 20
        data.append(row_xml(index, values, height))
    pane = ""
    if frozen_rows:
        pane = (
            f'<pane ySplit="{frozen_rows}" topLeftCell="A{frozen_rows + 1}" '
            'activePane="bottomLeft" state="frozen"/>'
            '<selection pane="bottomLeft" activeCell="A5" sqref="A5"/>'
        )
    merge_xml = ""
    if merges:
        merge_xml = f'<mergeCells count="{len(merges)}">' + "".join(
            f'<mergeCell ref={quoteattr(value)}/>' for value in merges
        ) + "</mergeCells>"
    filter_xml = f'<autoFilter ref={quoteattr(auto_filter)}/>' if auto_filter else ""
    dimension = f"A1:{col_name(max_columns)}{max_rows}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/>'
        '<sheetViews><sheetView showGridLines="0" workbookViewId="0">'
        f'{pane}</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f'<cols>{"".join(columns)}</cols><sheetData>{"".join(data)}</sheetData>'
        f'{merge_xml}{filter_xml}'
        '<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
        '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/>'
        '</worksheet>'
    )


STYLES_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="6">
    <numFmt numFmtId="164" formatCode="yyyy-mm-dd"/>
    <numFmt numFmtId="165" formatCode="yyyy-mm-dd hh:mm:ss"/>
    <numFmt numFmtId="166" formatCode="#,##0.00;[Red]-#,##0.00"/>
    <numFmt numFmtId="167" formatCode="0.00%;[Red]-0.00%"/>
    <numFmt numFmtId="168" formatCode="#,##0.########"/>
    <numFmt numFmtId="169" formatCode="0.00000000"/>
  </numFmts>
  <fonts count="5">
    <font><sz val="10"/><name val="Microsoft JhengHei"/><family val="2"/></font>
    <font><b/><sz val="16"/><color rgb="FFFFFFFF"/><name val="Microsoft JhengHei"/></font>
    <font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Microsoft JhengHei"/></font>
    <font><b/><sz val="10"/><color rgb="FF263238"/><name val="Microsoft JhengHei"/></font>
    <font><sz val="9"/><color rgb="FF546E7A"/><name val="Microsoft JhengHei"/></font>
  </fonts>
  <fills count="9">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF263238"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF00695C"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFE0F2F1"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF4F6F8"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFE8F5E9"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFEBEE"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFF8E1"/></patternFill></fill>
  </fills>
  <borders count="3">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border><left style="thin"><color rgb="FFD7DEE2"/></left><right style="thin"><color rgb="FFD7DEE2"/></right><top style="thin"><color rgb="FFD7DEE2"/></top><bottom style="thin"><color rgb="FFD7DEE2"/></bottom><diagonal/></border>
    <border><left/><right/><top/><bottom style="medium"><color rgb="FF00695C"/></bottom><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="19">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="4" fillId="5" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right"/></xf>
    <xf numFmtId="167" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right"/></xf>
    <xf numFmtId="168" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right"/></xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center"/></xf>
    <xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="left"/></xf>
    <xf numFmtId="166" fontId="0" fillId="6" borderId="0" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="166" fontId="0" fillId="7" borderId="0" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="0" fontId="3" fillId="4" borderId="2" xfId="0" applyAlignment="1"><alignment horizontal="right"/></xf>
    <xf numFmtId="166" fontId="3" fillId="4" borderId="2" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="167" fontId="3" fillId="4" borderId="2" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center"/></xf>
    <xf numFmtId="169" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="0" fontId="0" fillId="8" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center"/></xf>
    <xf numFmtId="0" fontId="3" fillId="4" borderId="2" xfId="0"/>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def profit_cell(value: float | None) -> Cell:
    if value is None:
        return c(None, 4)
    return c(value, 10 if value > 0 else 11 if value < 0 else 4)


def summary_sheet(summaries: list[dict]) -> tuple[list[list[Cell]], list[float], list[str], str]:
    headers = [
        "日期", "擷取時間", "活躍倉位", "新增", "關閉", "待確認", "缺口天數",
        "總倉位大小 (USDT)", "單日網格利潤 (USDT)", "單日 %", "報表期間累計 (USDT)",
        "Pionex 真實累計 (USDT)", "當下 MTM 累計 (USDT)", "幣本位匯率重估 (USDT)",
        "錢包總額 (USDT)", "USDT/TWD", "狀態",
    ]
    rows: list[list[Cell]] = [
        [c(REPORT_TITLE, 1)] + [c() for _ in range(len(headers) - 1)],
        [c("來源：v2 SQLite 08:05 快照；單日利潤採 v2 事件分類，幣本位匯率重估另列。", 2)] + [c() for _ in range(len(headers) - 1)],
        [c() for _ in headers],
        [c(value, 3) for value in headers],
    ]
    running = 0.0
    for row_index, summary in enumerate(summaries, 5):
        day_profit = fixed(summary["daily_profit_i"]) or 0.0
        investment = fixed(summary["investment_i"])
        day_pct = None if not investment else day_profit / investment
        running += day_profit
        wallet = number(summary["wallet_total_usdt"])
        captured = parse_datetime(summary["captured_at"])
        rows.append([
            c(excel_serial(date.fromisoformat(summary["capture_date"])), 7),
            c(excel_serial(captured), 8),
            c(summary["active_count"], 15), c(summary["new_count"], 15), c(summary["closed_count"], 15),
            c(summary["unresolved_count"], 17 if summary["unresolved_count"] else 15),
            c(summary["gap_days"], 17 if summary["gap_days"] else 15),
            c(investment, 4), profit_cell(day_profit),
            c(None, 5, formula=f'IFERROR(I{row_index}/H{row_index},"")', cached=day_pct),
            c(None, 4, formula=f"SUM($I$5:I{row_index})", cached=running),
            c(fixed(summary["true_profit_i"]), 4), c(fixed(summary["cumulative_i"]), 4),
            profit_cell(fixed(summary["fx_gap_i"])), c(wallet, 4), c(number(summary["fx_usdt_twd"]), 6),
            c("成功" if summary["run_status"] == "success" else summary["run_status"], 15),
        ])
    widths = [12, 21, 11, 9, 9, 10, 10, 18, 20, 11, 20, 20, 19, 22, 18, 13, 11]
    return rows, widths, ["A1:Q1", "A2:Q2"], f"A4:Q{len(rows)}"


def daily_sheet(day: str, summary: dict, records: list[dict]) -> tuple[list[list[Cell]], list[float], list[str], str]:
    headers = [
        "倉位大小 (USDT)", "標的", "現價", "強平價", "強平距離", "網格利潤 (USDT)",
        "單日網格利潤 (USDT)", "單天 %", "狀態", "事件", "方向 / 槓桿", "產品",
        "原幣累計網格利潤", "原幣單日增量", "換算價", "匯率重估差額 (USDT)",
        "建立時間", "倉位 ID", "缺口天數", "備註",
    ]
    captured = parse_datetime(summary["captured_at"])
    rows: list[list[Cell]] = [
        [c(f"{REPORT_TITLE} - {day}", 1)] + [c() for _ in range(len(headers) - 1)],
        [c(f"擷取時間：{captured.isoformat(sep=' ', timespec='seconds')}｜強平距離=(現價-強平價)/現價｜單天%=單日利潤/倉位大小", 2)] + [c() for _ in range(len(headers) - 1)],
        [c() for _ in headers],
        [c(value, 3) for value in headers],
    ]
    total_investment = total_daily = total_grid = total_fx = 0.0
    for row_index, record in enumerate(records, 5):
        investment = fixed(record["investment_i"])
        mark = number(record["mark_price_s"]) or fixed(record["mark_price_i"])
        liquidation = fixed(record["liquidation_price_i"])
        grid_profit = fixed(record["grid_profit_i"])
        daily_profit = fixed(record["daily_profit_i"])
        distance = None if not mark or liquidation is None else (mark - liquidation) / mark
        day_pct = None if not investment or daily_profit is None else daily_profit / investment
        fx_gap = fixed(record["fx_gap_i"])
        total_investment += investment or 0.0
        total_daily += daily_profit or 0.0
        total_grid += grid_profit or 0.0
        total_fx += fx_gap or 0.0
        status = translate_status(record["status"])
        event = translate_event(record["event"])
        note = ""
        if record["status"] == "closed_unresolved" or record["event"] == "review":
            note = "需人工確認"
        elif record["gap_days"]:
            note = f"與前次成功擷取相隔 {record['gap_days'] + 1} 天"
        rows.append([
            c(investment, 4), c(record["symbol"], 9), c(mark, 6), c(liquidation, 6),
            c(None, 5, formula=f'IFERROR((C{row_index}-D{row_index})/C{row_index},"")', cached=distance),
            c(grid_profit, 4), profit_cell(daily_profit),
            c(None, 5, formula=f'IFERROR(G{row_index}/A{row_index},"")', cached=day_pct),
            c(status, 17 if "確認" in status else 15), c(event, 17 if "確認" in event else 15),
            c(f"{record['trend'] or ''} {record['leverage'] or ''}".strip(), 15), c(product_label(record["product"]), 9),
            c(fixed(record["grid_profit_coin_i"]), 16), c(fixed(record["daily_profit_coin_i"]), 16),
            c(fixed(record["conversion_price_i"]), 6), profit_cell(fx_gap), c(record["created_at"], 9),
            c(record["bu_order_id"], 9), c(record["gap_days"], 15), c(note, 9),
        ])
    total_row = len(rows) + 1
    first_data, last_data = 5, total_row - 1
    sum_formula = lambda column: f"SUM({column}{first_data}:{column}{last_data})" if records else "0"
    total_pct = None if not total_investment else total_daily / total_investment
    rows.append([
        c(None, 13, formula=sum_formula("A"), cached=total_investment), c("當日合計", 18),
        c(None, 12), c(None, 12), c(None, 12),
        c(None, 13, formula=sum_formula("F"), cached=total_grid),
        c(None, 13, formula=sum_formula("G"), cached=total_daily),
        c(None, 14, formula=f'IFERROR(G{total_row}/A{total_row},"")', cached=total_pct),
        c(), c(), c(), c(), c(), c(), c(),
        c(None, 13, formula=sum_formula("P"), cached=total_fx), c(), c(), c(), c(),
    ])
    widths = [18, 16, 15, 15, 13, 19, 22, 12, 14, 14, 16, 18, 21, 18, 15, 22, 20, 38, 11, 22]
    return rows, widths, ["A1:T1", "A2:T2"], f"A4:T{last_data if records else 4}"


def workbook_xml(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name={quoteattr(name[:31])} sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<bookViews><workbookView activeTab="0"/></bookViews><sheets>{sheets}</sheets>'
        '<calcPr calcId="191029" calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/></workbook>'
    )


def verify_xlsx(path: Path, summaries: list[dict], sheet_names: list[str]) -> None:
    namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path, "r") as archive:
        bad = archive.testzip()
        if bad:
            raise ReportError(f"產生的 Excel ZIP 損壞：{bad}")
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        actual_names = [node.attrib["name"] for node in workbook.findall(".//m:sheet", namespace)]
        if actual_names != sheet_names:
            raise ReportError(f"Excel 工作表順序不正確：{actual_names}")
        summary_xml = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        summary_cells = {
            node.attrib["r"]: node.findtext("m:v", namespaces=namespace)
            for node in summary_xml.findall(".//m:c", namespace)
        }
        for row_index, summary in enumerate(summaries, 5):
            ref = f"I{row_index}"
            cached = summary_cells.get(ref)
            expected = fixed(summary["daily_profit_i"])
            if cached is None or expected is None or abs(float(cached) - expected) > 0.00000001:
                raise ReportError(f"Excel {ref} 與 SQLite 每日總額不一致。")
        formula_count = len(summary_xml.findall(".//m:f", namespace))
        for sheet_index in range(2, len(sheet_names) + 1):
            xml = ET.fromstring(archive.read(f"xl/worksheets/sheet{sheet_index}.xml"))
            formulas = [node.text or "" for node in xml.findall(".//m:f", namespace)]
            formula_count += len(formulas)
            if not any("/A" in formula for formula in formulas):
                raise ReportError(f"{sheet_names[sheet_index - 1]} 缺少單天百分比公式。")
            if not any("-D" in formula and "/C" in formula for formula in formulas):
                raise ReportError(f"{sheet_names[sheet_index - 1]} 缺少強平距離公式。")
        if formula_count == 0:
            raise ReportError("Excel 未寫入任何公式。")
        error_markers = (b"#REF!", b"#DIV/0!", b"#VALUE!", b"#NAME?", b"#N/A")
        for name in archive.namelist():
            if name.startswith("xl/worksheets/"):
                raw = archive.read(name)
                if any(marker in raw for marker in error_markers):
                    raise ReportError(f"Excel 公式錯誤出現在 {name}。")


def write_xlsx(path: Path, summaries: list[dict], rows_by_date: dict[str, list[dict]]) -> None:
    sheet_names = ["每日總表"] + [row["capture_date"] for row in summaries]
    summary_by_date = {row["capture_date"]: row for row in summaries}
    rendered: list[str] = []
    summary_rows, widths, merges, auto_filter = summary_sheet(summaries)
    rendered.append(sheet_xml(summary_rows, widths, merges, auto_filter))
    for day in sheet_names[1:]:
        daily_rows, widths, merges, auto_filter = daily_sheet(day, summary_by_date[day], rows_by_date[day])
        rendered.append(sheet_xml(daily_rows, widths, merges, auto_filter, hidden_columns={18}))

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            overrides = "".join(
                f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for index in range(1, len(sheet_names) + 1)
            )
            archive.writestr("[Content_Types].xml", (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                f'{overrides}</Types>'
            ))
            archive.writestr("_rels/.rels", (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
                '</Relationships>'
            ))
            relationships = "".join(
                f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
                for index in range(1, len(sheet_names) + 1)
            )
            relationships += f'<Relationship Id="rId{len(sheet_names) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            archive.writestr("xl/_rels/workbook.xml.rels", (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'{relationships}</Relationships>'
            ))
            archive.writestr("xl/workbook.xml", workbook_xml(sheet_names))
            archive.writestr("xl/styles.xml", STYLES_XML)
            for index, xml in enumerate(rendered, 1):
                archive.writestr(f"xl/worksheets/sheet{index}.xml", xml)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            archive.writestr("docProps/core.xml", (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                f'<dc:title>{REPORT_TITLE}</dc:title><dc:creator>Codex</dc:creator>'
                f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
                f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>'
            ))
            titles = "".join(f'<vt:lpstr>{escape(name)}</vt:lpstr>' for name in sheet_names)
            archive.writestr("docProps/app.xml", (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
                'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
                f'<Application>Microsoft Excel Compatible</Application><TitlesOfParts><vt:vector size="{len(sheet_names)}" baseType="lpstr">{titles}</vt:vector></TitlesOfParts></Properties>'
            ))
        verify_xlsx(temporary, summaries, sheet_names)
        os.replace(temporary, path)
    except PermissionError as exc:
        raise ReportError(f"無法更新 Excel，請先關閉檔案：{path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def archive_existing(path: Path) -> Path | None:
    if not path.exists():
        return None
    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = archive_dir / f"{path.stem}-legacy-{stamp}{path.suffix}"
    shutil.copy2(path, destination)
    return destination


def export_report(db_path: Path, output: Path, today: date, allow_stale: bool, archive: bool) -> dict:
    connection = open_readonly(db_path)
    try:
        summaries, rows_by_date = load_report(connection, today, allow_stale)
    finally:
        connection.close()
    archived = archive_existing(output) if archive else None
    write_xlsx(output, summaries, rows_by_date)
    return {
        "ok": True, "source": str(db_path), "output": str(output),
        "first_date": summaries[0]["capture_date"], "latest_date": summaries[-1]["capture_date"],
        "sheet_count": len(summaries) + 1, "position_rows": sum(len(rows) for rows in rows_by_date.values()),
        "archived": None if archived is None else str(archived),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export v2 Pionex SQLite ledger to daily Excel report")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--allow-stale", action="store_true", help="Allow a historical/manual rebuild when latest date is not today")
    parser.add_argument("--archive-existing", action="store_true", help="Archive the current workbook before replacing it")
    parser.add_argument("--today", type=date.fromisoformat, default=date.today(), help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        result = export_report(args.db, args.output, args.today, args.allow_stale, args.archive_existing)
    except (ReportError, sqlite3.Error, OSError) as exc:
        print(f"Excel 匯出失敗：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
