from __future__ import annotations

import csv
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import uuid
import urllib.request
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("TOPSCORE_DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
UPLOADS = DATA / "uploads"
DRAFTS = DATA / "drafts"
DB_PATH = DATA / "topscore.db"
for directory in (DATA, UPLOADS, DRAFTS):
    directory.mkdir(parents=True, exist_ok=True)

MOSCOW_NOW = lambda: datetime.now().astimezone().isoformat(timespec="seconds")

SOURCE_LABELS = {
    "subscribers": "Подписчики воронок из SaleBot",
    "id_map": "Постоянный справочник MAX ID для подписчиков SaleBot",
    "bs_funnel": "База БС — воронка",
    "bs_channel": "База БС — MAX-канал",
    "retail_funnel": "Лиды и клиенты с воронок в RetailCRM",
    "retail_channel": "Лиды и клиенты с MAX-канала в RetailCRM",
    "channel_subscribers": "Подписчики канала MAX",
    "campaigns": "Рекламные кампании",
    "ltv": "LTV по месяцу",
}

FIELD_ALIASES = {
    "client_id": ["client_id", "id", "внешний id", "tg id"],
    "platform_id": ["platform_id"], "full_name": ["full_name", "фио"],
    "subscription_at": ["current_date", "start_date", "дата подписки", "joindate"],
    "subscription_time": ["current_time", "start_time", "время подписки"],
    "utm_source": ["utm_source"], "utm_medium": ["utm_medium"],
    "utm_campaign": ["utm_campaign"], "utm_content": ["utm_content"],
    "utm_term": ["utm_term"], "tag": ["tag", "теги"],
    "max_id": ["tamtam_user_id [client]", "tamtam_user_id", "max id", "max_id", "userid", "t / max id"],
    "lead_at": ["af / дата первого контакта", "дата первого контакта", "дата регистрации"],
    "paid_at": ["ek / дата последней покупки", "дата последней покупки", "последний заказ"],
    "status": ["ak / crm-статус", "crm-статус", "status"],
    "revenue": ["ey / сумма заказов", "сумма заказов"],
    "left_at": ["leftdate"],
    "campaign_id": ["campaign_id", "campaign id"], "campaign_name": ["campaign_name"],
    "funnel_id": ["funnel_id"], "date_from": ["date_from"], "date_to": ["date_to"],
    "month": ["month", "месяц"], "spend": ["spend", "расходы", "бюджет"],
    "currency": ["currency", "валюта"], "vat_included": ["vat_included"],
    "ltv_value": ["ltv_value", "ltv"], "active_status": ["status"],
}

REQUIRED = {
    "subscribers": ["client_id", "subscription_at"], "id_map": ["client_id", "max_id"],
    "bs_funnel": ["max_id"], "bs_channel": ["max_id"],
    "retail_funnel": ["max_id"], "retail_channel": ["max_id"],
    "channel_subscribers": ["max_id"], "campaigns": ["campaign_id", "spend"],
    "ltv": ["month", "ltv_value"],
}


class DatabaseRow(Mapping):
    """A sqlite3.Row-compatible mapping for the remote libSQL driver."""

    def __init__(self, columns, values):
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._by_name = dict(zip(self._columns, self._values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._by_name[key]

    def __iter__(self):
        return iter(self._columns)

    def __len__(self):
        return len(self._columns)


class DatabaseCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def _convert(self, row):
        if row is None or isinstance(row, sqlite3.Row):
            return row
        columns = [column[0] for column in (self._cursor.description or ())]
        return DatabaseRow(columns, row)

    def fetchone(self):
        return self._convert(self._cursor.fetchone())

    def fetchall(self):
        return [self._convert(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class DatabaseConnection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        return DatabaseCursor(self._connection.execute(sql, parameters))

    def executemany(self, sql, parameters):
        # Remote libSQL requests have practical payload limits. Sending large
        # imports in bounded batches also avoids one HTTP request per row.
        iterator = iter(parameters)
        last_cursor = None
        while batch := list(islice(iterator, 500)):
            last_cursor = self._connection.executemany(sql, batch)
        return DatabaseCursor(last_cursor) if last_cursor is not None else None

    def executescript(self, script):
        return self._connection.executescript(script)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def connect():
    turso_url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    turso_token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if turso_url:
        import libsql

        conn = libsql.connect(turso_url, auth_token=turso_token)
        conn.execute("PRAGMA foreign_keys=ON")
        return DatabaseConnection(conn)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS funnels(id TEXT PRIMARY KEY,name TEXT NOT NULL,aliases TEXT NOT NULL DEFAULT '[]',rules TEXT NOT NULL DEFAULT '[]',active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,source_type TEXT NOT NULL,file_name TEXT NOT NULL,file_hash TEXT NOT NULL,sheet_name TEXT,row_count INTEGER NOT NULL DEFAULT 0,accepted_count INTEGER NOT NULL DEFAULT 0,duplicate_count INTEGER NOT NULL DEFAULT 0,error_count INTEGER NOT NULL DEFAULT 0,quality_json TEXT NOT NULL DEFAULT '{}',active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,created_by TEXT NOT NULL DEFAULT 'Администратор',UNIQUE(source_type,file_hash,sheet_name));
CREATE TABLE IF NOT EXISTS mappings(id INTEGER PRIMARY KEY,source_type TEXT NOT NULL,signature TEXT NOT NULL,mapping_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(source_type,signature));
CREATE TABLE IF NOT EXISTS subscriber_records(id INTEGER PRIMARY KEY,upload_id TEXT NOT NULL,funnel_id TEXT NOT NULL,client_id TEXT NOT NULL,platform_id TEXT,max_id TEXT,full_name TEXT,subscription_at TEXT NOT NULL,utm_source TEXT,utm_medium TEXT,utm_campaign TEXT,utm_content TEXT,utm_term TEXT,tag TEXT,row_number INTEGER,FOREIGN KEY(upload_id) REFERENCES uploads(id));
CREATE TABLE IF NOT EXISTS id_map_records(id INTEGER PRIMARY KEY,upload_id TEXT NOT NULL,client_id TEXT NOT NULL,max_id TEXT NOT NULL,row_number INTEGER,FOREIGN KEY(upload_id) REFERENCES uploads(id));
CREATE TABLE IF NOT EXISTS customer_records(id INTEGER PRIMARY KEY,upload_id TEXT NOT NULL,branch TEXT NOT NULL,source_kind TEXT NOT NULL,max_id TEXT NOT NULL,lead_at TEXT,paid_at TEXT,status TEXT,revenue TEXT NOT NULL DEFAULT '0',row_number INTEGER,FOREIGN KEY(upload_id) REFERENCES uploads(id));
CREATE TABLE IF NOT EXISTS channel_records(id INTEGER PRIMARY KEY,upload_id TEXT NOT NULL,max_id TEXT NOT NULL,event_at TEXT,status TEXT,left_at TEXT,row_number INTEGER,FOREIGN KEY(upload_id) REFERENCES uploads(id));
CREATE TABLE IF NOT EXISTS channel_membership_history(id INTEGER PRIMARY KEY,upload_id TEXT NOT NULL,client_id TEXT NOT NULL,row_number INTEGER,FOREIGN KEY(upload_id) REFERENCES uploads(id),UNIQUE(upload_id,client_id));
CREATE TABLE IF NOT EXISTS campaigns(id INTEGER PRIMARY KEY,upload_id TEXT,campaign_id TEXT NOT NULL,campaign_name TEXT,funnel_id TEXT,date_from TEXT,date_to TEXT,month TEXT,spend_original TEXT NOT NULL,spend_final TEXT NOT NULL,currency TEXT NOT NULL DEFAULT 'RUB',utm_source TEXT,utm_medium TEXT,utm_campaign TEXT,utm_content TEXT,utm_term TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ltv_values(id INTEGER PRIMARY KEY,upload_id TEXT,month TEXT NOT NULL,funnel_id TEXT,ltv_value TEXT NOT NULL,valid_from TEXT,valid_to TEXT,created_at TEXT NOT NULL,UNIQUE(month,funnel_id));
CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY,funnel_id TEXT NOT NULL,client_id TEXT NOT NULL,max_id TEXT,full_name TEXT,subscription_at TEXT NOT NULL,cohort_month TEXT NOT NULL,utm_source TEXT,utm_medium TEXT,utm_campaign TEXT,utm_content TEXT,utm_term TEXT,tag TEXT,campaign_id TEXT,ever_channel INTEGER NOT NULL DEFAULT 0,active_channel INTEGER NOT NULL DEFAULT 0,funnel_lead INTEGER NOT NULL DEFAULT 0,funnel_lead_at TEXT,funnel_paid INTEGER NOT NULL DEFAULT 0,funnel_paid_at TEXT,funnel_revenue TEXT NOT NULL DEFAULT '0',channel_lead INTEGER NOT NULL DEFAULT 0,channel_lead_at TEXT,channel_paid INTEGER NOT NULL DEFAULT 0,channel_paid_at TEXT,channel_revenue TEXT NOT NULL DEFAULT '0',source_batch_id TEXT NOT NULL,UNIQUE(funnel_id,client_id,cohort_month));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sub_client ON subscriber_records(client_id); CREATE INDEX IF NOT EXISTS idx_sub_funnel ON subscriber_records(funnel_id); CREATE INDEX IF NOT EXISTS idx_map_client ON id_map_records(client_id); CREATE INDEX IF NOT EXISTS idx_customer_max ON customer_records(max_id); CREATE INDEX IF NOT EXISTS idx_facts_dims ON facts(funnel_id,cohort_month,campaign_id); CREATE INDEX IF NOT EXISTS idx_facts_dates ON facts(subscription_at,funnel_lead_at,funnel_paid_at);
"""

FUNNELS = [
    ("7-materials", "7 материалов / 7 файлов", ["7 материалов", "7 файлов"]),
    ("readiness-test", "Тест «Насколько готов к ЕГЭ/ОГЭ»", ["готов к егэ", "готов к огэ"]),
    ("practice", "Пробные варианты / пробники для ребёнка", ["пробники", "пробные варианты"]),
    ("admission", "Калькулятор поступления", ["калькулятор поступления"]),
    ("plan", "Генератор плана подготовки", ["план подготовки"]),
    ("theory", "Вся теория для сдачи ЕГЭ / теория для ребёнка", ["вся теория", "теория для ребёнка"]),
    ("parent-kit", "Набор для родителя", ["набор для родителя"]),
    ("books", "Книги для сочинений / книги для русского", ["книги для сочинений", "книги для русского"]),
    ("courses", "Курсы", ["курсы"]),
]

SALEBOT_LINKS = {
    "7-materials": "https://salebot.pro/shared/table/JHB45bhtrRG1qMYpoExqFDF4h-hncNrYcgvtZf3Z7wI",
    "readiness-test": "https://salebot.pro/shared/table/3kR3e9vGKW4WsHsSfLcd3UfljgcEJPVpJKPzoCXuf88",
    "practice": "https://salebot.pro/shared/table/Y9jj6-B7eQVc-aPjozwLjLh95zCVxqxxgGFkWiJGhxQ",
    "admission": "https://salebot.pro/shared/table/W4pc5dUPavqz4Pi5zfLMngDKmTJtPs5T9UjQggiCfxQ",
    "plan": "https://salebot.pro/shared/table/J45s5XNX5XTf2Z7SWhF7l11LxGUMJ_KzfkgjNq4ahqM",
    "theory": "https://salebot.pro/shared/table/ZS-P5fAx7kQh-TOVidgZ6puE-enzAbfU4EkEV4t-0lU",
    "parent-kit": "https://salebot.pro/shared/table/cPNWh8sBjDli9Y0gR4S8QPRv4BSHgQvg4XwNh0YNikI",
    "books": "https://salebot.pro/shared/table/jGIb_CoS2zXrdYPgZhQIqm1lMgZswALlMKivbkuEVc8",
    "courses": "https://salebot.pro/shared/table/LyhO3dX29BqjKwIXIn3XrdAUBTKpyGAQJkgFmOZPzTE",
}

DEFAULT_LTV_VALUES = {
    "2026-04": Decimal("60233"), "2026-05": Decimal("59386"),
    "2026-06": Decimal("59980"), "2026-07": Decimal("59473"),
    "2026-08": Decimal("58954"), "2026-09": Decimal("58384"),
    "2026-10": Decimal("45164"),
}
DEFAULT_LTV_BY_MONTH_NUMBER = {int(month[5:7]): value for month, value in DEFAULT_LTV_VALUES.items()}


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        subscriber_columns = {row[1] for row in conn.execute("PRAGMA table_info(subscriber_records)")}
        if "max_id" not in subscriber_columns:
            conn.execute("ALTER TABLE subscriber_records ADD COLUMN max_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sub_max ON subscriber_records(max_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sub_upload_row ON subscriber_records(upload_id,row_number)")
        for fid, name, aliases in FUNNELS:
            conn.execute("INSERT OR IGNORE INTO funnels VALUES(?,?,?,?,1,?)", (fid, name, json.dumps(aliases, ensure_ascii=False), json.dumps(aliases, ensure_ascii=False), MOSCOW_NOW()))
        defaults = {"vat_mode": "included", "vat_rate": "22", "current_user_role": "admin"}
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (key, value))
        for month, value in DEFAULT_LTV_VALUES.items():
            if not conn.execute(
                "SELECT 1 FROM ltv_values WHERE month=? AND funnel_id IS NULL LIMIT 1", (month,)
            ).fetchone():
                conn.execute(
                    """INSERT INTO ltv_values(upload_id,month,funnel_id,ltv_value,valid_from,valid_to,created_at)
                       VALUES(NULL,?,NULL,?,?,?,?)""",
                    (month, str(value), f"{month}-01", None, MOSCOW_NOW()),
                )


def clean_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\n", " ").strip()).lower()


def infer_mapping(columns, source_type=None):
    result, normalized = {}, {clean_header(c): str(c) for c in columns}
    for field, aliases in FIELD_ALIASES.items():
        if field == "max_id" and source_type in {"retail_funnel", "retail_channel"}:
            # The reference workbook joins RetailCRM through its TG ID column
            # (column N). MAX ID is incomplete in these exports.
            aliases = ["tg id", *aliases]
        for alias in aliases:
            if clean_header(alias) in normalized:
                result[field] = normalized[clean_header(alias)]
                break
    return result


def preferred_revenue_column(columns, source_type, mapped_column=None):
    """BS revenue is always Excel column EY (order sum), never EX."""
    if source_type in {"bs_funnel", "bs_channel"}:
        ey_column = next(
            (column for column in columns if clean_header(column).startswith("ey / сумма заказов")),
            None,
        )
        if ey_column:
            return str(ey_column)
    return mapped_column


def parse_date(value):
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() in ("", "NaT", "nan"):
        return None
    parsed = pd.to_datetime(value, dayfirst=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.isoformat()


def subscription_moment(date_value, time_value=None):
    parsed_date = parse_date(date_value)
    if not parsed_date:
        return None
    day = parsed_date[:10]
    if time_value is None or str(time_value).strip().lower() in {"", "nan", "nat"}:
        return f"{day}T00:00:00"
    match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", str(time_value))
    if not match:
        return f"{day}T00:00:00"
    hour, minute, second = match.group(1), match.group(2), match.group(3) or "00"
    return f"{day}T{int(hour):02d}:{minute}:{second}"


def clean_id(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text): text = text[:-2]
    if not text or text.lower() in {"0", "-", "—", "nan", "none"}: return None
    return text


def decimal_value(value):
    if value is None or (isinstance(value, float) and pd.isna(value)): return Decimal("0")
    text = re.sub(r"[^0-9,.-]", "", str(value)).replace(",", ".")
    try: return Decimal(text or "0")
    except InvalidOperation: return Decimal("0")


def read_table(path: Path, sheet=None):
    suffix = path.suffix.lower()
    if suffix == ".csv":
        for enc in ("utf-8-sig", "cp1251", "utf-8"):
            try:
                with path.open("r", encoding=enc, newline="") as source:
                    sample = source.read(64 * 1024)
                delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
                return pd.read_csv(path, dtype=str, encoding=enc, sep=delimiter)
            except UnicodeDecodeError:
                continue
            except csv.Error:
                return pd.read_csv(path, dtype=str, encoding=enc)
    return pd.read_excel(path, sheet_name=sheet or 0, dtype=str)


def _can_decode(content: bytes, encoding: str) -> bool:
    try:
        content.decode(encoding)
        return True
    except UnicodeDecodeError:
        return False


def list_sheets(path: Path):
    if path.suffix.lower() == ".csv": return ["CSV"]
    return pd.ExcelFile(path).sheet_names


def active_clause(alias="u"):
    return f"EXISTS(SELECT 1 FROM uploads au WHERE au.id={alias}.upload_id AND au.active=1)"


def latest_active_upload(conn, source_type):
    row = conn.execute("SELECT id FROM uploads WHERE source_type=? AND active=1 ORDER BY created_at DESC,id DESC LIMIT 1", (source_type,)).fetchone()
    return row[0] if row else None


def latest_upload_as_of(conn, source_type, as_of):
    """Return the newest source snapshot that had been loaded by this date."""
    row = conn.execute(
        """SELECT id FROM uploads
           WHERE source_type=? AND substr(created_at,1,10)<=?
           ORDER BY created_at DESC,id DESC LIMIT 1""",
        (source_type, as_of),
    ).fetchone()
    return row[0] if row else None


def active_campaigns(conn):
    """Return manual campaigns and rows from active campaign uploads."""
    return [
        dict(row)
        for row in conn.execute(
            """SELECT c.* FROM campaigns c
               LEFT JOIN uploads u ON u.id=c.upload_id
               WHERE c.upload_id IS NULL OR u.active=1
               ORDER BY c.id DESC"""
        )
    ]


def best_customer(rows):
    if not rows: return {"lead": False, "lead_at": None, "paid": False, "paid_at": None, "revenue": Decimal("0")}
    # The workbook uses INDEX/MATCH: the first matching source row wins. It does
    # not select the newest sale or add several RetailCRM snapshots together.
    ordered = sorted(rows, key=lambda row: row["row_number"] or 0)
    lead_row = next((row for row in ordered if row["lead_at"]), ordered[0])
    paid_rows = [row for row in ordered if decimal_value(row["revenue"]) > 0 or "оплач" in (row["status"] or "").lower()]
    revenue_row = next((row for row in paid_rows if decimal_value(row["revenue"]) > 0), None)
    paid_row = revenue_row or (paid_rows[0] if paid_rows else None)
    return {
        "lead": True,
        "lead_at": lead_row["lead_at"],
        "paid": bool(paid_rows),
        "paid_at": paid_row["paid_at"] if paid_row else None,
        "revenue": decimal_value(revenue_row["revenue"]) if revenue_row else Decimal("0"),
    }


def workbook_customer(bs_rows, retail_rows):
    """Reproduce the workbook's field-by-field BS -> RetailCRM fallback."""
    bs = sorted(bs_rows, key=lambda row: row["row_number"] or 0)
    retail = sorted(retail_rows, key=lambda row: row["row_number"] or 0)
    bs_lead = next((row for row in bs if row["lead_at"]), None)
    retail_lead = next((row for row in retail if row["lead_at"]), None)
    bs_sale = next((row for row in bs if decimal_value(row["revenue"]) > 0), None)
    retail_sale = next((row for row in retail if decimal_value(row["revenue"]) > 0), None)
    # BS payment is determined by CRM status, RetailCRM by a positive order sum.
    paid = any("оплач" in (row["status"] or "").lower() for row in bs) or bool(retail_sale)
    revenue_row = bs_sale or (retail[0] if retail else None)
    paid_row = bs_sale or retail_sale
    return {
        "lead": bool(bs or retail),
        "lead_at": (bs_lead or retail_lead)["lead_at"] if (bs_lead or retail_lead) else None,
        "paid": paid,
        "paid_at": paid_row["paid_at"] if paid_row else None,
        "revenue": decimal_value(revenue_row["revenue"]) if revenue_row else Decimal("0"),
    }


def academic_year_key(value: str) -> str | None:
    """Match the unique-subscriber table used by the supplied workbook."""
    day = (value or "")[:10]
    if "2026-03-01" <= day < "2026-04-01":
        return "2026-pilot"
    if "2026-04-01" <= day < "2027-05-01":
        return "2026-27"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return None
    year, month = int(day[:4]), int(day[5:7])
    return f"{year if month >= 4 else year - 1}-{str(year + 1 if month >= 4 else year)[-2:]}"


def rebuild_facts(conn):
    fact_columns = (
        "funnel_id", "client_id", "max_id", "full_name", "subscription_at", "cohort_month",
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "tag",
        "campaign_id", "ever_channel", "active_channel", "funnel_lead", "funnel_lead_at",
        "funnel_paid", "funnel_paid_at", "funnel_revenue", "channel_lead", "channel_lead_at",
        "channel_paid", "channel_paid_at", "channel_revenue", "source_batch_id",
    )
    existing_facts = {}
    for row in conn.execute(f"SELECT {','.join(fact_columns)} FROM facts"):
        values = tuple(row[column] for column in fact_columns)
        existing_facts[(row["funnel_id"], row["client_id"], row["cohort_month"])] = values
    maps = {}
    conflicts = set()
    id_map_upload = latest_active_upload(conn, "id_map")
    for row in conn.execute("SELECT m.* FROM id_map_records m WHERE m.upload_id=?", (id_map_upload,)) if id_map_upload else []:
        if row["client_id"] in maps and maps[row["client_id"]] != row["max_id"]: conflicts.add(row["client_id"])
        else: maps[row["client_id"]] = row["max_id"]
    for key in conflicts: maps.pop(key, None)
    # Membership is historical: replacing the latest channel export must not
    # erase people who subscribed earlier.
    channels = {}
    for row in conn.execute("SELECT c.* FROM channel_records c ORDER BY COALESCE(event_at,''),id"):
        channels.setdefault(row["max_id"], []).append(row)
    active_channels = {}
    channel_upload = latest_active_upload(conn, "channel_subscribers")
    for row in conn.execute("SELECT c.* FROM channel_records c WHERE c.upload_id=? ORDER BY COALESCE(event_at,'')", (channel_upload,)) if channel_upload else []:
        active_channels.setdefault(row["max_id"], []).append(row)
    historical_channel_clients = {
        row[0] for row in conn.execute("SELECT DISTINCT client_id FROM channel_membership_history")
    }
    customers = {}
    customer_uploads = [latest_active_upload(conn, source) for source in ("bs_funnel","bs_channel","retail_funnel","retail_channel")]
    customer_uploads = [upload_id for upload_id in customer_uploads if upload_id]
    customer_query = f"SELECT c.* FROM customer_records c WHERE c.upload_id IN ({','.join('?' for _ in customer_uploads)})" if customer_uploads else None
    for row in conn.execute(customer_query, customer_uploads) if customer_query else []:
        customers.setdefault((row["max_id"], row["branch"], row["source_kind"]), []).append(row)
    campaign_rows = active_campaigns(conn)
    seen = set()
    latest_subscriber_uploads = {}
    for row in conn.execute(f"SELECT s.funnel_id,s.upload_id,au.created_at FROM subscriber_records s JOIN uploads au ON au.id=s.upload_id WHERE au.active=1 GROUP BY s.funnel_id,s.upload_id ORDER BY au.created_at,au.id"):
        latest_subscriber_uploads[row["funnel_id"]] = row["upload_id"]
    active_subscriber_ids = list(latest_subscriber_uploads.values())
    if not active_subscriber_ids:
        removed = len(existing_facts)
        if removed:
            conn.execute("DELETE FROM facts")
        return {"total": 0, "updated": 0, "removed": removed}
    placeholders = ",".join("?" for _ in active_subscriber_ids)
    subs = conn.execute(f"SELECT s.* FROM subscriber_records s WHERE s.upload_id IN ({placeholders}) ORDER BY subscription_at,id", active_subscriber_ids)
    fact_rows = []
    for s in subs:
        cohort = s["subscription_at"][:7]
        academic_year = academic_year_key(s["subscription_at"])
        if not academic_year:
            continue
        key = (s["client_id"], academic_year)
        if key in seen: continue
        seen.add(key)
        # SaleBot is authoritative. The separate ID map only fills a missing MAX ID.
        max_id = clean_id(s["max_id"]) or maps.get(s["client_id"])
        # Use SaleBot's MAX ID first. The separate mapping has already filled
        # max_id above only when SaleBot did not contain one.
        channel_lookup_id = max_id
        member_rows = channels.get(channel_lookup_id, []) if channel_lookup_id else []
        current_rows = active_channels.get(channel_lookup_id, []) if channel_lookup_id else []
        latest_member = current_rows[-1] if current_rows else None
        member_status = str(latest_member["status"] or "1").strip().lower() if latest_member else ""
        active_statuses = {"1", "true", "active", "подписан", "подписан на канал"}
        active = bool(latest_member and member_status in active_statuses and not latest_member["left_at"])
        values = {}
        for branch in ("funnel", "channel"):
            primary = customers.get((max_id, branch, "bs"), []) if max_id else []
            secondary = customers.get((max_id, branch, "retail"), []) if max_id else []
            values[branch] = workbook_customer(primary, secondary)
        campaign_id = None
        utm = s["utm_campaign"] or ""
        for c in campaign_rows:
            if c["month"] == cohort and c["funnel_id"] == s["funnel_id"] and (c["campaign_id"] == utm or (c["campaign_id"] and c["campaign_id"] in utm)):
                campaign_id = c["campaign_id"]; break
        fact_rows.append((
            s["funnel_id"],s["client_id"],max_id,s["full_name"],s["subscription_at"],cohort,s["utm_source"],s["utm_medium"],s["utm_campaign"],s["utm_content"],s["utm_term"],s["tag"],campaign_id,bool(member_rows) or s["client_id"] in historical_channel_clients,active,
            values["funnel"]["lead"],values["funnel"]["lead_at"],values["funnel"]["paid"],values["funnel"]["paid_at"],str(values["funnel"]["revenue"]),values["channel"]["lead"],values["channel"]["lead_at"],values["channel"]["paid"],values["channel"]["paid_at"],str(values["channel"]["revenue"]),s["upload_id"]
        ))
    desired_facts = {}
    for row in fact_rows:
        key = (row[0], row[1], row[5])
        existing = existing_facts.get(key)
        # A new source snapshot ID alone does not change any KPI. Keep the
        # previous provenance value when all business fields are identical.
        desired_facts[key] = existing if existing and existing[:-1] == row[:-1] else row
    changed_rows = [row for key, row in desired_facts.items() if existing_facts.get(key) != row]
    removed_keys = [key for key in existing_facts if key not in desired_facts]
    if changed_rows:
        conn.executemany(
            f"INSERT OR REPLACE INTO facts({','.join(fact_columns)}) VALUES({','.join('?' for _ in fact_columns)})",
            changed_rows,
        )
    if removed_keys:
        conn.executemany(
            "DELETE FROM facts WHERE funnel_id=? AND client_id=? AND cohort_month=?",
            removed_keys,
        )
    return {"total": len(fact_rows), "updated": len(changed_rows), "removed": len(removed_keys)}


def historical_facts(conn, as_of):
    """Rebuild the fact view from source versions available at end of `as_of`."""
    maps, conflicts = {}, set()
    id_map_upload = latest_upload_as_of(conn, "id_map", as_of)
    for row in conn.execute("SELECT m.* FROM id_map_records m WHERE m.upload_id=?", (id_map_upload,)) if id_map_upload else []:
        if row["client_id"] in maps and maps[row["client_id"]] != row["max_id"]:
            conflicts.add(row["client_id"])
        else:
            maps[row["client_id"]] = row["max_id"]
    for key in conflicts:
        maps.pop(key, None)

    channels = {}
    for row in conn.execute(
        """SELECT c.* FROM channel_records c JOIN uploads u ON u.id=c.upload_id
           WHERE substr(u.created_at,1,10)<=? ORDER BY COALESCE(c.event_at,''),c.id""",
        (as_of,),
    ):
        channels.setdefault(row["max_id"], []).append(row)
    active_channels = {}
    channel_upload = latest_upload_as_of(conn, "channel_subscribers", as_of)
    for row in conn.execute(
        "SELECT c.* FROM channel_records c WHERE c.upload_id=? ORDER BY COALESCE(event_at,'')",
        (channel_upload,),
    ) if channel_upload else []:
        active_channels.setdefault(row["max_id"], []).append(row)
    historical_channel_clients = {
        row[0]
        for row in conn.execute(
            """SELECT DISTINCT h.client_id FROM channel_membership_history h
               JOIN uploads u ON u.id=h.upload_id WHERE substr(u.created_at,1,10)<=?""",
            (as_of,),
        )
    }

    customers = {}
    customer_uploads = [
        latest_upload_as_of(conn, source, as_of)
        for source in ("bs_funnel", "bs_channel", "retail_funnel", "retail_channel")
    ]
    customer_uploads = [upload_id for upload_id in customer_uploads if upload_id]
    if customer_uploads:
        placeholders = ",".join("?" for _ in customer_uploads)
        for row in conn.execute(
            f"SELECT c.* FROM customer_records c WHERE c.upload_id IN ({placeholders})",
            customer_uploads,
        ):
            customers.setdefault((row["max_id"], row["branch"], row["source_kind"]), []).append(row)

    latest_subscriber_uploads = {}
    for row in conn.execute(
        """SELECT s.funnel_id,s.upload_id,u.created_at
           FROM subscriber_records s JOIN uploads u ON u.id=s.upload_id
           WHERE substr(u.created_at,1,10)<=?
           GROUP BY s.funnel_id,s.upload_id ORDER BY u.created_at,u.id""",
        (as_of,),
    ):
        latest_subscriber_uploads[row["funnel_id"]] = row["upload_id"]
    subscriber_uploads = list(latest_subscriber_uploads.values())
    if not subscriber_uploads:
        return []

    campaign_rows = active_campaigns(conn)
    placeholders = ",".join("?" for _ in subscriber_uploads)
    subscribers = conn.execute(
        f"SELECT s.* FROM subscriber_records s WHERE s.upload_id IN ({placeholders}) ORDER BY subscription_at,id",
        subscriber_uploads,
    )
    result, seen = [], set()
    for subscriber in subscribers:
        cohort = subscriber["subscription_at"][:7]
        academic_year = academic_year_key(subscriber["subscription_at"])
        if not academic_year:
            continue
        unique_key = (subscriber["client_id"], academic_year)
        if unique_key in seen:
            continue
        seen.add(unique_key)
        max_id = clean_id(subscriber["max_id"]) or maps.get(subscriber["client_id"])
        member_rows = channels.get(max_id, []) if max_id else []
        current_rows = active_channels.get(max_id, []) if max_id else []
        latest_member = current_rows[-1] if current_rows else None
        member_status = str(latest_member["status"] or "1").strip().lower() if latest_member else ""
        active_statuses = {"1", "true", "active", "РїРѕРґРїРёСЃР°РЅ", "РїРѕРґРїРёСЃР°РЅ РЅР° РєР°РЅР°Р»"}
        active = bool(latest_member and member_status in active_statuses and not latest_member["left_at"])
        values = {}
        for branch in ("funnel", "channel"):
            primary = customers.get((max_id, branch, "bs"), []) if max_id else []
            secondary = customers.get((max_id, branch, "retail"), []) if max_id else []
            values[branch] = workbook_customer(primary, secondary)
        campaign_id = None
        utm = subscriber["utm_campaign"] or ""
        for campaign in campaign_rows:
            if (
                campaign["month"] == cohort
                and campaign["funnel_id"] == subscriber["funnel_id"]
                and (
                    campaign["campaign_id"] == utm
                    or (campaign["campaign_id"] and campaign["campaign_id"] in utm)
                )
            ):
                campaign_id = campaign["campaign_id"]
                break
        result.append({
            "funnel_id": subscriber["funnel_id"], "client_id": subscriber["client_id"],
            "max_id": max_id, "full_name": subscriber["full_name"],
            "subscription_at": subscriber["subscription_at"], "cohort_month": cohort,
            "utm_source": subscriber["utm_source"], "utm_medium": subscriber["utm_medium"],
            "utm_campaign": subscriber["utm_campaign"], "utm_content": subscriber["utm_content"],
            "utm_term": subscriber["utm_term"], "tag": subscriber["tag"], "campaign_id": campaign_id,
            "ever_channel": bool(member_rows) or subscriber["client_id"] in historical_channel_clients,
            "active_channel": active,
            "funnel_lead": values["funnel"]["lead"], "funnel_lead_at": values["funnel"]["lead_at"],
            "funnel_paid": values["funnel"]["paid"], "funnel_paid_at": values["funnel"]["paid_at"],
            "funnel_revenue": str(values["funnel"]["revenue"]),
            "channel_lead": values["channel"]["lead"], "channel_lead_at": values["channel"]["lead_at"],
            "channel_paid": values["channel"]["paid"], "channel_paid_at": values["channel"]["paid_at"],
            "channel_revenue": str(values["channel"]["revenue"]), "source_batch_id": subscriber["upload_id"],
        })
    return result


def backfill_existing_subscriber_max_ids():
    """Enrich imports made before subscriber MAX IDs were stored."""
    migration_key = "subscriber_max_id_backfill_v1"
    with db() as conn:
        if conn.execute("SELECT 1 FROM settings WHERE key=?", (migration_key,)).fetchone():
            return 0
        updated = 0
        uploads = list(conn.execute("SELECT id,file_hash,sheet_name FROM uploads WHERE source_type='subscribers'"))
        for upload in uploads:
            digest = upload["file_hash"].split(":", 1)[0]
            path = next((candidate for suffix in (".csv", ".xlsx", ".xls") if (candidate := UPLOADS / f"{digest}{suffix}").exists()), None)
            if not path:
                continue
            if path.suffix.lower() == ".csv":
                with path.open("rb") as binary:
                    sample_bytes = binary.read(64 * 1024)
                encoding = next(enc for enc in ("utf-8-sig", "cp1251", "utf-8") if _can_decode(sample_bytes, enc))
                sample = sample_bytes.decode(encoding)
                delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
                with path.open("r", encoding=encoding, newline="") as source:
                    reader = csv.DictReader(source, delimiter=delimiter)
                    max_column = infer_mapping(reader.fieldnames or []).get("max_id")
                    updates = [
                        (max_id, upload["id"], row_number)
                        for row_number, row in enumerate(reader, 2)
                        if max_column and (max_id := clean_id(row.get(max_column)))
                    ]
            else:
                frame = read_table(path, upload["sheet_name"])
                frame.columns = [str(column).strip() for column in frame.columns]
                max_column = infer_mapping(frame.columns).get("max_id")
                updates = [
                    (max_id, upload["id"], index + 2)
                    for index, row in frame.iterrows()
                    if max_column and (max_id := clean_id(row.get(max_column)))
                ]
            if not updates:
                continue
            changes_before = conn.total_changes
            conn.executemany(
                "UPDATE subscriber_records SET max_id=? WHERE upload_id=? AND row_number=? AND (max_id IS NULL OR TRIM(max_id)='')",
                updates,
            )
            updated += conn.total_changes - changes_before
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (migration_key, str(updated)))
        if updated:
            rebuild_facts(conn)
        return updated


def restore_academic_year_subscribers():
    """Recover rows dropped by the old per-file client-only deduplication."""
    migration_key = "subscriber_timestamp_restore_v2"
    with db() as conn:
        if conn.execute("SELECT 1 FROM settings WHERE key=?", (migration_key,)).fetchone():
            return 0
        restored = 0
        # Only current snapshots affect facts; archived versions stay untouched.
        uploads = list(conn.execute("SELECT * FROM uploads WHERE source_type='subscribers' AND active=1"))
        for upload in uploads:
            digest = upload["file_hash"].split(":", 1)[0]
            path = next((candidate for suffix in (".csv", ".xlsx", ".xls") if (candidate := UPLOADS / f"{digest}{suffix}").exists()), None)
            if not path:
                continue
            frame = read_table(path, None if upload["sheet_name"] == "CSV" else upload["sheet_name"])
            frame.columns = [str(column).strip() for column in frame.columns]
            mapping = infer_mapping(frame.columns)
            if not mapping.get("client_id") or not mapping.get("subscription_at"):
                continue
            funnel_row = conn.execute("SELECT funnel_id FROM subscriber_records WHERE upload_id=? LIMIT 1", (upload["id"],)).fetchone()
            funnel = funnel_row[0] if funnel_row else upload["file_hash"].split(":", 1)[-1]
            date_column = mapping["subscription_at"]
            time_column = mapping.get("subscription_time")
            frame["__topscore_date"] = frame.apply(lambda row: subscription_moment(row.get(date_column), row.get(time_column) if time_column else None), axis=1)
            frame = frame.sort_values("__topscore_date", na_position="last", kind="stable")
            seen, records, errors = set(), [], 0

            def value(row, field):
                column = mapping.get(field)
                return row.get(column) if column in row else None

            for index, row in frame.iterrows():
                client_id = clean_id(value(row, "client_id"))
                subscription_at = subscription_moment(value(row, "subscription_at"),value(row,"subscription_time"))
                if not client_id or not subscription_at:
                    errors += 1
                    continue
                key = (client_id, academic_year_key(subscription_at))
                if key in seen:
                    continue
                seen.add(key)
                records.append((
                    upload["id"],funnel,client_id,clean_id(value(row,"platform_id")),clean_id(value(row,"max_id")),
                    str(value(row,"full_name") or ""),subscription_at,
                    *[str(value(row,field) or "") for field in ("utm_source","utm_medium","utm_campaign","utm_content","utm_term","tag")],
                    index + 2,
                ))
            before = conn.execute("SELECT COUNT(*) FROM subscriber_records WHERE upload_id=?", (upload["id"],)).fetchone()[0]
            conn.execute("DELETE FROM subscriber_records WHERE upload_id=?", (upload["id"],))
            conn.executemany("INSERT INTO subscriber_records(upload_id,funnel_id,client_id,platform_id,max_id,full_name,subscription_at,utm_source,utm_medium,utm_campaign,utm_content,utm_term,tag,row_number) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", records)
            restored += max(0, len(records) - before)
            quality = json.loads(upload["quality_json"] or "{}")
            quality["accepted"] = len(records)
            quality["duplicates"] = max(0, len(frame) - errors - len(records))
            conn.execute("UPDATE uploads SET accepted_count=?,duplicate_count=?,error_count=?,quality_json=? WHERE id=?", (len(records),quality["duplicates"],errors,json.dumps(quality,ensure_ascii=False),upload["id"]))
        rebuild_facts(conn)
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (migration_key, str(restored)))
        return restored


def migrate_latest_subscriber_snapshots():
    migration_key = "latest_source_snapshot_v2"
    with db() as conn:
        if conn.execute("SELECT 1 FROM settings WHERE key=?", (migration_key,)).fetchone():
            return
        latest = {}
        for row in conn.execute("SELECT s.funnel_id,s.upload_id,u.created_at FROM subscriber_records s JOIN uploads u ON u.id=s.upload_id WHERE u.active=1 GROUP BY s.funnel_id,s.upload_id ORDER BY u.created_at,u.id"):
            latest[row["funnel_id"]] = row["upload_id"]
        for funnel_id, upload_id in latest.items():
            conn.execute("UPDATE uploads SET active=0 WHERE id<>? AND id IN (SELECT DISTINCT upload_id FROM subscriber_records WHERE funnel_id=?)", (upload_id, funnel_id))
        for source_type in ("id_map","bs_funnel","bs_channel","retail_funnel","retail_channel","channel_subscribers"):
            upload_id = latest_active_upload(conn, source_type)
            if upload_id:
                conn.execute("UPDATE uploads SET active=0 WHERE source_type=? AND id<>?", (source_type, upload_id))
        rebuild_facts(conn)
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (migration_key, MOSCOW_NOW()))


def migrate_topscore_formula_v2():
    """Rebuild persisted facts once after adopting workbook dedupe rules."""
    migration_key = "topscore_workbook_formula_v3"
    with db() as conn:
        if conn.execute("SELECT 1 FROM settings WHERE key=?", (migration_key,)).fetchone():
            return
        rebuild_facts(conn)
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (migration_key, MOSCOW_NOW()))


def ensure_backend_id_map():
    """Keep the immutable SaleBot client ID -> MAX ID directory active."""
    with db() as conn:
        latest = conn.execute(
            """SELECT u.id,u.active,COUNT(m.id) record_count
               FROM uploads u JOIN id_map_records m ON m.upload_id=u.id
               WHERE u.source_type='id_map'
               GROUP BY u.id ORDER BY u.created_at DESC,u.id DESC LIMIT 1"""
        ).fetchone()
        if not latest:
            return 0
        changed = not latest["active"] or conn.execute(
            "SELECT 1 FROM uploads WHERE source_type='id_map' AND active=1 AND id<>? LIMIT 1",
            (latest["id"],),
        ).fetchone()
        conn.execute(
            "UPDATE uploads SET active=CASE WHEN id=? THEN 1 ELSE 0 END WHERE source_type='id_map'",
            (latest["id"],),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('backend_id_map_upload_id',?)",
            (latest["id"],),
        )
        if changed:
            rebuild_facts(conn)
        return latest["record_count"]


init_db()
ensure_backend_id_map()
backfill_existing_subscriber_max_ids()
restore_academic_year_subscribers()
migrate_latest_subscriber_snapshots()
migrate_topscore_formula_v2()
app = FastAPI(title="TopScore чатов", version="1.0.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def browser_session_token(username: str, password: str) -> str:
    return hmac.new(password.encode("utf-8"), username.encode("utf-8"), hashlib.sha256).hexdigest()


def login_page(error: str = "") -> HTMLResponse:
    message = '<p class="error">Неверный логин или пароль</p>' if error else ""
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход — TopScore</title><style>
    *{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f3f6fb;color:#10203b;font:16px Arial,sans-serif}}form{{width:min(420px,calc(100% - 32px));padding:34px;background:#fff;border-radius:18px;box-shadow:0 16px 50px #1b2d4a1f}}h1{{margin:0 0 8px}}p{{color:#73809a}}label{{display:block;margin-top:18px;font-size:13px;font-weight:700}}input{{width:100%;margin-top:7px;padding:13px;border:1px solid #d8dfeb;border-radius:10px;font-size:16px}}button{{width:100%;margin-top:24px;padding:14px;border:0;border-radius:10px;background:#6c57e9;color:#fff;font-weight:700;font-size:16px;cursor:pointer}}.error{{color:#c83232;background:#fff0f0;padding:10px;border-radius:8px}}</style></head><body><form method="post" action="/login"><h1>TopScore</h1><p>Войдите для работы с платформой</p>{message}<label>Логин<input name="username" autocomplete="username" required autofocus></label><label>Пароль<input name="password" type="password" autocomplete="current-password" required></label><button type="submit">Войти</button></form></body></html>""")


@app.middleware("http")
async def password_protection(request: Request, call_next):
    """Protect a shared deployment with a browser-friendly login page."""
    username = os.environ.get("TOPSCORE_USER", "").strip()
    password = os.environ.get("TOPSCORE_PASSWORD", "")
    if request.url.path in {"/health", "/login"} or not (username and password):
        return await call_next(request)

    expected_session = browser_session_token(username, password)
    valid = secrets.compare_digest(request.cookies.get("topscore_session", ""), expected_session)
    authorization = request.headers.get("Authorization", "")
    if not valid and authorization.startswith("Basic "):
        try:
            decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
            supplied_user, supplied_password = decoded.split(":", 1)
            valid = secrets.compare_digest(supplied_user, username) and secrets.compare_digest(
                supplied_password, password
            )
        except (ValueError, UnicodeDecodeError):
            valid = False
    if not valid:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Требуется повторный вход"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/login")
def login_form():
    return login_page()


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    expected_user = os.environ.get("TOPSCORE_USER", "").strip()
    expected_password = os.environ.get("TOPSCORE_PASSWORD", "")
    if not expected_user or not expected_password:
        return RedirectResponse("/", status_code=303)
    if not (
        secrets.compare_digest(username, expected_user)
        and secrets.compare_digest(password, expected_password)
    ):
        return login_page("invalid")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "topscore_session",
        browser_session_token(expected_user, expected_password),
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.get("/")
def index(): return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/bootstrap")
def bootstrap():
    with db() as conn:
        funnels = [dict(r) | {"aliases": json.loads(r["aliases"])} for r in conn.execute("SELECT * FROM funnels ORDER BY created_at")]
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}
        history = [dict(r) | {"quality": json.loads(r["quality_json"])} for r in conn.execute("SELECT * FROM uploads ORDER BY created_at DESC LIMIT 100")]
        ltv = [dict(r) for r in conn.execute("SELECT * FROM ltv_values ORDER BY month DESC")]
        id_map_count = conn.execute(
            """SELECT COUNT(*) FROM id_map_records m JOIN uploads u ON u.id=m.upload_id
               WHERE u.source_type='id_map' AND u.active=1"""
        ).fetchone()[0]
    return {"funnels": funnels, "settings": settings, "history": history, "ltv": ltv, "sources": SOURCE_LABELS, "salebot_links": SALEBOT_LINKS,
            "backend_sources": {"id_map": {"ready": bool(id_map_count), "count": id_map_count}}}


@app.post("/api/uploads/inspect")
async def inspect_upload(source_type: str = Form(...), file: UploadFile = File(...), sheet: str | None = Form(None)):
    if source_type not in SOURCE_LABELS: raise HTTPException(400, "Неизвестный тип источника")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".csv", ".xlsx", ".xls"}: raise HTTPException(400, "Поддерживаются CSV, XLSX и XLS")
    content = await file.read()
    digest = hashlib.sha256(content).hexdigest()
    saved = UPLOADS / f"{digest}{suffix}"
    if not saved.exists(): saved.write_bytes(content)
    return prepare_inspection(source_type, saved, file.filename or saved.name, digest, sheet)


