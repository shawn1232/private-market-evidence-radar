from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from wechat_source_pool import ARTICLE_OUTPUT_FIELDS, Pool, canonicalize_wechat_url


class WeChatSourcePoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "wechat-pool.sqlite3"
        self.pool = Pool(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def nested_payload() -> dict:
        return {
            "nickname": "硬科技前沿",
            "fakeid": "fake-001",
            "auth_key": "AUTH-KEY-MUST-NOT-PERSIST",
            "data": {
                "token": "TOKEN-MUST-NOT-PERSIST",
                "list": [
                    {
                        "comm_msg_info": {"id": "group-1", "datetime": 1787184000},
                        "app_msg_ext_info": {
                            "title": "头条：芯片量产",
                            "content_url": (
                                "http://mp.weixin.qq.com/s?scene=1&amp;mid=100&amp;idx=1"
                                "&amp;sn=head&amp;__biz=BizA#wechat_redirect"
                            ),
                            "author": "研究组",
                            "digest": "量产摘要",
                            "copyright_type": 1,
                            "copyright_stat": 1,
                            "multi_app_msg_item_list": [
                                {
                                    "title": "次条：客户验证",
                                    "content_url": (
                                        "https://mp.weixin.qq.com/s?__biz=BizA&mid=100&idx=2"
                                        "&sn=child&from=timeline"
                                    ),
                                    "copyright_type": 1,
                                    "copyright_stat": 1,
                                    "is_deleted": True,
                                }
                            ],
                        },
                    },
                    {
                        "comm_msg_info": {"id": "group-2", "datetime": 1787270400},
                        "app_msg_ext_info": {
                            "title": "头条：新材料订单",
                            "content_url": "https://mp.weixin.qq.com/s/material-story?scene=2",
                            "copyright_type": 1,
                            "copyright_stat": 1,
                            "read_count": 1234,
                            "like_count": 88,
                            "body_markdown_path": "articles/material.md",
                        },
                    },
                ],
            },
        }

    def test_preview_expands_multi_article_groups_and_requires_matching_fingerprint(self) -> None:
        data = json.dumps(self.nested_payload(), ensure_ascii=False).encode("utf-8")
        preview = self.pool.preview_import(data, "history.json")

        self.assertTrue(preview["confirmation_required"])
        self.assertRegex(preview["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(preview["raw_records"], 3)
        self.assertEqual(preview["publish_groups"], 2)
        self.assertEqual(preview["expanded_url_items"], 3)
        self.assertEqual(preview["original_articles"], 2)
        self.assertEqual(preview["credential_fields_ignored"], 2)
        self.assertEqual(len(preview["accounts"]), 1)
        self.assertEqual(preview["accounts"][0]["account_name"], "硬科技前沿")
        self.assertTrue(set(ARTICLE_OUTPUT_FIELDS).issubset(preview["articles"][0]))

        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.pool.import_file(data, "history.json", "0" * 64)
        self.assertEqual(self.pool.get_stats()["stored_article_rows"], 0)

    def test_import_is_atomic_deduped_and_reports_added_vs_exists(self) -> None:
        data = json.dumps(self.nested_payload(), ensure_ascii=False).encode("utf-8")
        fingerprint = self.pool.preview_import(data, "history.json")["fingerprint"]

        first = self.pool.import_file(data, "history.json", fingerprint)
        second = self.pool.import_file(data, "history.json", fingerprint)

        self.assertEqual(first["added"], 3)
        self.assertEqual(first["exists"], 0)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["exists"], 3)
        self.assertEqual(self.pool.get_stats()["stored_article_rows"], 3)
        self.assertEqual(self.pool.get_stats()["publish_groups"], 2)
        self.assertEqual(self.pool.get_stats()["original_articles"], 2)
        self.assertEqual(self.pool.latest_import()["expanded_url_items"], 3)

        rows = self.pool.list_articles(limit=10)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(set(ARTICLE_OUTPUT_FIELDS).issubset(row) for row in rows))
        material = next(row for row in rows if row["title"] == "头条：新材料订单")
        self.assertEqual(material["read_count"], 1234)
        self.assertEqual(material["body_markdown_path"], "articles/material.md")

    def test_credentials_are_ignored_not_persisted(self) -> None:
        data = json.dumps(self.nested_payload(), ensure_ascii=False).encode("utf-8")
        preview = self.pool.preview_import(data, "auth_key=LEAK.json")
        self.pool.import_file(data, "auth_key=LEAK.json", preview["fingerprint"])

        connection = sqlite3.connect(self.db_path)
        try:
            dump = "\n".join(connection.iterdump())
            table_names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            columns = {
                row[1]
                for table in table_names
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
        finally:
            connection.close()
        self.assertNotIn("AUTH-KEY-MUST-NOT-PERSIST", dump)
        self.assertNotIn("TOKEN-MUST-NOT-PERSIST", dump)
        self.assertNotIn("auth_key", columns)
        self.assertNotIn("token", columns)
        self.assertNotIn("cookie", columns)
        self.assertIn("credential_status", columns)

    def test_csv_import_and_canonical_url_dedup(self) -> None:
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=("account_name", "fakeid", "title", "url", "publish_time", "read_count"),
        )
        writer.writeheader()
        writer.writerow(
            {
                "account_name": "机器人观察",
                "fakeid": "robot-1",
                "title": "第一版",
                "url": "https://mp.weixin.qq.com/s?__biz=R&mid=9&idx=1&sn=X&scene=1",
                "publish_time": "1787184000",
                "read_count": "100",
            }
        )
        writer.writerow(
            {
                "account_name": "机器人观察",
                "fakeid": "robot-1",
                "title": "更新版",
                "url": "http://mp.weixin.qq.com/s?sn=X&idx=1&mid=9&__biz=R&from=timeline",
                "publish_time": "1787184000",
                "read_count": "120",
            }
        )
        data = buffer.getvalue().encode("utf-8-sig")
        preview = self.pool.preview_import(data, "history.csv")
        self.assertEqual(preview["raw_records"], 2)
        self.assertEqual(preview["accepted_records"], 1)
        self.assertEqual(preview["duplicate_records_removed"], 1)
        self.assertEqual(preview["expanded_url_items"], 1)

        result = self.pool.import_file(data, "history.csv", preview["fingerprint"])
        self.assertEqual(result["added"], 1)
        row = self.pool.list_articles()[0]
        self.assertEqual(row["title"], "更新版")
        self.assertEqual(row["read_count"], 120)
        self.assertEqual(
            row["url"],
            "https://mp.weixin.qq.com/s?__biz=R&mid=9&idx=1&sn=X",
        )

    def test_add_urls_separates_added_exists_and_keeps_missing_dates_pending(self) -> None:
        result = self.pool.add_urls(
            [
                "https://mp.weixin.qq.com/s/pending-story?scene=1",
                "http://mp.weixin.qq.com/s/pending-story?from=timeline#fragment",
                "https://example.com/not-wechat",
            ],
            account_name="待跟踪账号",
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["exists"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["results"][0]["date_status"], "pending")
        self.assertEqual(result["results"][1]["status"], "exists")

        self.assertEqual(self.pool.get_stats()["pending_date_items"], 1)
        self.assertEqual(self.pool.query_recent(days=3650), [])
        article_id = result["results"][0]["article_id"]
        self.assertTrue(self.pool.remove(article_id))
        self.assertFalse(self.pool.remove(article_id))

    def test_date_window_scope_and_summary_exclude_pending_and_old_items(self) -> None:
        def stamp(day: int) -> int:
            return int(datetime(2026, 8, day, 4, tzinfo=timezone.utc).timestamp())

        payload = [
            {
                "account_name": "窗口账号",
                "fakeid": "window-1",
                "title": "窗口内",
                "url": "https://mp.weixin.qq.com/s/in-window",
                "publish_time": stamp(20),
            },
            {
                "account_name": "窗口账号",
                "fakeid": "window-1",
                "title": "窗口外",
                "url": "https://mp.weixin.qq.com/s/old",
                "publish_time": stamp(1),
            },
            {
                "account_name": "窗口账号",
                "fakeid": "window-1",
                "title": "日期待补",
                "url": "https://mp.weixin.qq.com/s/pending",
            },
        ]
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        preview = self.pool.preview_import(data, "window.json")
        self.pool.import_file(data, "window.json", preview["fingerprint"])

        records = self.pool.records_for_window("2026-08-15", "2026-08-21", scope="window-1")
        self.assertEqual([row["title"] for row in records], ["窗口内"])
        report = self.pool.query_recent_with_summary(days=7, scope="window-1", as_of="2026-08-21")
        self.assertEqual(report["summary"]["stored_article_rows"], 1)
        self.assertEqual(len(report["articles"]), 1)
        self.assertEqual(self.pool.get_stats(scope="window-1")["pending_date_items"], 1)

    def test_same_url_is_deduped_per_account_not_globally(self) -> None:
        shared = "https://mp.weixin.qq.com/s/shared-story"
        payload = [
            {"account_name": "账号甲", "fakeid": "a", "title": "甲", "url": shared, "publish_time": 1787184000},
            {"account_name": "账号乙", "fakeid": "b", "title": "乙", "url": shared, "publish_time": 1787184000},
        ]
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        preview = self.pool.preview_import(data, "multi-account.json")
        self.assertEqual(preview["accepted_records"], 2)
        self.assertEqual(preview["expanded_url_items"], 1)
        self.pool.import_file(data, "multi-account.json", preview["fingerprint"])
        self.assertEqual(self.pool.get_stats()["stored_article_rows"], 2)
        self.assertEqual(len(self.pool.list_articles(scope="a")), 1)
        self.assertEqual(len(self.pool.list_articles(scope="b")), 1)

    def test_database_mutations_roll_back_as_one_transaction(self) -> None:
        payload = [
            {"account_name": "事务号", "url": "https://mp.weixin.qq.com/s/tx-1"},
            {"account_name": "事务号", "url": "https://mp.weixin.qq.com/s/tx-2"},
        ]
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        fingerprint = self.pool.preview_import(data, "tx.json")["fingerprint"]
        original = self.pool._upsert_article
        calls = 0

        def fail_second(connection, account_id, record, now):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("forced failure")
            return original(connection, account_id, record, now)

        with patch.object(self.pool, "_upsert_article", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "forced failure"):
                self.pool.import_file(data, "tx.json", fingerprint)
        self.assertEqual(self.pool.get_stats()["stored_article_rows"], 0)
        self.assertIsNone(self.pool.latest_import())

    def test_canonicalizer_removes_tracking_but_keeps_article_identity(self) -> None:
        first = canonicalize_wechat_url(
            "http://mp.weixin.qq.com/s?scene=1&mid=7&idx=2&sn=S&__biz=B#fragment"
        )
        second = canonicalize_wechat_url(
            "https://mp.weixin.qq.com/s?__biz=B&amp;mid=7&amp;idx=2&amp;sn=S&amp;from=timeline"
        )
        self.assertEqual(first, second)
        self.assertEqual(first, "https://mp.weixin.qq.com/s?__biz=B&mid=7&idx=2&sn=S")

    def test_timestamp_query_is_not_corrupted_by_html_entity_decoding(self) -> None:
        url = "https://mp.weixin.qq.com/s?src=11&timestamp=1786928493&ver=6909&signature=ABC&new=1"
        canonical = canonicalize_wechat_url(url)
        self.assertIn("timestamp=1786928493", canonical)
        self.assertNotIn("%C3%97", canonical)

    def test_public_discovery_is_auditable_and_can_dedupe_against_imported_rows(self) -> None:
        url = "https://mp.weixin.qq.com/s/new-public-source"
        imported = self.pool.add_urls(
            [{"url": url, "title": "已有文章"}],
            account_name="已有公众号",
        )
        self.assertEqual(imported["added"], 1)

        discovered = self.pool.add_urls(
            [
                {
                    "url": url,
                    "title": "搜索发现标题",
                    "fetch_mode": "web_discovery",
                    "discovery_query": "site:mp.weixin.qq.com/s 半导体 完成融资",
                    "discovery_provider": "exa_mcporter",
                    "discovered_at": "2026-08-21T03:00:00Z",
                    "discovery_rank": 1,
                }
            ],
            account_name="搜索结果作者",
            dedupe_globally=True,
        )

        self.assertEqual(discovered["added"], 0)
        self.assertEqual(discovered["exists"], 1)
        self.assertEqual(self.pool.get_stats()["stored_article_rows"], 1)
        self.assertEqual(self.pool.get_stats()["discovered_total"], 1)
        self.assertEqual(self.pool.get_stats()["discovered_accounts"], 1)
        self.assertEqual(self.pool.get_stats()["last_discovery_at"], "2026-08-21T03:00:00+00:00")
        rows = self.pool.list_articles(scope="discovered")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["discovery_provider"], "exa_mcporter")
        self.assertIn("完成融资", rows[0]["discovery_query"])
        self.assertEqual(rows[0]["discovery_rank"], 1)


if __name__ == "__main__":
    unittest.main()
