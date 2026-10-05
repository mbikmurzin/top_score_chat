"""Build a corrected manual TopScore workbook from the backend calculation."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import app
from import_reference_workbook import TOPSCORE_ROWS
from inspect_topscore_workbook import MAIN, sheet_path


BASE_COLUMNS = {
    "C": "budget",
    "E": "subscribers",
    "G": "month_leads",
    "K": "month_paid",
    "L": "month_revenue",
    "N": "month_ltv",
    "R": "year_leads",
    "W": "year_clients",
    "X": "year_revenue",
    "Z": "year_ltv",
}

VALUE_COLUMNS = {
    "C": "budget", "D": "cpf", "E": "subscribers",
    "F": "month_cr_subscriber", "G": "month_leads", "H": "month_cpl",
    "I": "month_cr_lead", "J": "month_cr10", "K": "month_paid",
    "L": "month_revenue", "M": "month_average_check", "N": "month_ltv",
    "O": "month_drr_ltv", "P": "month_cmc", "Q": "year_cr_subscriber",
    "R": "year_leads", "S": "year_cpl", "T": "year_cr_lead",
    "U": "year_cr1", "V": "year_cr1_cr10", "W": "year_clients",
    "X": "year_revenue", "Y": "year_average_check", "Z": "year_ltv",
    "AA": "year_drr_ltv", "AB": "month_year_drr", "AC": "year_cmc",
}

PERCENT_FIELDS = {
    "month_cr_subscriber", "month_cr_lead", "month_cr10", "month_drr_ltv",
    "year_cr_subscriber", "year_cr_lead", "year_cr1", "year_drr_ltv",
}

DERIVED_FORMULAS = {
    "D": "C{r}/E{r}", "F": "G{r}/E{r}", "H": "C{r}/G{r}",
    "I": "K{r}/G{r}", "J": "K{r}/E{r}", "M": "L{r}/K{r}",
    "O": "C{r}/N{r}", "P": "C{r}/K{r}", "Q": "R{r}/E{r}",
    "S": "C{r}/R{r}", "T": "W{r}/R{r}", "U": "W{r}/E{r}",
    "V": "U{r}/J{r}", "Y": "X{r}/W{r}", "AA": "C{r}/Z{r}",
    "AB": "O{r}/AA{r}", "AC": "C{r}/W{r}",
}

BLOCKS = [
    (range(14, 21), 21), (range(25, 32), 32), (range(36, 43), 43),
    (range(47, 54), 54), (range(58, 65), 65), (range(70, 77), 77),
    (range(81, 88), 88), (range(92, 99), 99), (range(103, 110), 110),
]


def excel_value(row, field):
    value = row.get(field)
    if value is None:
        return None
    value = Decimal(str(value))
    if field in PERCENT_FIELDS:
        value /= 100
    return value


def safe_div(left, right):
    return left / right if left is not None and right else None


def total_row(rows):
    total = {field: sum(Decimal(str(row.get(field) or 0)) for row in rows) for field in BASE_COLUMNS.values()}
    total.update({
        "cpf": safe_div(total["budget"], total["subscribers"]),
        "month_cr_subscriber": safe_div(total["month_leads"] * 100, total["subscribers"]),
        "month_cpl": safe_div(total["budget"], total["month_leads"]),
        "month_cr_lead": safe_div(total["month_paid"] * 100, total["month_leads"]),
        "month_cr10": safe_div(total["month_paid"] * 100, total["subscribers"]),
        "month_average_check": safe_div(total["month_revenue"], total["month_paid"]),
        "month_drr_ltv": safe_div(total["budget"] * 100, total["month_ltv"]),
        "month_cmc": safe_div(total["budget"], total["month_paid"]),
        "year_cr_subscriber": safe_div(total["year_leads"] * 100, total["subscribers"]),
        "year_cpl": safe_div(total["budget"], total["year_leads"]),
        "year_cr_lead": safe_div(total["year_clients"] * 100, total["year_leads"]),
        "year_cr1": safe_div(total["year_clients"] * 100, total["subscribers"]),
        "year_average_check": safe_div(total["year_revenue"], total["year_clients"]),
        "year_drr_ltv": safe_div(total["budget"] * 100, total["year_ltv"]),
        "year_cmc": safe_div(total["budget"], total["year_clients"]),
    })
    total["year_cr1_cr10"] = safe_div(total["year_cr1"], total["month_cr10"])
    total["month_year_drr"] = safe_div(total["month_drr_ltv"], total["year_drr_ltv"])
    return total


def set_cell(row_node, ref, value, formula=None):
    namespace = f"{{{MAIN}}}"
    cell = next((node for node in row_node.findall(f"{namespace}c") if node.attrib.get("r") == ref), None)
    if cell is None:
        cell = ET.SubElement(row_node, f"{namespace}c", {"r": ref})
    for node in list(cell):
        if node.tag in {f"{namespace}f", f"{namespace}v"}:
            cell.remove(node)
    if formula:
        ET.SubElement(cell, f"{namespace}f").text = formula
    cached = ET.SubElement(cell, f"{namespace}v")
    if value is None:
        cell.attrib["t"] = "e"
        cached.text = "#DIV/0!"
    else:
        cell.attrib.pop("t", None)
        cached.text = format(value, "f")


def build(source: Path, destination: Path):
    report_rows = app.topscore_report()["rows"]
    by_key = {(row["month"], row["funnel_id"]): row for row in report_rows}
    rows_by_number = {}
    for row_number, funnel_id in TOPSCORE_ROWS.items():
        month_number = ((row_number - min(number for number, fid in TOPSCORE_ROWS.items() if fid == funnel_id)) + 3)
        month = f"2026-{month_number:02d}"
        rows_by_number[row_number] = by_key.get((month, funnel_id), {field: 0 for field in BASE_COLUMNS.values()})
    for row_range, total_number in BLOCKS:
        rows_by_number[total_number] = total_row([rows_by_number[number] for number in row_range])

    with zipfile.ZipFile(source) as archive:
        target = sheet_path(archive, "Top Score по воронкам")
        worksheet = ET.fromstring(archive.read(target))
        row_nodes = {int(row.attrib["r"]): row for row in worksheet.iter(f"{{{MAIN}}}row")}
        for row_number, calculated in rows_by_number.items():
            row_node = row_nodes[row_number]
            for column, field in VALUE_COLUMNS.items():
                formula = DERIVED_FORMULAS.get(column)
                set_cell(
                    row_node,
                    f"{column}{row_number}",
                    excel_value(calculated, field),
                    formula.format(r=row_number) if formula else None,
                )
        xml = ET.tostring(worksheet, encoding="utf-8", xml_declaration=True)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as temporary:
            temporary_path = Path(temporary.name)
        try:
            with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED) as output:
                for info in archive.infolist():
                    output.writestr(info, xml if info.filename == target else archive.read(info.filename))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    build(args.source, args.destination)
