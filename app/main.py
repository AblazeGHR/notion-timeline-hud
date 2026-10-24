"""Timeline HUD 桌面壳。

- 无边框半透明置顶窗口（WebView2 渲染 ui/index.html）
- 全局热键（默认 Ctrl+Alt+Space，可在设置里改）唤出/收起，Ctrl+Alt+Q 退出
- 失焦自动隐藏（可关）
- DWM 亚克力（Windows 11 的模糊背景）
- 设置项落 app/config.json；隐藏后按配置延迟修剪工作集
- 开机自启开关：python app/main.py --autostart on|off

运行：E:/software/miniforge/python.exe app/main.py
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import queue
import sys
import threading
import time
import webbrowser
from datetime import datetime
from collections.abc import Mapping
from ctypes import wintypes
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any

import webview

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from data import DATA_SOURCE_ID, TRACK_DUE, TRACK_SCHED, load, to_ui  # noqa: E402
from quickadd import parse as parse_quick_add  # noqa: E402
from notion_timeline.api_client import NotionClient  # noqa: E402
from notion_timeline.cache_reader import read_tasks  # noqa: E402
from notion_timeline.dates import shift  # noqa: E402
from notion_timeline.models import DateValue, PropertyValue  # noqa: E402

WIDTH = 460          # 宽度固定；高度按屏幕算（整屏高）
APP_NAME = "TimelineHUD"
TZ = "Asia/Shanghai"

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_SPACE = 0x20
VK_Q = 0x51
GA_ROOT = 2
PM_REMOVE = 0x0001
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2
HWND_TOPMOST = -1
SWP_SHOWWINDOW = 0x0040
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_TRANSIENTWINDOW = 3
ERROR_ALREADY_EXISTS = 183
MUTEX_NAMES = (r"Global\TimelineHUD_v1", r"Local\TimelineHUD_v1")
PIPE_NAME = r"\\.\pipe\TimelineHUD_v1_activate"

dwmapi = ctypes.windll.dwmapi


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


class MARGINS(ctypes.Structure):
    _fields_ = [
        ("cLeft", ctypes.c_int),
        ("cRight", ctypes.c_int),
        ("cTop", ctypes.c_int),
        ("cBottom", ctypes.c_int),
    ]
HOTKEY_ID_TOGGLE = 1
HOTKEY_ID_QUIT = 2

user32 = ctypes.windll.user32
_mutex_handle: int | None = None
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = ctypes.c_bool
_set_window_pos = user32.SetWindowPos
try:
    _set_window_pos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    _set_window_pos.restype = ctypes.c_bool
except Exception:  # noqa: BLE001
    pass

_send_message = user32.SendMessageW
try:  # 64 位下 hwnd 不能按默认 32 位 int 传，否则会被截断
    _send_message.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                              ctypes.c_void_p, ctypes.c_void_p]
    _send_message.restype = ctypes.c_void_p
except Exception:  # noqa: BLE001
    pass

state: dict[str, Any] = {
    "visible": True,
    "shown_at": time.time(),
    "hwnd": 0,
    "armed": False,
    "config": app_config.load(),
}


class _HotkeyRequest:
    """改键请求。

    RegisterHotKey(hwnd=NULL) 把热键绑在**调用线程**的消息队列上，所以只能在
    hotkey_loop 那个线程里重新注册；JS 线程只投递请求并等结果。
    """

    __slots__ = ("mods", "vk", "done", "ok")

    def __init__(self, mods: int, vk: int) -> None:
        self.mods = mods
        self.vk = vk
        self.done = threading.Event()
        self.ok = False


_hotkey_requests: queue.Queue[_HotkeyRequest] = queue.Queue()


def _register_toggle_hotkey(mods: int, vk: int) -> bool:
    user32.UnregisterHotKey(None, HOTKEY_ID_TOGGLE)
    return bool(user32.RegisterHotKey(None, HOTKEY_ID_TOGGLE, mods | MOD_NOREPEAT, vk))


def _register_with_fallback(mods: int, vk: int, label: str) -> None:
    """启动时注册唤出键；被占用就退回 Ctrl+Alt+Space，别让面板彻底唤不出来。"""
    if _register_toggle_hotkey(mods, vk):
        return
    print(f"热键 {label} 注册失败（可能被占用）", flush=True)
    fallback_mods, fallback_vk = MOD_CONTROL | MOD_ALT, VK_SPACE
    if (mods, vk) != (fallback_mods, fallback_vk) and _register_toggle_hotkey(
        fallback_mods, fallback_vk
    ):
        print("已回落到 Ctrl+Alt+Space", flush=True)


# ---------------------------------------------------------------- 透明度 / 模糊


class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_int),
    ]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


def enable_transparency(hwnd: int, form: object) -> str:
    """让窗口真正透出桌面（之前面板底下垫的是 WinForms 的 #F0F0F0）。

    实测（2026-10-24）：BrowserForm.BackColor = Control(#F0F0F0)、TransparencyKey 未设、
    AllowTransparency=False、窗口没有 WS_EX_LAYERED —— 所以 WebView2 即使设了
    DefaultBackgroundColor=Transparent，透明处也只能透出 Form 底色，白/黑壁纸截图逐字节相同。
    修法两条：
      1) Form 底色改黑，别让 #F0F0F0 当背板；
      2) Win11 系统背景（DWMSBT_TRANSIENTWINDOW）+ 扩展边框，DWM 才会在客户区画亚克力背板。
    """
    notes: list[str] = []
    try:
        from System.Drawing import Color

        form.BackColor = Color.Black
        notes.append("BackColor=Black")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"BackColor 失败: {exc}")

    try:
        value = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
        hr = dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(value), ctypes.sizeof(value),
        )
        margins = MARGINS(-1, -1, -1, -1)
        hr2 = dwmapi.DwmExtendFrameIntoClientArea(ctypes.c_void_p(hwnd), ctypes.byref(margins))
        notes.append(f"backdrop hr={hr}/{hr2}")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"backdrop 失败: {exc}")
    return ", ".join(notes)


def tint_color(alpha: int) -> int:
    """AABBGGRR：alpha 是 tint 浓度，RGB 固定为 0x1A1B22。"""
    return ((alpha & 0xFF) << 24) | 0x1A1B22


def apply_acrylic(hwnd: int, alpha: int = 0x60) -> bool:
    """4 = ACCENT_ENABLE_ACRYLICBLURBEHIND，颜色按 AABBGGRR。

    默认 alpha 0x60（37.5%）对应面板 opacity 0.82；滑杆会按比例改它。
    """
    accent = ACCENTPOLICY(4, 0, tint_color(alpha), 0)
    data = WINCOMPATTRDATA(
        19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent)
    )
    try:
        return bool(user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 后台刷新


def data_digest(data: dict) -> str:
    """标题/状态/日期（外加 tag/方向/学科）的指纹，用来判断要不要重渲染。"""
    tasks = data.get("tasks") or []
    rows = [
        (t.get("id"), t.get("title"), t.get("status"), t.get("sched"), t.get("due"),
         bool(t.get("allDaySched")), bool(t.get("allDayDue")),
         t.get("tag"), t.get("dir"), t.get("subject"))
        for t in tasks
    ]
    rows.sort(key=lambda row: str(row[0]))
    undated = [(u.get("title"), u.get("status"), u.get("tag")) for u in data.get("undated") or []]
    raw = json.dumps([rows, undated], ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def refresh_loop(window: webview.Window, initial_digest: str) -> None:
    """每 refresh_seconds 秒静默拉一次；没变化不动界面，失败只记日志。"""
    last = initial_digest
    while True:
        seconds = int(state["config"].get("refresh_seconds", 120) or 0)
        time.sleep(max(5, seconds) if seconds > 0 else 30)
        if seconds <= 0:
            continue
        try:
            data, source, error = load()
        except Exception as exc:  # noqa: BLE001
            print("后台刷新失败:", exc, flush=True)
            continue
        digest = data_digest(data)
        if digest == last:
            print(f"后台刷新：无变化（source={source}）", flush=True)
            continue
        last = digest
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        status = json.dumps({"source": source, "error": (error or "")[:200]}, ensure_ascii=False)
        try:
            window.evaluate_js(
                f"window.__hud && window.__hud.setData({payload});"
                f"window.__hud && window.__hud.setStatus({status});"
            )
            print(f"后台刷新：已重渲染（source={source}）", flush=True)
        except Exception as exc:  # noqa: BLE001
            print("后台刷新推送失败:", exc, flush=True)


# ---------------------------------------------------------------- 卡片改时长


def _parse_point(value: Any, label: str) -> tuple[str, str | None] | None:
    """{"date": "YYYY-MM-DD", "time": "15:00"} → (date, time)；time 可为空。"""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}格式不对")
    date = str(value.get("date") or "").strip()
    clock = str(value.get("time") or "").strip()
    if not date:
        raise ValueError(f"{label}缺少日期")
    try:
        datetime.fromisoformat(date + ("T" + clock if clock else "T00:00"))
    except ValueError as exc:
        raise ValueError(f"{label}格式不对（{date} {clock}）") from exc
    if clock and len(clock) > 5:
        clock = clock[:5]        # HH:MM:SS -> HH:MM（Notion 里存到分钟就够）
    return date, (clock or None)


# ---------------------------------------------------------------- 块编辑

_EDITABLE_BLOCK_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "quote", "callout", "code", "toggle",
    "to_do",     # 只改 rich_text，不动 checked
}


def _block_error(exc: Exception) -> str:
    """把 HTTP 错误翻成能看的话。"""
    message = str(exc)
    if "HTTP 403" in message:
        return "没有权限编辑这一块"
    if "HTTP 404" in message:
        return "块不存在或已被删除"
    if "HTTP 429" in message:
        return "请求太频繁，稍后再试"
    return message


def _text_payload(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": str(text)}}]


# ---------------------------------------------------------------- 页面内容

_BLOCK_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do",
    "quote", "code", "divider", "callout", "toggle",
}
_PAGE_CACHE_TTL = 60.0
_page_content_cache: dict[str, tuple[float, dict]] = {}


def _plain_text(rich: Any) -> str:
    parts = []
    for item in rich or []:
        if not isinstance(item, dict):
            continue
        value = item.get("plain_text")
        if value is None:
            value = (item.get("text") or {}).get("content", "")
        parts.append(str(value))
    return "".join(parts).strip()


def _map_block(raw: dict, level: int) -> dict | None:
    btype = raw.get("type")
    if btype not in _BLOCK_TYPES:
        return None
    data = raw.get(btype) or {}
    text = _plain_text(data.get("rich_text"))
    return {
        "id": raw.get("id"),
        "type": btype,
        "text": text,
        "checked": bool(data.get("checked")) if btype == "to_do" else False,
        "lang": str(data.get("language") or "") if btype == "code" else "",
        "level": level,
    }


def _collect_children(client, block_id: str, level: int, out: list, counter: dict, depth: int) -> None:
    """递归取子块：最多 2 层、子块总数 ≤ 120。"""
    if depth > 2 or counter["children"] >= 120:
        return
    cursor = None
    while True:
        data = client.get_block_children(block_id, 100, cursor)
        for raw in data.get("results") or []:
            if counter["children"] >= 120:
                counter["truncated"] = True
                return
            counter["children"] += 1
            mapped = _map_block(raw, level)
            if mapped:
                out.append(mapped)
            if raw.get("has_children"):
                _collect_children(client, raw["id"], level + 1, out, counter, depth + 1)
        if not data.get("has_more"):
            return
        cursor = data.get("next_cursor")
        if not cursor:
            return


def fetch_page_blocks(client, page_id: str, max_top: int = 300) -> tuple[list, bool]:
    """顶层最多 300 块，子块最多 120 块（2 层）。"""
    blocks: list = []
    counter = {"children": 0, "truncated": False}
    cursor = None
    while True:
        data = client.get_block_children(page_id, 100, cursor)
        for raw in data.get("results") or []:
            if len(blocks) >= max_top:
                counter["truncated"] = True
                break
            mapped = _map_block(raw, 1)
            if mapped:
                blocks.append(mapped)
            if raw.get("has_children"):
                _collect_children(client, raw["id"], 2, blocks, counter, 1)
        if counter["truncated"] or not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return blocks, counter["truncated"]


# ---------------------------------------------------------------- 单实例


def acquire_single_instance() -> bool:
    r"""拿命名互斥体。True=我是首个实例；False=已有实例在跑。

    ``Global\`` 需要 SeCreateGlobalPrivilege，普通用户可能没有，拿不到就退到 ``Local\``。
    """
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    for name in MUTEX_NAMES:
        handle = kernel32.CreateMutexW(None, True, name)
        last_error = kernel32.GetLastError()   # ctypes.windll 不维护 last_error，得直接问
        if not handle:
            print(f"单实例：创建 {name} 失败（err={last_error}）", flush=True)
            continue
        if last_error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            print(f"单实例：{name} 已存在", flush=True)
            return False
        _mutex_handle = handle
        print(f"单实例：持有 {name}", flush=True)
        return True
    print("单实例：互斥体创建失败，按首个实例继续", flush=True)
    return True


def notify_existing_instance(timeout: float = 3.0) -> bool:
    """让已经在跑的那个实例把窗口唤出来（管道可能还没起，重试到超时）。"""
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with Client(PIPE_NAME, family="AF_PIPE") as conn:
                conn.send("show")
                if not conn.poll(2.0):
                    raise TimeoutError("等回执超时")
            print("单实例：已通知现有实例唤出窗口", flush=True)
            return True
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.2)
    print(f"单实例：通知现有实例失败（{last_error}），仍然退出", flush=True)
    return False


def start_activation_server(window: webview.Window) -> None:
    """后台监听激活请求（命名管道）。"""

    def serve() -> None:
        while True:
            try:
                with Listener(PIPE_NAME, family="AF_PIPE") as listener:
                    while True:
                        try:
                            conn = listener.accept()
                        except (EOFError, OSError):
                            break
                        try:
                            if conn.poll(2.0):
                                conn.recv()
                            conn.send("ok")     # 先回执，别让对端等 show 卡住
                            print("激活：收到唤出请求", flush=True)
                            show_window(window)
                        except Exception as exc:  # noqa: BLE001
                            print("激活处理失败:", exc, flush=True)
                        finally:
                            conn.close()
            except Exception as exc:  # noqa: BLE001 - 端口占用等情况，等一会重试
                print("激活监听异常，1s 后重试:", exc, flush=True)
                time.sleep(1.0)

    threading.Thread(target=serve, daemon=True).start()


# ---------------------------------------------------------------- 内存修剪

_trim_lock = threading.Lock()
_trim_timer: threading.Timer | None = None


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
_get_process_memory_info.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
    ctypes.c_ulong,
]
_get_process_memory_info.restype = ctypes.c_bool
ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
_set_working_set = ctypes.windll.kernel32.SetProcessWorkingSetSize
_set_working_set.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
_set_working_set.restype = ctypes.c_bool
_empty_working_set = ctypes.windll.psapi.EmptyWorkingSet
_empty_working_set.argtypes = [ctypes.c_void_p]
_empty_working_set.restype = ctypes.c_bool


def working_set_mb() -> float:
    """当前进程的工作集（MB），用于验证修剪效果。"""
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not _get_process_memory_info(handle, ctypes.byref(counters), counters.cb):
            return 0.0
        return round(counters.WorkingSetSize / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001
        return 0.0


def trim_memory() -> bool:
    """SetProcessWorkingSetSize(-1,-1) + EmptyWorkingSet，把工作集还给系统。"""
    before = working_set_mb()
    try:
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        unlimited = ctypes.c_size_t(0xFFFFFFFFFFFFFFFF)  # (SIZE_T)-1
        _set_working_set(handle, unlimited, unlimited)
        if not _empty_working_set(handle):
            ctypes.windll.kernel32.K32EmptyWorkingSet(handle)
        after = working_set_mb()
        print(f"内存修剪: {before}MB -> {after}MB", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print("内存修剪失败:", exc, flush=True)
        return False


def schedule_memory_trim() -> None:
    """隐藏后按 release_memory_minutes 延迟修剪；重新唤出会取消。"""
    global _trim_timer
    minutes = int(state["config"].get("release_memory_minutes", 0) or 0)
    with _trim_lock:
        if _trim_timer is not None:
            _trim_timer.cancel()
            _trim_timer = None
        if minutes <= 0:
            return
        timer = threading.Timer(minutes * 60, trim_memory)
        timer.daemon = True
        timer.start()
        _trim_timer = timer


def cancel_memory_trim() -> None:
    global _trim_timer
    with _trim_lock:
        if _trim_timer is not None:
            _trim_timer.cancel()
            _trim_timer = None


def monitor_rect(point: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    """pt 所在显示器的 rcMonitor（物理像素）；point 为空则用鼠标位置。"""
    pt = wintypes.POINT(0, 0)
    if point is None:
        user32.GetCursorPos(ctypes.byref(pt))
    else:
        pt.x, pt.y = point
    hmon = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not hmon or not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    r = info.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def list_monitors() -> list[str]:
    """日志用：列出所有显示器（纯 Win32，不依赖 pythonnet）。"""
    items: list[str] = []
    proc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(wintypes.RECT), ctypes.c_double,
    )

    def each(hmon, _hdc, _rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            tag = "主" if info.dwFlags & 1 else "从"
            items.append(f"[{tag}] {r.right - r.left}x{r.bottom - r.top} @({r.left},{r.top})")
        return True

    user32.EnumDisplayMonitors(None, None, proc(each), 0)
    return items or ["<未枚举到显示器>"]


def fit_to_monitor(hwnd: int, point: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    """贴到 point（默认鼠标）所在显示器的右缘、整屏高。"""
    left, top, right, bottom = monitor_rect(point)
    rect = wintypes.RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    width = max(320, rect.right - rect.left)
    x = max(left, right - width)
    _set_window_pos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST), x, top,
                    width, bottom - top, SWP_SHOWWINDOW)
    return x, top, x + width, bottom


def fit_to_screen(hwnd: int, *, show: bool = True) -> tuple[int, int, int, int]:
    """按物理像素把窗口钉到右边缘、整屏高。

    pywebview 的 x/y/width/height 会各自再乘一次 DPI，还对高度做了工作区裁剪，
    算不准；这里直接用 SetWindowPos 覆盖成实测值，宽度沿用创建后的实际宽度。
    show=False 时只改位置尺寸、不激活也不显示（隐藏启动用）。
    """
    rect = wintypes.RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    width = max(320, rect.right - rect.left)
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    left = max(0, screen_w - width)
    flags = 0x0010 | (SWP_SHOWWINDOW if show else 0)   # 0x10 = SWP_NOACTIVATE
    _set_window_pos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST), left, 0,
                    width, screen_h, flags)
    return left, 0, left + width, screen_h


# ---------------------------------------------------------------- 窗口动作


def reset_timeline_scale(window: webview.Window, *, offload: bool = False) -> None:
    """把 Ctrl+滚轮调出来的临时密度丢回配置默认值（必须在 hide 之前调）。

    offload=True 丢到后台线程：Api.hide 是从 JS 线程进来的，同步 evaluate_js
    会和该线程的消息循环互锁。
    """

    def run() -> None:
        try:
            window.evaluate_js("window.__hud && window.__hud.resetScale()")
        except Exception as exc:  # noqa: BLE001 - 窗口正在销毁时忽略
            print("重置密度失败（可忽略）：", exc, flush=True)

    if offload:
        threading.Thread(target=run, daemon=True).start()
    else:
        run()


def show_window(window: webview.Window) -> None:
    cancel_memory_trim()
    hwnd = state["hwnd"]
    if hwnd:  # 唤出时按鼠标所在显示器重新定位（多屏）
        try:
            left, top, right, bottom = fit_to_monitor(hwnd)
            print(f"唤出定位 -> ({left},{top},{right},{bottom})", flush=True)
        except Exception as exc:  # noqa: BLE001
            print("唤出定位失败:", exc, flush=True)
    window.show()
    if hwnd:
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        user32.SetForegroundWindow(hwnd)
    state["visible"] = True
    state["shown_at"] = time.time()


def toggle(window: webview.Window) -> None:
    try:
        if state["visible"]:
            reset_timeline_scale(window)
            window.hide()
            state["visible"] = False
            schedule_memory_trim()
        else:
            show_window(window)
    except Exception as exc:  # noqa: BLE001
        print("toggle failed:", exc, flush=True)


def hotkey_loop(window: webview.Window, mods: int, vk: int, label: str) -> None:
    _register_with_fallback(mods, vk, label)
    user32.RegisterHotKey(None, HOTKEY_ID_QUIT, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_Q)
    msg = wintypes.MSG()
    while True:
        # 改键请求必须在本线程执行（见 _HotkeyRequest 注释）
        while True:
            try:
                request = _hotkey_requests.get_nowait()
            except queue.Empty:
                break
            request.ok = _register_toggle_hotkey(request.mods, request.vk)
            request.done.set()

        if not user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            time.sleep(0.02)
            continue
        if msg.message == WM_HOTKEY:
            if msg.wParam == HOTKEY_ID_TOGGLE:
                toggle(window)
            elif msg.wParam == HOTKEY_ID_QUIT:
                window.destroy()
                break
        elif msg.message == WM_QUIT:
            break


def foreground_root() -> int:
    """当前前台窗口的顶层句柄（测试可替换）。"""
    fg = user32.GetForegroundWindow()
    return user32.GetAncestor(fg, GA_ROOT) if fg else 0


def blur_watch(window: webview.Window, stop: threading.Event | None = None) -> None:
    """失焦 700ms 后自动收起；hide_on_blur 关闭时完全不介入。

    stop 只给测试用（正常运行时线程跟随进程退出）。
    """
    while True:
        if stop is not None and stop.is_set():
            return
        time.sleep(0.25)
        if not state["visible"] or not state["hwnd"]:
            continue
        if not state["config"].get("hide_on_blur", False):
            state["armed"] = False
            continue
        if time.time() - state["shown_at"] < 1.2:
            continue
        root = foreground_root()
        if root == state["hwnd"]:
            state["armed"] = True
            continue
        if not state["armed"]:
            continue
        if root and root != state["hwnd"]:
            try:
                reset_timeline_scale(window)
                window.hide()
                state["visible"] = False
                schedule_memory_trim()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 开机自启


def get_autostart() -> bool | None:
    """读 HKCU Run 里有没有我们的自启项；读不到返回 None。"""
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(str(value).strip())
    except FileNotFoundError:
        return False
    except OSError:
        return None


def set_autostart(enable: bool) -> str:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            exe = Path(sys.executable)
            pythonw = exe.with_name("pythonw.exe")
            runner = pythonw if pythonw.exists() else exe
            command = f'"{runner}" "{ROOT / "main.py"}" --hidden'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
            return f"已写入自启：{command}"
        try:
            winreg.DeleteValue(key, APP_NAME)
            return "已移除自启"
        except FileNotFoundError:
            return "本来就没有自启项"


# ---------------------------------------------------------------- Notion 链接


def notion_protocol_registered() -> bool:
    """notion:// 有没有被注册（先看用户级，再看 HKCR）。读不到就当没注册。"""
    import winreg

    for root, path in (
        (winreg.HKEY_CURRENT_USER, r"Software\Classes\notion\shell\open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"notion\shell\open\command"),
    ):
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, "")
                if str(value).strip():
                    return True
        except OSError:
            continue
    return False


