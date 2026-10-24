"""Timeline HUD 桌面壳。

- 无边框半透明置顶窗口（WebView2 渲染 ui/index.html）
- 全局热键 Ctrl+Alt+Space 唤出/收起，Ctrl+Alt+Q 退出
- 失焦自动隐藏
- DWM 亚克力（Windows 11 的模糊背景）
- 开机自启开关：python app/main.py --autostart on|off

运行：E:/software/miniforge/python.exe app/main.py
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import webview

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from data import DATA_SOURCE_ID, load, to_ui  # noqa: E402
from quickadd import parse as parse_quick_add  # noqa: E402
from notion_timeline.api_client import NotionClient  # noqa: E402
from notion_timeline.models import DateValue, PropertyValue  # noqa: E402

WIDTH, HEIGHT = 460, 720
MARGIN_RIGHT = 80
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

user32 = ctypes.windll.user32

state = {"visible": True, "shown_at": time.time(), "hwnd": 0, "armed": False}


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
    """4 = ACCENT_ENABLE_ACRYLICBLURBEHIND，颜色按 AABBGGRR。"""
    accent = ACCENTPOLICY(4, 0, 0xD01D1816, 0)
    data = WINCOMPATTRDATA(
        19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent)
    )
    try:
        return bool(user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 窗口动作


def toggle(window: webview.Window) -> None:
    try:
        if state["visible"]:
            window.hide()
            state["visible"] = False
        else:
            window.show()
            hwnd = state["hwnd"]
            if hwnd:
                user32.ShowWindow(hwnd, 5)  # SW_SHOW
                user32.SetForegroundWindow(hwnd)
            state["visible"] = True
            state["shown_at"] = time.time()
    except Exception as exc:  # noqa: BLE001
        print("toggle failed:", exc, flush=True)


def hotkey_loop(window: webview.Window) -> None:
    if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_SPACE):
        print("热键 Ctrl+Alt+Space 注册失败（可能被占用）", flush=True)
    user32.RegisterHotKey(None, 2, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_Q)
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            if msg.wParam == 1:
                toggle(window)
            elif msg.wParam == 2:
                window.destroy()
                break


def blur_watch(window: webview.Window) -> None:
    """失焦 700ms 后自动收起；刚唤出的 1.2 秒内不判定，避免误收。"""
    while True:
        time.sleep(0.25)
        if not state["visible"] or not state["hwnd"]:
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
                window.hide()
                state["visible"] = False
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


# ---------------------------------------------------------------- JS 桥


class Api:
    def __init__(self) -> None:
        self._window: webview.Window | None = None

    def hide(self) -> None:
        if self._window:
            self._window.hide()
            state["visible"] = False

    def quit(self) -> None:
        if self._window:
            self._window.destroy()

    def reload_data(self) -> dict:
        data, source, error = load()
        return {"data": data, "source": source, "error": error}

    def open_task(self, task_id: str) -> None:
        import webbrowser

        webbrowser.open("https://www.notion.so/" + task_id.replace("-", ""))

    # ---- 写入（全部走 API，只动 DATA_SOURCE_ID 指向的库） ----

    def create_from_text(self, text: str) -> dict:
        title, day, clock = parse_quick_add(text)
        if not title:
            return {"ok": False, "error": "只有时间，没有标题"}
        try:
            with NotionClient() as client:
                task = client.create_task(
                    title,
                    dates={"截止日期": DateValue(day, start_time=clock, time_zone=TZ)},
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


# ---------------------------------------------------------------- 入口


def screen_size() -> tuple[int, int]:
    """优先用 WinForms 的屏幕尺寸，保证和 pywebview 的坐标单位一致。"""
    try:
        from System.Windows.Forms import Screen

        area = Screen.PrimaryScreen.WorkingArea
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
    return template.replace("window.__DATA__ = null;", f"window.__DATA__ = {payload};")


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
    x = max(0, int(logical_w - WIDTH - MARGIN_RIGHT))
    y = max(0, int((logical_h - HEIGHT) / 2))

    api = Api()
    window = webview.create_window(
        "Timeline HUD",
        html=render_html(),
        js_api=api,
        width=WIDTH,
        height=HEIGHT,
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
            ok = apply_acrylic(hwnd) if not args.no_blur else False
            print(f"hwnd={hwnd} pos=({x},{y}) screen=({screen_w},{screen_h}) "
                  f"acrylic={'ok' if ok else 'skip/fail'}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print("native handle failed:", exc, flush=True)
        state["shown_at"] = time.time()

    window.events.loaded += on_loaded
    threading.Thread(target=hotkey_loop, args=(window,), daemon=True).start()
    if not args.keep_open:
        threading.Thread(target=blur_watch, args=(window,), daemon=True).start()

    print("Ctrl+Alt+Space 唤出/收起，Ctrl+Alt+Q 退出", flush=True)
    print("pid:", os.getpid(), flush=True)
    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