def prepare_inspection(source_type: str, saved: Path, file_name: str, digest: str, sheet: str | None = None):
    sheets = list_sheets(saved)
    selected = sheet if sheet in sheets else sheets[0]
    frame = read_table(saved, None if selected == "CSV" else selected)
    frame.columns = [str(c).strip() for c in frame.columns]
    mapping = infer_mapping(frame.columns, source_type)
    signature = hashlib.sha1("|".join(map(clean_header, frame.columns)).encode()).hexdigest()
    with db() as conn:
        stored = conn.execute("SELECT mapping_json FROM mappings WHERE source_type=? AND signature=?", (source_type, signature)).fetchone()
        if stored: mapping.update(json.loads(stored[0]))
    revenue_column = preferred_revenue_column(frame.columns, source_type, mapping.get("revenue"))
    if revenue_column:
        mapping["revenue"] = revenue_column
    token = uuid.uuid4().hex
    meta = {"path": str(saved), "source_type": source_type, "file_name": file_name, "file_hash": digest, "sheet": selected, "signature": signature}
    (DRAFTS / f"{token}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    preview = frame.head(8).fillna("").astype(str).to_dict(orient="records")
    return {"token": token, "sheets": sheets, "selected_sheet": selected, "columns": list(frame.columns), "mapping": mapping, "required": REQUIRED[source_type], "missing": [x for x in REQUIRED[source_type] if x not in mapping], "rows": len(frame), "preview": preview}


class LinkUpload(BaseModel):
    url: str
    source_type: str = "subscribers"


def downloadable_table_url(url: str) -> str:
    """Turn supported public table links into direct downloadable files."""
    salebot = re.fullmatch(
        r"https?://(?:www\.)?salebot\.pro/shared/table/([A-Za-z0-9_-]+)"
        r"(?:/export\.csv)?/?(?:\?.*)?",
        url,
        flags=re.IGNORECASE,
    )
    if salebot:
        return f"https://salebot.pro/shared/table/{salebot.group(1)}/export.csv"
    if "docs.google.com/spreadsheets" in url:
        match = re.search(r"/d/([^/]+)", url)
        if not match:
            raise HTTPException(422, "Не удалось определить Google-таблицу")
        return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=xlsx"
    return url


@app.post("/api/uploads/inspect-link")
def inspect_link(body: LinkUpload):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")): raise HTTPException(422, "Укажите полную ссылку на таблицу")
    url = downloadable_table_url(url)
    try:
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 TopScore/1.0",
            "Accept": "text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
        })
        with urllib.request.urlopen(request, timeout=90) as response:
            content = response.read(50 * 1024 * 1024 + 1)
            content_type = response.headers.get("content-type", "")
    except Exception as exc:
        raise HTTPException(422, f"Не удалось скачать таблицу: {exc}")
    if len(content) > 50 * 1024 * 1024: raise HTTPException(413, "Таблица больше 50 МБ")
    if "text/html" in content_type.lower() or content.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise HTTPException(422, "Ссылка открывает веб-страницу, а не таблицу. Нужна публичная ссылка SaleBot, Google Sheets или прямой CSV/XLSX")
    suffix = ".xlsx" if "sheet" in content_type or content.startswith(b"PK") else ".csv"
    digest = hashlib.sha256(content).hexdigest(); saved = UPLOADS / f"{digest}{suffix}"
    if not saved.exists(): saved.write_bytes(content)
    return prepare_inspection(body.source_type, saved, url, digest)


