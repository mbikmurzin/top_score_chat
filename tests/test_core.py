import tempfile
import unittest
import json
import hashlib
from decimal import Decimal
from pathlib import Path

import app


class CoreRulesTest(unittest.TestCase):
    def test_identifiers_keep_leading_zeroes(self):
        self.assertEqual(app.clean_id("001234"), "001234")
        self.assertEqual(app.clean_id("123.0"), "123")
        self.assertIsNone(app.clean_id("—"))

    def test_decimal_parser(self):
        self.assertEqual(app.decimal_value("12 345,67 ₽"), Decimal("12345.67"))
        self.assertEqual(app.decimal_value("bad"), Decimal("0"))

    def test_safe_division(self):
        self.assertIsNone(app.safe_div(10, 0))
        self.assertEqual(app.safe_div(10, 4), 2.5)

    def test_column_mapping(self):
        mapping = app.infer_mapping(["ID", "Дата подписки", "UTM_Source", "tamtam_user_id [client]"])
        self.assertEqual(mapping["client_id"], "ID")
        self.assertEqual(mapping["subscription_at"], "Дата подписки")
        self.assertEqual(mapping["max_id"], "tamtam_user_id [client]")

    def test_retail_mapping_prefers_tg_id_used_by_workbook(self):
        mapping = app.infer_mapping(["MAX ID", "TG ID", "Дата регистрации"], "retail_funnel")
        self.assertEqual(mapping["max_id"], "TG ID")

    def test_bs_revenue_always_prefers_ey_order_sum(self):
        columns = ["T / MAX ID", "EX / старое поле", "EY / сумма заказов"]
        self.assertEqual(
            app.preferred_revenue_column(columns, "bs_channel", "EX / старое поле"),
            "EY / сумма заказов",
        )

    def test_salebot_shared_link_becomes_csv_export(self):
        shared = "https://salebot.pro/shared/table/JHB45bhtrRG1qMYpoExqFDF4h-hncNrYcgvtZf3Z7wI"
        self.assertEqual(app.downloadable_table_url(shared), shared + "/export.csv")
        self.assertEqual(app.downloadable_table_url(shared + "/export.csv"), shared + "/export.csv")

    def test_salebot_dates_use_current_date(self):
        mapping = app.infer_mapping(["client_id", "current_date", "start_date"])
        self.assertEqual(mapping["subscription_at"], "current_date")

    def test_salebot_plain_tamtam_column_is_mapped(self):
        mapping = app.infer_mapping(["client_id", "tamtam_user_id"])
        self.assertEqual(mapping["max_id"], "tamtam_user_id")

    def test_source_priority_and_snapshot_revenue(self):
        rows = [
            {"lead_at": "2026-09-02", "paid_at": "2026-09-10", "revenue": "100", "status": "Оплачено", "row_number": 2},
            {"lead_at": "2026-09-01", "paid_at": "2026-09-12", "revenue": "250", "status": "Оплачено", "row_number": 3},
        ]
        result = app.best_customer(rows)
        self.assertEqual(result["lead_at"], "2026-09-02")
        self.assertEqual(result["paid_at"], "2026-09-10")
        self.assertEqual(result["revenue"], Decimal("100"))

    def test_csv_read_preserves_id_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.csv"
            path.write_text("client_id;start_date\n00123;01.09.2026\n", encoding="utf-8")
            frame = app.read_table(path)
            self.assertEqual(frame.iloc[0]["client_id"], "00123")

    def test_only_active_campaign_uploads_are_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = app.DB_PATH
            try:
                app.DB_PATH = Path(tmp) / "test.db"
                app.init_db()
                with app.db() as conn:
                    conn.executemany(
                        "INSERT INTO uploads(id,source_type,file_name,file_hash,sheet_name,quality_json,active,created_at) VALUES(?,?,?,?,?,'{}',?,?)",
                        (("old", "campaigns", "old.xlsx", "old", "S", 0, "2026-09-01"),
                         ("new", "campaigns", "new.xlsx", "new", "S", 1, "2026-09-02")),
                    )
                    conn.executemany(
                        "INSERT INTO campaigns(upload_id,campaign_id,funnel_id,month,spend_original,spend_final,currency,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (("old", "rk-old", "courses", "2026-08", "100", "100", "RUB", "2026-09-01"),
                         ("new", "rk-new", "courses", "2026-08", "200", "200", "RUB", "2026-09-02"),
                         (None, "rk-manual", "courses", "2026-08", "300", "300", "RUB", "2026-09-03")),
                    )
                    campaigns = app.active_campaigns(conn)
                self.assertEqual({row["campaign_id"] for row in campaigns}, {"rk-new", "rk-manual"})
            finally:
                app.DB_PATH = old_db


