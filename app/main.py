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
import json
import os
import queue
import sys
import threading
import time
from ctypes import wintypes
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
HOTKEY_ID_TOGGLE = 1
HOTKEY_ID_QUIT = 2

user32 = ctypes.windll.user32
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


def apply_acrylic(hwnd: int) -> bool:
    """4 = ACCENT_ENABLE_ACRYLICBLURBEHIND，颜色按 AABBGGRR。

    0x401A1B22：alpha 0x40（25%），比原来的 0xD0 淡，透出更多桌面内容。
    """
    accent = ACCENTPOLICY(4, 0, 0x401A1B22, 0)
    data = WINCOMPATTRDATA(
        19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent)
    )
    try:
        return bool(user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 内存修剪

_trim_lock = threading.Lock()
_trim_timer: threading.Timer | None = None


def trim_memory() -> bool:
    """SetProcessWorkingSetSize(-1,-1) + EmptyWorkingSet，把工作集还给系统。"""
    try:
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        unlimited = ctypes.c_size_t(0xFFFFFFFFFFFFFFFF)  # (SIZE_T)-1
        ctypes.windll.kernel32.SetProcessWorkingSetSize(handle, unlimited, unlimited)
        try:
            ctypes.windll.psapi.EmptyWorkingSet(handle)
        except Exception:  # noqa: BLE001 - 少数系统只有 kernel32 导出版
            ctypes.windll.kernel32.K32EmptyWorkingSet(handle)
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


def fit_to_screen(hwnd: int) -> tuple[int, int, int, int]:
    """按物理像素把窗口钉到右边缘、整屏高。

    pywebview 的 x/y/width/height 会各自再乘一次 DPI，还对高度做了工作区裁剪，
    算不准；这里直接用 SetWindowPos 覆盖成实测值，宽度沿用创建后的实际宽度。
    """
    rect = wintypes.RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    width = max(320, rect.right - rect.left)
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    left = max(0, screen_w - width)
    _set_window_pos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST), left, 0,
                    width, screen_h, SWP_SHOWWINDOW)
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


def toggle(window: webview.Window) -> None:
    try:
        if state["visible"]:
            reset_timeline_scale(window)
            window.hide()
            state["visible"] = False
            schedule_memory_trim()
        else:
            cancel_memory_trim()
            window.show()
            hwnd = state["hwnd"]
            if hwnd:
                user32.ShowWindow(hwnd, 5)  # SW_SHOW
                user32.SetForegroundWindow(hwnd)
            state["visible"] = True
            state["shown_at"] = time.time()
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


def blur_watch(window: webview.Window) -> None:
    """失焦 700ms 后自动收起；刚唤出的 1.2 秒内不判定，避免误收。"""
    while True:
        time.sleep(0.25)
        if not state["visible"] or not state["hwnd"]:
            continue
        if not state["config"].get("hide_on_blur", True):
            state["armed"] = False
            continue
        if time.time() - state["shown_at"] < 1.2:
            continue
        fg = user32.GetForegroundWindow()
        root = user32.GetAncestor(fg, GA_ROOT) if fg else 0
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


def set_autostart(enable: bool) -> str:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            exe = Path(sys.executable)
            pythonw = exe.with_name("pythonw.exe")
            runner = pythonw if pythonw.exists() else exe
            command = f'"{runner}" "{ROOT / "main.py"}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
            return f"已写入自启：{command}"
        try:
            winreg.DeleteValue(key, APP_NAME)
            return "已移除自启"
        except FileNotFoundError:
            return "本来就没有自启项"


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
        """窗口加载完刷一遍透明度 / 主题。"""
        config = state["config"]
        self.apply_opacity(config["opacity"])
        self.apply_theme(config["theme"])
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

    def set_autostart(self, enabled: bool) -> dict:
        try:
            message = set_autostart(bool(enabled))
            updated = app_config.patch({"autostart": bool(enabled)})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        state["config"] = updated
        return {"ok": True, "message": message, "config": updated}

    def reload_data(self) -> dict:
        data, source, error = load()
        return {"data": data, "source": source, "error": error}

    def open_task(self, task_id: str) -> None:
        import webbrowser

        webbrowser.open("https://www.notion.so/" + task_id.replace("-", ""))

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
                client.update_task(task_id, properties={"状态": PropertyValue("status", name)})
            return {"ok": True, "status": nxt}
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
    html = template.replace("window.__DATA__ = null;", f"window.__DATA__ = {payload};")
    # 密度在首帧就用配置值渲染，避免加载完再跳一下
    return html.replace("window.__CFG__ = null;", f"window.__CFG__ = {config_payload};")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--autostart", choices=("on", "off"))
    parser.add_argument("--no-blur", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    if args.autostart:
        print(set_autostart(args.autostart == "on"))
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
    )
    api._window = window

    def on_loaded() -> None:
        time.sleep(0.15)
        try:
            hwnd = window.native.Handle.ToInt64()
            state["hwnd"] = hwnd
            left, top, right, bottom = fit_to_screen(hwnd)
            print(f"fit_to_screen -> ({left},{top},{right},{bottom})", flush=True)
            ok = apply_acrylic(hwnd) if not args.no_blur else False
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

    print(f"{config['hotkey']} 唤出/收起，Ctrl+Alt+Q 退出", flush=True)
    print("pid:", os.getpid(), flush=True)
    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
