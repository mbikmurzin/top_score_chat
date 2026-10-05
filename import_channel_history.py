"""Import the historical MAX-channel membership cached in the reference workbook."""

from __future__ import annotations

import argparse
import hashlib
import uuid
from pathlib import Path

import openpyxl

import app


def is_true(value) -> bool:
    return value is True or str(value or "").strip().casefold() in {"1", "true", "да", "yes"}


def import_history(path: Path) -> int:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook["Подписчики"]
    max_ids = set()
    client_ids = set()
    for row in sheet.iter_rows(min_row=2, min_col=4, max_col=19, values_only=True):
        client_id = app.clean_id(row[0])
        max_id = app.clean_id(row[14])
        subscribed = is_true(row[15])
        if subscribed and client_id:
            client_ids.add(client_id)
        if subscribed and max_id:
            max_ids.add(max_id)

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    file_hash = f"channel-history:{digest}"
    with app.db() as connection:
        existing = connection.execute(
            "SELECT id FROM uploads WHERE source_type='channel_history' AND file_hash=?",
            (file_hash,),
        ).fetchone()
        upload_id = existing[0] if existing else uuid.uuid4().hex
        if existing:
            connection.execute("DELETE FROM channel_records WHERE upload_id=?", (upload_id,))
            connection.execute("DELETE FROM channel_membership_history WHERE upload_id=?", (upload_id,))
            connection.execute(
                "UPDATE uploads SET row_count=?,accepted_count=?,created_at=? WHERE id=?",
                (len(client_ids), len(client_ids), app.MOSCOW_NOW(), upload_id),
            )
        else:
            connection.execute(
                """INSERT INTO uploads(
                       id,source_type,file_name,file_hash,sheet_name,row_count,
                       accepted_count,quality_json,active,created_at,created_by
                   ) VALUES(?,?,?,?,?,?,?,'{}',1,?,'Администратор')""",
                (upload_id, "channel_history", path.name, file_hash,
                 "Подписчики — сохранённая история канала", len(client_ids),
                 len(client_ids), app.MOSCOW_NOW()),
            )
        connection.executemany(
            """INSERT INTO channel_records(
                   upload_id,max_id,event_at,status,left_at,row_number
               ) VALUES(?,?,'','1',NULL,?)""",
            [(upload_id, max_id, row_number) for row_number, max_id in enumerate(sorted(max_ids), 2)],
        )
        connection.executemany(
            "INSERT INTO channel_membership_history(upload_id,client_id,row_number) VALUES(?,?,?)",
            [(upload_id, client_id, row_number) for row_number, client_id in enumerate(sorted(client_ids), 2)],
        )
        app.rebuild_facts(connection)
    return len(client_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    args = parser.parse_args()
    print(f"imported_channel_history={import_history(args.workbook)}")
