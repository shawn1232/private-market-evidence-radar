from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_archive_migration import migrate_archive
from wechat_source_pool import WeChatSourcePool


class WeChatArchiveMigrationTests(unittest.TestCase):
    def test_saved_body_and_dated_history_link_are_migrated_without_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            bundle = root / "bundle"
            bundle.mkdir(parents=True)
            body = bundle / "article_text.txt"
            body.write_text("一家半导体公司完成天使轮融资。", encoding="utf-8")
            raw = bundle / "article_raw.html"
            raw.write_text("<script>var ct='1787068800';</script>", encoding="utf-8")
            (bundle / "meta.json").write_text(
                json.dumps(
                    {
                        "title": "测试半导体完成天使轮融资",
                        "author": "硬科技前沿",
                        "source_url": "https://mp.weixin.qq.com/s/test-body",
                        "text_path": str(body),
                        "raw_html_path": str(raw),
                        "cookie": "must-not-persist",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            history_dir = root / "history_extracted"
            history_dir.mkdir()
            (history_dir / "footer_all_links.json").write_text(
                json.dumps(
                    [
                        {
                            "title": "另一个项目完成客户认证 -2026-08-20",
                            "url": "https://mp.weixin.qq.com/s?__biz=B&mid=1&idx=1&sn=S&scene=21",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            pool = WeChatSourcePool(Path(directory) / "pool" / "pool.sqlite3")
            result = migrate_archive(root, pool)
            rows = pool.list_articles(limit=10)

            self.assertTrue(result["ok"])
            self.assertEqual(result["added"], 2)
            self.assertEqual(len(rows), 2)
            ready = next(row for row in rows if row["body_markdown_path"])
            copied = Path(ready["body_markdown_path"])
            self.assertTrue(copied.is_file())
            self.assertTrue(copied.is_relative_to(pool.db_path.parent))
            serialized = json.dumps(rows, ensure_ascii=False)
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("must-not-persist", serialized)


if __name__ == "__main__":
    unittest.main()
