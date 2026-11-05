"""反和谐开关（game/uncensor.py + 控制面板设置）：只在临时目录里造假 Dota 目录，不碰本机游戏。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dota_assistant.app.live import consts as live_consts
from dota_assistant.core import config
from dota_assistant.game import uncensor
from tests.test_webui import make_live, wait

TMP = Path(tempfile.mkdtemp(prefix="dota-uncensor-"))


def fake_dota(with_lv: bool = True) -> Path:
    d = Path(tempfile.mkdtemp(prefix="lib-", dir=TMP)) / "dota 2 beta"
    (d / "game" / "dota").mkdir(parents=True)
    (d / "game" / "dota" / "pak01_dir.vpk").write_bytes(b"")
    if with_lv:
        (d / "game" / "dota_lv").mkdir()
        (d / "game" / "dota_lv" / "pak01_dir.vpk").write_bytes(b"lv")
    return d


class ApplyTest(unittest.TestCase):
    def test_on_off_roundtrip(self):
        d = fake_dota()
        lv, off = uncensor.paths(d)
        self.assertEqual(uncensor.state(d), "off")
        r = uncensor.apply(d, True, running=False)
        self.assertEqual(r.status, "done")
        self.assertFalse(lv.exists())
        self.assertEqual((off / "pak01_dir.vpk").read_bytes(), b"lv")      # 只改名，内容原样
        self.assertEqual(uncensor.state(d), "on")
        self.assertEqual(uncensor.apply(d, True, running=False).status, "noop")
        r = uncensor.apply(d, False, running=True)                          # 放回去不怕游戏开着
        self.assertEqual(r.status, "done")
        self.assertEqual((lv / "pak01_dir.vpk").read_bytes(), b"lv")
        self.assertFalse(off.exists())
        self.assertEqual(uncensor.apply(d, False).status, "noop")

    def test_wait_while_dota_running(self):
        d = fake_dota()
        r = uncensor.apply(d, True, running=True)
        self.assertEqual(r.status, "busy")
        self.assertTrue(uncensor.paths(d)[0].exists())                      # 游戏开着不动

    def test_steam_restored_lv(self):
        """Steam 更新把 dota_lv 补回来了：开着就删旧的挪开份、再挪一次；关着就只删旧的"""
        d = fake_dota()
        lv, off = uncensor.paths(d)
        uncensor.apply(d, True, running=False)
        lv.mkdir()
        (lv / "pak01_dir.vpk").write_bytes(b"new")
        self.assertEqual(uncensor.apply(d, True, running=False).status, "done")
        self.assertFalse(lv.exists())
        self.assertEqual((off / "pak01_dir.vpk").read_bytes(), b"new")
        lv.mkdir()
        (lv / "pak01_dir.vpk").write_bytes(b"newer")
        self.assertEqual(uncensor.apply(d, False).status, "done")
        self.assertFalse(off.exists())
        self.assertEqual((lv / "pak01_dir.vpk").read_bytes(), b"newer")

    def test_missing(self):
        self.assertEqual(uncensor.apply(None, True).status, "missing")
        d = fake_dota(with_lv=False)
        self.assertEqual(uncensor.state(d), "none")
        self.assertEqual(uncensor.apply(d, True, running=False).status, "missing")
        self.assertEqual(uncensor.apply(d, False).status, "noop")

    def test_process_names(self):
        names = uncensor.process_names()
        if names is not None:                                               # Windows 上至少有 python 自己
            self.assertTrue(any(n.startswith("python") for n in names))


class SettingTest(unittest.TestCase):
    def setUp(self):
        self.live = make_live().start()
        self.save = lambda **kw: wait(self.live.submit(self.live.action, {"type": "save_settings", **kw}))

    def tearDown(self):
        self.live.close()

    def test_toggle(self):
        d = fake_dota()
        self.live.dota_dir = d
        with mock.patch.object(uncensor, "dota_running", return_value=False):
            self.save(uncensor=True)
            self.assertTrue(config.load(self.live.config_file).uncensor)
            self.assertEqual(uncensor.state(d), "on")
            self.assertIn("已反和谐", self.live.message)
            self.assertTrue(self.live.settings_view()["uncensor"])
            self.save(uncensor=False)
        self.assertFalse(config.load(self.live.config_file).uncensor)
        self.assertEqual(uncensor.state(d), "off")
        self.assertIn("关闭反和谐", self.live.message)

    def test_retry_after_dota_closed(self):
        d = fake_dota()
        self.live.dota_dir = d
        with mock.patch.object(uncensor, "dota_running", return_value=True):
            self.save(uncensor=True)
        self.assertEqual(uncensor.state(d), "off")
        self.assertIn("关掉游戏后", self.live.message)
        self.assertIn("关掉游戏后", self.live.settings_view()["uncensor_note"])
        self.assertIsNotNone(self.live._uncensor_due)
        with mock.patch.object(uncensor, "dota_running", return_value=False), \
                mock.patch.object(live_consts, "UNCENSOR_RETRY", 0):
            self.live._uncensor_due = 0.0
            wait(self.live.submit(self.live._uncensor_tick))
        self.assertEqual(uncensor.state(d), "on")
        self.assertIsNone(self.live._uncensor_due)

    def test_tests_never_touch_real_dota(self):
        """测试 / 回放（use_gsi=False）不在启动时去找真实的 Dota 目录改名"""
        self.assertIsNone(self.live._uncensor_due)


if __name__ == "__main__":
    unittest.main()
