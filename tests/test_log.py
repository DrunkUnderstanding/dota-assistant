"""日志：写文件、token 不落盘、旧日志清理、后台线程异常、Electron 输出转发。不联网、不开窗口。"""
import io
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from dota_assistant.core import log as logs
from dota_assistant import webui
from dota_assistant.core.config import Config

TOKEN = "eyJhbGciOiJIUzI1NiJ9.secret-token-value"


class LogTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dota-log-"))
        self.patch = mock.patch.object(logs, "LOG_DIR", self.dir)
        self.patch.start()
        self.hooks = (__import__("sys").excepthook, threading.excepthook)

    def tearDown(self):
        root = logging.getLogger()
        for h in logs._handlers:
            root.removeHandler(h)
            h.close()
        logs._handlers.clear()
        import sys
        sys.excepthook, threading.excepthook = self.hooks
        logging.getLogger(logs.PKG).setLevel(logging.NOTSET)
        self.patch.stop()

    def setup(self, **kw):
        with mock.patch.object(logs, "_git_rev", return_value="test"):
            path = logs.setup(Config(stratz_token=TOKEN, **kw), "run")
        # 控制台输出收进内存，测试时不打印
        logs._handlers[0].setStream(io.StringIO())
        return path

    def read(self, path):
        for h in logs._handlers:
            h.flush()
        return path.read_text(encoding="utf-8")

    def test_writes_file_with_header_and_hides_token(self):
        key = "sk-test-api-key-value"
        path = self.setup(ai_api_key=key)
        logging.getLogger("dota_assistant.llm").warning("请求失败 x-api-key %s", key)
        logging.getLogger("dota_assistant.data").warning("请求失败 Bearer %s", TOKEN)
        try:
            raise RuntimeError(f"带 token 的异常 {TOKEN}")
        except RuntimeError:
            logging.getLogger("dota_assistant.live").exception("操作出错")
        text = self.read(path)
        self.assertIn("==== 启动", text)
        self.assertIn("stratz_token': '已设置'", text)
        self.assertIn("data: 请求失败 Bearer ***", text)
        self.assertIn("Traceback", text)
        self.assertNotIn(TOKEN, text)
        self.assertIn("ai_api_key': '已设置'", text)
        self.assertNotIn(key, text)                              # 2026-10-26 以前启动头里明文写了 AI Key
        console = logs._handlers[0].stream.getvalue()
        self.assertNotIn(TOKEN, console)

    def test_console_shows_warnings_without_traceback(self):
        self.setup()
        logging.getLogger("dota_assistant.live").info("普通信息")
        try:
            raise ValueError("坏了")
        except ValueError:
            logging.getLogger("dota_assistant.live").exception("操作出错")
        logging.getLogger("urllib3").warning("第三方库的警告")
        console = logs._handlers[0].stream.getvalue()
        self.assertIn("操作出错（详情见日志）", console)
        self.assertNotIn("Traceback", console)
        self.assertNotIn("普通信息", console)
        self.assertNotIn("第三方库", console)

    def test_debug_level_from_config(self):
        path = self.setup(log_level="debug")
        logging.getLogger("dota_assistant.live").debug("识别分数")
        logging.getLogger("urllib3").debug("第三方 debug")
        text = self.read(path)
        self.assertIn("识别分数", text)
        self.assertNotIn("第三方 debug", text)

    def test_keeps_only_recent_logs(self):
        for i in range(logs.KEEP + 5):
            (self.dir / f"20260101-{i:06d}-run.log").write_text("x", encoding="utf-8")
        (self.dir / "20260101-000000-run.log.1").write_text("x", encoding="utf-8")
        path = self.setup()
        files = sorted(self.dir.glob("*.log"))
        self.assertEqual(len(files), logs.KEEP)
        self.assertEqual(files[-1], path)
        self.assertFalse((self.dir / "20260101-000000-run.log.1").exists())
        self.assertEqual(logs.latest(), path)

    def test_thread_exception_logged(self):
        path = self.setup()

        def boom():
            raise KeyError("后台炸了")
        t = threading.Thread(target=boom, name="brief")
        t.start()
        t.join()
        text = self.read(path)
        self.assertIn("后台线程 brief 出错", text)
        self.assertIn("后台炸了", text)

    def test_electron_output_forwarded(self):
        path = self.setup()
        out = io.BytesIO("WARN [叠加层] 页面报错 (overlay.js:3)\nERROR 主进程出错\n[1234:gpu] chromium 自己的输出\n"
                         .encode("utf-8"))
        webui._pipe_electron(out)
        text = self.read(path)
        self.assertRegex(text, r"WARNING .*electron: \[叠加层\] 页面报错")
        self.assertRegex(text, r"ERROR .*electron: 主进程出错")
        self.assertRegex(text, r"INFO .*electron: \[1234:gpu\] chromium")
        console = logs._handlers[0].stream.getvalue()
        self.assertNotIn("chromium", console)


if __name__ == "__main__":
    unittest.main()