class ConfirmUpload(BaseModel):
    token: str
    mapping: dict[str, str]
    funnel_id: str | None = None
    defer_rebuild: bool = False


@app.post("/api/uploads/confirm")
def confirm_upload(body: ConfirmUpload):
    draft = DRAFTS / f"{body.token}.json"
    if not draft.exists(): raise HTTPException(404, "Черновик загрузки не найден")
    meta = json.loads(draft.read_text(encoding="utf-8"))
    source = meta["source_type"]
    missing = [f for f in REQUIRED[source] if not body.mapping.get(f)]
    if missing: raise HTTPException(422, f"Не сопоставлены поля: {', '.join(missing)}")
    if source == "subscribers" and not body.funnel_id: raise HTTPException(422, "Выберите воронку")
    frame = read_table(Path(meta["path"]), None if meta["sheet"] == "CSV" else meta["sheet"])
    frame.columns = [str(c).strip() for c in frame.columns]
    effective_mapping = dict(body.mapping)
    revenue_column = preferred_revenue_column(frame.columns, source, effective_mapping.get("revenue"))
    if revenue_column:
        effective_mapping["revenue"] = revenue_column
    if source == "subscribers":
        # Dedupe must retain the earliest subscription, regardless of source row order.
        date_column = body.mapping.get("subscription_at")
        time_column = body.mapping.get("subscription_time")
        frame["__topscore_date"] = frame.apply(lambda row: subscription_moment(row.get(date_column), row.get(time_column) if time_column else None), axis=1)
        frame = frame.sort_values("__topscore_date", na_position="last", kind="stable")
    effective_hash = meta["file_hash"] + (f":{body.funnel_id}" if source == "subscribers" else "")
    if source in {"retail_funnel", "retail_channel"}:
        # Parser version: older imports silently lost rows whose MAX ID was
        # empty even though the workbook's TG ID join key was present.
        effective_hash += ":retail-tg-v2"
    if source in {"bs_funnel", "bs_channel"}:
        # Parser version: BS revenue must come from EY (order sum), not EX.
        effective_hash += ":bs-ey-revenue-v1"
    with db() as conn:
        old = conn.execute("SELECT * FROM uploads WHERE source_type=? AND file_hash=? AND sheet_name=?", (source, effective_hash, meta["sheet"])).fetchone()
        if old:
            changed = False
            if source == "subscribers" and body.mapping.get("max_id") in frame.columns:
                max_column = body.mapping["max_id"]
                existing_max_ids = {
                    row["row_number"]: clean_id(row["max_id"])
                    for row in conn.execute(
                        "SELECT row_number,max_id FROM subscriber_records WHERE upload_id=?",
                        (old["id"],),
                    )
                }
                updates = [
                    (mid, old["id"], idx + 2)
                    for idx, row in frame.iterrows()
                    if (mid := clean_id(row.get(max_column))) and not existing_max_ids.get(idx + 2)
                ]
                if updates:
                    conn.executemany(
                        "UPDATE subscriber_records SET max_id=? WHERE upload_id=? AND row_number=? AND (max_id IS NULL OR TRIM(max_id)='')",
                        updates,
                    )
                    changed = True
            if not old["active"]:
                conn.execute("UPDATE uploads SET active=1 WHERE id=?", (old["id"],))
                changed = True
            rebuild_required = source == "subscribers" and changed
            if rebuild_required and not body.defer_rebuild:
                rebuild_facts(conn)
            return {"id": old["id"], "duplicate_file": True, "quality": json.loads(old["quality_json"]), "rebuild_required": rebuild_required and body.defer_rebuild}
        upload_id = uuid.uuid4().hex
        quality = {"read": len(frame), "accepted": 0, "duplicates": 0, "errors": 0, "missing_client_id": 0, "missing_max_id": 0, "invalid_dates": 0, "without_utm": 0, "id_conflicts": 0}
        seen = {} if source == "id_map" else set()
        rows_to_insert = []
        def val(row, key):
            col = effective_mapping.get(key); return row.get(col) if col in row else None
        retail_tg_column = next((column for column in frame.columns if clean_header(column) == "tg id"), None)
        for idx, row in frame.iterrows():
            rn = idx + 2
            if source == "subscribers":
                cid, dt = clean_id(val(row,"client_id")), subscription_moment(val(row,"subscription_at"),val(row,"subscription_time"))
                if not cid: quality["missing_client_id"] += 1; quality["errors"] += 1; continue
                if not dt: quality["invalid_dates"] += 1; quality["errors"] += 1; continue
                key = (body.funnel_id,cid,academic_year_key(dt))
                if key in seen: quality["duplicates"] += 1; continue
                seen.add(key)
                if not val(row,"utm_source") and not val(row,"utm_campaign"): quality["without_utm"] += 1
                rows_to_insert.append((upload_id,body.funnel_id,cid,clean_id(val(row,"platform_id")),clean_id(val(row,"max_id")),str(val(row,"full_name") or ""),dt,*[str(val(row,k) or "") for k in ("utm_source","utm_medium","utm_campaign","utm_content","utm_term","tag")],rn))
            elif source == "id_map":
                cid, mid = clean_id(val(row,"client_id")), clean_id(val(row,"max_id"))
                if not cid: quality["missing_client_id"] += 1; quality["errors"] += 1; continue
                if not mid: quality["missing_max_id"] += 1; quality["errors"] += 1; continue
                if cid in seen:
                    if seen[cid] == mid:
                        quality["duplicates"] += 1
                    else:
                        # Remove the earlier candidate: a conflict must never be resolved silently.
                        quality["id_conflicts"] += 1; quality["errors"] += 1
                        rows_to_insert = [item for item in rows_to_insert if item[1] != cid]
                        seen[cid] = None
                    continue
                seen[cid] = mid; rows_to_insert.append((upload_id,cid,mid,rn))
            elif source in {"bs_funnel","bs_channel","retail_funnel","retail_channel"}:
                # Google Sheets uses RetailCRM column N (TG ID) in all of its
                # INDEX/MATCH formulas. Prefer it and fall back to mapped MAX ID.
                mid = clean_id(row.get(retail_tg_column)) if source.startswith("retail") and retail_tg_column else None
                mid = mid or clean_id(val(row,"max_id"))
                if not mid: quality["missing_max_id"] += 1; quality["errors"] += 1; continue
                rows_to_insert.append((upload_id,"channel" if source.endswith("channel") else "funnel","bs" if source.startswith("bs") else "retail",mid,parse_date(val(row,"lead_at")),parse_date(val(row,"paid_at")),str(val(row,"status") or ""),str(decimal_value(val(row,"revenue"))),rn))
            elif source == "channel_subscribers":
                mid = clean_id(val(row,"max_id"))
                if not mid: quality["missing_max_id"] += 1; quality["errors"] += 1; continue
                rows_to_insert.append((upload_id,mid,parse_date(val(row,"subscription_at")),str(val(row,"active_status") or "active"),parse_date(val(row,"left_at")),rn))
            elif source == "campaigns":
                cid = clean_id(val(row,"campaign_id")); spend = decimal_value(val(row,"spend"))
                if not cid: quality["errors"] += 1; continue
                settings = {r["key"]:r["value"] for r in conn.execute("SELECT * FROM settings")}
                mode, rate = settings.get("vat_mode","included"), Decimal(settings.get("vat_rate","22"))/100
                final = spend * (1 + rate) if mode == "add" else (spend / (1 + rate) if mode == "exclude" else spend)
                rows_to_insert.append((upload_id,cid,str(val(row,"campaign_name") or ""),clean_id(val(row,"funnel_id")),parse_date(val(row,"date_from")),parse_date(val(row,"date_to")),str(val(row,"month") or "")[:7],str(spend),str(final),str(val(row,"currency") or "RUB"),*[str(val(row,k) or "") for k in ("utm_source","utm_medium","utm_campaign","utm_content","utm_term")],MOSCOW_NOW()))
            elif source == "ltv":
                month = str(val(row,"month") or "")[:7]; ltv = decimal_value(val(row,"ltv_value"))
                if not re.fullmatch(r"\d{4}-\d{2}",month) or ltv <= 0: quality["errors"] += 1; continue
                rows_to_insert.append((upload_id,month,clean_id(val(row,"funnel_id")),str(ltv),parse_date(val(row,"date_from")),parse_date(val(row,"date_to")),MOSCOW_NOW()))
        quality["accepted"] = len(rows_to_insert)
        conn.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)",(upload_id,source,meta["file_name"],effective_hash,meta["sheet"],len(frame),len(rows_to_insert),quality["duplicates"],quality["errors"],json.dumps(quality,ensure_ascii=False),MOSCOW_NOW(),"Администратор"))
        if source == "subscribers": conn.executemany("INSERT INTO subscriber_records(upload_id,funnel_id,client_id,platform_id,max_id,full_name,subscription_at,utm_source,utm_medium,utm_campaign,utm_content,utm_term,tag,row_number) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",rows_to_insert)
        elif source == "id_map": conn.executemany("INSERT INTO id_map_records(upload_id,client_id,max_id,row_number) VALUES(?,?,?,?)",rows_to_insert)
        elif source in {"bs_funnel","bs_channel","retail_funnel","retail_channel"}: conn.executemany("INSERT INTO customer_records(upload_id,branch,source_kind,max_id,lead_at,paid_at,status,revenue,row_number) VALUES(?,?,?,?,?,?,?,?,?)",rows_to_insert)
        elif source == "channel_subscribers": conn.executemany("INSERT INTO channel_records(upload_id,max_id,event_at,status,left_at,row_number) VALUES(?,?,?,?,?,?)",rows_to_insert)
        elif source == "campaigns": conn.executemany("INSERT INTO campaigns(upload_id,campaign_id,campaign_name,funnel_id,date_from,date_to,month,spend_original,spend_final,currency,utm_source,utm_medium,utm_campaign,utm_content,utm_term,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",rows_to_insert)
        elif source == "ltv": conn.executemany("INSERT OR REPLACE INTO ltv_values(upload_id,month,funnel_id,ltv_value,valid_from,valid_to,created_at) VALUES(?,?,?,?,?,?,?)",rows_to_insert)
        if source == "subscribers":
            conn.execute("UPDATE uploads SET active=0 WHERE id<>? AND id IN (SELECT DISTINCT upload_id FROM subscriber_records WHERE funnel_id=?)", (upload_id, body.funnel_id))
        elif source in {"id_map","bs_funnel","bs_channel","retail_funnel","retail_channel","channel_subscribers","campaigns"}:
            conn.execute("UPDATE uploads SET active=0 WHERE source_type=? AND id<>?", (source, upload_id))
        conn.execute("INSERT OR REPLACE INTO mappings(source_type,signature,mapping_json,created_at) VALUES(?,?,?,?)",(source,meta["signature"],json.dumps(effective_mapping,ensure_ascii=False),MOSCOW_NOW()))
        if not body.defer_rebuild:
            rebuild_facts(conn)
    draft.unlink(missing_ok=True)
    return {"id":upload_id,"duplicate_file":False,"quality":quality,"rebuild_required":body.defer_rebuild}


