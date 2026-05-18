from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from xml.sax.saxutils import escape


def write_sweep_workbook(
    path: Path,
    *,
    rows: list[dict[str, object]],
    assumptions: dict[str, object],
    top_limit: int = 50,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    result_rows = [row for row in rows if not row.get("error")]
    error_rows = [row for row in rows if row.get("error")]
    top_rows = sorted(
        result_rows,
        key=lambda row: _decimal(row.get("rank_score", row.get("total_return_pct"))),
        reverse=True,
    )[:top_limit]

    sheets = [
        ("Summary", _summary_rows(result_rows, error_rows, assumptions, top_rows[:10])),
        ("All Results", _matrix_from_rows(result_rows)),
        ("Top Results", _matrix_from_rows(top_rows)),
        ("Errors", _matrix_from_rows(error_rows)),
        ("Assumptions", _assumption_rows(assumptions)),
    ]

    with ZipFile(path, "w", ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", _content_types(len(sheets)))
        workbook.writestr("_rels/.rels", _root_relationships())
        workbook.writestr("xl/workbook.xml", _workbook_xml(sheets))
        workbook.writestr("xl/_rels/workbook.xml.rels", _workbook_relationships(len(sheets)))
        workbook.writestr("xl/styles.xml", _styles_xml())
        for index, (_, matrix) in enumerate(sheets, start=1):
            workbook.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _worksheet_xml(matrix),
            )


def _summary_rows(
    result_rows: list[dict[str, object]],
    error_rows: list[dict[str, object]],
    assumptions: dict[str, object],
    top_rows: list[dict[str, object]],
) -> list[list[Any]]:
    best = top_rows[0] if top_rows else {}
    rows: list[list[Any]] = [
        ["CoinDCX Futures Backtest Sweep", ""],
        ["Generated At", datetime.now().astimezone().isoformat()],
        ["Status", assumptions.get("status", "running")],
        ["Pairs", assumptions.get("pairs", "")],
        ["Intervals", assumptions.get("intervals", "")],
        ["Completed Runs", len(result_rows)],
        ["Errors", len(error_rows)],
        ["Best Pair", best.get("pair", "")],
        ["Best Interval", best.get("interval", "")],
        ["Best Variant", best.get("variant", "")],
        ["Best Rank Score", best.get("rank_score", "")],
        ["Best Rank Grade", best.get("rank_grade", "")],
        ["Best Return %", best.get("total_return_pct", "")],
        ["Best Profit Factor", best.get("profit_factor", "")],
        ["", ""],
        ["Top Results", ""],
    ]
    rows.append(
        [
            "Rank",
            "Pair",
            "Interval",
            "Variant",
            "Rank Score",
            "Grade",
            "Return %",
            "Profit Factor",
            "Win Rate %",
            "Trades",
            "Max DD %",
            "Risk %",
            "Lev",
            "TP %",
            "Trail",
            "ATR Exits",
        ]
    )
    for rank, row in enumerate(top_rows, start=1):
        rows.append(
            [
                rank,
                row.get("pair", ""),
                row.get("interval", ""),
                row.get("variant", ""),
                row.get("rank_score", ""),
                row.get("rank_grade", ""),
                row.get("total_return_pct", ""),
                row.get("profit_factor", ""),
                row.get("win_rate_pct", ""),
                row.get("trade_count", ""),
                row.get("max_drawdown_pct", ""),
                row.get("risk_per_trade_pct", ""),
                row.get("leverage", ""),
                row.get("take_profit_pct", ""),
                row.get("trailing_profile", ""),
                row.get("atr_dynamic_exits_enabled", ""),
            ]
        )
    return rows


def _matrix_from_rows(rows: list[dict[str, object]]) -> list[list[Any]]:
    if not rows:
        return [["No rows yet"]]
    headers = list(rows[0].keys())
    return [headers, *[[row.get(header, "") for header in headers] for row in rows]]


def _assumption_rows(assumptions: dict[str, object]) -> list[list[Any]]:
    return [["Field", "Value"], *[[key, value] for key, value in assumptions.items()]]


def _worksheet_xml(matrix: list[list[Any]]) -> str:
    row_count = max(len(matrix), 1)
    col_count = max((len(row) for row in matrix), default=1)
    ref = f"A1:{_column_name(col_count)}{row_count}"
    col_widths = "".join(
        f'<col min="{index}" max="{index}" width="{_column_width(matrix, index - 1)}" customWidth="1"/>'
        for index in range(1, col_count + 1)
    )
    rows_xml = []
    for row_index, row in enumerate(matrix, start=1):
        cells = []
        for col_index in range(1, col_count + 1):
            value = row[col_index - 1] if col_index <= len(row) else ""
            style = "1" if row_index == 1 or (row_index == 15 and matrix[0][0].startswith("CoinDCX")) else "0"
            cells.append(_cell_xml(row_index, col_index, value, style))
        rows_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>{col_widths}</cols>
  <sheetData>{"".join(rows_xml)}</sheetData>
  <autoFilter ref="{ref}"/>
</worksheet>'''


def _cell_xml(row_index: int, col_index: int, value: Any, style: str) -> str:
    ref = f"{_column_name(col_index)}{row_index}"
    if value is None:
        value = ""
    numeric = _as_number(value)
    if numeric is not None:
        return f'<c r="{ref}" s="{style}"><v>{numeric}</v></c>'
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'


def _as_number(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        if value.strip() == "":
            return None
        try:
            Decimal(value)
        except InvalidOperation:
            return None
        return value
    return None


def _column_width(matrix: list[list[Any]], col_index: int) -> int:
    max_len = 10
    for row in matrix[:200]:
        if col_index < len(row):
            max_len = max(max_len, len(str(row[col_index])))
    return min(max(max_len + 2, 10), 42)


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("-999999")


def _content_types(sheet_count: int) -> str:
    sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  {sheets}
</Types>'''


def _root_relationships() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''


def _workbook_xml(sheets: list[tuple[str, list[list[Any]]]]) -> str:
    sheet_xml = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _) in enumerate(sheets, start=1)
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>{sheet_xml}</sheets>
</workbook>'''


def _workbook_relationships(sheet_count: int) -> str:
    sheet_rels = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    style_id = sheet_count + 1
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  {sheet_rels}
  <Relationship Id="rId{style_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''


def _styles_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/></font>
    <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F2937"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
  </cellXfs>
</styleSheet>'''
