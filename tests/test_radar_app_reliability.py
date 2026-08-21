from __future__ import annotations

import copy
import unittest
from datetime import date
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from app import radar_app


class RadarAppReliabilityTests(unittest.TestCase):
    TODAY = date(2026, 8, 21)

    def setUp(self) -> None:
        self.runtime_snapshot = copy.deepcopy(radar_app._runtime_state)
        radar_app._runtime_state.clear()
        radar_app._runtime_state.update(
            refreshing=False,
            last_error="",
            last_started_at="",
            last_finished_at="",
            last_attempt={},
        )

    def tearDown(self) -> None:
        radar_app._runtime_state.clear()
        radar_app._runtime_state.update(self.runtime_snapshot)

    @staticmethod
    def report(
        status: str = "ok",
        *,
        window_end: str = "2026-08-21",
        generated_at: str | None = "2026-08-21T08:00:00+08:00",
        candidates: list[dict] | None = None,
    ) -> dict:
        return {
            "window": {
                "start_date": "2026-08-15",
                "end_date": window_end,
                "timezone": "Asia/Shanghai",
            },
            "status": status,
            "source_status": {},
            "candidates": candidates if candidates is not None else [],
            "empty_slots": 5,
            "generated_at": generated_at,
        }

    def test_old_nonempty_cache_is_stale_and_warmed(self) -> None:
        cached = self.report(
            window_end="2026-07-14",
            generated_at="2026-07-14T16:57:24+08:00",
            candidates=[{"company": "旧项目"}],
        )
        normalized = radar_app._normalize_report(cached, today=self.TODAY)

        self.assertTrue(normalized["is_stale"])
        self.assertTrue(normalized["needs_refresh"])
        self.assertEqual(normalized["cache_age_days"], 38)
        self.assertEqual(normalized["data_as_of"], "2026-07-14")
        self.assertIn("缓存已过期 38 天", normalized["freshness_label"])

        with (
            patch.object(radar_app, "load_cached_report", return_value=cached),
            patch.object(radar_app, "_today_local", return_value=self.TODAY),
        ):
            self.assertTrue(radar_app._should_warm_cache())

    def test_current_empty_report_is_a_valid_cache_and_is_not_rewarmed(self) -> None:
        cached = self.report(status="empty", candidates=[])
        with (
            patch.object(radar_app, "load_cached_report", return_value=cached),
            patch.object(radar_app, "_today_local", return_value=self.TODAY),
        ):
            normalized = radar_app._normalize_report(cached)
            self.assertFalse(normalized["is_stale"])
            self.assertFalse(normalized["needs_refresh"])
            self.assertFalse(radar_app._should_warm_cache())

    def test_failed_refresh_states_never_return_ok(self) -> None:
        for state in ("stale_cache", "refresh_failed", "cache_invalid", "failed", "error"):
            with self.subTest(state=state):
                failed = self.report(status=state)
                with (
                    patch.object(radar_app, "refresh_report", return_value=failed),
                    patch.object(radar_app, "_today_local", return_value=self.TODAY),
                ):
                    result = radar_app._refresh_now()

                self.assertFalse(result["ok"])
                self.assertEqual(result["run_state"], state)
                self.assertFalse(radar_app._runtime_state["last_attempt"]["ok"])
                self.assertTrue(radar_app._runtime_state["last_error"])

    def test_stale_refresh_attempt_keeps_source_error_in_health(self) -> None:
        failed = self.report(status="stale_cache", window_end="2026-08-20")
        failed["refresh_attempt"] = {
            "source_status": {
                "google_news": {
                    "status": "error",
                    "errors": [{"error": "network offline"}],
                }
            }
        }
        with (
            patch.object(radar_app, "refresh_report", return_value=failed),
            patch.object(radar_app, "_today_local", return_value=self.TODAY),
        ):
            result = radar_app._refresh_now()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_detail"], "network offline")
        health = radar_app.app.test_client().get("/health").get_json()
        self.assertFalse(health["last_attempt"]["ok"])
        self.assertEqual(health["last_attempt"]["error_detail"], "network offline")
        self.assertIn("network offline", health["last_error"])

    def test_refresh_api_uses_distinct_http_statuses(self) -> None:
        client = radar_app.app.test_client()
        cases = (
            ({"ok": True, "busy": False, "message": "done"}, 200),
            ({"ok": False, "busy": True, "message": "busy"}, 409),
            ({"ok": False, "busy": False, "message": "failed"}, 502),
        )
        for result, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                with patch.object(radar_app, "_refresh_now", return_value=result):
                    response = client.post("/api/refresh", json={})
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.get_json()["ok"], result["ok"])

    def test_home_prominently_marks_stale_cache(self) -> None:
        cached = self.report(
            window_end="2026-07-14",
            generated_at="2026-07-14T16:57:24+08:00",
            candidates=[{"company": "回看项目"}],
        )
        with (
            patch.object(radar_app, "load_cached_report", return_value=cached),
            patch.object(radar_app, "_today_local", return_value=self.TODAY),
        ):
            response = radar_app.app.test_client().get("/")

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("缓存已过期 38 天", page)
        self.assertIn("当前卡片仅供回看，不代表最近 7 天", page)
        self.assertIn("证据评估工作台", page)
        self.assertIn("带入证据评估", page)

    def test_candidate_workbench_url_round_trips_company_name(self) -> None:
        company = "甲芯&科技 / A"
        candidate = radar_app._normalize_candidate({"company": company}, 1)
        parsed = urlsplit(candidate["workbench_url"])
        query = parse_qs(parsed.query)

        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", radar_app.DEEP_WORKBENCH_HOME_URL)
        self.assertEqual(query["q"], [company])
        self.assertEqual(query["company"], [company])


if __name__ == "__main__":
    unittest.main()
