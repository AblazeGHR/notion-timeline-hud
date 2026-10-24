"""应用配置：JSON 落盘 + 默认值 + 类型校验 + 原子写入。

配置文件固定为 app/config.json；不存在时用默认值（不主动创建）。
损坏（非法 JSON / 类型不对）时回退默认值，并把坏文件改名留档。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

DEFAULTS: dict[str, Any] = {
    "hotkey": "Ctrl+Alt+Space",
    "opacity": 0.82,
    "timeline_scale": 14.0,
    "font_scale": 1.0,
    "seen_intro": False,
    "open_in_app": True,
    "hide_on_blur": False,
    "release_memory_minutes": 15,
    "refresh_seconds": 120,
    "autostart": False,
}

SCALE_MIN = 6.0
SCALE_MAX = 48.0
FONT_MIN = 0.8
FONT_MAX = 1.5

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
}

_VK_NAMES = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
}


# ---------------------------------------------------------------- 校验


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field} 必须是布尔值")


def _as_opacity(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("opacity 必须是 0.2~1.0 的数字")
    number = float(value)
    if not 0.2 <= number <= 1.0:
        raise ValueError("opacity 必须在 0.2~1.0 之间")
    return round(number, 3)


def _as_scale(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeline_scale 必须是 6~40 的数字")
    number = float(value)
    if not SCALE_MIN <= number <= SCALE_MAX:
        raise ValueError(f"timeline_scale 必须在 {SCALE_MIN:g}~{SCALE_MAX:g} 之间")
    return round(number * 2) / 2  # 取整到 0.5，和界面一致


def _as_font_scale(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"font_scale 必须是 {FONT_MIN}~{FONT_MAX} 的数字")
    number = float(value)
    if not FONT_MIN <= number <= FONT_MAX:
        raise ValueError(f"font_scale 必须在 {FONT_MIN}~{FONT_MAX} 之间")
    return round(number, 2)


def _as_refresh_seconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("refresh_seconds 必须是 0~3600 的整数秒")
    if isinstance(value, float) and not value.is_integer():
        number = value
    else:
        number = value
    if not 0 <= number <= 3600:
        raise ValueError("refresh_seconds 必须在 0~3600 之间")
    return int(number)


def _as_minutes(value: Any) -> int:
    # pywebview 桥可能把 15 传成 15.0，整数值的浮点也收
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("release_memory_minutes 必须是整数分钟")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("release_memory_minutes 必须是整数分钟")
        value = int(value)
    if not 0 <= value <= 1440:
        raise ValueError("release_memory_minutes 必须在 0~1440 之间")
    return value


def _as_hotkey(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("hotkey 必须是非空字符串")
    parse_hotkey(value)  # 解析不过直接抛 ValueError
    return value.strip()


_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "hotkey": _as_hotkey,
    "opacity": _as_opacity,
    "timeline_scale": _as_scale,
    "font_scale": _as_font_scale,
    "hide_on_blur": lambda v: _as_bool(v, "hide_on_blur"),
    "release_memory_minutes": _as_minutes,
    "refresh_seconds": _as_refresh_seconds,
    "autostart": lambda v: _as_bool(v, "autostart"),
    "open_in_app": lambda v: _as_bool(v, "open_in_app"),
    "seen_intro": lambda v: _as_bool(v, "seen_intro"),
}


def parse_hotkey(text: str) -> tuple[int, int]:
    """'Ctrl+Alt+Space' → (mods, vk)。解析不了抛 ValueError。"""
    parts = [part.strip() for part in str(text).split("+") if part.strip()]
    if not parts:
        raise ValueError("hotkey 为空")
    mods = 0
    key: str | None = None
    for part in parts:
        low = part.lower()
        if low in _MODIFIERS:
            mods |= _MODIFIERS[low]
        elif key is None:
            key = low
        else:
            raise ValueError(f"hotkey 里有多个主键：{text}")
    if key is None:
        raise ValueError(f"hotkey 缺少主键：{text}")
    if not mods:
        raise ValueError(f"hotkey 至少需要一个修饰键：{text}")

    if len(key) == 1 and key.isalpha():
        vk = ord(key.upper())
    elif len(key) == 1 and key.isdigit():
        vk = ord(key)
    elif key in _VK_NAMES:
        vk = _VK_NAMES[key]
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"不认识的按键：{part}")
    return mods, vk


def format_hotkey(text: str) -> list[str]:
    """给界面显示用：拆成 [Ctrl, Alt, Space]。"""
    return [part.strip() for part in str(text).split("+") if part.strip()]


# ---------------------------------------------------------------- 读写


def _normalize(raw: Mapping[str, Any]) -> dict[str, Any]:
    """只接受已知键，逐项校验；未知键丢弃。"""
    result = dict(DEFAULTS)
    for key, validator in _VALIDATORS.items():
        if key in raw:
            result[key] = validator(raw[key])
    return result


def load(path: str | Path | None = None) -> dict[str, Any]:
    """读配置；文件缺失或损坏都回退默认值。"""
    target = Path(path) if path else CONFIG_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return dict(DEFAULTS)
    except OSError:
        return dict(DEFAULTS)

    try:
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("配置根节点必须是对象")
        return _normalize(raw)
    except (ValueError, TypeError):
        _quarantine(target)
        return dict(DEFAULTS)


def _quarantine(path: Path) -> None:
    """坏文件改名留档，别让它每次都触发回退。"""
    try:
        backup = path.with_suffix(path.suffix + ".bad")
        os.replace(path, backup)
    except OSError:
        pass


def save(config: Mapping[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    """校验后原子写入（临时文件 + replace），返回落盘值。"""
    target = Path(path) if path else CONFIG_PATH
    normalized = _normalize(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    payload = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return normalized


def patch(changes: Mapping[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    """合并一批改动并落盘；任一字段不合法则整体不写。"""
    if not isinstance(changes, Mapping):
        raise ValueError("patch 必须是对象")
    merged = load(path)
    for key, value in changes.items():
        if key not in _VALIDATORS:
            continue
        merged[key] = _VALIDATORS[key](value)
    return save(merged, path)
