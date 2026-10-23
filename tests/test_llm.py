"""大模型调用的几种来源 + config.toml 写回。不联网：接口用假的。"""
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from dota_assistant import config, llm


class FakeResponse:
    def __init__(self, status: int, body):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


def chat(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class OpenAICompatibleTest(unittest.TestCase):
    def ask(self, *responses, **kw):
        with mock.patch("requests.post", side_effect=list(responses)) as post:
            out = llm.ask_json("资料", "系统", {"type": "object"}, "deepseek-chat", provider="openai",
                               api_key="sk-x", base_url=kw.get("base_url", "https://api.deepseek.com/v1"))
        return out, post

    def test_json_in_code_fence(self):
        (out, cost, err), post = self.ask(FakeResponse(200, chat('```json\n{"sections": []}\n```')))
        self.assertEqual((out, err), ({"sections": []}, ""))
        url = post.call_args.args[0]
        self.assertEqual(url, "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer sk-x"})
        self.assertIn("JSON Schema", post.call_args.kwargs["json"]["messages"][0]["content"])

    def test_retry_without_response_format(self):
        (out, _, err), post = self.ask(FakeResponse(400, "response_format not supported"), FakeResponse(200, chat('{"a": 1}')))
        self.assertEqual(out, {"a": 1})
        self.assertNotIn("response_format", post.call_args.kwargs["json"])

    def test_errors_reported(self):
        (out, _, err), _ = self.ask(FakeResponse(401, "bad key"))
        self.assertIsNone(out)
        self.assertIn("Key", err)
        (out, _, err), _ = self.ask(FakeResponse(200, chat("我觉得应该先出相位鞋")))
        self.assertIsNone(out)
        self.assertIn("不是 JSON", err)
        out, _, err = llm.ask_json("x", "s", {}, "m", provider="openai")
        self.assertIn("接口地址", err)

    def test_options_from_config(self):
        cfg = config.Config(ai_provider="anthropic", ai_api_key="k", ai_base_url="")
        self.assertEqual(llm.options(cfg), {"provider": "anthropic", "api_key": "k", "base_url": ""})
        self.assertIn("API Key", llm.ask_json("x", "s", {}, "m", provider="anthropic")[2])   # 没填 key 不发请求


class ClaudeCodeRelayTest(unittest.TestCase):
    def run_with(self, **kw):
        seen = {}

        def fake_run(cmd, **opts):
            i = cmd.index("--settings") if "--settings" in cmd else -1
            seen["settings"] = json.loads(Path(cmd[i + 1]).read_text(encoding="utf-8")) if i >= 0 else None
            seen["cmd"], seen["env"] = cmd, opts.get("env")
            out = json.dumps({"structured_output": {"ok": "好"}, "total_cost_usd": 0.01})
            return mock.Mock(stdout=out.encode(), stderr=b"", returncode=0)

        with mock.patch("subprocess.run", side_effect=fake_run):
            res = llm.ask_json("x", "s", {}, "claude-opus-5-5", provider="claude_code", **kw)
        return res, seen

    def test_relay_passed_by_settings_file(self):
        (out, _, err), seen = self.run_with(api_key="sk-relay-1234", base_url="https://api.duckcoding.ai/v1/")
        self.assertEqual(out, {"ok": "好"})
        # ~/.claude/settings.json 的 env 会盖过环境变量，所以用 --settings；key 不出现在命令行里
        self.assertEqual(seen["settings"], {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-relay-1234",
                                                    "ANTHROPIC_BASE_URL": "https://api.duckcoding.ai"}})
        self.assertNotIn("sk-relay-1234", " ".join(seen["cmd"]))
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", seen["env"])

    def test_no_relay_uses_own_login(self):
        _, seen = self.run_with()
        self.assertIsNone(seen["settings"])
        self.assertIsNone(seen["env"])


class ConfigSaveTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="dota-config-")) / "config.toml"

    def test_keeps_comments_and_escapes(self):
        self.path.write_text('account_id = 0        # 好友代码\nstratz_token = ""     # token\nai_plan = true\n',
                             encoding="utf-8")
        config.save({"account_id": 42, "stratz_token": 'a"b\\c', "ai_plan": False, "ai_model": "deepseek-chat"}, self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("account_id = 42        # 好友代码", text)
        self.assertIn("# token", text)
        raw = tomllib.loads(text)
        self.assertEqual((raw["stratz_token"], raw["ai_plan"], raw["ai_model"]), ('a"b\\c', False, "deepseek-chat"))

    def test_unknown_key_rejected(self):
        with self.assertRaises(KeyError):
            config.save({"nope": 1}, self.path)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
