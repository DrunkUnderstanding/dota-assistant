"""工程配置的一致性：pyproject.toml 的依赖和 requirements.txt 一样、只打包 dota_assistant、命令入口能 import、锁版本文件覆盖每个依赖。"""
import importlib
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def requirement_lines(path: Path) -> list[str]:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def dist_name(spec: str) -> str:
    """'PySide6-Essentials>=6.6' → 'pyside6-essentials'（按 PEP 503 规范化）"""
    for i, ch in enumerate(spec):
        if ch in "<>=!~;[ ":
            spec = spec[:i]
            break
    return spec.strip().lower().replace("_", "-").replace(".", "-")


class ProjectConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_dependencies_match_requirements_txt(self):
        self.assertEqual(self.pyproject["project"]["dependencies"], requirement_lines(ROOT / "requirements.txt"))

    def test_python_version(self):
        self.assertEqual(self.pyproject["project"]["requires-python"], ">=3.11")

    def test_only_package_dota_assistant(self):
        find = self.pyproject["tool"]["setuptools"]["packages"]["find"]
        self.assertEqual(find["include"], ["dota_assistant*"])

    def test_script_entry_point_importable(self):
        target = self.pyproject["project"]["scripts"]["dota-assistant"]
        self.assertEqual(target, "dota_assistant.cli:main")
        mod, func = target.split(":")
        self.assertTrue(callable(getattr(importlib.import_module(mod), func)))

    def test_lock_pins_every_requirement(self):
        pinned = {}
        for line in requirement_lines(ROOT / "requirements.lock"):
            name, _, ver = line.partition("==")
            self.assertTrue(ver, f"requirements.lock 里没锁版本：{line}")
            pinned[dist_name(name)] = ver
        for spec in requirement_lines(ROOT / "requirements.txt"):
            self.assertIn(dist_name(spec), pinned, f"requirements.lock 缺 {spec}")


if __name__ == "__main__":
    unittest.main()
