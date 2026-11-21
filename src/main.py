"""
CampusHunter — 第二课堂自动抢课助手
A Flet-based desktop application to monitor and auto-register for university activities.
"""

import flet as ft
import requests
import threading
import time
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

if os.name == "nt":
    import ctypes
    import winreg
    import winsound

# ──────────────────────────── Constants ────────────────────────────

BASE_URL = "https://qcbldekt.bit.edu.cn"
APP_NAME = "CampusHunter"
AUTOSTART_FLAG = "--autostart"
AUTO_REFRESH_INTERVAL_SECONDS = 300
MAX_SEEN_COURSE_IDS = 2000
MAX_GRABBED_COURSES = 20
MAX_WATCHED_COURSES = 100
ALL_CATEGORIES = "全部类别"
NEW_COURSES_FILTER = "近期上新"
COURSE_PAGE_SIZE = 20
CATALOG_PAGE_LIMIT = 100
MAX_CATALOG_PAGES = 200
_http_local = threading.local()
_catalog_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="catalog")
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_REG_NAME = "CampusHunter"

# ── Token loading from external file ──


def _get_app_dir() -> str:
    """Return the directory where the executable (or script) lives."""
    if os.getenv("FLET_APP_STORAGE_DATA"):
        return os.environ["FLET_APP_STORAGE_DATA"]
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_STATE_FILE = os.path.join(_get_app_dir(), "app_state.json")
MOBILE_RESULTS_FILE = os.path.join(_get_app_dir(), "mobile_results.jsonl")
TOKEN_PLACEHOLDER = "请在此粘贴你的Bearer Token"