@app.post("/api/rebuild")
def rebuild_all_facts():
    with db() as conn:
        result = rebuild_facts(conn)
    return {"ok": True, **(result or {})}


def safe_div(a, b): return float(a / b) if a is not None and b else None


@app.get("/api/dashboard")
def dashboard(cohort: str | None=None, funnel_id: str | None=None, campaign_id: str | None=None, utm_source: str | None=None, path: str="both", channel_mode: str="ever"):
    where, params = ["1=1"], []
    for col,value in (("cohort_month",cohort),("funnel_id",funnel_id),("campaign_id",campaign_id),("utm_source",utm_source)):
        if value:
            vals=[v for v in value.split(",") if v]; where.append(f"{col} IN ({','.join('?'*len(vals))})"); params.extend(vals)
    with db() as conn:
        rows=[dict(r) for r in conn.execute("SELECT * FROM facts WHERE "+" AND ".join(where),params)]
        funnel_names={r["id"]:r["name"] for r in conn.execute("SELECT id,name FROM funnels")}
        campaigns=active_campaigns(conn)
        ltv_rows=[dict(r) for r in conn.execute("SELECT * FROM ltv_values")]
    spend=sum(decimal_value(c["spend_final"]) for c in campaigns if (not campaign_id or c["campaign_id"] in campaign_id.split(",")) and (not cohort or c["month"] in cohort.split(",")))
    subs=len({r["client_id"] for r in rows}); channel=sum(1 for r in rows if r["active_channel" if channel_mode=="active" else "ever_channel"])
    def branch_metric(name):
        return sum(1 for r in rows if r[f"{name}_lead"]),sum(1 for r in rows if r[f"{name}_paid"]),sum(decimal_value(r[f"{name}_revenue"]) for r in rows)
    fm,cm=branch_metric("funnel"),branch_metric("channel")
    if path=="funnel": leads,paid,revenue=fm
    elif path=="channel": leads,paid,revenue=cm
    else: leads,paid,revenue=fm[0]+cm[0],fm[1]+cm[1],fm[2]+cm[2]
    ltv=Decimal("0"); missing_ltv=set()
    for r in rows:
        pay_count=(r["funnel_paid"] if path!="channel" else 0)+(r["channel_paid"] if path!="funnel" else 0)
        if not pay_count: continue
        pay_month=(r["funnel_paid_at"] or r["channel_paid_at"] or "")[:7]
        match=next((x for x in ltv_rows if x["month"]==pay_month and (not x["funnel_id"] or x["funnel_id"]==r["funnel_id"])),None)
        if match: ltv += decimal_value(match["ltv_value"])*pay_count
        else: missing_ltv.add(pay_month or "без даты")
    metrics={"spend":float(spend),"subscribers":subs,"cpf":safe_div(spend,subs),"channel_subscribers":channel,"leads":leads,"cpl":safe_div(spend,leads),"paid":paid,"cmc":safe_div(spend,paid),"revenue":float(revenue),"average_check":safe_div(revenue,paid),"ltv":float(ltv) if not missing_ltv else None,"drr_ltv":safe_div(spend*100,ltv),"drr_revenue":safe_div(spend*100,revenue)}
    groups={}
    for r in rows:
        key=(r["cohort_month"],r["funnel_id"]); g=groups.setdefault(key,{"cohort":key[0],"funnel_id":key[1],"funnel":funnel_names.get(key[1],key[1]),"subscribers":0,"leads":0,"paid":0,"revenue":0.0})
        g["subscribers"]+=1; g["leads"]+=r["funnel_lead"]+r["channel_lead"]; g["paid"]+=r["funnel_paid"]+r["channel_paid"]; g["revenue"]+=float(decimal_value(r["funnel_revenue"])+decimal_value(r["channel_revenue"]))
    dimensions={k:sorted({str(r[k]) for r in rows if r.get(k)}) for k in ("cohort_month","funnel_id","campaign_id","utm_source")}
    detail=[{k:r[k] for k in ("client_id","max_id","cohort_month","funnel_id","utm_source","utm_campaign","campaign_id","funnel_lead","funnel_paid","channel_lead","channel_paid","source_batch_id")} for r in rows[:500]]
    return {"metrics":metrics,"rows":list(groups.values()),"detail":detail,"dimensions":dimensions,"warnings":[f"Нет LTV: {', '.join(sorted(missing_ltv))}"] if missing_ltv else [],"branches":{"funnel":{"leads":fm[0],"paid":fm[1],"revenue":float(fm[2])},"channel":{"leads":cm[0],"paid":cm[1],"revenue":float(cm[2])}}}