def build_notion_uri(
    page_id: str, prefer_app: bool = True, *, registered: bool | None = None
) -> str:
    """拼 Notion 链接：能拉起桌面端就用 notion://，否则用网页地址。

    registered 只给测试用（None 表示现场读注册表）。
    """
    clean = str(page_id).replace("-", "").strip()
    if not clean:
        raise ValueError("page_id 不能为空")
    use_app = prefer_app and (notion_protocol_registered() if registered is None else registered)
    scheme = "notion://" if use_app else "https://"
    return f"{scheme}www.notion.so/{clean}"


# ---------------------------------------------------------------- 状态选项

_status_option_cache: dict[str, list[str]] = {}
_STATUS_ALIASES = {
    "未开始": ("未开始", "待办", "Todo", "Not started"),
    "进行中": ("进行中", "In progress", "进行"),
    "已完成": ("已完成", "完成", "Done"),
}


def _status_options(client: NotionClient) -> list[str]:
    """读该库真实的状态选项（同一次运行只查一次）。"""
    cached = _status_option_cache.get(DATA_SOURCE_ID)
    if cached is not None:
        return cached
    options: list[str] = []
    try:
        data = client.get_data_source(DATA_SOURCE_ID)
        status = (data.get("properties") or {}).get("状态") or {}
        options = [str(o["name"]) for o in (status.get("status") or {}).get("options") or []]
    except Exception as exc:  # noqa: BLE001 - 查不到就按原值写，让 API 报错
        print("读取状态选项失败：", exc, flush=True)
    _status_option_cache[DATA_SOURCE_ID] = options
    return options


