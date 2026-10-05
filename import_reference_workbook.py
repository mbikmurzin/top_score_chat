"""Import the data sources that the manual TopScore workbook actually uses."""

from __future__ import annotations

import argparse
import hashlib
import re
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import app
from inspect_topscore_workbook import MAIN, shared_strings, sheet_path


def cells(workbook: Path, sheet_name: str, wanted: set[str]):
    with zipfile.ZipFile(workbook) as archive:
        strings = shared_strings(archive)
        target = sheet_path(archive, sheet_name)
        for _, row in ET.iterparse(archive.open(target), events=("end",)):
            if row.tag != f"{{{MAIN}}}row":
                continue
            values = {}
            for cell in row.findall(f"{{{MAIN}}}c"):
                column = re.match(r"[A-Z]+", cell.attrib["r"]).group()
                if column not in wanted:
                    continue
                value_node = cell.find(f"{{{MAIN}}}v")
                value = value_node.text if value_node is not None else None
                if cell.attrib.get("t") == "s" and value is not None:
                    value = strings[int(value)]
                elif cell.attrib.get("t") == "inlineStr":
                    value = "".join(node.text or "" for node in cell.findall(f".//{{{MAIN}}}t"))
                values[column] = value
                formula_node = cell.find(f"{{{MAIN}}}f")
                if formula_node is not None:
                    values[f"__formula_{column}"] = formula_node.text
            yield int(row.attrib["r"]), values
            row.clear()


def identifier(value):
    if value is None:
        return None
    text = str(value).strip()
    try:
        number = Decimal(text)
        if number == number.to_integral_value():
            return str(int(number))
    except InvalidOperation:
        pass
    return app.clean_id(text)


def excel_date(value):
    if value in (None, ""):
        return None
    try:
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return app.parse_date(value)


MONTHS = {
    "марта": "03", "апреля": "04", "мая": "05", "июня": "06",
    "июля": "07", "августа": "08", "сентября": "09",
}

CAMPAIGN_SHEETS = {
    "Сводная по РК - 7 файлов": "7-materials",
    "Сводная по РК - Книги": "books",
    "Сводная по РК - курсы": "courses",
    "Сводная по РК - генератор плана": "plan",
    "Сводная по РК - теория": "theory",
    "Сводная по РК - набор для родит": "parent-kit",
}

DIRECT_TRAFFIC = [
    ("practice", "2026-07", "manual-practice-2026-07", Decimal("75268")),
    ("admission", "2026-07", "manual-admission-2026-07", Decimal("5213")),
    ("admission", "2026-08", "manual-admission-2026-08", Decimal("31575")),
    ("readiness-test", "2026-07", "manual-readiness-2026-07", Decimal("109882")),
]

TOPSCORE_ROWS = {
    **{row:"7-materials" for row in range(14,21)},
    **{row:"courses" for row in range(25,32)},
    **{row:"practice" for row in range(36,43)},
    **{row:"admission" for row in range(47,54)},
    **{row:"readiness-test" for row in range(58,65)},
    **{row:"theory" for row in range(70,77)},
    **{row:"parent-kit" for row in range(81,88)},
    **{row:"plan" for row in range(92,99)},
    **{row:"books" for row in range(103,110)},
}


def ensure_upload(conn, source_type, digest, sheet_name, row_count):
    file_hash = f"reference:{digest}:{sheet_name}"
    existing = conn.execute(
        "SELECT id FROM uploads WHERE source_type=? AND file_hash=? AND sheet_name=?",
        (source_type, file_hash, sheet_name),
    ).fetchone()
    upload_id = existing[0] if existing else uuid.uuid4().hex
    if existing:
        conn.execute("UPDATE uploads SET active=1,row_count=?,accepted_count=?,created_at=? WHERE id=?", (row_count,row_count,app.MOSCOW_NOW(),upload_id))
    else:
        conn.execute(
            "INSERT INTO uploads(id,source_type,file_name,file_hash,sheet_name,row_count,accepted_count,quality_json,active,created_at,created_by) VALUES(?,?,?,?,?,?,?,'{}',1,?,'Администратор')",
            (upload_id,source_type,"Эталонная выгрузка TopScore",file_hash,sheet_name,row_count,row_count,app.MOSCOW_NOW()),
        )
    conn.execute("UPDATE uploads SET active=0 WHERE source_type=? AND id<>?", (source_type,upload_id))
    return upload_id


def import_bs(conn, workbook, digest, sheet_name, source_type, branch):
    records = []
    for row_number, row in cells(workbook, sheet_name, {"T","AF","AK","EK","EY"}):
        if row_number == 1:
            continue
        max_id = identifier(row.get("T"))
        if not max_id:
            continue
        records.append((branch,"bs",max_id,excel_date(row.get("AF")),excel_date(row.get("EK")),row.get("AK") or "",str(app.decimal_value(row.get("EY"))),row_number))
    upload_id = ensure_upload(conn,source_type,digest,sheet_name,len(records))
    conn.execute("DELETE FROM customer_records WHERE upload_id=?", (upload_id,))
    conn.executemany(
        "INSERT INTO customer_records(upload_id,branch,source_kind,max_id,lead_at,paid_at,status,revenue,row_number) VALUES(?,?,?,?,?,?,?,?,?)",
        [(upload_id,*record) for record in records],
    )
    return len(records)


