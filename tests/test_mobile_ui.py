"""Offline UI regression checks; never use a real token or start registration workers."""

import importlib.util
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import flet as ft


with tempfile.TemporaryDirectory() as storage, patch.dict(os.environ, {"FLET_APP_STORAGE_DATA": storage}):
    spec = importlib.util.spec_from_file_location("campus_android", Path(__file__).parents[1] / "src" / "main.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)


def course(cid, **overrides):
    item = {
        "id": cid, "title": f"活动 {cid}：科学与人文讲座",
        "transcript_index": {"transcript_name": "科学素养"},
        "time_place": "2026-11-01 14:00 良乡校区文萃楼 A101",
        "sign_start_time": "2026-10-31 12:00", "sign_end_time": "2026-11-01 12:00",
        "sign_status_label": "未开始", "max": 100, "course_apply_count": 12, "score": "5",
    }
    item.update(overrides)
    return item


def walk(control):
    yield control
    for child in control._get_children():
        yield from walk(child)


class MobileUITests(unittest.TestCase):
    def setUp(self):
        self.threads = []

        def thread_factory(target, kwargs=None, **unused):
            self.threads.append((target, kwargs or {}))
            return SimpleNamespace(start=lambda: None)

        self.page = SimpleNamespace(
            platform=ft.PagePlatform.ANDROID, height=640, width=320,
            window=SimpleNamespace(), overlay=[], update=lambda: None,
            client_storage=SimpleNamespace(set=lambda *args: None),
        )
        self.page.add = lambda root: setattr(self.page, "root", root)
        self.page.open = lambda overlay: self.page.overlay.append(overlay)
        self.page.close = lambda overlay: self.page.overlay.remove(overlay)
        self.state = {"watched_courses": [], "watched_course_ids": [], "seen_course_ids": [], "grabbed_courses": []}
        for mock in (
            patch.object(app, "_TOKEN", "offline-test"),
            patch.object(app, "load_app_state", return_value=self.state),
            patch.object(app, "save_app_state"),
            patch.object(app, "threading", SimpleNamespace(Lock=threading.Lock, Thread=thread_factory)),
            patch.object(app.SniperEngine, "start"),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        app.main(self.page)
        self.nav = self.find(ft.NavigationBar)
        self.activity_list = next(c for c in walk(self.page.root) if isinstance(c, ft.ListView) and c.expand)
        self.refresh = next(target for target, _ in self.threads if target.__name__ == "do_refresh")
        self.categories = self.find(ft.Row, lambda c: c.height == 44 and c.scroll == ft.ScrollMode.HIDDEN)

    def find(self, kind, predicate=lambda c: True, root=None):
        return next(c for c in walk(root or self.page.root) if isinstance(c, kind) and predicate(c))

    def button(self, label, root=None):
        return self.find((ft.ElevatedButton, ft.OutlinedButton), lambda c: c.text == label, root)

    def load(self, items):
        with patch.object(app, "fetch_course_catalog", return_value=(items, len(items), None)):
            self.refresh()

    def select_category(self, name):
        button = next(c for c in self.categories.controls if c.data == name)
        button.on_click(SimpleNamespace(control=button))

    def select(self, index):
        self.nav.selected_index = index
        self.nav.on_change(SimpleNamespace(control=self.nav))

    def texts(self, root):
        return [c.value for c in walk(root) if isinstance(c, ft.Text)]

    def test_navigation_safe_area_and_serialization(self):
        self.load([course(1, title="超长活动标题" * 20)])
        self.assertIsInstance(self.page.root, ft.SafeArea)
        self.assertEqual([d.label for d in self.nav.destinations], ["活动", "监控", "我的"])
        for index in (0, 1, 2, 0):
            self.select(index)
            views = self.page.root.content.controls[1].content.controls
            self.assertEqual([v.visible is not False for v in views], [i == index for i in range(3)])
            self.assertTrue(self.page.root._build_add_commands(index={}, added_controls=[]))

    def test_watch_can_be_cancelled_from_other_page(self):
        self.load([course(i) for i in range(1, 22)])
        self.button("加入监控", self.activity_list).on_click(None)
        self.assertEqual(self.state["watched_course_ids"], [1])
        self.assertIsInstance(self.page.overlay[-1], ft.SnackBar)
        self.button("下一页").on_click(None)
        self.select(1)
        monitor_view = self.page.root.content.controls[1].content.controls[1]
        self.assertIn(course(1)["title"], self.texts(monitor_view))
        self.button("取消监控", monitor_view).on_click(None)
        self.assertEqual(self.state["watched_course_ids"], [])
        self.assertIn("暂无监控活动", self.texts(monitor_view))
        self.assertIn(course(21)["title"], self.texts(self.activity_list))

    def test_details_keep_full_title_location_and_registration_window(self):
        item = course(1, title="长标题" * 30, time_place="长地点" * 40)
        self.load([item])
        self.find(ft.IconButton, lambda c: c.tooltip == "查看活动详情", self.activity_list).on_click(None)
        sheet = self.page.overlay[-1]
        self.assertIsInstance(sheet, ft.BottomSheet)
        values = self.texts(sheet)
        self.assertIn(item["title"], values)
        for label, value in (("时间地点", item["time_place"]), ("报名截止", item["sign_end_time"])):
            self.assertEqual(values[values.index(label) + 1], value)
        self.assertLessEqual(sheet.size_constraints.max_height, self.page.height)
        self.assertTrue(sheet._build_add_commands(index={}, added_controls=[]))
        self.button("关闭详情", sheet).on_click(None)
        self.assertNotIn(sheet, self.page.overlay)

    def test_full_activity_does_not_allow_new_watch(self):
        self.load([course(1, course_apply_count=100)])
        self.assertTrue(self.button("名额已满", self.activity_list).disabled)
        self.assertEqual(self.state["watched_course_ids"], [])

    def test_last_page_and_failed_refresh_preserve_navigation_and_data(self):
        self.load([course(i) for i in range(1, 22)])
        self.assertFalse(self.button("下一页").disabled)
        self.button("下一页").on_click(None)
        self.assertFalse(self.button("上一页").disabled)
        self.assertTrue(self.button("下一页").disabled)
        with patch.object(app, "fetch_course_catalog", return_value=([], 0, "离线测试")):
            self.refresh()
        self.assertIn(course(21)["title"], self.texts(self.activity_list))
        self.assertTrue(self.button("下一页").disabled)
        self.assertFalse(self.button("上一页").disabled)
        self.assertFalse(self.button("刷新列表").disabled)

    def test_background_refresh_preserves_current_page_controls(self):
        items = [course(i) for i in range(1, 22)]
        self.load(items)
        self.button("下一页").on_click(None)
        with patch.object(app, "fetch_course_catalog", return_value=(items, len(items), None)):
            self.refresh(quiet=True)
        self.assertTrue(self.button("下一页").disabled)
        self.assertIn(course(21)["title"], self.texts(self.activity_list))

    def test_new_activity_detection_includes_later_pages(self):
        baseline = [course(i) for i in range(1, 45)]
        with patch.object(app, "show_new_course_popup") as popup:
            with patch.object(app, "fetch_course_catalog", return_value=(baseline, len(baseline), None)):
                self.refresh(detect_new=True)
            popup.assert_not_called()
            updated = baseline + [course(45)]
            with patch.object(app, "fetch_course_catalog", return_value=(updated, len(updated), None)):
                self.refresh(detect_new=True)
                self.refresh(detect_new=True)
            self.assertEqual([item["id"] for item in popup.call_args.args[1]], [45])
            popup.assert_called_once()

    def test_manual_refresh_shows_new_course_even_when_later_page_fails(self):
        baseline = [course(i) for i in range(1, 22)]
        new_course = course(22, transcript_index={"transcript_name": "理想信念"},
                            sign_start_time=None, sign_end_time=None)
        with patch.object(app, "fetch_course_catalog", return_value=(baseline, len(baseline), None)):
            self.refresh(detect_new=True)
        self.select_category("科学素养")
        self.button("下一页").on_click(None)
        self.button("刷新列表").on_click(None)
        target, kwargs = self.threads[-1]
        pages = {
            1: {"data": {"items": [new_course, course(1, sign_status_label="已结束")], "last_page": 2}},
            2: {"_error": "第 2 页请求超时"},
        }
        with patch.object(app, "fetch_courses", side_effect=lambda page, limit: pages[page]), patch.object(
            app, "show_new_course_popup"
        ):
            target(**kwargs)
            target(**kwargs)
        self.assertTrue(any(str(value).startswith("部分更新") for value in self.texts(self.page.root)))
        self.assertIn(course(21)["title"], self.texts(self.activity_list))
        self.select_category(app.NEW_COURSES_FILTER)
        self.assertIn(new_course["title"], self.texts(self.activity_list))
        self.assertIn("报名时间待定", self.texts(self.activity_list))
        self.select_category(app.ALL_CATEGORIES)
        self.assertNotIn(course(1)["title"], self.texts(self.activity_list))

    def test_new_course_detected_after_initial_empty_catalog(self):
        with patch.object(app, "show_new_course_popup"):
            with patch.object(app, "fetch_course_catalog", return_value=([], 0, None)):
                self.refresh(detect_new=True)
            with patch.object(app, "fetch_course_catalog", return_value=([course(1)], 1, None)):
                self.refresh(detect_new=True)
        self.select_category(app.NEW_COURSES_FILTER)
        self.assertIn(course(1)["title"], self.texts(self.activity_list))

    def test_unchanged_refresh_reuses_visible_cards(self):
        items = [course(1)]
        self.load(items)
        card = self.activity_list.controls[0]
        self.load(items)
        self.assertIs(self.activity_list.controls[0], card)

    def test_background_success_updates_monitor_and_capture_views(self):
        self.load([course(1)])
        self.button("加入监控", self.activity_list).on_click(None)
        with tempfile.TemporaryDirectory() as storage:
            results = Path(storage) / "mobile_results.jsonl"
            results.write_text(app.json.dumps({"id": 1, "title": course(1)["title"], "score": "5"}) + "\n", encoding="utf-8")
            with patch.object(app, "MOBILE_RESULTS_FILE", str(results)):
                self.select(1)
        monitor_view = self.page.root.content.controls[1].content.controls[1]
        self.assertIn("暂无监控活动", self.texts(monitor_view))
        self.assertEqual(self.state["watched_course_ids"], [])
        self.select(2)
        self.assertIn(course(1)["title"], self.texts(self.page.root.content.controls[1].content.controls[2]))

    def test_open_registration_updates_activity_view_without_refresh_or_navigation(self):
        item = course(1, sign_start_time="", sign_end_time="", sign_status_label="报名中")
        self.load([item])
        self.button("加入监控", self.activity_list).on_click(None)
        self.assertIn(f"正在尝试报名：{item['title']}", self.texts(self.page.overlay[-1]))
        sync_loop = next(target for target, _ in self.threads if target.__name__ == "mobile_result_loop")
        with tempfile.TemporaryDirectory() as storage:
            results = Path(storage) / "mobile_results.jsonl"
            results.write_text(app.json.dumps({"id": 1, "title": item["title"]}) + "\n", encoding="utf-8")
            with patch.object(app, "MOBILE_RESULTS_FILE", str(results)), patch.object(
                app.time, "sleep", side_effect=[None, InterruptedError],
            ), self.assertRaises(InterruptedError):
                sync_loop()
        self.assertEqual(self.state["watched_course_ids"], [])
        self.assertTrue(self.button("已报名", self.activity_list).disabled)
        self.assertIn(f"报名成功：{item['title']}", self.texts(self.page.overlay[-1]))

    def test_terminal_result_displays_server_message_and_is_not_a_success(self):
        item = course(1)
        self.load([item])
        self.button("加入监控", self.activity_list).on_click(None)
        with tempfile.TemporaryDirectory() as storage:
            results = Path(storage) / "mobile_results.jsonl"
            event = {"id": 1, "title": item["title"], "outcome": "terminal", "message": "名额已满"}
            results.write_text(app.json.dumps(event) + "\n", encoding="utf-8")
            with patch.object(app, "MOBILE_RESULTS_FILE", str(results)):
                self.select(1)
                self.assertEqual(self.state["watched_course_ids"], [])
                self.assertEqual(self.state["grabbed_courses"], [])
                self.assertIn(f"{item['title']}：名额已满", self.texts(self.page.overlay[-1]))
                self.button("加入监控", self.activity_list).on_click(None)
                app.main(self.page)
                self.assertEqual(self.state["watched_course_ids"], [1])

    def test_first_launch_token_dialog_is_scrollable_and_readable(self):
        with patch.object(app, "_TOKEN", ""):
            app.main(self.page)
        dialog = self.page.overlay[-1]
        self.assertTrue(dialog.scrollable)
        token = self.find(ft.TextField, root=dialog)
        self.assertTrue(token.password)
        self.assertEqual(token.color, "#F5F5F5")
        self.assertEqual(dialog.bgcolor, "#212121")
        self.assertTrue(dialog._build_add_commands(index={}, added_controls=[]))

    def test_rotation_uses_two_columns_and_keeps_navigation_visible(self):
        self.load([course(1), course(2), course(3)])
        self.page.width, self.page.height = 844, 390
        self.page.on_resized(None)
        self.assertEqual(len(self.activity_list.controls), 2)
        self.assertTrue(all(isinstance(c, ft.Row) for c in self.activity_list.controls))
        self.assertEqual(self.nav.height, 56)
        self.assertEqual(len(self.texts(self.activity_list)), 18)
        self.page.width, self.page.height = 320, 640
        self.page.on_resized(None)
        self.assertEqual(len(self.activity_list.controls), 3)
        self.assertTrue(all(isinstance(c, ft.Container) for c in self.activity_list.controls))
        self.assertEqual(self.nav.height, 64)
        self.assertTrue(self.page.root._build_add_commands(index={}, added_controls=[]))

    def test_categories_count_full_catalog_before_pagination(self):
        items = [course(i, transcript_index={"transcript_name": "科学素养" if i <= 21 else "理想信念"})
                 for i in range(1, 45)] + [course(45, transcript_index=None)]
        self.load(items)
        labels = {c.data: c.text for c in self.categories.controls}
        self.assertEqual(labels[app.ALL_CATEGORIES], "全部类别 (45)")
        self.assertEqual(labels["理想信念"], "理想信念 (23)")
        self.assertEqual(len(self.activity_list.controls), 20)
        activity_view = self.page.root.content.controls[1].content.controls[0]
        self.assertTrue(activity_view.controls[-1].visible)
        self.select_category("理想信念")
        self.assertIn(course(22)["title"], self.texts(self.activity_list))
        self.assertNotIn(course(1)["title"], self.texts(self.activity_list))
        with patch.object(app, "fetch_course_catalog", side_effect=AssertionError("Local paging must not fetch")):
            self.button("下一页").on_click(None)
        self.assertEqual(len(self.activity_list.controls), 3)
        self.assertIn(course(42)["title"], self.texts(self.activity_list))
        self.select_category("未分类")
        self.assertTrue(self.button("上一页").disabled)
        self.assertEqual(len(self.activity_list.controls), 1)
        self.assertFalse(activity_view.controls[-1].visible)
        self.select_category(app.ALL_CATEGORIES)
        self.assertIn(course(1)["title"], self.texts(self.activity_list))

    def test_filter_and_watch_survive_refresh_and_empty_category(self):
        self.load([course(1)])
        self.select_category("科学素养")
        self.button("加入监控", self.activity_list).on_click(None)
        self.select_category("理想信念")
        self.assertIn("该类别暂无活动", self.texts(self.activity_list))
        self.load([course(1)])
        selected = next(c.data for c in self.categories.controls if c.style.bgcolor == "#3D3D3D")
        self.assertEqual(selected, "理想信念")
        self.select_category("科学素养")
        self.assertEqual(self.button("取消监控", self.activity_list).text, "取消监控")

    def test_refresh_clamps_filtered_page_after_catalog_shrinks(self):
        self.load([course(i) for i in range(1, 23)])
        self.select_category("科学素养")
        self.button("下一页").on_click(None)
        self.load([course(1)])
        self.assertTrue(self.button("上一页").disabled)
        self.assertTrue(self.button("下一页").disabled)
        self.assertIn(course(1)["title"], self.texts(self.activity_list))

    def test_compact_card_keeps_three_rows_and_large_touch_targets(self):
        self.load([course(1)])
        card = self.activity_list.controls[0]
        self.assertEqual(len(card.content.controls), 3)
        self.assertTrue(all(isinstance(c, ft.Row) for c in card.content.controls))
        self.assertEqual(self.button("加入监控", card).height, 44)
        self.assertEqual(self.find(ft.IconButton, root=card).height, 44)


class CatalogTests(unittest.TestCase):
    def test_categories_match_desktop_and_handle_missing_names(self):
        self.assertEqual(app.get_course_category(course(1, transcript_index={"transcript_name": " 科学素养 "})), "科学素养")
        for transcript in (None, {}, {"transcript_name": "  "}):
            self.assertEqual(app.get_course_category(course(1, transcript_index=transcript)), "未分类")

    def test_reads_every_page_and_deduplicates_before_filtering(self):
        pages = [
            {"data": {"items": [course(1), course(2, sign_status_label="已结束")]}},
            {"data": [course(1), course(3)]},
            {"data": {"items": [course(4)]}},
            {"data": []},
        ]
        with patch.object(app, "fetch_courses", side_effect=pages) as fetch:
            items, total, error = app.fetch_course_catalog()
        self.assertIsNone(error)
        self.assertEqual(total, 4)
        self.assertEqual([c["id"] for c in items], [1, 2, 3, 4])
        self.assertEqual([c.kwargs["page"] for c in fetch.call_args_list], [1, 2, 3, 4])

    def test_bad_first_page_does_not_replace_catalog(self):
        for pages in (
            [{"code": 401, "message": "登录已过期"}],
            [{"data": {"items": "invalid"}}],
        ):
            with self.subTest(pages=pages), patch.object(app, "fetch_courses", side_effect=pages):
                items, total, error = app.fetch_course_catalog()
                self.assertEqual(items, [])
                self.assertEqual(total, 0)
                self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
