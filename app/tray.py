"""系统托盘：显示/隐藏、刷新数据、开机自启、退出。

pystray / Pillow 缺失或启动失败都只记日志，主程序继续无托盘运行。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception as exc:  # noqa: BLE001 - 缺依赖不能挡住主程序
    pystray = None
    Image = ImageDraw = None  # type: ignore[assignment]
    _IMPORT_ERROR: Exception | None = exc
else:
    _IMPORT_ERROR = None

ICON_SIZE = 32
TOOLTIP = "Timeline HUD"

# 与面板同色系：深色底、灰蓝时间轴、暖色当前时刻点
_BG = (23, 25, 32, 255)
_AXIS = (122, 128, 146, 255)
_TICK = (86, 91, 108, 255)
_DOT = (255, 197, 92, 255)


def make_icon_image(size: int = ICON_SIZE) -> Any:
    """用 PIL 画托盘图标：圆角深底 + 一道竖向时间轴 + 一个亮点。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((1, 1, size - 2, size - 2), radius=8, fill=_BG)
    axis_x = round(size * 0.38)
    draw.line([(axis_x, 7), (axis_x, size - 7)], fill=_AXIS, width=2)
    # 左侧刻度：两条短线，点出「时间轴」
    for ratio in (0.34, 0.66):
        y = round(size * ratio)
        draw.line([(axis_x - 6, y), (axis_x - 2, y)], fill=_TICK, width=1)
    # 亮点比刻度略靠右，避免和轴线挤在一起
    dot_x, dot_y = axis_x + 5, round(size * 0.58)
    draw.ellipse((dot_x - 3, dot_y - 3, dot_x + 3, dot_y + 3), fill=_DOT)
    return img


def create(
    *,
    is_visible: Callable[[], bool],
    on_toggle: Callable[[Any], None],
    on_refresh: Callable[[Any], None],
    autostart_enabled: Callable[[], bool],
    on_autostart: Callable[[Any, bool], None],
    on_quit: Callable[[Any], None],
    log: Callable[[str], None] = print,
) -> Any | None:
    """建 Icon（不启动）。失败返回 None。"""
    if pystray is None:
        log(f"托盘不可用（pystray 导入失败：{_IMPORT_ERROR}），继续无托盘运行")
        return None
    try:
        image = make_icon_image()
        menu = pystray.Menu(
            # default=True：双击托盘图标触发这一项 = 显示/隐藏
            pystray.MenuItem(
                lambda _item: "隐藏" if is_visible() else "显示",
                lambda icon, _item: on_toggle(icon),
                default=True,
            ),
            pystray.MenuItem("刷新数据", lambda icon, _item: on_refresh(icon)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "开机自启",
                lambda icon, item: on_autostart(icon, not bool(item.checked)),
                checked=lambda _item: autostart_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda icon, _item: on_quit(icon)),
        )
        return pystray.Icon("TimelineHUD", image, TOOLTIP, menu)
    except Exception as exc:  # noqa: BLE001
        log(f"托盘创建失败：{exc}，继续无托盘运行")
        return None


def start(icon: Any, log: Callable[[str], None] = print) -> bool:
    """后台线程跑消息循环；返回是否真的启动。"""
    if icon is None:
        return False
    try:
        icon.run_detached()
        log(f"托盘已创建（tooltip={TOOLTIP}，菜单：显示/隐藏·刷新数据·开机自启·退出）")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"托盘启动失败：{exc}，继续无托盘运行")
        return False


def stop(icon: Any) -> None:
    if icon is None:
        return
    try:
        icon.stop()
    except Exception as exc:  # noqa: BLE001
        print("托盘停止失败（可忽略）：", exc, flush=True)


def stop_async(icon: Any) -> None:
    """退出流程里别在托盘回调线程上同步等自己停。"""
    if icon is None:
        return
    threading.Thread(target=stop, args=(icon,), daemon=True).start()
