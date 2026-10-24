"""app/config.py 的断言测试：直接 python tests/test_config.py 运行。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import config  # noqa: E402


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="cfgtest_"))


def test_defaults_when_missing() -> None:
    path = _tmpdir() / "config.json"
    cfg = config.load(path)
    assert cfg == config.DEFAULTS, cfg
    assert not path.exists(), "读默认值不应该创建文件"


def test_roundtrip_and_atomic() -> None:
    folder = _tmpdir()
    path = folder / "config.json"
    saved = config.save({**config.DEFAULTS, "opacity": 0.5, "theme": "light"}, path)
    assert saved["opacity"] == 0.5 and saved["theme"] == "light", saved
    assert config.load(path) == saved
    leftovers = [p.name for p in folder.iterdir() if p.name != "config.json"]
    assert leftovers == [], leftovers


def test_patch_merges_and_persists() -> None:
    path = _tmpdir() / "config.json"
    config.save(dict(config.DEFAULTS), path)
    cfg = config.patch({"opacity": 0.8, "hide_on_blur": False}, path)
    assert cfg["opacity"] == 0.8 and cfg["hide_on_blur"] is False, cfg
    assert cfg["hotkey"] == "Ctrl+Alt+Space", cfg
    assert config.load(path)["hide_on_blur"] is False


def test_patch_rejects_bad_values() -> None:
    path = _tmpdir() / "config.json"
    config.save(dict(config.DEFAULTS), path)
    for bad in (
        {"opacity": 1.5},
        {"opacity": "0.5"},
        {"theme": "blue"},
        {"release_memory_minutes": -1},
        {"release_memory_minutes": 1.5},
        {"hide_on_blur": "yes"},
        {"hotkey": "Space"},
        {"hotkey": "Ctrl+Nope"},
    ):
        try:
            config.patch(bad, path)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应该拒绝 {bad}")
    assert config.load(path) == config.DEFAULTS, "校验失败时不应写入"


def test_corrupt_file_falls_back() -> None:
    folder = _tmpdir()
    path = folder / "config.json"
    path.write_text("{ not json ", encoding="utf-8")
    assert config.load(path) == config.DEFAULTS
    assert (folder / "config.json.bad").exists(), "坏文件应改名留档"
    assert not path.exists()
    # 再次读取不应再触发回退（坏文件已经挪走）
    assert config.load(path) == config.DEFAULTS


def test_wrong_types_fall_back() -> None:
    path = _tmpdir() / "config.json"
    path.write_text(json.dumps({"opacity": "high", "theme": 3}), encoding="utf-8")
    assert config.load(path) == config.DEFAULTS


def test_unknown_keys_dropped() -> None:
    path = _tmpdir() / "config.json"
    path.write_text(json.dumps({"opacity": 0.7, "junk": 1}), encoding="utf-8")
    cfg = config.load(path)
    assert "junk" not in cfg and cfg["opacity"] == 0.7, cfg


def test_minutes_accepts_integral_float() -> None:
    path = _tmpdir() / "config.json"
    cfg = config.patch({"release_memory_minutes": 15.0}, path)  # pywebview 可能传 15.0
    assert cfg["release_memory_minutes"] == 15 and isinstance(cfg["release_memory_minutes"], int)
    for bad in (1.5, True, "15"):
        try:
            config.patch({"release_memory_minutes": bad}, path)
        except ValueError:
            continue
        raise AssertionError(f"应该拒绝 {bad!r}")


def test_timeline_scale() -> None:
    path = _tmpdir() / "config.json"
    cfg = config.load(path)
    assert cfg["timeline_scale"] == 14.0, cfg
    for good, want in ((6, 6.0), (40, 40.0), (22.5, 22.5), (14.3, 14.5), ("18", None)):
        if want is None:
            try:
                config.patch({"timeline_scale": good}, path)
            except ValueError:
                continue
            raise AssertionError(f"应该拒绝 {good!r}")
        assert config.patch({"timeline_scale": good}, path)["timeline_scale"] == want
    for bad in (5.9, 40.1, 0, -1, True, "high", None):
        try:
            config.patch({"timeline_scale": bad}, path)
        except ValueError:
            continue
        raise AssertionError(f"应该拒绝 {bad!r}")
    assert config.load(path)["timeline_scale"] == 14.5, "失败后应保留上一次合法值"


def test_parse_hotkey() -> None:
    assert config.parse_hotkey("Ctrl+Alt+Space") == (config.MOD_CONTROL | config.MOD_ALT, 0x20)
    assert config.parse_hotkey("ctrl+shift+f9") == (config.MOD_CONTROL | config.MOD_SHIFT, 0x78)
    assert config.format_hotkey("Ctrl+Alt+Space") == ["Ctrl", "Alt", "Space"]
    for bad in ("", "Space", "Ctrl+Alt", "Ctrl+Alt+F13", "Ctrl+A+B"):
        try:
            config.parse_hotkey(bad)
        except ValueError:
            continue
        raise AssertionError(f"应该拒绝 {bad!r}")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
