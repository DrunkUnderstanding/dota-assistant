"""编码 / 换行这类基础问题的守门测试：Windows 上 cmd 默认 GBK、git 可能把 CRLF 转成 LF，靠人记不住。"""
import ast
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".py", ".md", ".toml", ".txt", ".json", ".js", ".html", ".css", ".bat", ".cmd"}


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise unittest.SkipTest("没有 git")
    return [ROOT / p for p in out.decode("utf-8").split("\0") if p]


def python_files() -> list[Path]:
    """递归查（子包里的也算），跳过 __pycache__"""
    return [p for d in ("dota_assistant", "tests", "packaging", "tools") for p in (ROOT / d).rglob("*.py")
            if "__pycache__" not in p.parts]


class TextFileTest(unittest.TestCase):
    def test_text_files_are_utf8_without_bom(self):
        for p in tracked_files():
            if p.suffix.lower() not in TEXT_SUFFIXES or not p.exists():
                continue
            raw = p.read_bytes()
            with self.subTest(file=p.name):
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "带 BOM：.bat 第一行会多出乱码，Python / TOML 也可能读错")
                raw.decode("utf-8")

    def test_batch_files_use_crlf(self):
        """LF 换行的 bat 在 chcp 65001 + 中文下，cmd 会按错的字节偏移读行，把半截命令当命令执行"""
        bats = [p for p in tracked_files() if p.suffix.lower() in (".bat", ".cmd") and p.exists()]
        self.assertTrue(bats)
        for p in bats:
            raw = p.read_bytes()
            with self.subTest(file=p.name):
                self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), "有 LF 换行，要用 CRLF（见 .gitattributes）")


class PythonEncodingTest(unittest.TestCase):
    """Windows 上不写 encoding 默认是 GBK，中文读写会乱码或直接报错"""

    def test_text_io_names_encoding(self):
        bad = []
        for p in python_files():
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if not isinstance(n, ast.Call):
                    continue
                name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
                kws = {k.arg for k in n.keywords}
                if "encoding" in kws:
                    continue
                if name in ("read_text", "write_text"):
                    bad.append(f"{p.name}:{n.lineno} {name}")
                elif name == "open" and isinstance(n.func, ast.Name):
                    mode = next((k.value for k in n.keywords if k.arg == "mode"), n.args[1] if len(n.args) > 1 else None)
                    if not (isinstance(mode, ast.Constant) and "b" in str(mode.value)):
                        bad.append(f"{p.name}:{n.lineno} open")
                elif name in ("run", "Popen", "check_output") and kws & {"text", "universal_newlines"}:
                    bad.append(f"{p.name}:{n.lineno} {name}(text=True)")
        self.assertEqual(bad, [], "这些文本读写没写 encoding")


if __name__ == "__main__":
    unittest.main()
