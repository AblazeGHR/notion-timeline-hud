"""截取 Timeline HUD 窗口所在矩形，用于自检（只截窗口，不截整屏）。"""

import ctypes
import sys

from PIL import ImageGrab

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


hwnd = user32.FindWindowW(None, "Timeline HUD")
if not hwnd:
    print("window not found")
    raise SystemExit(1)

rect = RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
box = (rect.left, rect.top, rect.right, rect.bottom)
print("hwnd:", hwnd, "rect:", box)

img = ImageGrab.grab(bbox=box, all_screens=True)
out = sys.argv[1] if len(sys.argv) > 1 else "spike/window.png"
img.save(out)
print("saved:", out, img.size)