def _get_token_file() -> str:
    app_dir = _get_app_dir()
    if os.getenv("FLET_APP_STORAGE_DATA"):
        return os.path.join(app_dir, "token.txt")
    candidates = [
        os.path.join(app_dir, "token.txt"),
        os.path.join(os.path.dirname(app_dir), "token.txt"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


TOKEN_FILE = _get_token_file()


def load_token() -> str:
    """Read the Bearer token from token.txt. Create a placeholder file if missing."""
    if not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(TOKEN_PLACEHOLDER)
        return ""
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        token = f.read().strip()
    if not token or token == TOKEN_PLACEHOLDER:
        return ""
    return token


_TOKEN = load_token()


def store_token(value: str) -> bool:
    global _TOKEN
    token = value.strip()
    if not token:
        return False
    if not token.lower().startswith("bearer "):
        token = f"Bearer {token}"
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(token)
    _TOKEN = token
    HEADERS["Authorization"] = token
    return True

HEADERS = {
    "Host": "qcbldekt.bit.edu.cn",
    "Connection": "keep-alive",
    "Authorization": _TOKEN if _TOKEN else "",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 "
        "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
        "MiniProgramEnv/Windows WindowsWechat/WMPF "
        "WindowsWechat(0x63090b13) XWEB/11275"
    ),
    "Content-Type": "application/json",
    "Referer": "https://servicewechat.com/wx2149021480f24a19/270/page-frame.html",
}

DEFAULT_TEMPLATE_ID = "2GNFjVv2S7xYnoWeIxGsJGP1Fu2zSs28R6mZI7Fc2kU"

def load_app_state() -> dict:
    default_state = {
        "baseline_initialized": False,
        "seen_course_ids": [],
        "grabbed_courses": [],
        "watched_courses": [],
        "watched_course_ids": [],
        "mobile_result_offset": 0,
    }
    if not os.path.exists(APP_STATE_FILE):
        return default_state
    try:
        with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_state
        seen_ids = data.get("seen_course_ids", [])
        if not isinstance(seen_ids, list):
            seen_ids = []
        grabbed_courses = data.get("grabbed_courses", [])
        if not isinstance(grabbed_courses, list):
            grabbed_courses = []
        watched_courses = data.get("watched_courses", [])
        if not isinstance(watched_courses, list):
            watched_courses = []
        watched_course_ids = data.get("watched_course_ids", [])
        if not isinstance(watched_course_ids, list):
            watched_course_ids = []
        offset = data.get("mobile_result_offset", 0)
        return {
            "baseline_initialized": bool(data.get("baseline_initialized")),
            "seen_course_ids": seen_ids[-MAX_SEEN_COURSE_IDS:],
            "grabbed_courses": grabbed_courses[-MAX_GRABBED_COURSES:],
            "watched_courses": watched_courses[-MAX_WATCHED_COURSES:],
            "watched_course_ids": watched_course_ids,
            "mobile_result_offset": max(0, offset) if isinstance(offset, int) else 0,
        }
    except (OSError, json.JSONDecodeError):
        return default_state


def save_app_state(state: dict) -> bool:
    payload = {
        "baseline_initialized": bool(state.get("baseline_initialized")),
        "seen_course_ids": list(state.get("seen_course_ids", []))[-MAX_SEEN_COURSE_IDS:],
        "grabbed_courses": list(state.get("grabbed_courses", []))[-MAX_GRABBED_COURSES:],
        "watched_courses": list(state.get("watched_courses", []))[-MAX_WATCHED_COURSES:],
        "watched_course_ids": list(state.get("watched_course_ids", [])),
        "mobile_result_offset": state.get("mobile_result_offset", 0),
    }
    try:
        pending_file = APP_STATE_FILE + ".tmp"
        with open(pending_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(pending_file, APP_STATE_FILE)
    except OSError:
        return False
    return True


def build_autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {AUTOSTART_FLAG}'

    python_executable = sys.executable
    exe_dir, exe_name = os.path.split(python_executable)
    if exe_name.lower() == "python.exe":
        pythonw = os.path.join(exe_dir, "pythonw.exe")
        if os.path.exists(pythonw):
            python_executable = pythonw

    return f'"{python_executable}" "{os.path.abspath(__file__)}" {AUTOSTART_FLAG}'


def ensure_windows_autostart() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "当前系统不是 Windows，未启用开机自启动。"

    command = build_autostart_command()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_REG_PATH,
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            existing_value = None
            try:
                existing_value, _ = winreg.QueryValueEx(key, RUN_REG_NAME)
            except FileNotFoundError:
                existing_value = None

            if existing_value != command:
                winreg.SetValueEx(key, RUN_REG_NAME, 0, winreg.REG_SZ, command)
        return True, "已启用开机自启动。"
    except OSError as exc:
        return False, f"开机自启动配置失败: {exc}"


def restore_window(page: ft.Page) -> None:
    try:
        page.window.visible = True
        page.window.minimized = False
        page.window.to_front()
        page.update()
    except Exception:
        pass


def show_new_course_popup(page: ft.Page, courses: list[dict]) -> None:
    if not courses:
        return

    title_lines = []
    for course in courses[:5]:
        title_lines.append(str(course.get("title", "未知活动")))
    if len(courses) > 5:
        title_lines.append(f"...以及另外 {len(courses) - 5} 个活动")

    message = "检测到新活动：\n\n" + "\n".join(f"- {line}" for line in title_lines)
    restore_window(page)

    if os.name == "nt":
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                message,
                "CampusHunter 新活动提醒",
                0x40 | 0x1000 | 0x10000,
            )
            return
        except Exception:
            pass

    print(message)


# ──────────────────────────── API Layer ────────────────────────────


def http_session() -> requests.Session:
    """Reuse connections within a worker without sharing mutable sessions."""
    if not hasattr(_http_local, "session"):
        _http_local.session = requests.Session()
    return _http_local.session


def api_get(endpoint: str, params: dict | None = None, debug_label: str = "", *, revalidate: bool = False) -> dict | None:
    try:
        resp = http_session().get(
            f"{BASE_URL}{endpoint}",
            headers={**HEADERS, "Cache-Control": "no-cache"} if revalidate else HEADERS,
            params=params,
            timeout=(3, 10),
        )
        resp.raise_for_status()
        result = resp.json()
        return result if isinstance(result, dict) else {"_error": "返回数据格式异常"}
    except requests.RequestException as exc:
        return {"_error": f"网络请求失败: {exc}"}
    except json.JSONDecodeError:
        return {"_error": "返回数据解析失败（非JSON）"}


def api_post(endpoint: str, payload: dict) -> dict | None:
    try:
        resp = http_session().post(
            f"{BASE_URL}{endpoint}",
            headers=HEADERS,
            json=payload,
            timeout=(3, 5),
        )
        resp.raise_for_status()
        result = resp.json()
        return result if isinstance(result, dict) else {"_error": "返回数据格式异常"}
    except requests.RequestException as exc:
        return {"_error": f"网络请求失败: {exc}"}
    except json.JSONDecodeError:
        return {"_error": "返回数据解析失败（非JSON）"}


def fetch_user_info() -> dict:
    return api_get("/api/user/info") or {}


def fetch_scores() -> dict:
    return api_get("/api/transcript/score", debug_label="SCORE") or {}


def fetch_courses(page: int = 1, limit: int = 20) -> dict:
    return api_get("/api/course/list", {"page": page, "limit": limit}, revalidate=True) or {}


def get_course_category(course: dict) -> str:
    transcript = course.get("transcript_index")
    name = transcript.get("transcript_name") if isinstance(transcript, dict) else None
    return str(name).strip() if name and str(name).strip() else "未分类"


def filter_courses(courses: list[dict], category: str = "") -> list[dict]:
    return [course for course in courses if not category or category == ALL_CATEGORIES or get_course_category(course) == category]


def fetch_course_catalog() -> tuple[list[dict], int, str | None]:
    """Return validated rows from available pages, with an error if the snapshot is incomplete."""
    courses = {}

    def read_page(pg):
        result = fetch_courses(page=pg, limit=CATALOG_PAGE_LIMIT)
        if not isinstance(result, dict):
            raise ValueError("活动列表格式异常")
        if "_error" in result:
            raise ValueError(result["_error"])
        if result.get("code") not in (None, 0, 200, "0", "200"):
            raise ValueError(result.get("message") or result.get("msg") or "活动列表加载失败")
        data = result.get("data")
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("活动列表格式异常")
        normalized = []
        for course in items:
            if not isinstance(course, dict):
                continue
            try:
                cid = int(str(course.get("id")))
            except (TypeError, ValueError):
                continue
            if cid > 0:
                normalized.append(dict(course, id=cid))
        if items and not normalized:
            raise ValueError(f"第 {pg} 页活动编号格式异常")
        return result, data, normalized, len(normalized) < len(items)

    def merge(items):
        before = len(courses)
        for course in items:
            # Preserve API ordering; the later snapshot contains fresher counts/status.
            courses[course["id"]] = course
        if items and len(courses) == before:
            raise ValueError("活动接口未返回新的分页数据，请稍后重试")

    def count(metadata, key):
        value = metadata.get(key) if isinstance(metadata, dict) else None
        try:
            return int(str(value)) if value is not None else None
        except ValueError:
            return None

    try:
        result, data, items, skipped_rows = read_page(1)
        merge(items)
        metadata = {**result, **data} if isinstance(data, dict) else result
        expected_total = count(metadata, "total")
        last_page = count(metadata, "last_page")
        per_page = count(metadata, "per_page")
        if last_page == 0 and not items and expected_total in (None, 0):
            last_page = 1
        if last_page is None and expected_total is not None and per_page and per_page > 0:
            last_page = max(1, (expected_total + per_page - 1) // per_page)
        if last_page is not None and not 1 <= last_page <= MAX_CATALOG_PAGES:
            raise ValueError("活动分页信息异常")
        if last_page is not None:
            if not items and last_page > 1:
                raise ValueError("活动分页数据不完整，请刷新重试")
            for first in range(2, last_page + 1, 3):
                futures = [_catalog_pool.submit(read_page, pg)
                           for pg in range(first, min(first + 3, last_page + 1))]
                page_error = None
                try:
                    for future in futures:
                        try:
                            _, _, items, skipped = future.result()
                            skipped_rows |= skipped
                            if not items:
                                raise ValueError("活动分页数据不完整，请刷新重试")
                            merge(items)
                        except ValueError as exc:
                            page_error = page_error or str(exc)
                finally:
                    for future in futures:
                        future.cancel()
                if page_error:
                    raise ValueError(page_error)
        elif items:
            # A short page may be a server-side page-size cap, not the last page.
            for pg in range(2, MAX_CATALOG_PAGES + 1):
                _, _, items, skipped = read_page(pg)
                skipped_rows |= skipped
                if not items:
                    break
                merge(items)
            else:
                raise ValueError("活动分页超过读取上限，请稍后重试")
        if expected_total is not None and len(courses) != expected_total:
            raise ValueError("活动数量发生变化或分页不完整，请刷新重试")
        if skipped_rows:
            raise ValueError("部分活动编号格式异常")
    except ValueError as exc:
        return list(courses.values()), len(courses), str(exc)

    return list(courses.values()), len(courses), None


def apply_course(course_id: int) -> dict:
    payload = {"course_id": course_id, "template_id": DEFAULT_TEMPLATE_ID}
    return api_post("/api/course/apply", payload) or {}


def parse_api_time(time_str: str) -> datetime | None:
    if not time_str:
        return None
    try:
        parsed = datetime.fromisoformat(time_str.strip())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.astimezone().replace(tzinfo=None)
    except (ValueError, TypeError, AttributeError):
        return None


# ──────────────────────────── Sniper Engine ────────────────────────


class SniperEngine:
    def __init__(self, log_callback, success_callback=None):
        self._watched: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._log = log_callback
        self._on_success = success_callback

    def add(self, course: dict, silent: bool = False):
        cid = course.get("id")
        with self._lock:
            self._watched[cid] = course
        if not silent:
            self._log(f"[监控] 已添加活动 {cid}: {course.get('title', '未知')}")

    def remove(self, course_id: int, silent: bool = False):
        with self._lock:
            if course_id in self._watched:
                del self._watched[course_id]
        if not silent:
            self._log(f"[监控] 已移除活动 {course_id}")

    def is_watched(self, course_id: int) -> bool:
        with self._lock:
            return course_id in self._watched

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            now = datetime.now()
            to_fire: list[dict] = []

            with self._lock:
                for cid, course in list(self._watched.items()):
                    sign_start = course.get("sign_start_time", "")
                    start_dt = parse_api_time(sign_start)
                    if start_dt is None or now >= start_dt:
                        to_fire.append(course)

            for course in to_fire:
                cid = course.get("id")
                self._log(f"[抢课] 正在尝试报名活动 {cid}...")
                result = apply_course(cid)
                if result and "_error" not in result:
                    msg = result.get("message") or result.get("msg") or "请求已发送"
                    code = result.get("code", "")
                    if code == 200 or code == 0 or "成功" in str(msg):
                        self._log(f"[成功] 抢课成功! 活动 {cid}: {msg}")
                        if self._on_success:
                            try:
                                self._on_success(course, result)
                            except Exception as exc:
                                self._log(f"[错误] 已抢到课程列表更新失败: {exc}")
                    else:
                        self._log(f"[结果] 活动 {cid}: code={code}, {msg}")
                    with self._lock:
                        self._watched.pop(cid, None)
                elif result and "_error" in result:
                    err = result["_error"]
                    if "Token" in err or "401" in err or "Unauthenticated" in err:
                        self._log(f"[失败] Token已过期或无效: {err}")
                    else:
                        self._log(f"[失败] 活动 {cid}: {err}")
                else:
                    self._log(f"[失败] 活动 {cid}: 未收到响应")

            time.sleep(0.5)


# ──────────────────────────── Flet Application ─────────────────────


def main(page: ft.Page):
    # ChatGPT mobile-inspired dark palette: black canvas, charcoal panels, blue actions.
    SURFACE_BLACK = "#000000"
    SURFACE_DARK = "#171717"
    SURFACE_PANEL = "#212121"
    SURFACE_INSET = "#303030"
    SURFACE_FIELD = "#303030"
    SURFACE_PRESSED = "#383838"
    SURFACE_SELECTED = "#3D3D3D"
    SURFACE_ERROR = "#482522"
    BORDER_DARK = "#3D3D3D"
    BORDER_SUBTLE = "#343434"
    TEXT_WHITE = "#FFFFFF"
    TEXT_PRIMARY = "#F5F5F5"
    TEXT_BODY = "#E0E0E0"
    TEXT_META = "#B4B4B4"
    TEXT_TERTIARY = "#9A9A9A"
    TEXT_SILVER = "#B4B4B4"
    ACCENT_PRIMARY = "#4163EB"
    ACCENT_FOCUS = "#8AA4FF"
    ACCENT_DARK_LINK = "#8AA4FF"
    ACCENT_WARNING = TEXT_PRIMARY
    ACCENT_DANGER = "#FF8A80"
    PILL = ft.RoundedRectangleBorder(radius=980)

    is_android = page.platform == ft.PagePlatform.ANDROID
    page.title = "第二课堂自动抢课助手"
    if not is_android:
        page.window.width = 1280
        page.window.height = 820
    page.padding = 0
    page.spacing = 0
    page.bgcolor = SURFACE_BLACK
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = ft.Theme(
        color_scheme_seed=ACCENT_PRIMARY,
        color_scheme=ft.ColorScheme(
            primary=ACCENT_PRIMARY, on_primary=TEXT_WHITE,
            primary_container=SURFACE_SELECTED, on_primary_container=TEXT_PRIMARY,
            secondary=ACCENT_FOCUS, on_secondary=SURFACE_BLACK,
            secondary_container=SURFACE_SELECTED, on_secondary_container=TEXT_PRIMARY,
            background=SURFACE_BLACK, on_background=TEXT_PRIMARY,
            surface=SURFACE_PANEL, on_surface=TEXT_PRIMARY,
            surface_variant=SURFACE_INSET, on_surface_variant=TEXT_META,
            surface_container_lowest=SURFACE_BLACK, surface_container_low=SURFACE_DARK,
            surface_container=SURFACE_PANEL, surface_container_high=SURFACE_INSET,
            surface_bright=SURFACE_SELECTED, surface_dim=SURFACE_DARK,
            inverse_surface=SURFACE_INSET, on_inverse_surface=TEXT_PRIMARY,
            error=ACCENT_DANGER, on_error=SURFACE_BLACK,
            error_container=SURFACE_ERROR, on_error_container=ACCENT_DANGER,
            surface_tint=ft.Colors.TRANSPARENT, outline=BORDER_SUBTLE, outline_variant=BORDER_SUBTLE,
        ),
        scaffold_bgcolor=SURFACE_BLACK, canvas_color=SURFACE_BLACK,
        dialog_theme=ft.DialogTheme(bgcolor=SURFACE_PANEL), card_color=SURFACE_PANEL,
        divider_color=BORDER_SUBTLE, hint_color=TEXT_TERTIARY,
        font_family=None if is_android else "Microsoft YaHei",
        system_overlay_style=ft.SystemOverlayStyle(
            status_bar_color=SURFACE_BLACK,
            status_bar_brightness=ft.Brightness.DARK,
            status_bar_icon_brightness=ft.Brightness.LIGHT,
            system_navigation_bar_color=SURFACE_BLACK,
            system_navigation_bar_icon_brightness=ft.Brightness.LIGHT,
        ) if is_android else ft.SystemOverlayStyle(),
        navigation_bar_theme=ft.NavigationBarTheme(
            label_text_style={
                ft.ControlState.SELECTED: ft.TextStyle(
                    size=11, color=TEXT_PRIMARY, weight=ft.FontWeight.W_600, letter_spacing=-0.1),
                ft.ControlState.DEFAULT: ft.TextStyle(size=11, color=TEXT_META, letter_spacing=-0.1),
            },
            indicator_color=SURFACE_INSET,
            indicator_shape=PILL,
            overlay_color=ft.Colors.TRANSPARENT,
        ) if is_android else None,
    )
    page.dark_theme = page.theme

    def _tracking(size: float) -> float:
        """Apple runs tight at every size: -0.374 @17, -0.224 @14, -0.12 @12."""
        if size >= 24:
            return -0.28
        if size >= 17:
            return -0.37
        if size >= 14:
            return -0.22
        return -0.12

    def _t(value, size: float = 15, color: str = TEXT_PRIMARY,
           weight: ft.FontWeight = ft.FontWeight.W_400, height: float | None = None, **kwargs) -> ft.Text:
        return ft.Text(str(value), size=size, color=color, weight=weight,
                       style=ft.TextStyle(letter_spacing=_tracking(size), height=height), **kwargs)

    def _pill_style(fill: str, fg: str, side: str | None = None, size: float = 15,
                    horizontal: int = 18) -> ft.ButtonStyle:
        return ft.ButtonStyle(
            shape=PILL,
            elevation=0,
            bgcolor={ft.ControlState.DISABLED: SURFACE_PRESSED, ft.ControlState.DEFAULT: fill},
            color={ft.ControlState.DISABLED: TEXT_TERTIARY, ft.ControlState.DEFAULT: fg},
            side={
                ft.ControlState.DISABLED: ft.BorderSide(0, ft.Colors.TRANSPARENT),
                ft.ControlState.DEFAULT: ft.BorderSide(1, side) if side else ft.BorderSide(0, ft.Colors.TRANSPARENT),
            },
            overlay_color=ft.Colors.with_opacity(0.12, TEXT_WHITE if fill == ACCENT_PRIMARY else ACCENT_PRIMARY),
            padding=ft.padding.symmetric(horizontal=horizontal, vertical=0),
            text_style=ft.TextStyle(size=size, weight=ft.FontWeight.W_500, letter_spacing=_tracking(size)),
            icon_size=18,
        )
    launched_from_autostart = not is_android and AUTOSTART_FLAG in sys.argv
    _autostart_ok, autostart_message = (
        (True, "Android 前台巡检服务已配置。")
        if is_android else ensure_windows_autostart()
    )
    if not _TOKEN:
        token_input = ft.TextField(
            label="Bearer Token",
            password=True,
            can_reveal_password=True,
            autofocus=is_android,
            color=TEXT_PRIMARY,
            bgcolor=SURFACE_FIELD,
            filled=True,
            border_radius=11,
            border_color=BORDER_SUBTLE,
            focused_border_color=ACCENT_PRIMARY,
            focused_border_width=2,
            text_size=15,
        )
        token_error = _t("", size=12, color=ACCENT_DANGER)

        def save_initial_token(e):
            if not store_token(token_input.value or ""):
                token_error.value = "请填写 Token"
                page.update()
                return
            token_dialog.open = False
            page.overlay.remove(token_dialog)
            page.update()
            main(page)

        token_dialog = ft.AlertDialog(
            modal=True,
            title=_t("欢迎使用", size=24, weight=ft.FontWeight.W_600, height=1.14),
            content=ft.Column(
                [
                    _t("粘贴第二课堂的 Bearer Token 以开始巡检与自动报名。" if is_android else
                       "请打开与本程序同目录下的 token.txt 文件，\n"
                       "粘贴 Bearer Token（包含 'Bearer ' 前缀），\n"
                       "然后重新启动本程序。",
                       size=15, color=TEXT_META, height=1.47),
                    token_input if is_android else ft.Container(),
                    token_error,
                    _t(f"文件位置: {TOKEN_FILE}", size=12, color=TEXT_TERTIARY, selectable=True)
                    if not is_android else _t("Token 仅保存在本机应用私有目录。", size=12, color=TEXT_TERTIARY),
                ],
                tight=True,
                spacing=12,
            ),
            actions=[
                ft.FilledButton(
                    "保存并进入" if is_android else "关闭",
                    on_click=save_initial_token if is_android else lambda e: page.window.close(),
                    height=44,
                    style=_pill_style(ACCENT_PRIMARY, TEXT_WHITE, size=17, horizontal=22),
                )
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            bgcolor=SURFACE_PANEL,
            shape=ft.RoundedRectangleBorder(radius=12),
            scrollable=True,
            inset_padding=16 if is_android else None,
        )
        page.overlay.append(token_dialog)
        token_dialog.open = True
        page.update()
        return

    courses_data: list[dict] = []
    catalog_loaded = False
    log_lines: list[str] = []
    app_state = load_app_state()
    seen_course_ids: set[int] = set()
    grabbed_courses: list[dict] = [
        item for item in app_state.get("grabbed_courses", []) if isinstance(item, dict)
    ]
    watched_course_ids: set[int] = set()
    watched_course_snapshots: dict[int, dict] = {}
    state_lock = threading.Lock()
    refresh_lock = threading.Lock()
    mobile_result_offset = app_state.get("mobile_result_offset", 0)

    for raw_id in app_state.get("seen_course_ids", []):
        try:
            seen_course_ids.add(int(raw_id))
        except (TypeError, ValueError):
            continue

    for raw_id in app_state.get("watched_course_ids", []):
        try:
            watched_course_ids.add(int(raw_id))
        except (TypeError, ValueError):
            continue

    for item in app_state.get("watched_courses", []):
        if not isinstance(item, dict):
            continue
        try:
            cid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        watched_course_ids.add(cid)
        watched_course_snapshots[cid] = dict(item)

    known_categories = {"理想信念"}
    known_categories.update(get_course_category(item) for item in watched_course_snapshots.values())

    def _caption(text: str, color: str = TEXT_META) -> ft.Text:
        return _t(text, size=13, color=color, weight=ft.FontWeight.W_600)

    def _metric_block(value_control: ft.Text, label_text: str, note_text: str) -> ft.Container:
        return ft.Container(
            expand=True,
            bgcolor=SURFACE_PANEL,
            border_radius=12,
            padding=ft.padding.symmetric(horizontal=14, vertical=12),
            content=ft.Column(
                [
                    _t(label_text, size=12, color=TEXT_META, weight=ft.FontWeight.W_600),
                    value_control,
                    _t(note_text, size=11, color=TEXT_TERTIARY),
                ],
                spacing=2,
            ),
        )

    def _group(controls: list, padding=None) -> ft.Container:
        """Charcoal grouped panel with a compact, borderless layout."""
        return ft.Container(
            bgcolor=SURFACE_PANEL,
            border_radius=12,
            padding=padding if padding is not None else ft.padding.symmetric(horizontal=16, vertical=4),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            content=ft.Column(controls, spacing=0),
        )

    def _hairline() -> ft.Container:
        return ft.Container(height=1, bgcolor=BORDER_SUBTLE)

    def _kv_row(label: str, value, dark: bool = False, selectable: bool = False) -> ft.Container:
        return ft.Container(
            padding=ft.padding.symmetric(vertical=11),
            content=ft.Row(
                [
                    _t(label, size=15, color=TEXT_SILVER if dark else TEXT_META),
                    _t(value, size=15, color=TEXT_WHITE if dark else TEXT_PRIMARY, expand=True,
                       text_align=ft.TextAlign.RIGHT, selectable=selectable),
                ],
                spacing=16,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        )

    def _metric_value(text: str) -> ft.Text:
        return _t(text, size=28, color=TEXT_PRIMARY, weight=ft.FontWeight.W_600, height=1.14)

    monitored_count_text = _metric_value("00")
    captured_count_text = _metric_value("00")
    interval_count_text = _metric_value(f"{AUTO_REFRESH_INTERVAL_SECONDS // 60:02d}")

    username_text = _t("加载中…", size=28, color=TEXT_WHITE, weight=ft.FontWeight.W_600, height=1.14)
    user_detail_col = ft.Column(spacing=0)

    score_total_text = _t("--", size=48, color=TEXT_PRIMARY, weight=ft.FontWeight.W_600, height=1.07)
    score_breakdown_row = ft.ResponsiveRow(spacing=10, run_spacing=10)

    hero_status_text = _t("巡检已就绪 · 暂无监控", size=15, color=TEXT_PRIMARY, weight=ft.FontWeight.W_600)
    latest_event_text = _t("等待首次刷新", size=13, color=TEXT_META, max_lines=3,
                           overflow=ft.TextOverflow.ELLIPSIS)

    grabbed_list = ft.Column(spacing=0)

    visible_count_text = _t("00", size=13, color=TEXT_META)
    monitored_summary_text = _t("00", size=48, color=TEXT_WHITE, weight=ft.FontWeight.W_600, height=1.07)
    page_label = _t("第 1 页", size=13, color=TEXT_META)
    last_refresh_text = _t("尚未刷新", size=12, color=TEXT_TERTIARY)
    catalog_status_text = _t("正在加载活动…", size=12, color=TEXT_META, expand=True,
                             max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
    selected_category = [ALL_CATEGORIES]
    recent_course_ids: set[int] = set()
    category_filters = ft.Row(spacing=8, scroll=ft.ScrollMode.HIDDEN, height=44)

    activity_list = ft.ListView(
        spacing=10, expand=True, auto_scroll=False,
        padding=ft.padding.only(left=16, right=16, top=4, bottom=16),
    )
    watched_list = ft.ListView(spacing=10, expand=True,
                               padding=ft.padding.only(left=16, right=16, top=16, bottom=16))
    watched_status_text = _t("到点自动报名 · 每 5 分钟巡检新活动", size=15, color=TEXT_SILVER)
    mobile_wide = [(page.width or 390) >= 600]

    refresh_btn = ft.FilledButton(
        text="刷新列表",
        icon=ft.Icons.REFRESH,
        height=36,
        style=_pill_style(ACCENT_PRIMARY, TEXT_WHITE, size=14, horizontal=14),
    )
    prev_btn = ft.OutlinedButton(
        "上一页",
        icon=ft.Icons.CHEVRON_LEFT,
        disabled=True,
        height=44,
        expand=True,
        style=_pill_style(SURFACE_PANEL, ACCENT_FOCUS, side=ACCENT_FOCUS),
    )
    next_btn = ft.OutlinedButton(
        "下一页",
        icon=ft.Icons.CHEVRON_RIGHT,
        height=44,
        expand=True,
        style=_pill_style(SURFACE_PANEL, ACCENT_FOCUS, side=ACCENT_FOCUS),
    )
    current_page = [1]
    page_count = [1]

    log_field = ft.TextField(
        multiline=True,
        min_lines=8,
        max_lines=12,
        read_only=True,
        value="",
        text_size=12,
        border=ft.InputBorder.NONE,
        color=TEXT_SILVER,
        bgcolor=SURFACE_DARK,
        filled=True,
        border_radius=12,
        content_padding=ft.padding.all(14),
        text_style=ft.TextStyle(letter_spacing=-0.12, height=1.4),
    )

    def append_log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        log_lines.append(f"[{ts}] {msg}")
        if len(log_lines) > 200:
            log_lines.pop(0)
        log_field.value = "\n".join(log_lines)
        latest_event_text.value = msg
        try:
            page.update()
        except Exception:
            pass

    def show_snack(msg: str, error: bool = False):
        snack = ft.SnackBar(
            _t(msg, size=14, color=ACCENT_DANGER if error else TEXT_WHITE, weight=ft.FontWeight.W_500),
            bgcolor=SURFACE_ERROR if error else SURFACE_INSET,
            duration=3000,
            behavior=ft.SnackBarBehavior.FLOATING,
            margin=16,
            shape=ft.RoundedRectangleBorder(radius=12),
            elevation=0,
        )
        page.open(snack)

    def remember_latest_courses(courses: list[dict]) -> list[dict]:
        pairs: list[tuple[int, dict]] = []
        for course in courses:
            try:
                cid = int(course.get("id"))
            except (TypeError, ValueError):
                continue
            pairs.append((cid, course))

        with state_lock:
            if not app_state.get("baseline_initialized"):
                seen_course_ids.update(cid for cid, _ in pairs)
                app_state["baseline_initialized"] = True
                app_state["seen_course_ids"] = sorted(seen_course_ids)[-MAX_SEEN_COURSE_IDS:]
                save_app_state(app_state)
                return []

            new_courses = [course for cid, course in pairs if cid not in seen_course_ids]
            if new_courses:
                seen_course_ids.update(cid for cid, _ in pairs)
                app_state["seen_course_ids"] = sorted(seen_course_ids)[-MAX_SEEN_COURSE_IDS:]
                save_app_state(app_state)
            return new_courses

    def persist_watched_courses():
        app_state["watched_course_ids"] = sorted(watched_course_ids)
        app_state["watched_courses"] = list(watched_course_snapshots.values())[-MAX_WATCHED_COURSES:]
        return save_app_state(app_state)

    def set_course_watch(course: dict, enabled: bool):
        try:
            course_id = int(course.get("id"))
        except (TypeError, ValueError):
            return False

        with state_lock:
            previous_snapshot = watched_course_snapshots.get(course_id)
            was_watched = course_id in watched_course_ids
            if was_watched == enabled:
                return False
            if enabled and any(str(item.get("id")) == str(course_id) for item in grabbed_courses):
                return False
            if enabled:
                watched_course_ids.add(course_id)
                watched_course_snapshots[course_id] = dict(course)
            else:
                watched_course_ids.discard(course_id)
                watched_course_snapshots.pop(course_id, None)
            if persist_watched_courses():
                return True
            if was_watched:
                watched_course_ids.add(course_id)
            else:
                watched_course_ids.discard(course_id)
            if previous_snapshot is not None:
                watched_course_snapshots[course_id] = previous_snapshot
            else:
                watched_course_snapshots.pop(course_id, None)
            app_state["watched_course_ids"] = sorted(watched_course_ids)
            app_state["watched_courses"] = list(watched_course_snapshots.values())[-MAX_WATCHED_COURSES:]
            return False

    def refresh_watched_course_snapshots(courses: list[dict]):
        changed = False
        with state_lock:
            for course in courses:
                try:
                    course_id = int(course.get("id"))
                except (TypeError, ValueError):
                    continue
                if course_id not in watched_course_ids:
                    continue
                if watched_course_snapshots.get(course_id) == course:
                    continue
                watched_course_snapshots[course_id] = dict(course)
                changed = True
            if changed:
                persist_watched_courses()

    def update_dashboard_metrics():
        watched_count = len(watched_course_ids)
        monitored_count_text.value = f"{watched_count:02d}"
        monitored_summary_text.value = f"{watched_count:02d}"
        captured_count_text.value = f"{len(grabbed_courses):02d}"
        visible_count_text.value = f"{len(filter_courses(courses_data, selected_category[0])):02d}"

        if watched_count:
            hero_status_text.value = f"{watched_count} 项监控 · 到点自动报名"
            hero_status_text.color = ACCENT_PRIMARY
        else:
            hero_status_text.value = "巡检已就绪 · 暂无监控"
            hero_status_text.color = TEXT_PRIMARY

    def mobile_cards(items: list[dict]) -> list:
        cards = [build_course_card(item) for item in items]
        if not mobile_wide[0]:
            return cards
        rows = []
        for index in range(0, len(cards), 2):
            pair = cards[index:index + 2]
            for card in pair:
                card.expand = True
            if len(pair) == 1:
                pair.append(ft.Container(expand=True))
            rows.append(ft.Row(pair, spacing=10, vertical_alignment=ft.CrossAxisAlignment.START))
        return rows

    def empty_state(icon: str, title: str, note: str) -> ft.Container:
        return ft.Container(
            padding=ft.padding.symmetric(horizontal=24, vertical=56),
            alignment=ft.alignment.center,
            content=ft.Column(
                [
                    ft.Icon(icon, size=40, color=TEXT_SILVER),
                    _t(title, size=21, color=TEXT_PRIMARY, weight=ft.FontWeight.W_600, height=1.19),
                    _t(note, size=14, color=TEXT_META, text_align=ft.TextAlign.CENTER),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def render_watched_courses():
        watched_list.controls = mobile_cards([
            item for cid, item in list(watched_course_snapshots.items())
            if cid in watched_course_ids
        ])
        if not watched_list.controls:
            watched_list.controls = [empty_state(
                ft.Icons.NOTIFICATIONS_NONE, "暂无监控活动", "在活动页点击「加入监控」，到点自动尝试报名。")]

    def render_grabbed_courses():
        grabbed_list.controls.clear()

        if not grabbed_courses:
            grabbed_list.controls.append(
                ft.Container(
                    padding=ft.padding.symmetric(vertical=14),
                    content=ft.Column(
                        [
                            _t("还没有抢到活动", size=15, color=TEXT_PRIMARY),
                            _t("成功记录会在这里留档。", size=13, color=TEXT_META),
                        ],
                        spacing=2,
                    ),
                )
            )
        else:
            for index, item in enumerate(grabbed_courses):
                title = item.get("title") or "未知活动"
                captured_at = item.get("captured_at") or "--"
                time_place = item.get("time_place") or "时间地点待定"
                score = item.get("score", "—")
                if index:
                    grabbed_list.controls.append(_hairline())
                grabbed_list.controls.append(
                    ft.Container(
                        padding=ft.padding.symmetric(vertical=12),
                        content=ft.Row(
                            [
                                ft.Icon(ft.Icons.CHECK_CIRCLE, size=20, color=ACCENT_PRIMARY),
                                ft.Column(
                                    [
                                        _t(title, size=15, color=TEXT_PRIMARY, weight=ft.FontWeight.W_500,
                                           max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                                        _t(time_place, size=12, color=TEXT_META, max_lines=1,
                                           overflow=ft.TextOverflow.ELLIPSIS),
                                        _t(f"{score} 分 · {captured_at}", size=12, color=TEXT_TERTIARY),
                                    ],
                                    spacing=2,
                                    expand=True,
                                ),
                            ],
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.START,
                        ),
                    )
                )

        update_dashboard_metrics()
        try:
            page.update()
        except Exception:
            pass

    def sync_mobile_results():
        nonlocal mobile_result_offset
        if not is_android:
            return

        notices = []
        with state_lock:
            try:
                size = os.path.getsize(MOBILE_RESULTS_FILE)
                if size == mobile_result_offset:
                    return
                if size < mobile_result_offset:
                    mobile_result_offset = 0
                with open(MOBILE_RESULTS_FILE, "rb") as f:
                    f.seek(mobile_result_offset)
                    events = []
                    for line in f:
                        # The Android service may still be appending the last line.
                        if not line.endswith(b"\n"):
                            break
                        mobile_result_offset += len(line)
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError:
                return

            changed = False
            captured_ids = {str(item.get("id")) for item in grabbed_courses}
            for item in events:
                if not isinstance(item, dict):
                    continue
                try:
                    course_id = int(item["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                was_watched = course_id in watched_course_ids
                terminal = item.get("outcome") == "terminal"
                if not terminal and str(course_id) not in captured_ids:
                    grabbed_courses.insert(0, item)
                    captured_ids.add(str(course_id))
                    changed = True
                watched_course_ids.discard(course_id)
                watched_course_snapshots.pop(course_id, None)
                if was_watched:
                    title = item.get("title", "活动")
                    message = item.get("message") or "报名已停止"
                    notices.append((f"{title}：{message}" if terminal else f"报名成功：{title}", terminal))
                    changed = True
            if changed:
                del grabbed_courses[MAX_GRABBED_COURSES:]
                app_state["grabbed_courses"] = list(grabbed_courses)
            if mobile_result_offset != app_state.get("mobile_result_offset", 0):
                app_state["mobile_result_offset"] = mobile_result_offset
                persist_watched_courses()
        if changed:
            render_grabbed_courses()
            render_activity_cards(courses_data)
        if notices:
            for message, _ in notices:
                append_log(message)
            message, error = notices[-1]
            show_snack(message, error=error)

    def mobile_result_loop():
        while True:
            time.sleep(0.5)
            sync_mobile_results()

    def render_activity_cards(items: list[dict]):
        known_categories.update(get_course_category(course) for course in items)
        counts = {}
        for course in items:
            name = get_course_category(course)
            counts[name] = counts.get(name, 0) + 1
        recent = [course for course in items if course["id"] in recent_course_ids]
        categories = [ALL_CATEGORIES]
        if recent:
            categories.append(NEW_COURSES_FILTER)
            counts[NEW_COURSES_FILTER] = len(recent)
        elif selected_category[0] == NEW_COURSES_FILTER:
            selected_category[0] = ALL_CATEGORIES
            current_page[0] = 1
        categories.extend(sorted(known_categories))
        category_filters.controls = [
            ft.OutlinedButton(
                f"{name} ({len(items) if name == ALL_CATEGORIES else counts.get(name, 0)})",
                data=name, on_click=on_category_change, height=36,
                style=ft.ButtonStyle(
                    shape=PILL,
                    side=ft.BorderSide(0, ft.Colors.TRANSPARENT),
                    bgcolor=SURFACE_SELECTED if name == selected_category[0] else SURFACE_PANEL,
                    color=TEXT_WHITE if name == selected_category[0] else TEXT_PRIMARY,
                    overlay_color=ft.Colors.with_opacity(0.08, TEXT_PRIMARY),
                    padding=ft.padding.symmetric(horizontal=14, vertical=0),
                    text_style=ft.TextStyle(size=13, weight=ft.FontWeight.W_500, letter_spacing=-0.12),
                ),
            ) for name in categories
        ]
        filtered = recent if selected_category[0] == NEW_COURSES_FILTER else filter_courses(items, selected_category[0])
        page_count[0] = max(1, (len(filtered) + COURSE_PAGE_SIZE - 1) // COURSE_PAGE_SIZE)
        current_page[0] = min(current_page[0], page_count[0])
        start = (current_page[0] - 1) * COURSE_PAGE_SIZE
        page_items = filtered[start:start + COURSE_PAGE_SIZE]
        page_label.value = f"{current_page[0]} / {page_count[0]}"
        prev_btn.disabled = current_page[0] <= 1
        next_btn.disabled = current_page[0] >= page_count[0]
        mobile_pagination.visible = page_count[0] > 1
        catalog_status_text.value = f"{selected_category[0]} · {len(filtered)} / {len(items)} 项"
        activity_list.controls.clear()

        if not page_items:
            activity_list.controls.append(empty_state(
                ft.Icons.EVENT_BUSY,
                "该类别暂无活动" if selected_category[0] != ALL_CATEGORIES else "暂无可报名活动",
                "可切换类别或刷新全部活动。",
            ))
        else:
            activity_list.controls = mobile_cards(page_items)

        update_dashboard_metrics()
        render_watched_courses()
        try:
            page.update()
        except Exception:
            pass

    def remember_grabbed_course(course: dict, result: dict):
        try:
            course_id = int(course.get("id"))
        except (TypeError, ValueError):
            course_id = course.get("id")

        with state_lock:
            watched_course_ids.discard(course_id)
            watched_course_snapshots.pop(course_id, None)
            grabbed_courses[:] = [item for item in grabbed_courses if item.get("id") != course_id]
            grabbed_courses.insert(
                0,
                {
                    "id": course_id,
                    "title": course.get("title", "未知活动"),
                    "time_place": course.get("time_place", "时间地点待定"),
                    "score": course.get("score", "—"),
                    "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "message": result.get("message") or result.get("msg") or "",
                },
            )
            del grabbed_courses[MAX_GRABBED_COURSES:]
            app_state["grabbed_courses"] = list(grabbed_courses)
            persist_watched_courses()

        render_grabbed_courses()
        render_activity_cards(courses_data)

    sniper = SniperEngine(log_callback=append_log, success_callback=remember_grabbed_course)
    for saved_course in watched_course_snapshots.values():
        sniper.add(saved_course, silent=True)
    if not is_android:
        sniper.start()

    def build_course_card(course: dict) -> ft.Container:
        try:
            cid = int(course.get("id", 0))
        except (TypeError, ValueError):
            cid = 0

        title = course.get("title", "未知活动")
        time_place = course.get("time_place", "时间地点待定")
        score = course.get("score", "—")
        sign_start = str(course.get("sign_start_time") or "")
        sign_end = str(course.get("sign_end_time") or "")
        sign_status = course.get("sign_status_label", "") or "待开放"
        max_slots = course.get("max", 0)
        apply_count = course.get("course_apply_count", 0)
        dimension = get_course_category(course)

        try:
            surplus = int(max_slots) - int(apply_count)
        except (ValueError, TypeError):
            surplus = 0
        if surplus < 0:
            surplus = 0

        is_monitored = cid in watched_course_ids
        is_registered = any(str(item.get("id")) == str(cid) for item in grabbed_courses)
        is_full = surplus <= 0

        if is_monitored and not sniper.is_watched(cid):
            sniper.add(course, silent=True)
        if is_monitored:
            watched_course_snapshots[cid] = dict(course)

        if is_full:
            surplus_text, surplus_color = "名额已满", ACCENT_DANGER
        else:
            surplus_text = f"剩余 {surplus} / {max_slots}"
            surplus_color = TEXT_PRIMARY if surplus <= 5 else TEXT_META

        def on_action(e):
            if (cid in watched_course_ids) != is_monitored:
                return
            if not set_course_watch(course, not is_monitored):
                show_snack("无法保存监控，请重试", error=True)
                return
            if is_monitored:
                sniper.remove(cid)
                append_log(f"[监控] 已取消活动 {cid}: {title}")
                show_snack(f"已取消监控：{title}")
            else:
                sniper.add(course)
                append_log(f"[监控] 已加入活动 {cid}: {title}")
                now = datetime.now()
                start, end = parse_api_time(sign_start), parse_api_time(sign_end)
                open_now = (end is None or now < end) and (
                    sign_status == "报名中" or (start is not None and now >= start)
                )
                show_snack(f"正在尝试报名：{title}" if is_android and open_now else f"已加入监控：{title}")
            render_activity_cards(courses_data)

        if is_monitored:
            action_button = ft.OutlinedButton(
                "取消监控", on_click=on_action, height=44,
                style=_pill_style(SURFACE_INSET, TEXT_PRIMARY, size=14, horizontal=16),
            )
        else:
            action_button = ft.FilledButton(
                "已报名" if is_registered else "名额已满" if is_full else "加入监控",
                on_click=on_action if not (is_full or is_registered) else None,
                disabled=is_full or is_registered,
                height=44,
                style=_pill_style(ACCENT_PRIMARY, TEXT_WHITE, size=14, horizontal=16),
            )

        def show_details(e):
            facts = [
                ("活动类别", dimension),
                ("活动分值", f"{score} 分"),
                ("时间地点", time_place),
                ("报名开始", sign_start or "待定"),
                ("报名截止", sign_end or "待定"),
                ("报名状态", sign_status),
                ("名额", surplus_text),
            ]
            fact_rows = []
            for index, (label, value) in enumerate(facts):
                if index:
                    fact_rows.append(_hairline())
                fact_rows.append(_kv_row(label, value, selectable=True))
            detail_sheet = ft.BottomSheet(
                content=ft.Container(
                    padding=ft.padding.only(left=20, right=20, bottom=20),
                    content=ft.Column([
                        _t("活动详情", size=13, color=TEXT_META, weight=ft.FontWeight.W_600),
                        _t(title, size=21, color=TEXT_PRIMARY, weight=ft.FontWeight.W_600,
                           height=1.19, selectable=True),
                        ft.Container(
                            bgcolor=SURFACE_INSET, border_radius=12,
                            padding=ft.padding.symmetric(horizontal=16, vertical=2),
                            content=ft.Column(fact_rows, spacing=0),
                        ),
                        ft.Row([
                            ft.OutlinedButton(
                                "关闭详情", height=44, expand=True,
                                style=_pill_style(SURFACE_INSET, TEXT_PRIMARY),
                                on_click=lambda e: page.close(detail_sheet),
                            ),
                        ]),
                    ], tight=True, spacing=16, scroll=ft.ScrollMode.AUTO),
                ),
                bgcolor=SURFACE_PANEL,
                show_drag_handle=True,
                use_safe_area=True,
                is_scroll_controlled=True,
                size_constraints=ft.BoxConstraints(max_height=max(220, (page.height or 720) * 0.85)),
                shape=ft.RoundedRectangleBorder(radius=12),
            )
            page.open(detail_sheet)

        short_start = sign_start[5:] if len(sign_start) >= 16 else sign_start
        status_line = (
            ft.Row([
                ft.Container(width=6, height=6, border_radius=3, bgcolor=ACCENT_PRIMARY),
                _t(f"监控中 · {dimension}", size=12, color=ACCENT_FOCUS, weight=ft.FontWeight.W_600,
                   max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
            ], spacing=6)
            if is_monitored else
            _t(f"{dimension} · {sign_status}", size=12, color=TEXT_META,
               max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        )
        return ft.Container(
            data=cid,
            bgcolor=SURFACE_PANEL,
            border_radius=12,
            padding=ft.padding.only(left=16, right=10, top=14, bottom=8),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            _t(title, size=17, color=TEXT_PRIMARY, weight=ft.FontWeight.W_600, height=1.24,
                               max_lines=2, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                            ft.Container(
                                padding=ft.padding.only(right=6),
                                content=_t(f"{score} 分", size=15, color=TEXT_PRIMARY, weight=ft.FontWeight.W_600),
                            ),
                        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    ft.Row([
                        ft.Icon(ft.Icons.PLACE_OUTLINED, size=14, color=TEXT_TERTIARY),
                        _t(time_place, size=13, color=TEXT_META, expand=True,
                           max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ], spacing=4),
                    ft.Row(
                        [
                            ft.Column([
                                status_line,
                                ft.Row([
                                    _t(f"{short_start} 开报" if short_start else "报名时间待定",
                                       size=12, color=TEXT_META, max_lines=1,
                                       overflow=ft.TextOverflow.ELLIPSIS),
                                    _t(f"余 {surplus}" if not is_full else "已满", size=12,
                                       color=surplus_color, weight=ft.FontWeight.W_600),
                                ], spacing=8),
                            ], spacing=2, expand=True),
                            ft.IconButton(ft.Icons.INFO_OUTLINE, icon_size=20,
                                          icon_color=ACCENT_FOCUS, width=44, height=44,
                                          tooltip="查看活动详情", on_click=show_details),
                            action_button,
                        ], spacing=2,
                    ),
                ],
                spacing=6,
            ),
        )

    def render_course_page(items: list[dict], total_count: int, trigger_label: str, error: str | None):
        nonlocal catalog_loaded
        changed = not catalog_loaded or courses_data != items
        courses_data.clear()
        courses_data.extend(items)
        catalog_loaded = True
        if changed:
            render_activity_cards(items)
        now = datetime.now().strftime("%H:%M")
        if error:
            last_refresh_text.value = f"部分更新 {now}"
            append_log(f"{trigger_label}读取了 {total_count} 条活动，其余保留上次数据。{error}")
        else:
            last_refresh_text.value = f"更新于 {now}"
            append_log(f"{trigger_label}完成，已读取全部 {total_count} 条活动，可浏览 {len(items)} 条")
        page.update()

    def do_refresh(
        trigger_label: str = "刷新",
        detect_new: bool = False,
        quiet: bool = False,
    ) -> list[dict] | None:
        sync_mobile_results()
        if not refresh_lock.acquire(blocking=False):
            if not quiet:
                append_log("已有刷新任务正在执行，跳过本次请求。")
            return None

        if not quiet:
            append_log(f"{trigger_label}活动列表...")

        refresh_btn.disabled = True
        prev_btn.disabled = True
        next_btn.disabled = True
        refresh_btn.text = "刷新中"
        try:
            page.update()
        except Exception:
            pass

        try:
            items, total_count, error = fetch_course_catalog()
            if error and not items:
                append_log(f"{trigger_label}失败: {error}")
                show_snack(error, error=True)
                return None

            refresh_watched_course_snapshots(items)
            visible_items = [item for item in items if item.get("sign_status_label") != "已结束"]
            new_courses = []
            if detect_new and (not error or app_state.get("baseline_initialized")):
                new_courses = remember_latest_courses(visible_items)
                if new_courses:
                    recent_course_ids.clear()
                    recent_course_ids.update(course["id"] for course in new_courses)

            if error:
                # Only a complete snapshot proves an unreturned activity was removed.
                refreshed_ids = {item["id"] for item in items}
                visible_items.extend(item for item in courses_data if item["id"] not in refreshed_ids)
            render_course_page(visible_items, total_count, trigger_label, error)
            if new_courses:
                append_log(f"[提醒] 检测到 {len(new_courses)} 个新活动。")
                show_new_course_popup(page, new_courses)
                show_snack(f"发现 {len(new_courses)} 个新活动，点「近期上新」查看。")
            elif error and not quiet:
                show_snack("部分分页加载失败，已更新可用活动。", error=True)

            return visible_items
        finally:
            refresh_btn.disabled = False
            prev_btn.disabled = current_page[0] <= 1
            next_btn.disabled = current_page[0] >= page_count[0]
            refresh_btn.text = "刷新列表"
            try:
                page.update()
            except Exception:
                pass
            refresh_lock.release()

    def auto_refresh_loop():
        while True:
            time.sleep(AUTO_REFRESH_INTERVAL_SECONDS)
            do_refresh(trigger_label="自动刷新", detect_new=True, quiet=True)

    def on_refresh(e):
        threading.Thread(
            target=do_refresh,
            kwargs={"trigger_label": "手动刷新", "detect_new": True},
            daemon=True,
        ).start()

    def on_prev(e):
        if current_page[0] > 1:
            current_page[0] -= 1
            render_activity_cards(courses_data)

    def on_next(e):
        if current_page[0] < page_count[0]:
            current_page[0] += 1
            render_activity_cards(courses_data)

    def on_category_change(e):
        selected_category[0] = e.control.data
        current_page[0] = 1
        render_activity_cards(courses_data)

    refresh_btn.on_click = on_refresh
    prev_btn.on_click = on_prev
    next_btn.on_click = on_next

    token_editor = ft.TextField(
        label="Bearer Token",
        value=_TOKEN,
        password=True,
        can_reveal_password=True,
        color=TEXT_PRIMARY,
        bgcolor=SURFACE_FIELD,
        filled=True,
        border_radius=11,
        border_color=BORDER_SUBTLE,
        focused_border_color=ACCENT_PRIMARY,
        focused_border_width=2,
        text_size=15,
    )

    def save_mobile_token(e):
        if store_token(token_editor.value or ""):
            show_snack("Token 已保存")
            threading.Thread(target=load_user_info, daemon=True).start()
            threading.Thread(target=load_scores, daemon=True).start()
            threading.Thread(target=do_refresh, kwargs={"trigger_label": "Token 更新"}, daemon=True).start()
        else:
            show_snack("Token 不能为空", error=True)

    # ── Activity view: charcoal cards on a black canvas ──
    mobile_pagination = ft.Container(
        visible=False,
        padding=ft.padding.symmetric(horizontal=16, vertical=8),
        bgcolor=SURFACE_BLACK,
        content=ft.Row([prev_btn, ft.Container(content=page_label, padding=ft.padding.symmetric(horizontal=8)),
                        next_btn], spacing=8),
    )
    activity_view = ft.Column([
        ft.Container(
            padding=ft.padding.only(left=16, right=16, top=12),
            content=category_filters,
        ),
        ft.Container(
            padding=ft.padding.only(left=20, right=20, top=2, bottom=8),
            content=ft.Row([catalog_status_text, last_refresh_text], spacing=8),
        ),
        activity_list,
        mobile_pagination,
    ], spacing=0, expand=True)

    # ── Monitor view: black hero and watched list ──
    monitor_view = ft.Column([
        ft.Container(
            bgcolor=SURFACE_BLACK,
            padding=ft.padding.only(left=20, right=20, top=8, bottom=24),
            content=ft.Column([
                ft.Row([
                    monitored_summary_text,
                    _t("项活动正在监控", size=17, color=TEXT_WHITE),
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.END),
                watched_status_text,
            ], spacing=6),
        ),
        watched_list,
    ], spacing=0, expand=True, visible=False)

    # ── Profile view: black account hero, then grouped settings ──
    def profile_expander(icon: str, title: str, subtitle: str, controls: list) -> ft.ExpansionTile:
        return ft.ExpansionTile(
            leading=ft.Icon(icon, size=22, color=TEXT_PRIMARY),
            title=_t(title, size=17, color=TEXT_PRIMARY),
            subtitle=_t(subtitle, size=12, color=TEXT_META),
            controls=controls,
            tile_padding=ft.padding.symmetric(horizontal=16, vertical=2),
            controls_padding=ft.padding.only(left=16, right=16, bottom=16),
            text_color=TEXT_PRIMARY,
            collapsed_text_color=TEXT_PRIMARY,
            icon_color=TEXT_TERTIARY,
            collapsed_icon_color=TEXT_TERTIARY,
            bgcolor=SURFACE_PANEL,
            collapsed_bgcolor=SURFACE_PANEL,
            shape=ft.RoundedRectangleBorder(radius=0),
            collapsed_shape=ft.RoundedRectangleBorder(radius=0),
            maintain_state=True,
            expanded_cross_axis_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def profile_section(title: str, body: ft.Control) -> ft.Container:
        return ft.Container(
            padding=ft.padding.only(left=16, right=16, top=24),
            content=ft.Column([
                ft.Container(padding=ft.padding.only(left=4), content=_caption(title)),
                body,
            ], spacing=8),
        )

    profile_view = ft.Container(
        expand=True, visible=False, bgcolor=SURFACE_BLACK,
        content=ft.ListView(controls=[
            ft.Container(
                bgcolor=SURFACE_BLACK,
                padding=ft.padding.only(left=20, right=20, top=8, bottom=20),
                content=ft.Column([
                    _t("账户", size=13, color=TEXT_SILVER, weight=ft.FontWeight.W_600),
                    username_text,
                    user_detail_col,
                ], spacing=6),
            ),
            ft.Container(
                padding=ft.padding.only(left=16, right=16, top=16),
                content=ft.Row([
                    _metric_block(monitored_count_text, "监控中", "等待报名"),
                    _metric_block(captured_count_text, "已抢到", "成功记录"),
                    _metric_block(interval_count_text, "巡检", "分钟一次"),
                ], spacing=10),
            ),
            profile_section("积分", _group([
                ft.Row([score_total_text, _t("累计基础分", size=14, color=TEXT_META)],
                       spacing=10, vertical_alignment=ft.CrossAxisAlignment.END),
                ft.Container(height=14),
                score_breakdown_row,
            ], padding=ft.padding.all(16))),
            profile_section("已抢到的活动", _group([grabbed_list])),
            profile_section("设置", _group([
                profile_expander(ft.Icons.KEY_OUTLINED, "登录凭证", "更新 Bearer Token", [
                    token_editor,
                    ft.Container(height=12),
                    ft.FilledButton(
                        "保存 Token", on_click=save_mobile_token, height=44,
                        style=_pill_style(ACCENT_PRIMARY, TEXT_WHITE),
                    ),
                    ft.Container(height=8),
                    _t("Token 仅保存在本机应用私有目录。", size=12, color=TEXT_TERTIARY),
                ]),
                _hairline(),
                profile_expander(ft.Icons.TERMINAL, "运行状态与日志", "查看巡检与报名结果", [
                    hero_status_text, latest_event_text,
                    ft.Container(height=12), log_field,
                ]),
            ], padding=0)),
        ], spacing=0, padding=ft.padding.only(bottom=24)),
    )

    mobile_title = _t("活动", size=28, color=TEXT_WHITE, weight=ft.FontWeight.W_600, height=1.14)
    mobile_brand = _t("第二课堂助手", size=12, color=TEXT_SILVER, weight=ft.FontWeight.W_600)
    mobile_views = [activity_view, monitor_view, profile_view]

    def switch_mobile_view(e):
        index = e.control.selected_index
        for i, view in enumerate(mobile_views):
            view.visible = i == index
        mobile_title.value = ["活动", "监控", "我的"][index]
        refresh_btn.visible = index != 2
        if index == 1:
            sync_mobile_results()
            render_watched_courses()
        page.update()

    mobile_nav = ft.NavigationBar(
        selected_index=0,
        height=64, bgcolor=SURFACE_BLACK, elevation=0,
        indicator_color=SURFACE_INSET,
        indicator_shape=PILL,
        overlay_color=ft.Colors.TRANSPARENT,
        border=ft.border.only(top=ft.BorderSide(1, BORDER_SUBTLE)),
        label_behavior=ft.NavigationBarLabelBehavior.ALWAYS_SHOW,
        on_change=switch_mobile_view,
        destinations=[
            ft.NavigationBarDestination(label="活动", icon=ft.Icon(ft.Icons.LIST_ALT_OUTLINED, color=TEXT_META),
                                        selected_icon=ft.Icon(ft.Icons.LIST_ALT, color=TEXT_PRIMARY)),
            ft.NavigationBarDestination(label="监控",
                                        icon=ft.Icon(ft.Icons.NOTIFICATIONS_ACTIVE_OUTLINED, color=TEXT_META),
                                        selected_icon=ft.Icon(ft.Icons.NOTIFICATIONS_ACTIVE, color=TEXT_PRIMARY)),
            ft.NavigationBarDestination(label="我的", icon=ft.Icon(ft.Icons.PERSON_OUTLINE, color=TEXT_META),
                                        selected_icon=ft.Icon(ft.Icons.PERSON, color=TEXT_PRIMARY)),
        ],
    )
    mobile_header = ft.Container(
        bgcolor=SURFACE_BLACK,
        padding=ft.padding.only(left=20, right=16, top=12, bottom=12),
        content=ft.Row([
            ft.Column([mobile_brand, mobile_title], spacing=2, expand=True),
            refresh_btn,
        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.END),
    )

    def resize_mobile_layout(e=None):
        wide = (page.width or 390) >= 600
        compact = (page.height or 844) < 500 and (page.width or 390) > (page.height or 844)
        mobile_brand.visible = not compact
        mobile_title.size = 21 if compact else 28
        mobile_header.padding = ft.padding.only(left=20, right=16, top=4 if compact else 12,
                                                bottom=4 if compact else 12)
        mobile_nav.height = 56 if compact else 64
        if mobile_wide[0] != wide:
            mobile_wide[0] = wide
            render_activity_cards(courses_data)
        page.update()

    page.on_resized = resize_mobile_layout
    render_activity_cards(courses_data)
    resize_mobile_layout()
    page.add(ft.SafeArea(
        expand=True, content=ft.Column([
            mobile_header,
            ft.Container(
                expand=True, bgcolor=SURFACE_BLACK,
                content=ft.Column(mobile_views, spacing=0, expand=True),
            ),
            mobile_nav,
        ], spacing=0, expand=True),
    ))
    render_watched_courses()
    sync_mobile_results()
    render_grabbed_courses()

    def load_user_info():
        append_log("正在获取用户信息...")
        info = fetch_user_info()
        if info and "_error" in info:
            username_text.value = "获取失败"
            append_log(f"获取用户信息失败: {info['_error']}")
            page.update()
            return

        data = info.get("data") or info.get("result") or info
        if isinstance(data, dict):
            name = data.get("name") or data.get("username") or data.get("realname") or "未知用户"
            username_text.value = name
            detail_map = {
                "学号": data.get("student_id") or data.get("number") or data.get("sn"),
                "学院": data.get("college") or data.get("department"),
                "班级": data.get("class_name") or data.get("class"),
            }
            user_detail_col.controls.clear()
            for key, value in detail_map.items():
                if value:
                    user_detail_col.controls.append(_kv_row(key, value, dark=True))
            append_log(f"用户信息加载成功: {name}")
        else:
            username_text.value = "未知用户"
            append_log("用户信息格式异常")
        page.update()

    def load_scores():
        append_log("正在获取成绩单...")
        score_breakdown_row.controls.clear()

        result = fetch_scores()
        if result and "_error" in result:
            score_total_text.value = "--"
            score_total_text.color = TEXT_TERTIARY
            append_log(f"获取成绩单失败: {result['_error']}")
            page.update()
            return

        try:
            data_obj = result.get("data")
            leader_scores = data_obj.get("leaderScore", []) if isinstance(data_obj, dict) else []

            if not leader_scores:
                score_total_text.value = "--"
                score_total_text.color = TEXT_TERTIARY
                append_log("成绩单为空或未找到 leaderScore")
            else:
                total_score = sum(float(item.get("score", 0)) for item in leader_scores)
                score_total_text.value = f"{total_score:.0f}"
                score_total_text.color = TEXT_PRIMARY

                for item in leader_scores:
                    name = item.get("name", "未知")
                    if name and name != "未知":
                        known_categories.add(str(name).strip())
                    value = float(item.get("score", 0))
                    score_breakdown_row.controls.append(
                        ft.Container(
                            col={"xs": 6, "sm": 3},
                            bgcolor=SURFACE_INSET,
                            border_radius=8,
                            padding=ft.padding.symmetric(horizontal=12, vertical=10),
                            content=ft.Column(
                                [
                                    _t(name, size=12, color=TEXT_META),
                                    _t(f"{value:.0f}", size=21, color=TEXT_PRIMARY,
                                       weight=ft.FontWeight.W_600, height=1.19),
                                ],
                                spacing=2,
                            ),
                        )
                    )

                append_log(f"成绩单加载成功，累计基础分: {total_score:.0f}")

        except Exception as exc:
            score_total_text.value = "--"
            score_total_text.color = TEXT_TERTIARY
            append_log(f"[错误] 分数解析异常: {exc}")

        page.update()
        if catalog_loaded:
            render_activity_cards(courses_data)

    threading.Thread(target=load_user_info, daemon=True).start()
    threading.Thread(target=load_scores, daemon=True).start()
    threading.Thread(
        target=do_refresh,
        kwargs={"trigger_label": "启动刷新", "detect_new": True},
        daemon=True,
    ).start()
    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    if is_android:
        threading.Thread(target=mobile_result_loop, daemon=True).start()

    append_log("第二课堂自动抢课助手已启动。")
    append_log(f"自动巡检已启用，每 {AUTO_REFRESH_INTERVAL_SECONDS // 60} 分钟刷新一次。")
    append_log(autostart_message)
    if watched_course_ids:
        append_log(f"已恢复 {len(watched_course_ids)} 个监控活动，刷新后会继续守候。")
    if launched_from_autostart:
        page.window.minimized = True
        append_log("当前为开机自启动模式，窗口已最小化。")


# ──────────────────────────── Entry Point ──────────────────────────

if __name__ == "__main__":
    ft.app(target=main)
