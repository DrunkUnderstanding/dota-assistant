"""探一下 STRATZ 的字段名（一次 introspection 请求），结果写 data/research/role-play/schema.json。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from dota_assistant.core import config  # noqa: E402
from dota_assistant.core.config import DATA_DIR  # noqa: E402

spec = importlib.util.spec_from_file_location("iv_fetch", ROOT / "docs" / "research" / "item-vs" / "scripts" / "b_fetch.py")
iv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iv)

TYPES = sys.argv[1:] or ["MatchType", "MatchPlayerType", "MatchPlayerStatsType"]
q = "{ " + " ".join(f't{i}: __type(name: "{t}") {{ name fields {{ name type {{ name kind ofType {{ name kind ofType {{ name }} }} }} }} }}'
                    for i, t in enumerate(TYPES)) + " }"
data, left = iv.post(config.load(), q)
out = DATA_DIR / "research" / "role-play" / "schema.json"
out.parent.mkdir(parents=True, exist_ok=True)
old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
for v in data.values():
    if v:
        old[v["name"]] = v["fields"]
        print(v["name"], ":", " ".join(f["name"] for f in v["fields"]))
out.write_text(json.dumps(old, indent=1), encoding="utf-8")
print("left", left)