class FunnelIn(BaseModel):
    name: str; aliases: list[str]=[]

@app.post("/api/funnels")
def add_funnel(body:FunnelIn):
    fid=re.sub(r"[^a-z0-9]+","-",body.name.lower().encode("translit","ignore").decode() if False else uuid.uuid4().hex[:8])
    with db() as conn: conn.execute("INSERT INTO funnels VALUES(?,?,?,?,1,?)",(fid,body.name,json.dumps(body.aliases,ensure_ascii=False),json.dumps(body.aliases,ensure_ascii=False),MOSCOW_NOW()))
    return {"id":fid}


class SettingsIn(BaseModel):
    vat_mode:str; vat_rate:float

@app.put("/api/settings")
def settings(body:SettingsIn):
    if body.vat_mode not in {"included","add","exclude"} or not 0 <= body.vat_rate <= 100: raise HTTPException(422,"Некорректные настройки НДС")
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings VALUES('vat_mode',?)",(body.vat_mode,)); conn.execute("INSERT OR REPLACE INTO settings VALUES('vat_rate',?)",(str(body.vat_rate),))
    return {"ok":True}


class CampaignIn(BaseModel):
    funnel_id: str
    month: str
    campaign_id: str
    campaign_name: str | None = None
    spend: float


class CampaignBatchIn(BaseModel):
    campaigns: list[CampaignIn]


