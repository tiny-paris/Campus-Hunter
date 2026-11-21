"""Offline coverage for pagination completeness, connection reuse and timing."""

from datetime import datetime, timedelta, timezone
import threading
import unittest
from unittest.mock import Mock, patch

from test_mobile_ui import app, course


class CatalogPerformanceTests(unittest.TestCase):
    def test_root_metadata_and_empty_catalog(self):
        for response, expected in (
            ({"data": {"items": [course(1)]}, "total": 1, "last_page": 1}, [1]),
            ({"data": {"items": [], "total": 0, "last_page": 0}}, []),
        ):
            with self.subTest(response=response), patch.object(app, "fetch_courses", return_value=response) as fetch:
                items, _, error = app.fetch_course_catalog()
                self.assertIsNone(error)
                self.assertEqual([c["id"] for c in items], expected)
                fetch.assert_called_once()

    def test_metadata_uses_actual_page_size_and_avoids_empty_probe(self):
        pages = {
            1: {"code": "200", "data": {"items": [course(1), course(2)], "total": "3", "per_page": "2"}},
            2: {"data": {"items": [course(3)]}},
        }
        with patch.object(app, "fetch_courses", side_effect=lambda page, limit: pages[page]) as fetch:
            items, total, error = app.fetch_course_catalog()
        self.assertIsNone(error)
        self.assertEqual([c["id"] for c in items], [1, 2, 3])
        self.assertEqual(total, 3)
        self.assertEqual(fetch.call_count, 2)

    def test_known_pages_run_concurrently_and_keep_api_order(self):
        barrier = threading.Barrier(3, timeout=3)

        def fetch(page, limit):
            if page == 1:
                return {"data": {"items": [course(1)], "total": 4, "last_page": 4}}
            barrier.wait()
            return {"data": [course(page)]}

        with patch.object(app, "fetch_courses", side_effect=fetch):
            items, total, error = app.fetch_course_catalog()
        self.assertIsNone(error)
        self.assertEqual(total, 4)
        self.assertEqual([c["id"] for c in items], [1, 2, 3, 4])

    def test_overlapping_pages_keep_latest_status_and_normalize_ids(self):
        pages = [
            {"data": [course(1), course("2")]},
            {"data": [course(2, sign_status_label="已结束"), course(3)]},
            {"data": []},
        ]
        with patch.object(app, "fetch_courses", side_effect=pages):
            items, total, error = app.fetch_course_catalog()
        self.assertIsNone(error)
        self.assertEqual(total, 3)
        self.assertEqual([c["id"] for c in items], [1, 2, 3])
        self.assertEqual(items[1]["sign_status_label"], "已结束")

    def test_missing_or_malformed_data_never_becomes_empty_success(self):
        for response in ({}, {"data": {}}, {"data": None}, [], {"data": [None]},
                         {"data": [{"id": None}]}, {"data": [course(True)]},
                         {"data": [course(-1)]}, {"data": [course("1.5")]}):
            with self.subTest(response=response), patch.object(app, "fetch_courses", return_value=response):
                items, total, error = app.fetch_course_catalog()
                self.assertEqual((items, total), ([], 0))
                self.assertTrue(error)

    def test_incomplete_catalog_keeps_confirmed_rows(self):
        for second, expected in (([], [1]), ([course(1)], [1]), ([course(2)], [1, 2])):
            pages = [
                {"data": {"items": [course(1)], "total": 3, "last_page": 2}},
                {"data": second},
            ]
            with self.subTest(second=second), patch.object(app, "fetch_courses", side_effect=pages):
                items, total, error = app.fetch_course_catalog()
                self.assertEqual([c["id"] for c in items], expected)
                self.assertEqual(total, len(expected))
                self.assertTrue(error)

    def test_known_pages_keep_valid_rows_after_another_page_fails(self):
        pages = {
            1: {"data": {"items": [course(1)], "last_page": 3}},
            2: {"_error": "请求超时"},
            3: {"data": [course(3), {"title": "活动编号缺失"}]},
        }
        with patch.object(app, "fetch_courses", side_effect=lambda page, limit: pages[page]):
            items, total, error = app.fetch_course_catalog()
        self.assertEqual([c["id"] for c in items], [1, 3])
        self.assertEqual(total, 2)
        self.assertEqual(error, "请求超时")

    def test_unknown_pagination_has_finite_limit(self):
        with patch.object(app, "MAX_CATALOG_PAGES", 3), patch.object(
            app, "fetch_courses", side_effect=lambda page, limit: {"data": [course(page)]}
        ) as fetch:
            self.assertTrue(app.fetch_course_catalog()[2])
        self.assertEqual(fetch.call_count, 3)


class TransportTests(unittest.TestCase):
    def test_catalog_requests_cache_revalidation_without_changing_other_requests(self):
        session = Mock()
        session.get.return_value.json.return_value = {"data": []}
        with patch.object(app, "http_session", return_value=session):
            app.fetch_courses(page=2)
            app.fetch_user_info()
        catalog, account = session.get.call_args_list
        self.assertEqual(catalog.kwargs["headers"]["Cache-Control"], "no-cache")
        self.assertNotIn("Cache-Control", account.kwargs["headers"])

    def test_session_reused_and_registration_has_shorter_timeout(self):
        session = Mock()
        session.get.return_value.json.return_value = {"data": []}
        session.post.return_value.json.return_value = {"code": 200}
        with patch.object(app, "_http_local", threading.local()), patch.object(
            app.requests, "Session", return_value=session
        ) as factory:
            app.api_get("/offline")
            app.api_get("/offline")
            app.apply_course(42)
        factory.assert_called_once_with()
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.post.call_args.kwargs["timeout"], (3, 5))
        self.assertEqual(session.post.call_args.kwargs["json"]["course_id"], 42)

    def test_session_is_not_shared_between_workers(self):
        sessions = []
        with patch.object(app, "_http_local", threading.local()):
            first = app.http_session()
            worker = threading.Thread(target=lambda: sessions.append(app.http_session()))
            worker.start()
            worker.join(timeout=3)
            self.assertIs(first, app.http_session())
            self.assertIsNot(first, sessions[0])
            first.close()
            sessions[0].close()

    def test_non_object_json_is_reported_as_error(self):
        session = Mock()
        session.get.return_value.json.return_value = []
        session.post.return_value.json.return_value = None
        with patch.object(app, "http_session", return_value=session):
            self.assertIn("_error", app.api_get("/offline"))
            self.assertIn("_error", app.api_post("/offline", {}))


class TimeTests(unittest.TestCase):
    def test_china_local_iso_offset_and_utc_are_same_instant(self):
        expected = datetime(2026, 11, 12, 12, tzinfo=timezone(timedelta(hours=8)))
        expected = expected.astimezone().replace(tzinfo=None)
        for value in ("2026-11-12 12:00", "2026-11-12 12:00:00", "2026-11-12T12:00:00+08:00",
                      "2026-11-12T04:00:00Z"):
            with self.subTest(value=value):
                self.assertEqual(app.parse_api_time(value), expected)
        for value in (None, "", "invalid", 123):
            self.assertIsNone(app.parse_api_time(value))


if __name__ == "__main__":
    unittest.main()