class ImportIntegrationTest(unittest.TestCase):
    def test_october_payments_use_45164_ltv(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = app.DB_PATH
            try:
                app.DB_PATH = Path(tmp) / "test.db"
                app.init_db()
                with app.db() as conn:
                    conn.execute(
                        """INSERT INTO facts(
                               funnel_id,client_id,subscription_at,cohort_month,utm_source,utm_campaign,campaign_id,
                               funnel_lead,funnel_lead_at,funnel_paid,funnel_paid_at,funnel_revenue,
                               channel_lead,channel_paid,channel_revenue,source_batch_id
                           ) VALUES('courses','oct-client','2026-09-10','2026-09','ya','rk-oct','rk-oct',
                                    1,'2026-09-10',1,'2026-10-03','1000',0,0,'0','source')"""
                    )
                    conn.execute(
                        """INSERT INTO campaigns(
                               campaign_id,funnel_id,month,spend_original,spend_final,currency,created_at
                           ) VALUES('rk-oct','courses','2026-09','500','500','RUB','2026-10-05')"""
                    )
                top_row = app.topscore_report(month="2026-09", funnel_id="courses")["rows"][0]
                summary_row = app.campaign_report(month="2026-09", funnel_id="courses")["rows"][0]
                self.assertEqual(top_row["year_ltv"], 45164.0)
                self.assertEqual(summary_row["funnel_ltv"], 45164.0)
            finally:
                app.DB_PATH = old_db

    def test_static_id_map_is_reactivated_from_backend_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = app.DB_PATH
            try:
                app.DB_PATH = Path(tmp) / "test.db"
                app.init_db()
                with app.db() as conn:
                    conn.executemany(
                        "INSERT INTO uploads(id,source_type,file_name,file_hash,sheet_name,quality_json,active,created_at) VALUES(?,?,?,?,?,'{}',0,?)",
                        (("map-old", "id_map", "old.xlsx", "old", "S", "2026-09-01"),
                         ("map-new", "id_map", "new.xlsx", "new", "S", "2026-09-02")),
                    )
                    conn.execute("INSERT INTO id_map_records(upload_id,client_id,max_id) VALUES('map-old','1','old-max')")
                    conn.executemany(
                        "INSERT INTO id_map_records(upload_id,client_id,max_id) VALUES('map-new',?,?)",
                        (("1", "new-max-1"), ("2", "new-max-2")),
                    )
                self.assertEqual(app.ensure_backend_id_map(), 2)
                with app.db() as conn:
                    active = conn.execute(
                        "SELECT id FROM uploads WHERE source_type='id_map' AND active=1"
                    ).fetchone()["id"]
                self.assertEqual(active, "map-new")
                self.assertEqual(app.bootstrap()["backend_sources"]["id_map"]["count"], 2)
            finally:
                app.DB_PATH = old_db

    def test_bs_upload_ignores_old_ex_revenue_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_db, old_drafts = app.DB_PATH, app.DRAFTS
            try:
                app.DB_PATH = tmp_path / "test.db"
                app.DRAFTS = tmp_path / "drafts"
                app.DRAFTS.mkdir()
                app.init_db()
                source = tmp_path / "bs-channel.csv"
                source.write_text(
                    "MAX ID;EX / старое поле;EY / сумма заказов\nmax-1;111;222\n",
                    encoding="utf-8",
                )
                token = "bs-ey"
                meta = {
                    "path": str(source), "source_type": "bs_channel",
                    "file_name": source.name, "file_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "sheet": "CSV", "signature": "bs-ey-signature",
                }
                (app.DRAFTS / f"{token}.json").write_text(json.dumps(meta), encoding="utf-8")
                app.confirm_upload(app.ConfirmUpload(
                    token=token,
                    mapping={"max_id": "MAX ID", "revenue": "EX / старое поле"},
                ))
                with app.db() as conn:
                    record = conn.execute("SELECT revenue FROM customer_records").fetchone()
                    saved = conn.execute(
                        "SELECT mapping_json FROM mappings WHERE source_type='bs_channel'"
                    ).fetchone()
                self.assertEqual(record["revenue"], "222")
                self.assertEqual(json.loads(saved["mapping_json"])["revenue"], "EY / сумма заказов")
            finally:
                app.DB_PATH, app.DRAFTS = old_db, old_drafts

    def test_reports_can_rebuild_facts_as_of_upload_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = app.DB_PATH
            try:
                app.DB_PATH = Path(tmp) / "test.db"
                app.init_db()
                with app.db() as conn:
                    conn.executemany(
                        "INSERT INTO uploads(id,source_type,file_name,file_hash,sheet_name,quality_json,active,created_at) VALUES(?,?,?,?,?,'{}',?,?)",
                        (("subs-old", "subscribers", "old.csv", "subs-old", "CSV", 0, "2026-09-28T09:00:00"),
                         ("subs-new", "subscribers", "new.csv", "subs-new", "CSV", 1, "2026-10-01T09:00:00"),
                         ("crm-old", "retail_funnel", "old-crm.csv", "crm-old", "CSV", 0, "2026-09-28T10:00:00"),
                         ("crm-new", "retail_funnel", "new-crm.csv", "crm-new", "CSV", 1, "2026-10-01T10:00:00")),
                    )
                    conn.execute("INSERT INTO subscriber_records(upload_id,funnel_id,client_id,max_id,subscription_at,utm_source,utm_campaign) VALUES('subs-old','courses','1','max-1','2026-08-01','ya','rk-1')")
                    conn.executemany(
                        "INSERT INTO subscriber_records(upload_id,funnel_id,client_id,max_id,subscription_at,utm_source,utm_campaign) VALUES('subs-new','courses',?,?,?,'ya','rk-1')",
                        (("1", "max-1", "2026-08-01"), ("2", "max-2", "2026-08-02")),
                    )
                    conn.execute("INSERT INTO customer_records(upload_id,branch,source_kind,max_id,lead_at,revenue,row_number) VALUES('crm-old','funnel','retail','max-1','2026-08-03','0',2)")
                    conn.executemany(
                        "INSERT INTO customer_records(upload_id,branch,source_kind,max_id,lead_at,revenue,row_number) VALUES('crm-new','funnel','retail',?,?, '0',?)",
                        (("max-1", "2026-08-03", 2), ("max-2", "2026-09-30", 3)),
                    )
                    conn.execute("INSERT INTO campaigns(campaign_id,funnel_id,month,spend_original,spend_final,currency,created_at) VALUES('rk-1','courses','2026-08','1000','1000','RUB','2026-10-01')")
                old_report = app.topscore_report(month="2026-08", funnel_id="courses", as_of="2026-09-28")
                new_report = app.topscore_report(month="2026-08", funnel_id="courses", as_of="2026-10-01")
                self.assertEqual(old_report["rows"][0]["subscribers"], 1)
                self.assertEqual(old_report["rows"][0]["year_leads"], 1)
                self.assertEqual(new_report["rows"][0]["subscribers"], 2)
                self.assertEqual(new_report["rows"][0]["year_leads"], 2)
                self.assertEqual(old_report["fact_dates"], ["2026-10-01", "2026-09-28"])
            finally:
                app.DB_PATH = old_db

    def test_campaign_report_keeps_month_and_funnel_cohorts_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = app.DB_PATH
            try:
                app.DB_PATH = Path(tmp) / "test.db"
                app.init_db()
                with app.db() as conn:
                    conn.executemany(
                        """INSERT INTO facts(
                               funnel_id,client_id,subscription_at,cohort_month,
                               utm_source,utm_campaign,campaign_id,ever_channel,funnel_lead,
                               funnel_paid,funnel_paid_at,funnel_revenue,channel_lead,channel_paid,
                               channel_paid_at,channel_revenue,source_batch_id
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (("courses", "1", "2026-07-01", "2026-07", "ya", "rk-1", "rk-1", 1, 1, 1, "2026-07-10", "400", 1, 1, "2026-07-11", "100", "s"),
                         ("books", "2", "2026-08-01", "2026-08", "ya", "rk-1", "rk-1", 0, 1, 0, None, "0", 0, 0, None, "0", "s")),
                    )
                    conn.executemany(
                        "INSERT INTO campaigns(campaign_id,funnel_id,month,spend_original,spend_final,currency,created_at) VALUES(?,?,?,?,?,?,?)",
                        (("rk-1", "courses", "2026-07", "100", "100", "RUB", "2026-09-01"),
                         ("rk-1", "books", "2026-08", "200", "200", "RUB", "2026-09-01")),
                    )
                rows = app.campaign_report()["rows"]
                self.assertEqual(len(rows), 2)
                self.assertEqual({(row["month"], row["funnel_id"], row["spend"]) for row in rows},
                                 {("2026-07", "courses", 100.0), ("2026-08", "books", 200.0)})
                courses = next(row for row in rows if row["funnel_id"] == "courses")
                self.assertEqual(courses["channel_subscription_cr"], 100.0)
                self.assertEqual(courses["funnel_lead_cr"], 100.0)
                self.assertEqual(courses["funnel_cpl"], 100.0)
                self.assertEqual(courses["funnel_paid_cr_lead"], 100.0)
                self.assertEqual(courses["funnel_paid_cr_subscriber"], 100.0)
                self.assertEqual(courses["funnel_ltv"], 59473.0)
                self.assertEqual(courses["channel_ltv"], 59473.0)
                self.assertEqual(courses["funnel_average_check"], 59473.0)
                self.assertEqual(courses["total_lead_cr"], 200.0)
                self.assertAlmostEqual(courses["total_drr"], 100 / 118946 * 100)
            finally:
                app.DB_PATH = old_db

    def test_id_map_only_fills_missing_salebot_max_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = app.DB_PATH
            try:
                app.DB_PATH = Path(tmp) / "test.db"
                app.init_db()
                with app.db() as conn:
                    for upload_id, source in (("subs", "subscribers"), ("map", "id_map")):
                        conn.execute(
                            "INSERT INTO uploads(id,source_type,file_name,file_hash,sheet_name,quality_json,active,created_at) VALUES(?,?,?,?,?,'{}',1,?)",
                            (upload_id, source, "test.csv", upload_id, "CSV", "2026-09-01"),
                        )
                    conn.executemany(
                        "INSERT INTO subscriber_records(upload_id,funnel_id,client_id,max_id,subscription_at) VALUES('subs','courses',?,?,?)",
                        (("with-own", "sale-111", "2026-09-01"), ("without-own", None, "2026-09-01")),
                    )
                    conn.executemany(
                        "INSERT INTO id_map_records(upload_id,client_id,max_id) VALUES('map',?,?)",
                        (("with-own", "map-999"), ("without-own", "map-222")),
                    )
                    conn.execute(
                        "INSERT INTO uploads(id,source_type,file_name,file_hash,sheet_name,quality_json,active,created_at) VALUES('channel','channel_subscribers','test.csv','channel','CSV','{}',1,'2026-09-01')"
                    )
                    conn.executemany(
                        "INSERT INTO channel_records(upload_id,max_id,status,row_number) VALUES('channel',?,'1',?)",
                        (("sale-111", 2), ("map-222", 3)),
                    )
                    for upload_id, source in (("rf", "retail_funnel"), ("rc", "retail_channel")):
                        conn.execute(
                            "INSERT INTO uploads(id,source_type,file_name,file_hash,sheet_name,quality_json,active,created_at) VALUES(?,?,?,?,?,'{}',1,?)",
                            (upload_id, source, "test.csv", upload_id, "CSV", "2026-09-01"),
                        )
                    conn.executemany(
                        """INSERT INTO customer_records(
                               upload_id,branch,source_kind,max_id,lead_at,paid_at,status,revenue,row_number
                           ) VALUES('rf','funnel','retail',?,?,?,'','100',?)""",
                        (("sale-111", "2026-09-02", "2026-09-03", 2),
                         ("map-999", "2026-09-02", "2026-09-03", 3)),
                    )
                    conn.execute(
                        """INSERT INTO customer_records(
                               upload_id,branch,source_kind,max_id,lead_at,paid_at,status,revenue,row_number
                           ) VALUES('rf','funnel','retail','map-222','2026-09-04','2026-09-05','','200',4)"""
                    )
                    conn.executemany(
                        """INSERT INTO customer_records(
                               upload_id,branch,source_kind,max_id,lead_at,paid_at,status,revenue,row_number
                           ) VALUES('rc','channel','retail',?,?,?,'',?,?)""",
                        (("sale-111", "2026-09-06", "2026-09-07", "300", 2),
                         ("map-222", "2026-09-08", "2026-09-09", "400", 3)),
                    )
                    app.rebuild_facts(conn)
                    facts = {row["client_id"]: dict(row) for row in conn.execute("SELECT * FROM facts")}
                self.assertEqual(facts["with-own"]["max_id"], "sale-111")
                self.assertEqual(facts["without-own"]["max_id"], "map-222")
                self.assertEqual(facts["with-own"]["ever_channel"], 1)
                self.assertEqual(facts["without-own"]["ever_channel"], 1)
                self.assertEqual(facts["with-own"]["funnel_lead"], 1)
                self.assertEqual(facts["with-own"]["funnel_paid"], 1)
                self.assertEqual(facts["with-own"]["funnel_revenue"], "100")
                self.assertEqual(facts["with-own"]["channel_revenue"], "300")
                self.assertEqual(facts["without-own"]["funnel_revenue"], "200")
                self.assertEqual(facts["without-own"]["channel_revenue"], "400")
            finally:
                app.DB_PATH = old_db

    def test_confirm_is_idempotent_and_keeps_earliest_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_db, old_drafts = app.DB_PATH, app.DRAFTS
            try:
                app.DB_PATH = tmp_path / "test.db"
                app.DRAFTS = tmp_path / "drafts"
                app.DRAFTS.mkdir()
                app.init_db()
                source = tmp_path / "subscribers.csv"
                source.write_text(
                    "client_id;start_date;utm_source;utm_campaign\n001;15.09.2026;late;rk-101\n001;01.09.2026;ya;rk-101\n",
                    encoding="utf-8",
                )
                token = "draft"
                meta = {
                    "path": str(source), "source_type": "subscribers",
                    "file_name": source.name,
                    "file_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "sheet": "CSV", "signature": "sig",
                }
                (app.DRAFTS / f"{token}.json").write_text(json.dumps(meta), encoding="utf-8")
                body = app.ConfirmUpload(
                    token=token,
                    mapping={"client_id": "client_id", "subscription_at": "start_date", "utm_source": "utm_source", "utm_campaign": "utm_campaign"},
                    funnel_id="7-materials",
                )
                result = app.confirm_upload(body)
                self.assertEqual(result["quality"]["accepted"], 1)
                self.assertEqual(result["quality"]["duplicates"], 1)
                with app.db() as conn:
                    row = conn.execute("SELECT subscription_at,utm_source FROM facts").fetchone()
                    self.assertTrue(row["subscription_at"].startswith("2026-09-01"))
                    self.assertEqual(row["utm_source"], "ya")
                (app.DRAFTS / f"{token}.json").write_text(json.dumps(meta), encoding="utf-8")
                repeat = app.confirm_upload(body)
                self.assertTrue(repeat["duplicate_file"])
                with app.db() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 1)
                app.create_campaign(app.CampaignIn(
                    funnel_id="7-materials", month="2026-09",
                    campaign_id="rk-101", campaign_name="Тест РК", spend=1000,
                ))
                summary = app.campaign_report(month="2026-09")
                topscore = app.topscore_report(month="2026-09")
                self.assertEqual(summary["rows"][0]["spend"], 1000.0)
                self.assertEqual(topscore["rows"][0]["subscribers"], 1)
                self.assertEqual(topscore["rows"][0]["budget"], 1000.0)
                batch = app.create_campaign_batch(app.CampaignBatchIn(campaigns=[
                    app.CampaignIn(funnel_id="7-materials", month="2026-09", campaign_id="rk-102", campaign_name="РК 2", spend=200),
                    app.CampaignIn(funnel_id="7-materials", month="2026-09", campaign_id="rk-103", campaign_name="РК 3", spend=300),
                ]))
                self.assertEqual(batch["created"], 2)
                with app.db() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0], 3)
            finally:
                app.DB_PATH, app.DRAFTS = old_db, old_drafts

    def test_topscore_matches_workbook_month_and_lifetime_formulas(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = app.DB_PATH
            try:
                app.DB_PATH = Path(tmp) / "test.db"
                app.init_db()
                with app.db() as conn:
                    conn.execute("INSERT INTO uploads(id,source_type,file_name,file_hash,sheet_name,quality_json,active,created_at) VALUES('s','subscribers','s.csv','s','CSV','{}',1,'2026-09-01')")
                    conn.execute("INSERT INTO subscriber_records(upload_id,funnel_id,client_id,subscription_at,utm_source,utm_campaign) VALUES('s','courses','1','2026-08-01','yandex','rk-1')")
                    conn.execute("INSERT INTO facts(funnel_id,client_id,subscription_at,cohort_month,utm_source,utm_campaign,campaign_id,funnel_lead,funnel_lead_at,funnel_paid,funnel_paid_at,funnel_revenue,channel_lead,channel_paid,channel_revenue,source_batch_id) VALUES('courses','1','2026-08-01','2026-08','yandex','rk-1','rk-1',1,'2026-08-03',1,'2026-09-05','100000',0,0,'0','s')")
                    # TopScore is a Yandex-only report. A matching campaign from
                    # another source must not affect subscribers or conversions.
                    conn.execute("INSERT INTO facts(funnel_id,client_id,subscription_at,cohort_month,utm_source,utm_campaign,campaign_id,funnel_lead,funnel_lead_at,funnel_paid,funnel_paid_at,funnel_revenue,channel_lead,channel_paid,channel_revenue,source_batch_id) VALUES('courses','2','2026-08-02','2026-08','telegram','rk-1','rk-1',1,'2026-08-03',1,'2026-08-04','999999',0,0,'0','s')")
                    conn.execute("INSERT INTO facts(funnel_id,client_id,subscription_at,cohort_month,utm_source,utm_campaign,campaign_id,funnel_lead,funnel_paid,funnel_revenue,channel_lead,channel_paid,channel_revenue,source_batch_id) VALUES('books','3','2026-07-02','2026-07','yandex','rk-books','rk-books',0,0,'0',0,0,'0','s')")
                    conn.execute("INSERT INTO campaigns(campaign_id,campaign_name,funnel_id,month,spend_original,spend_final,currency,created_at) VALUES('rk-1','','courses','2026-08','1000','1000','RUB','2026-09-01')")
                report = app.topscore_report(month="2026-08", funnel_id="courses")
                row = report["rows"][0]
                self.assertEqual(row["subscribers"], 1)
                self.assertEqual(row["month_leads"], 1)
                self.assertEqual(row["month_paid"], 0)
                self.assertEqual(row["year_clients"], 1)
                self.assertEqual(row["year_revenue"], 100000.0)
                self.assertEqual(row["year_ltv"], 58384.0)
                self.assertEqual(report["months"], ["2026-07", "2026-08"])
                self.assertEqual({item["id"] for item in report["funnels"]}, {"books", "courses"})
            finally:
                app.DB_PATH = old_db


if __name__ == "__main__":
    unittest.main()
