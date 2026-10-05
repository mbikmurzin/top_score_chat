"""Compare backend TopScore base measures with a manual TopScore workbook."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

import app
from import_reference_workbook import TOPSCORE_ROWS, cells


COLUMNS = {
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


def number(value):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def compare(workbook: Path):
    expected = {}
    wanted = {"B", *COLUMNS}
    for row_number, row in cells(workbook, "Top Score по воронкам", wanted):
        funnel_id = TOPSCORE_ROWS.get(row_number)
        if not funnel_id or not row.get("B"):
            continue
        month = f"2026-{int(float(row['B'])):02d}"
        expected[(month, funnel_id)] = {field: number(row.get(column)) for column, field in COLUMNS.items()}

    actual = {
        (row["month"], row["funnel_id"]): row
        for row in app.topscore_report()["rows"]
    }
    differences = []
    matching = 0
    for key, expected_row in sorted(expected.items()):
        actual_row = actual.get(key, {})
        row_differences = []
        for field, expected_value in expected_row.items():
            actual_value = number(actual_row.get(field))
            if abs(expected_value - actual_value) > Decimal("0.01"):
                row_differences.append((field, expected_value, actual_value, actual_value - expected_value))
        if row_differences:
            differences.append((key, row_differences))
        else:
            matching += 1
    print(f"rows={len(expected)} matching={matching} different={len(differences)}")
    for (month, funnel_id), fields in differences:
        print(month, funnel_id)
        for field, expected_value, actual_value, delta in fields:
            print(f"  {field}: workbook={expected_value} backend={actual_value} delta={delta}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    compare(parser.parse_args().workbook)
