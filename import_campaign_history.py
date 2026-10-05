"""Import historical campaign IDs and non-zero spend from TopScore summaries."""

from __future__ import annotations

import argparse
import hashlib
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import app
from import_reference_workbook import cells, ensure_upload, identifier


SHEETS = {
    "Сводная по РК - 7 файлов": "7-materials",
    "Сводная по РК - Книги": "books",
    "Сводная по РК - курсы": "courses",
    "Сводная по РК - генератор плана": "plan",
    "Сводная по РК - теория": "theory",
    "Сводная по РК - набор для родит": "parent-kit",
}

MONTHS = {
    "января": "01", "февраля": "02", "марта": "03", "апреля": "04",
    "мая": "05", "июня": "06", "июля": "07", "августа": "08",
    "сентября": "09", "октября": "10", "ноября": "11", "декабря": "12",
}


def extract(workbook: Path):
    totals = defaultdict(Decimal)
    source_rows = defaultdict(int)
    for sheet_name, funnel_id in SHEETS.items():
        month = None
        for row_number, row in cells(workbook, sheet_name, {"A", "B"}):
            label = str(row.get("A") or "").strip()
            folded = label.casefold()
            if folded.startswith("когорта "):
                month_name = folded.split()[-1]
                month = f"2026-{MONTHS[month_name]}"
                continue
            if not month or not label or folded.startswith("тотал"):
                continue
            spend = app.decimal_value(row.get("B"))
            if spend == 0:
                continue
            campaign_id = identifier(label)
            if not campaign_id:
                continue
            totals[(funnel_id, month, campaign_id)] += spend
            source_rows[(funnel_id, month)] += 1
    return totals, source_rows


def import_history(workbook: Path):
    extracted, source_rows = extract(workbook)
    digest = hashlib.sha256(workbook.read_bytes()).hexdigest()
    imported_funnels = set(SHEETS.values())
    with app.db() as conn:
        # Preserve previously stored campaigns for funnels that have no
        # "Сводная по РК" sheet in this workbook.
        retained = [
            row for row in app.active_campaigns(conn)
            if row["funnel_id"] not in imported_funnels
            and app.decimal_value(row["spend_final"]) != 0
        ]
        records = [
            (campaign_id, "История из ручного TopScore", funnel_id, month, spend)
            for (funnel_id, month, campaign_id), spend in sorted(extracted.items())
        ]
        records.extend(
            (
                row["campaign_id"], row["campaign_name"] or "",
                row["funnel_id"], row["month"], app.decimal_value(row["spend_final"]),
            )
            for row in retained
        )
        upload_id = ensure_upload(
            conn, "campaigns", digest,
            "Все листы «Сводная по РК» — ненулевые траты",
            len(records),
        )
        conn.execute("DELETE FROM campaigns WHERE upload_id=?", (upload_id,))
        conn.executemany(
            """INSERT INTO campaigns(
                   upload_id,campaign_id,campaign_name,funnel_id,month,
                   spend_original,spend_final,currency,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                (upload_id, campaign_id, name, funnel_id, month,
                 str(spend), str(spend), "RUB", app.MOSCOW_NOW())
                for campaign_id, name, funnel_id, month, spend in records
            ],
        )
        app.rebuild_facts(conn)

    print(f"imported={len(extracted)} retained={len(retained)} total={len(records)}")
    for (funnel_id, month), row_count in sorted(source_rows.items()):
        spend = sum(
            value for (fid, item_month, _), value in extracted.items()
            if fid == funnel_id and item_month == month
        )
        campaigns = sum(
            1 for fid, item_month, _ in extracted
            if fid == funnel_id and item_month == month
        )
        print(f"{month} {funnel_id}: rows={row_count} campaigns={campaigns} spend={spend}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    import_history(parser.parse_args().workbook)
