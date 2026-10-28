"""运行目录 ROOT：默认是仓库根；DOTA_ASSISTANT_HOME 有值时用它；打包版是 exe 所在目录。
都在子进程里验，免得改了当前进程里已经导入的 config。搬 config.py 进子包时这个测试防 parent 层数写错。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PROBE = """
import json, sys
from dota_assistant.core import config
print(json.dumps({"root": str(config.ROOT), "data": str(config.DATA_DIR), "frozen": bool(config.FROZEN)}))
"""

FROZEN_PROBE = """
import json, sys
sys.frozen = True
sys.executable = {exe!r}
from dota_assistant.core import config
print(json.dumps({{"root": str(config.ROOT), "data": str(config.DATA_DIR), "frozen": bool(config.FROZEN)}}))
"""


def probe(code: str, home: str | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "DOTA_ASSISTANT_HOME"}
    env["PYTHONIOENCODING"] = "utf-8"
    if home is not None:
        env["DOTA_ASSISTANT_HOME"] = home
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True,
                         text=True, encoding="utf-8", check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


class RootTest(unittest.TestCase):
    def test_default_is_repo_root(self):
        r = probe(PROBE)
        root = Path(r["root"])
        self.assertEqual(root, REPO)
        self.assertTrue((root / "config.example.toml").is_file())
        self.assertTrue((root / "dota_assistant").is_dir())
        self.assertEqual(Path(r["data"]), root / "data")
        self.assertFalse(r["frozen"])

    def test_env_overrides_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = probe(PROBE, home=tmp)
            self.assertEqual(Path(r["root"]), Path(tmp).resolve())
            self.assertEqual(Path(r["data"]), Path(tmp).resolve() / "data")

    def test_empty_env_is_ignored(self):
        self.assertEqual(Path(probe(PROBE, home="")["root"]), REPO)

    def test_frozen_uses_exe_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = str(Path(tmp) / "DotaAssistant.exe")
            r = probe(FROZEN_PROBE.format(exe=exe))
            self.assertTrue(r["frozen"])
            self.assertEqual(Path(r["root"]), Path(tmp).resolve())
            self.assertEqual(Path(r["data"]), Path(tmp).resolve() / "data")


if __name__ == "__main__":
    unittest.main()