def _resolve_status(client: NotionClient, name: str) -> str:
    """界面写「已完成」、正式库叫「完成」时按库里的真实选项落。"""
    options = _status_options(client)
    if not options or name in options:
        return name
    for candidate in _STATUS_ALIASES.get(name, ()):
        if candidate in options:
            return candidate
    return name


# ---------------------------------------------------------------- 读当前日期


def _read_current_date(client: NotionClient, task_id: str, prop: str) -> DateValue | None:
    """取任务当前的日期值：单页 API → 本地缓存 → 全量 query 按 id 找。

    取不到就返回 None 让调用方报错，绝不猜（猜会把时间叠加到错误的值上）。
    """
    try:
        current = client.get_task(task_id).dates.get(prop)
        if current is not None:
            return current
    except Exception as exc:  # noqa: BLE001 - 网络/token 问题，走缓存兜底
        print("读取任务失败，改用本地缓存：", exc, flush=True)

    try:
        for task in read_tasks(collection_id=DATA_SOURCE_ID):
            if task.id == task_id:
                if prop in task.dates:
                    return task.dates[prop]
                break
    except Exception as exc:  # noqa: BLE001
        print("缓存读取失败：", exc, flush=True)

    try:  # 单页读到了但没这个属性，或缓存里也没有：全量查一遍按 id 找
        found = next(
            (t for t in client.query_tasks(data_source_id=DATA_SOURCE_ID) if t.id == task_id),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        print("按 id 查询任务失败：", exc, flush=True)
        return None
    return found.dates.get(prop) if found is not None else None


# ---------------------------------------------------------------- JS 桥


class Api:
    def __init__(self) -> None:
        self._window: webview.Window | None = None

    def hide(self) -> None:
        if self._window:
            reset_timeline_scale(self._window, offload=True)  # 从 JS 线程进来，别同步等
            self._window.hide()
            state["visible"] = False
            schedule_memory_trim()

    def set_panel_opacity(self, value: float) -> dict:
        """面板透明度滑块 → 重算亚克力 tint（0x18~0x90），重新应用合成属性。"""
        try:
            opacity = float(value)
        except (TypeError, ValueError):
            return {"ok": False, "error": "opacity 必须是数字"}
        opacity = max(0.15, min(0.95, opacity))
        alpha = max(0x18, min(0x90, int(0x60 * opacity / 0.82)))
        hwnd = state["hwnd"]
        if not hwnd:
            return {"ok": True, "alpha": alpha, "applied": False}
        ok = apply_acrylic(hwnd, alpha)
        print(f"亚克力 tint -> 0x{alpha:02X}（opacity {opacity:.2f}）", flush=True)
        return {"ok": ok, "alpha": alpha, "applied": True}

    def start_drag(self) -> None:
        """拖动窗口：ReleaseCapture 后把“标题栏按下”转给系统，由 Windows 接管移动。"""
        hwnd = state["hwnd"]
        if not hwnd:
            return
        try:
            user32.ReleaseCapture()
            _send_message(ctypes.c_void_p(hwnd), WM_NCLBUTTONDOWN,
                          ctypes.c_void_p(HTCAPTION), ctypes.c_void_p(0))
        except Exception as exc:  # noqa: BLE001
            print("窗口拖动失败:", exc, flush=True)

    def quit(self) -> None:
        if self._window:
            self._window.destroy()

    # ---- 设置项 ----

    def get_config(self) -> dict:
        return dict(state["config"])

    def set_config(self, changes: dict | None = None) -> dict:
        """落盘 + 即时应用。样式类失败不回滚（可重试），热键注册失败回滚。"""
        if not isinstance(changes, dict):
            return {"ok": False, "error": "参数必须是对象"}
        previous = dict(state["config"])
        try:
            updated = app_config.patch(changes)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "config": previous}

        try:
            # 先把最可能失败的（热键注册）做掉，再改纯样式项，失败时视觉不受影响
            if "hotkey" in changes:
                mods, vk = app_config.parse_hotkey(updated["hotkey"])
                if not self.apply_hotkey(mods, vk):
                    app_config.save(previous)  # 旧键仍然有效，配置也退回去
                    return {
                        "ok": False,
                        "error": "热键注册失败（可能被别的程序占用）",
                        "config": previous,
                    }
            if "opacity" in changes:
                self.apply_opacity(updated["opacity"])
            if "theme" in changes:
                self.apply_theme(updated["theme"])
        except Exception as exc:  # noqa: BLE001
            app_config.save(previous)
            return {"ok": False, "error": str(exc), "config": previous}

        state["config"] = updated
        if "release_memory_minutes" in changes and not state["visible"]:
            schedule_memory_trim()
        return {"ok": True, "config": updated}

    def apply_visuals(self) -> dict:
        """窗口加载完刷一遍透明度 / 主题（亚克力 tint 也跟着配置走）。"""
        config = state["config"]
        self.apply_opacity(config["opacity"])
        self.apply_theme(config["theme"])
        self.set_panel_opacity(config["opacity"])
        return dict(config)

    def apply_opacity(self, opacity: float) -> None:
        if not self._window:
            return
        alpha = max(0.15, min(1.0, float(opacity)))
        self._window.evaluate_js(
            f"document.documentElement.style.setProperty('--panel-alpha','{alpha:.3f}')"
        )

    def apply_theme(self, theme: str) -> None:
        if not self._window:
            return
        flag = "true" if theme == "light" else "false"
        self._window.evaluate_js(f"document.body.classList.toggle('theme-light', {flag})")

    def apply_hotkey(self, mods: int, vk: int) -> bool:
        request = _HotkeyRequest(mods, vk)
        _hotkey_requests.put(request)
        if not request.done.wait(2.0):
            return False
        return request.ok

    def release_memory(self) -> dict:
        return {"ok": trim_memory()}

    def get_autostart(self) -> dict:
        """让开关反映注册表真实状态（可能被别处改过）。"""
        return {"ok": True, "enabled": get_autostart()}

    def set_autostart(self, enabled: bool) -> dict:
        """写 HKCU Run（pythonw 启动，无控制台）；返回值以注册表回读为准。"""
        try:
            message = set_autostart(bool(enabled))
            actual = get_autostart()          # 回读，避免"以为写成功"
            updated = app_config.patch({"autostart": bool(actual)})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        state["config"] = updated
        return {"ok": True, "message": message, "enabled": actual, "config": updated}

    def reload_data(self) -> dict:
        data, source, error = load()
        return {"data": data, "source": source, "error": error}

    def open_task(self, task_id: str) -> dict:
        """卡片 ↗ 和详情里的按钮都走这里：优先桌面端，失败回退网页。"""
        prefer_app = bool(state["config"].get("open_in_app", True))
        uri = build_notion_uri(task_id, prefer_app)
        if uri.startswith("notion://"):
            try:
                os.startfile(uri)  # 交给协议处理器（Notion.exe）
                print(f"open_task: 桌面端 {uri}", flush=True)
                return {"ok": True, "via": "app", "uri": uri}
            except Exception as exc:  # noqa: BLE001 - 没装/被拦都回退网页
                print(f"open_task: 桌面端失败（{exc}），回退网页", flush=True)
        web_url = build_notion_uri(task_id, False)
        webbrowser.open(web_url)
        print(f"open_task: 网页 {web_url}", flush=True)
        return {"ok": True, "via": "web", "uri": web_url}

    # ---- 写入（全部走 API，只动 DATA_SOURCE_ID 指向的库） ----

    def create_from_text(self, text: str, prop: str = TRACK_DUE) -> dict:
        """快速新增。prop 决定日期落在哪条轨道（安排日期 / 截止日期）。"""
        title, day, clock = parse_quick_add(text)
        if not title:
            return {"ok": False, "error": "只有时间，没有标题"}
        if prop not in (TRACK_SCHED, TRACK_DUE):
            prop = TRACK_DUE
        try:
            with NotionClient() as client:
                task = client.create_task(
                    title,
                    dates={prop: DateValue(day, start_time=clock, time_zone=TZ)},
                    data_source_id=DATA_SOURCE_ID,
                )
            ui = to_ui([task])
            if not ui["tasks"]:
                return {"ok": False, "error": "创建成功但没有日期，已进待安排"}
            return {"ok": True, "task": ui["tasks"][0]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def cycle_status(self, task_id: str, current: str) -> dict:
        order = ["todo", "doing", "done"]
        nxt = order[(order.index(current) + 1) % len(order)] if current in order else "doing"
        name = {"todo": "未开始", "doing": "进行中", "done": "已完成"}[nxt]
        try:
            with NotionClient() as client:
                real = _resolve_status(client, name)
                client.update_task(task_id, properties={"状态": PropertyValue("status", real)})
            return {"ok": True, "status": nxt}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def resize_task(
        self, task_id: str, prop: str, start: dict | None, end: dict | None
    ) -> dict:
        """拖卡片上下沿：改起止时间（prop 只能是 安排日期 / 截止日期）。"""
        if prop not in (TRACK_SCHED, TRACK_DUE):
            return {"ok": False, "error": f"不支持的日期属性：{prop}"}
        try:
            start_point = _parse_point(start, "开始时间")
            end_point = _parse_point(end, "结束时间")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if start_point is None:
            return {"ok": False, "error": "缺少开始时间"}

        start_dt = datetime.fromisoformat(start_point[0] + "T" + (start_point[1] or "00:00"))
        if end_point is not None:
            end_dt = datetime.fromisoformat(end_point[0] + "T" + (end_point[1] or "00:00"))
            if end_dt <= start_dt:
                return {"ok": False, "error": "结束时间必须晚于开始时间"}

        value = DateValue(
            start=start_point[0],
            end=end_point[0] if end_point else None,
            start_time=start_point[1],
            end_time=end_point[1] if end_point else None,
            time_zone=TZ,
        )
        try:
            with NotionClient() as client:
                updated = client.update_task(task_id, dates={prop: value})
            ui = to_ui([updated])
            if not ui["tasks"]:
                return {"ok": False, "error": "改时长后任务没有日期"}
            return {"ok": True, "task": ui["tasks"][0], "date": value.to_dict()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def update_block_text(self, block_id: str, text: str) -> dict:
        """改一个文本块的文字（rich_text 会合并成单 run）。"""
        try:
            with NotionClient() as client:
                block = client.get_block(block_id)
                btype = str(block.get("type") or "")
                if btype not in _EDITABLE_BLOCK_TYPES:
                    return {"ok": False, "error": f"{btype} 块不支持改文字"}
                client.update_block(block_id, {btype: {"rich_text": _text_payload(text)}})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _block_error(exc)}
        _page_content_cache.clear()
        return {"ok": True}

    def set_todo_checked(self, block_id: str, checked: bool) -> dict:
        try:
            with NotionClient() as client:
                client.update_block(block_id, {"to_do": {"checked": bool(checked)}})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _block_error(exc)}
        _page_content_cache.clear()
        return {"ok": True, "checked": bool(checked)}

    def append_paragraph(self, page_id: str, text: str = "") -> dict:
        try:
            with NotionClient() as client:
                data = client.append_children(page_id, [{
                    "object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": _text_payload(text)},
                }])
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _block_error(exc)}
        _page_content_cache.clear()
        results = data.get("results") or [{}]
        return {"ok": True, "block_id": results[0].get("id"), "text": text}

    def delete_block(self, block_id: str) -> dict:
        """DELETE 只是移入回收站（Notion 没有硬删除）。"""
        try:
            with NotionClient() as client:
                client.delete_task(block_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": _block_error(exc)}
        _page_content_cache.clear()
        return {"ok": True}

    def get_page_content(self, page_id: str) -> dict:
        """详情弹窗「内容」页：只读拉取该页的块（60s 内复用）。"""
        key = str(page_id)
        cached = _page_content_cache.get(key)
        if cached and time.time() - cached[0] < _PAGE_CACHE_TTL:
            payload = dict(cached[1])
            payload["cached"] = True
            return payload
        try:
            with NotionClient() as client:
                blocks, truncated = fetch_page_blocks(client, key)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "HTTP 404" in message:
                message = "页面不存在或已被删除"
            elif "HTTP 403" in message:
                message = "没有权限访问这一页"
            return {"ok": False, "blocks": [], "truncated": False, "error": message}
        payload = {"ok": True, "blocks": blocks, "truncated": truncated, "error": None}
        _page_content_cache[key] = (time.time(), payload)
        return dict(payload)

    def update_task_fields(self, task_id: str, patch: dict | None = None) -> dict:
        """详情弹窗的自动保存：只提交 patch 里出现的字段。"""
        if not isinstance(patch, dict):
            return {"ok": False, "error": "patch 必须是对象"}

        title = patch.get("title")
        if title is not None:
            title = str(title).strip()
            if not title:
                return {"ok": False, "error": "标题不能为空"}

        status = patch.get("status")
        if status is not None and status not in _STATUS_ALIASES:
            return {"ok": False, "error": f"不支持的状态：{status}"}

        dates: dict[str, DateValue] = {}
        for name, value in (patch.get("dates") or {}).items():
            if name not in (TRACK_SCHED, TRACK_DUE):
                return {"ok": False, "error": f"不支持的日期属性：{name}"}
            if not isinstance(value, dict) or not value.get("start"):
                return {"ok": False, "error": f"「{name}」缺少 start"}
            dates[name] = DateValue(
                start=str(value["start"]),
                end=str(value["end"]) if value.get("end") else None,
                start_time=str(value["start_time"]) if value.get("start_time") else None,
                end_time=str(value["end_time"]) if value.get("end_time") else None,
                time_zone=str(value.get("time_zone") or TZ),
            )

        properties: dict[str, PropertyValue] = {}
        for name, value in (patch.get("properties") or {}).items():
            if not isinstance(value, dict) or "type" not in value:
                return {"ok": False, "error": f"属性「{name}」格式不对"}
            properties[name] = PropertyValue(str(value["type"]), value.get("value"))

        if title is None and status is None and not dates and not properties:
            return {"ok": False, "error": "没有要改的字段"}

        try:
            with NotionClient() as client:
                if status is not None:
                    properties["状态"] = PropertyValue("status", _resolve_status(client, status))
                updated = client.update_task(
                    task_id,
                    title=title,
                    dates=dates or None,
                    properties=properties or None,
                )
            ui = to_ui([updated])
            if not ui["tasks"]:
                return {"ok": False, "error": "保存后任务没有日期"}
            return {"ok": True, "task": ui["tasks"][0]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def delete_task(self, task_id: str) -> dict:
        try:
            with NotionClient() as client:
                client.delete_task(task_id)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def reschedule(self, task_id: str, prop: str, delta_minutes: int) -> dict:
        """拖拽改期：读当前值 → shift → 写回，返回 UI 形状的新任务。"""
        try:
            delta = int(round(float(delta_minutes)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "delta_minutes 必须是数字"}
        if prop not in (TRACK_SCHED, TRACK_DUE):
            return {"ok": False, "error": f"不支持的日期属性：{prop}"}
        if delta == 0:
            return {"ok": False, "error": "没有移动"}
        try:
            with NotionClient() as client:
                current = _read_current_date(client, task_id, prop)
                if current is None:
                    return {"ok": False, "error": f"任务没有「{prop}」"}
                moved = shift(current, delta)
                updated = client.update_task(task_id, dates={prop: moved})
            ui = to_ui([updated])
            if not ui["tasks"]:
                return {"ok": False, "error": "改期后任务没有日期"}
            return {"ok": True, "task": ui["tasks"][0], "prop": prop, "date": moved.to_dict()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- 入口


def screen_size() -> tuple[int, int]:
    """整屏尺寸（不是工作区）：面板要上下贴边、盖住任务栏那一行。

    优先用 WinForms，保证和 pywebview 的坐标单位一致。
    """
    try:
        from System.Windows.Forms import Screen

        area = Screen.PrimaryScreen.Bounds
        return int(area.Width), int(area.Height)
    except Exception:  # noqa: BLE001
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def render_html() -> str:
    template = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    data, source, error = load()
    print(f"数据来源: {source} | 任务 {len(data['tasks'])} 条 | 待安排 {len(data['undated'])} 条", flush=True)
    if error:
        print("回落原因:", error[:200], flush=True)
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    config_payload = json.dumps(state["config"], ensure_ascii=False).replace("</", "<\\/")
    state["data_digest"] = data_digest(data)
    state["data_source"] = source
    status_payload = json.dumps({"source": source, "error": (error or "")[:200]}, ensure_ascii=False)
    html = template.replace("window.__DATA__ = null;", f"window.__DATA__ = {payload};")
    html = html.replace("window.__STATUS__ = null;", f"window.__STATUS__ = {status_payload};")
    # 密度在首帧就用配置值渲染，避免加载完再跳一下
    return html.replace("window.__CFG__ = null;", f"window.__CFG__ = {config_payload};")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--autostart", choices=("on", "off"))
    parser.add_argument("--no-blur", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--hidden", action="store_true", help="启动时不显示窗口（自启用）")
    args = parser.parse_args()
    state["visible"] = not args.hidden   # 隐藏启动时，第一次唤出应该是"显示"而不是"隐藏"

    if args.autostart:
        print(set_autostart(args.autostart == "on"))
        return

    if not acquire_single_instance():
        notify_existing_instance()
        print(f"已有实例在运行，本进程退出（pid={os.getpid()}）", flush=True)
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass

    screen_w, screen_h = screen_size()
    # pywebview 的宽高是逻辑像素，系统 DPI 会再放大；这里按缩放比换算，
    # 否则窗口会被推到屏幕右外侧（2026-10-24 实测 175% 缩放时踩过）。
    try:
        scale = user32.GetDpiForSystem() / 96.0
    except Exception:  # noqa: BLE001
        scale = 1.0
    logical_w, logical_h = screen_w / scale, screen_h / scale
    # 右侧贴边 + 上下贴边：宽固定，高铺满整屏
    x = round(logical_w - WIDTH)
    y = 0
    height = round(logical_h)

    api = Api()
    window = webview.create_window(
        "Timeline HUD",
        html=render_html(),
        js_api=api,
        width=WIDTH,
        height=height,
        x=x,
        y=y,
        frameless=True,
        transparent=True,
        on_top=True,
        resizable=False,
        shadow=False,
        easy_drag=False,
        hidden=args.hidden,
    )
    api._window = window
    start_activation_server(window)

    def on_loaded() -> None:
        time.sleep(0.15)
        try:
            hwnd = window.native.Handle.ToInt64()
            state["hwnd"] = hwnd
            left, top, right, bottom = fit_to_screen(hwnd, show=not args.hidden)
            print(f"fit_to_screen -> ({left},{top},{right},{bottom})", flush=True)
            print(f"显示器: {list_monitors()}", flush=True)
            ok = apply_acrylic(hwnd) if not args.no_blur else False
            # 顺序要紧：DWM 系统背景必须最后设，否则会被 SetWindowCompositionAttribute 覆盖
            transparency = enable_transparency(hwnd, window.native)
            print(f"transparency -> {transparency}", flush=True)
            print(f"hwnd={hwnd} pos=({x},{y}) size=({WIDTH},{height}) "
                  f"screen=({screen_w},{screen_h}) acrylic={'ok' if ok else 'skip/fail'}",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print("native handle failed:", exc, flush=True)
        api.apply_visuals()
        state["shown_at"] = time.time()

    config = state["config"]
    try:
        hotkey_mods, hotkey_vk = app_config.parse_hotkey(config["hotkey"])
    except ValueError:
        hotkey_mods, hotkey_vk = MOD_CONTROL | MOD_ALT, VK_SPACE
        print("配置里的热键无效，回落到 Ctrl+Alt+Space", flush=True)

    window.events.loaded += on_loaded
    threading.Thread(
        target=hotkey_loop,
        args=(window, hotkey_mods, hotkey_vk, config["hotkey"]),
        daemon=True,
    ).start()
    if not args.keep_open:
        threading.Thread(target=blur_watch, args=(window,), daemon=True).start()

    threading.Thread(
        target=refresh_loop, args=(window, state.get("data_digest") or ""), daemon=True
    ).start()

    if args.hidden:
        print("以 --hidden 启动：窗口保持隐藏，按热键或再次启动可唤出", flush=True)
    else:
        print(f"{config['hotkey']} 唤出/收起，Ctrl+Alt+Q 退出", flush=True)
    print("pid:", os.getpid(), flush=True)
    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