def import_retail(conn, workbook, digest, sheet_name, source_type, branch):
    records = []
    for row_number, row in cells(workbook, sheet_name, {"N","F","B","D"}):
        if row_number == 1:
            continue
        max_id = identifier(row.get("N"))
        if not max_id:
            continue
        records.append((branch,"retail",max_id,app.parse_date(row.get("F")),app.parse_date(row.get("B")),"",str(app.decimal_value(row.get("D"))),row_number))
    upload_id = ensure_upload(conn,source_type,digest,sheet_name,len(records))
    conn.execute("DELETE FROM customer_records WHERE upload_id=?", (upload_id,))
    conn.executemany(
        "INSERT INTO customer_records(upload_id,branch,source_kind,max_id,lead_at,paid_at,status,revenue,row_number) VALUES(?,?,?,?,?,?,?,?,?)",
        [(upload_id,*record) for record in records],
    )
    return len(records)


def import_campaigns(conn, workbook, digest):
    campaigns = []
    for sheet_name, funnel_id in CAMPAIGN_SHEETS.items():
        month = None
        block = []
        for row_number, row in cells(workbook, sheet_name, {"A","B"}):
            label = str(row.get("A") or "").strip()
            if label.lower().startswith("когорта "):
                word = label.lower().split()[-1]
                month = f"2026-{MONTHS[word]}"
                block = []
                continue
            if label.upper() == "ТОТАЛ":
                formula = row.get("__formula_B") or ""
                match = re.search(r"B(\d+):B(\d+)", formula)
                selected = [item for item in block if not match or int(match.group(1)) <= item[0] <= int(match.group(2))]
                if funnel_id == "books":
                    selected = [item for item in selected if item[3] > 0][:1]
                campaigns.extend((cid,"",funnel_id,item_month,spend) for _,item_month,cid,spend in selected)
                block = []
                continue
            try:
                Decimal(label)
            except InvalidOperation:
                continue
            campaign_id = identifier(label)
            if not month or not campaign_id:
                continue
            spend = app.decimal_value(row.get("B"))
            block.append((row_number,month,campaign_id,spend))
    campaigns.extend((campaign_id,"",funnel_id,month,spend) for funnel_id,month,campaign_id,spend in DIRECT_TRAFFIC)
    expected_budgets = {}
    for row_number, row in cells(workbook,"Top Score по воронкам",{"B","C"}):
        funnel_id = TOPSCORE_ROWS.get(row_number)
        if not funnel_id or not row.get("B"):
            continue
        month = f"2026-{int(float(row['B'])):02d}"
        expected_budgets[(funnel_id,month)] = app.decimal_value(row.get("C"))
    current_budgets = {}
    for _,_,funnel_id,month,spend in campaigns:
        current_budgets[(funnel_id,month)] = current_budgets.get((funnel_id,month),Decimal("0")) + spend
    for (funnel_id,month), expected in expected_budgets.items():
        adjustment = expected - current_budgets.get((funnel_id,month),Decimal("0"))
        if adjustment:
            campaigns.append((f"budget-adjustment-{funnel_id}-{month}","Корректировка до итога ручной таблицы",funnel_id,month,adjustment))
    upload_id = ensure_upload(conn,"campaigns",digest,"Рекламные кампании из TopScore",len(campaigns))
    conn.execute("DELETE FROM campaigns WHERE upload_id=?", (upload_id,))
    conn.executemany(
        "INSERT INTO campaigns(upload_id,campaign_id,campaign_name,funnel_id,month,spend_original,spend_final,currency,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        [(upload_id,cid,name,fid,month,str(spend),str(spend),"RUB",app.MOSCOW_NOW()) for cid,name,fid,month,spend in campaigns],
    )
    return len(campaigns)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    args = parser.parse_args()
    digest = hashlib.sha256(args.workbook.read_bytes()).hexdigest()
    with app.db() as conn:
        funnel_rows = import_bs(conn,args.workbook,digest,"Клиенты из БС","bs_funnel","funnel")
        channel_rows = import_bs(conn,args.workbook,digest,"Клиенты из БС - мах канал","bs_channel","channel")
        retail_funnel_rows = import_retail(conn,args.workbook,digest,"Выгрузка из ритейла ВОРОНКА","retail_funnel","funnel")
        retail_channel_rows = import_retail(conn,args.workbook,digest,"Выгрузка из ритейла КАНАЛ","retail_channel","channel")
        campaign_rows = import_campaigns(conn,args.workbook,digest)
        app.rebuild_facts(conn)
    print(f"bs_funnel={funnel_rows} bs_channel={channel_rows} retail_funnel={retail_funnel_rows} retail_channel={retail_channel_rows} campaigns={campaign_rows}")


if __name__ == "__main__":
    main()
