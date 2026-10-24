"""用真实数据渲染一份静态 HTML，供无头浏览器截图验收（不弹窗）。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import data as appdata  # noqa: E402

template = (ROOT / "app" / "ui" / "index.html").read_text(encoding="utf-8")
payload = json.dumps(appdata.load()[0], ensure_ascii=False).replace("</", "<\\/")
html = template.replace("window.__DATA__ = null;", f"window.__DATA__ = {payload};")

out = Path(__file__).with_name("static.html")
out.write_text(html, encoding="utf-8")
print("written:", out, len(html), "chars")
