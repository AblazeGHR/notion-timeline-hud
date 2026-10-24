"""临时验证：无边框透明窗口 + DWM 亚克力，是否能在 WebView2 上透出桌面。
跑 8 秒自动关闭，仅用于确认技术路线。"""

import ctypes
import threading
import time

import webview

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:transparent;font-family:'Segoe UI','Microsoft YaHei',sans-serif}
.card{position:absolute;inset:0;border-radius:20px;background:rgba(16,17,22,.55);
      border:1px solid rgba(255,255,255,.14);box-shadow:0 40px 120px rgba(0,0,0,.6);
      padding:26px;color:#eaeaf0;box-sizing:border-box}
h3{margin:0 0 10px;font-size:17px;font-weight:600}
p{color:#9a9cae;font-size:13px;line-height:1.7}
b{color:#7C8CFF}
</style></head><body><div class="card">
<h3>Acrylic / 透明测试</h3>
<p>看到<b>模糊的桌面</b> = DWM 亚克力生效<br>
看到<b>纯黑底</b> = WebView2 透明失败<br>
看到<b>清晰桌面</b> = 窗口透明但没模糊</p>
</div></body></html>"""


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


def apply_acrylic(hwnd: int) -> None:
    # 4 = ACCENT_ENABLE_ACRYLICBLURBEHIND, 19 = WCA_ACCENT_POLICY
    accent = ACCENTPOLICY(4, 0, 0xCC181A20, 0)  # AABBGGRR
    data = WINCOMPATTRDATA(
        19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent)
    )
    ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))


def after_start() -> None:
    time.sleep(1.0)
    win = webview.windows[0]
    try:
        hwnd = win.native.Handle.ToInt64()
        apply_acrylic(hwnd)
        print("acrylic applied to hwnd", hwnd, flush=True)
    except Exception as exc:  # noqa: BLE001
        print("acrylic failed:", repr(exc), flush=True)
    time.sleep(6.0)
    win.destroy()


if __name__ == "__main__":
    window = webview.create_window(
        "acrylic-test",
        html=HTML,
        width=460,
        height=720,
        x=420,
        y=110,
        frameless=True,
        transparent=True,
        on_top=True,
        resizable=False,
        shadow=True,
    )
    threading.Thread(target=after_start, daemon=True).start()
    webview.start(gui="edgechromium", debug=False)