def vat_spend(conn, amount: Decimal) -> Decimal:
    values = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}
    mode = values.get("vat_mode", "included")
    rate = Decimal(values.get("vat_rate", "22")) / 100
    if mode == "add": return amount * (1 + rate)
    if mode == "exclude": return amount / (1 + rate)
    return amount


def validate_campaign(body: CampaignIn, available_funnels: set[str]) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}", body.month):
        raise ValueError("месяц должен быть в формате ГГГГ-ММ")
    if not body.campaign_id.strip():
        raise ValueError("не указан ID рекламной кампании")
    if body.spend < 0:
        raise ValueError("траты не могут быть отрицательными")
    if body.funnel_id not in available_funnels:
        raise ValueError("воронка ещё не появилась в итоговой таблице")


def insert_campaign(conn, body: CampaignIn) -> None:
    original = Decimal(str(body.spend)); final = vat_spend(conn, original)
    conn.execute(
        "INSERT INTO campaigns(upload_id,campaign_id,campaign_name,funnel_id,month,spend_original,spend_final,currency,created_at) VALUES(NULL,?,?,?,?,?,?,?,?)",
        (body.campaign_id.strip(), body.campaign_name or "", body.funnel_id, body.month, str(original), str(final), "RUB", MOSCOW_NOW()),
    )


