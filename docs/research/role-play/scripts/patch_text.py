"""把中文版 7.42 更新日志网页转成纯文本（data/research/role-play/p742.txt），方便查通用改动。"""
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from dota_assistant.core.config import DATA_DIR  # noqa: E402

src = DATA_DIR / "mockups" / "Dota2-7.42-更新日志-中文版.html"
out = DATA_DIR / "research" / "role-play" / "p742.txt"
out.parent.mkdir(parents=True, exist_ok=True)
s = src.read_text(encoding="utf-8")
s = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", s, flags=re.S)
s = re.sub(r"data:[^\"')]+", "", s)
t = re.sub(r"<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", s)
t = html.unescape(re.sub(r"<[^>]+>", "", t))
t = re.sub(r"\n\s*\n+", "\n", t)
out.write_text(t, encoding="utf-8")
print(len(t), out)