@app.get("/api/campaigns")
def list_campaigns():
    with db() as conn:
        rows = sorted(active_campaigns(conn), key=lambda row: (row["month"] or "", row["campaign_id"]), reverse=True)
        available = [dict(r) for r in conn.execute("SELECT DISTINCT f.funnel_id id, COALESCE(n.name,f.funnel_id) name FROM facts f LEFT JOIN funnels n ON n.id=f.funnel_id ORDER BY name")]
    return {"campaigns": rows, "funnels": available}


@app.post("/api/campaigns")
def create_campaign(body: CampaignIn):
    with db() as conn:
        available = {row[0] for row in conn.execute("SELECT DISTINCT funnel_id FROM facts")}
        try: validate_campaign(body, available)
        except ValueError as exc: raise HTTPException(422, str(exc)) from exc
        insert_campaign(conn, body)
        rebuild_facts(conn)
    return {"ok": True}


@app.post("/api/campaigns/batch")
def create_campaign_batch(body: CampaignBatchIn):
    if not body.campaigns:
        raise HTTPException(422, "Добавьте хотя бы одну рекламную кампанию")
    if len(body.campaigns) > 500:
        raise HTTPException(422, "За один раз можно добавить не более 500 кампаний")
    with db() as conn:
        available = {row[0] for row in conn.execute("SELECT DISTINCT funnel_id FROM facts")}
        for index, campaign in enumerate(body.campaigns, 1):
            try: validate_campaign(campaign, available)
            except ValueError as exc: raise HTTPException(422, f"Строка {index}: {exc}") from exc
        for campaign in body.campaigns:
            insert_campaign(conn, campaign)
        rebuild_facts(conn)
    return {"ok": True, "created": len(body.campaigns)}


@app.delete("/api/campaigns/{campaign_row_id}")
def delete_campaign(campaign_row_id: int):
    with db() as conn:
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_row_id,)); rebuild_facts(conn)
    return {"ok": True}


def validate_as_of(as_of):
    if not as_of:
        return None
    try:
        datetime.strptime(as_of, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(422, "дата факта должна быть в формате ГГГГ-ММ-ДД") from exc
    return as_of


def fact_snapshot_dates(conn):
    source_types = ("subscribers", "id_map", "bs_funnel", "bs_channel", "retail_funnel", "retail_channel", "channel_subscribers", "channel_history")
    placeholders = ",".join("?" for _ in source_types)
    return [
        row[0]
        for row in conn.execute(
            f"""SELECT DISTINCT substr(created_at,1,10) fact_date FROM uploads
                WHERE source_type IN ({placeholders}) AND created_at<>''
                ORDER BY fact_date DESC""",
            source_types,
        )
    ]


def report_rows(conn, month=None, funnel_id=None, campaign_id=None, as_of=None):
    if as_of:
        rows = historical_facts(conn, as_of)
        return [
            row for row in rows
            if (not month or row["cohort_month"] == month)
            and (not funnel_id or row["funnel_id"] == funnel_id)
            and (not campaign_id or row["campaign_id"] == campaign_id)
        ]
    where, params = ["1=1"], []
    for col, value in (("cohort_month", month), ("funnel_id", funnel_id), ("campaign_id", campaign_id)):
        if value: where.append(f"{col}=?"); params.append(value)
    return [dict(r) for r in conn.execute("SELECT * FROM facts WHERE " + " AND ".join(where), params)]


@app.get("/api/reports/campaigns")
def campaign_report(month: str | None=None, funnel_id: str | None=None, campaign_id: str | None=None, as_of: str | None=None):
    as_of = validate_as_of(as_of)
    with db() as conn:
        rows = report_rows(conn, month, funnel_id, campaign_id, as_of)
        campaigns = active_campaigns(conn)
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM funnels")}
        configured_ltv = {
            (r["month"], r["funnel_id"]): decimal_value(r["ltv_value"])
            for r in conn.execute("SELECT * FROM ltv_values")
        }
        all_months = {r[0] for r in conn.execute("SELECT DISTINCT cohort_month FROM facts WHERE cohort_month<>''")}
        all_funnel_ids = {r[0] for r in conn.execute("SELECT DISTINCT funnel_id FROM facts WHERE funnel_id<>''")}
        snapshot_dates = fact_snapshot_dates(conn)
    # A campaign ID may be reused in another month or funnel. Keeping only the
    # ID merged unrelated cohorts and made the unfiltered report misleading.
    keys = sorted(
        {(r["cohort_month"], r["funnel_id"], r["campaign_id"]) for r in rows if r["campaign_id"]}
        | {
            (c["month"], c["funnel_id"], c["campaign_id"])
            for c in campaigns
            if c["campaign_id"] and c["month"] and c["funnel_id"]
            and (not month or c["month"] == month)
            and (not funnel_id or c["funnel_id"] == funnel_id)
            and (not campaign_id or c["campaign_id"] == campaign_id)
        }
    )
    output=[]
    def campaign_ltv(item, branch):
        """Columns M/Q in the workbook: fixed LTV by payment month."""
        if not item[f"{branch}_paid"]:
            return Decimal("0")
        paid_month=(item[f"{branch}_paid_at"] or "")[:7]
        if not paid_month:
            return Decimal("0")
        custom=configured_ltv.get((paid_month,item["funnel_id"])) or configured_ltv.get((paid_month,None))
        if custom is not None:
            return custom
        return DEFAULT_LTV_BY_MONTH_NUMBER.get(int(paid_month[5:7]),Decimal("0"))

    for cohort, fid, cid in keys:
        people=[r for r in rows if r["cohort_month"]==cohort and r["funnel_id"]==fid and r["campaign_id"]==cid]
        matching_campaigns=[c for c in campaigns if c["month"]==cohort and c["funnel_id"]==fid and c["campaign_id"]==cid]
        costs=sum(decimal_value(c["spend_final"]) for c in matching_campaigns)
        subs=len({r["client_id"] for r in people}); channel=sum(bool(r["ever_channel"]) for r in people)
        fl=sum(bool(r["funnel_lead"]) for r in people); fp=sum(bool(r["funnel_paid"]) for r in people); fr=sum(decimal_value(r["funnel_revenue"]) for r in people)
        cl=sum(bool(r["channel_lead"]) for r in people); cp=sum(bool(r["channel_paid"]) for r in people); cr=sum(decimal_value(r["channel_revenue"]) for r in people)
        fltv=sum((campaign_ltv(r,"funnel") for r in people),Decimal("0"))
        cltv=sum((campaign_ltv(r,"channel") for r in people),Decimal("0"))
        total_leads=fl+cl; total_paid=fp+cp; total_ltv=fltv+cltv
        output.append({
            "month":cohort,"funnel_id":fid,"funnel":names.get(fid,fid),
            "campaign_id":cid,"campaign_name":next((c["campaign_name"] for c in matching_campaigns),""),
            "spend":float(costs),"subscribers":subs,"cpf":safe_div(costs,subs),
            # Formulas mirror columns E:F in the workbook's campaign sheets.
            "channel_subscription_cr":safe_div(channel*100,subs),"channel_subscribers":channel,
            # Funnel block G:N.
            "funnel_lead_cr":safe_div(fl*100,subs),"funnel_leads":fl,
            "funnel_cpl":safe_div(costs,fl),"funnel_paid_cr_lead":safe_div(fp*100,fl),
            "funnel_paid_cr_subscriber":safe_div(fp*100,subs),"funnel_paid":fp,
            "funnel_revenue":float(fr),"funnel_ltv":float(fltv),"funnel_average_check":safe_div(fltv,fp),
            # MAX channel block O:Q.
            "channel_leads":cl,"channel_paid":cp,"channel_revenue":float(cr),"channel_ltv":float(cltv),
            # Total block R:X.
            "total_lead_cr":safe_div(total_leads*100,subs),"total_leads":total_leads,
            "cpl":safe_div(costs,total_leads),"total_paid":total_paid,
            "cmc":safe_div(costs,total_paid),"total_revenue":float(fr+cr),"total_ltv":float(total_ltv),
            "total_drr":safe_div(costs*100,total_ltv),
        })
    # Filter controls must not shrink after a selection. Otherwise users have
    # to return to "All" before they can choose another month or funnel.
    dims={
        "months":sorted(all_months | {c["month"] for c in campaigns if c["month"]}),
        "funnels":[{"id":k,"name":v} for k,v in names.items() if k in all_funnel_ids or any(c["funnel_id"]==k for c in campaigns)],
        "campaigns":sorted({c["campaign_id"] for c in campaigns if c["campaign_id"]} | {r["campaign_id"] for r in rows if r["campaign_id"]}),
    }
    return {"rows":output,"filters":dims,"as_of":as_of,"fact_dates":snapshot_dates}


@app.get("/api/reports/topscore")
def topscore_report(month: str | None=None, funnel_id: str | None=None, as_of: str | None=None):
    as_of = validate_as_of(as_of)
    with db() as conn:
        rows=report_rows(conn,month,funnel_id,None,as_of)
        campaigns=active_campaigns(conn)
        configured_ltv={(r["month"],r["funnel_id"]):decimal_value(r["ltv_value"]) for r in conn.execute("SELECT * FROM ltv_values")}
        names={r["id"]:r["name"] for r in conn.execute("SELECT id,name FROM funnels")}
        all_months={r[0] for r in conn.execute("SELECT DISTINCT cohort_month FROM facts WHERE cohort_month<>''")}
        all_funnel_ids={r[0] for r in conn.execute("SELECT DISTINCT funnel_id FROM facts WHERE funnel_id<>''")}
        snapshot_dates=fact_snapshot_dates(conn)
    direct_yandex_subscribers={"practice","admission","readiness-test"}
    groups={}
    for r in rows: groups.setdefault((r["cohort_month"],r["funnel_id"]),[]).append(r)

    def ltv_for(item, branch):
        if not item[f"{branch}_paid"]:
            return Decimal("0")
        paid_month=(item[f"{branch}_paid_at"] or "")[:7]
        revenue=decimal_value(item[f"{branch}_revenue"])
        if not paid_month:
            return Decimal("0")
        custom=configured_ltv.get((paid_month,item["funnel_id"])) or configured_ltv.get((paid_month,None))
        if custom is not None:
            return custom
        month_number=int(paid_month[5:7])
        # March is the only month without a fixed LTV in the workbook. Its
        # actual revenue is used there instead (cell N14 and the +L14 in Z14).
        return DEFAULT_LTV_BY_MONTH_NUMBER.get(month_number,revenue if month_number == 3 else Decimal("0"))

    output=[]
    for (cohort,fid),items in sorted(groups.items(),reverse=True):
        group_campaigns=[c for c in campaigns if c["month"]==cohort and c["funnel_id"]==fid]
        campaign_ids={c["campaign_id"] for c in group_campaigns}
        spend=sum(decimal_value(c["spend_final"]) for c in group_campaigns)
        # In the campaign summaries, subscribers are counted by campaign ID in
        # utm_campaign. The remaining TopScore formulas use utm_source=*ya*.
        yandex=[item for item in items if "ya" in (item["utm_source"] or "").lower()]
        if fid in direct_yandex_subscribers:
            subscribers=len(yandex)
        else:
            subscribers=sum(
                1
                for item in yandex
                for campaign_id in campaign_ids
                if campaign_id and campaign_id in (item["utm_campaign"] or "")
            )

        month_leads=month_paid=year_leads=year_clients=0
        month_revenue=year_revenue=Decimal("0")
        month_ltv=year_ltv=Decimal("0")
        for item in yandex:
            for branch in ("funnel","channel"):
                is_lead=bool(item[f"{branch}_lead"])
                is_paid=bool(item[f"{branch}_paid"])
                lead_month=(item[f"{branch}_lead_at"] or "")[:7]
                paid_month=(item[f"{branch}_paid_at"] or "")[:7]
                revenue=decimal_value(item[f"{branch}_revenue"])
                year_leads += is_lead
                year_clients += is_paid
                if is_paid:
                    year_revenue += revenue
                    year_ltv += ltv_for(item,branch)
                if is_lead and lead_month == cohort:
                    month_leads += 1
                if is_paid and paid_month == cohort:
                    month_paid += 1
                    month_revenue += revenue
                    month_ltv += ltv_for(item,branch)

        # In the supplied workbook the monthly LTV block for “7 files” is
        # explicitly tied to actual monthly revenue, unlike the other funnels.
        if fid == "7-materials":
            month_ltv = month_revenue

        month_drr=safe_div(spend*100,month_ltv)
        year_drr=safe_div(spend*100,year_ltv)
        cr10=safe_div(month_paid*100,subscribers)
        cr1=safe_div(year_clients*100,subscribers)
        output.append({
            "month":cohort,"funnel_id":fid,"funnel":names.get(fid,fid),
            "budget":float(spend),"subscribers":subscribers,"cpf":safe_div(spend,subscribers),
            "month_cr_subscriber":safe_div(month_leads*100,subscribers),"month_leads":month_leads,
            "month_cpl":safe_div(spend,month_leads),"month_cr_lead":safe_div(month_paid*100,month_leads),
            "month_cr10":cr10,"month_paid":month_paid,"month_revenue":float(month_revenue),
            "month_average_check":safe_div(month_revenue,month_paid),"month_ltv":float(month_ltv),
            "month_drr_ltv":month_drr,"month_cmc":safe_div(spend,month_paid),
            "year_cr_subscriber":safe_div(year_leads*100,subscribers),"year_leads":year_leads,
            "year_cpl":safe_div(spend,year_leads),"year_cr_lead":safe_div(year_clients*100,year_leads),
            "year_cr1":cr1,"year_cr1_cr10":safe_div(cr1,cr10),"year_clients":year_clients,
            "year_revenue":float(year_revenue),"year_average_check":safe_div(year_revenue,year_clients),
            "year_ltv":float(year_ltv),"year_drr_ltv":year_drr,
            "month_year_drr":safe_div(month_drr,year_drr),"year_cmc":safe_div(spend,year_clients),
        })
    return {
        "rows":output,
        "months":sorted(all_months | {c["month"] for c in campaigns if c["month"]}),
        "funnels":[{"id":k,"name":v} for k,v in names.items() if k in all_funnel_ids or any(c["funnel_id"]==k for c in campaigns)],
        "as_of":as_of,"fact_dates":snapshot_dates,
    }


@app.post("/api/uploads/{upload_id}/toggle")
def toggle_upload(upload_id:str):
    with db() as conn:
        row=conn.execute("SELECT active FROM uploads WHERE id=?",(upload_id,)).fetchone()
        if not row: raise HTTPException(404,"Загрузка не найдена")
        conn.execute("UPDATE uploads SET active=? WHERE id=?",(0 if row[0] else 1,upload_id)); rebuild_facts(conn)
    return {"ok":True}


@app.get("/api/export.csv")
def export_csv(cohort:str|None=None,funnel_id:str|None=None):
    where,params=["1=1"],[]
    if cohort: where.append("cohort_month=?");params.append(cohort)
    if funnel_id: where.append("funnel_id=?");params.append(funnel_id)
    with db() as conn: rows=[dict(r) for r in conn.execute("SELECT * FROM facts WHERE "+" AND ".join(where),params)]
    output=io.StringIO();
    if rows:
        writer=csv.DictWriter(output,fieldnames=rows[0].keys());writer.writeheader();writer.writerows(rows)
    data=("\ufeff"+output.getvalue()).encode("utf-8")
    return StreamingResponse(io.BytesIO(data),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=topscore-export.csv"})
