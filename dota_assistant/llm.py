"""调大模型，返回按 JSON schema 约束的结构化结果。选人盲测（aibacktest.py）和本局打法（gameplan.py）共用。

三种来源（config.toml 的 ai_provider，控制面板「设置」里也能改）：
  claude_code  无头调本机 Claude Code（玩家现有订阅，不另外花 API 钱；要装 Claude Code）；
               填了 ai_api_key / ai_base_url 就让它走中转站，否则用 Claude Code 自己的登录
  anthropic    玩家自己的 Anthropic API Key，按量付费（官方 anthropic SDK）；填了 ai_base_url 就走中转站（Bearer 认证）
  openai       OpenAI 兼容接口：自填 base_url + key + 模型（DeepSeek、通义、Kimi 等）。
               这些接口大多只支持 json_object，schema 写进提示词里约束；不是 Claude，打法质量没验证过
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

PROVIDERS = {"claude_code": "Claude Code 订阅", "anthropic": "Anthropic API Key", "openai": "OpenAI 兼容接口"}


def options(cfg) -> dict:
    """从 Config 取出 ask_json 要的来源参数"""
    return {"provider": cfg.ai_provider, "api_key": cfg.ai_api_key, "base_url": cfg.ai_base_url}


def claude_exe() -> str:
    """直接调 claude.exe：经过 npm 的 claude.cmd 时，空参数（--tools ""）和多行参数会被 cmd.exe 弄丢 / 截断"""
    shim = shutil.which("claude")
    if shim:
        exe = Path(shim).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)
    return shim or "claude"


def ask_json(prompt: str, system: str, schema: dict, model: str, timeout: float = 900, provider: str = "claude_code",
             api_key: str = "", base_url: str = "") -> tuple[dict | None, float, str]:
    """返回 (结构化结果, 折合 API 价格, 错误信息)。价格只有 Claude Code 会报，另外两种记 0（看各家后台）"""
    if provider == "anthropic":
        out, cost, err = _ask_anthropic(prompt, system, schema, model, timeout, api_key, base_url)
    elif provider == "openai":
        out, cost, err = _ask_openai(prompt, system, schema, model, timeout, api_key, base_url)
    else:
        out, cost, err = _ask_claude_code(prompt, system, schema, model, timeout, api_key, base_url)
    if err:
        log.warning("%s 调用失败（%s）：%s", PROVIDERS.get(provider, provider), model, err)
    return out, cost, err


def _ask_claude_code(prompt: str, system: str, schema: dict, model: str, timeout: float, api_key: str = "",
                     base_url: str = "") -> tuple[dict | None, float, str]:
    """填了 key / 地址就让这次调起的 Claude Code 走中转站（等于它的 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL）；
    有的中转站（例如 DuckCoding 的 Claude Code 分组）只认官方 Claude Code 发的请求，只能走这条路。都不填 = 用 Claude Code 自己的登录"""
    t0 = time.monotonic()
    relay = {}
    if api_key:
        relay["ANTHROPIC_AUTH_TOKEN"] = api_key
    if base_url:
        relay["ANTHROPIC_BASE_URL"] = base_url.rstrip("/").removesuffix("/v1")
    cmd = [claude_exe(), "-p", "--model", model, "--safe-mode", "--tools", "", "--system-prompt", system,
           "--no-session-persistence", "--json-schema", json.dumps(schema), "--output-format", "json"]
    try:
        # --safe-mode：不加载 CLAUDE.md / MCP / 技能；空目录里跑，避免读到项目文件
        with tempfile.TemporaryDirectory() as cwd:
            if relay:
                # 用 --settings 传：~/.claude/settings.json 里的 env 会盖过进程环境变量，只有命令行 settings 比它优先。
                # 写进临时目录的文件，不放命令行参数，免得 key 出现在进程列表里
                settings = Path(cwd) / "relay.json"
                settings.write_text(json.dumps({"env": relay}), encoding="utf-8")
                cmd = [*cmd, "--settings", str(settings)]
            env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                                                                    "ANTHROPIC_BASE_URL")} if relay else None
            r = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        return None, 0.0, "超时"
    except OSError as e:
        return None, 0.0, f"调不起 Claude Code：{e}"
    line = r.stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        d = json.loads(line[-1])
    except (IndexError, json.JSONDecodeError):
        return None, 0.0, r.stderr.decode("utf-8", "replace")[-300:]
    out = d.get("structured_output")
    log.info("Claude Code（%s）返回：%.0f 秒，折合 $%.3f，提示词 %d 字", model, time.monotonic() - t0,
             float(d.get("total_cost_usd") or 0), len(prompt))
    return out, float(d.get("total_cost_usd") or 0), "" if out else str(d.get("result"))[:300]


def _ask_anthropic(prompt: str, system: str, schema: dict, model: str, timeout: float,
                   api_key: str, base_url: str = "") -> tuple[dict | None, float, str]:
    if not api_key:
        return None, 0.0, "没填 Anthropic API Key（控制面板 → 设置）"
    try:
        import anthropic
    except ImportError:
        return None, 0.0, "没装 anthropic 库：pip install anthropic"
    t0 = time.monotonic()
    if base_url:
        # 中转站（例如 DuckCoding）：地址不带 /v1，SDK 自己补 /v1/messages；按 Claude Code 的 ANTHROPIC_AUTH_TOKEN 发 Bearer
        client = anthropic.Anthropic(auth_token=api_key, base_url=base_url.rstrip("/").removesuffix("/v1"), timeout=timeout)
    else:
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    try:
        r = client.messages.create(
            model=model, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.AuthenticationError:
        return None, 0.0, "API Key 不对或已失效"
    except anthropic.PermissionDeniedError:
        return None, 0.0, "这个 Key 没有权限用该模型"
    except anthropic.NotFoundError:
        return None, 0.0, f"没有这个模型：{model}"
    except anthropic.RateLimitError:
        return None, 0.0, "请求太频繁或额度用完了，稍后再试"
    except anthropic.APIStatusError as e:
        return None, 0.0, f"接口报错 {e.status_code}：{e.message}"[:300]
    except anthropic.APIConnectionError as e:
        return None, 0.0, f"连不上 Anthropic（网络 / 代理）：{e}"[:300]
    if r.stop_reason == "refusal":
        return None, 0.0, "模型拒绝回答"
    text = next((b.text for b in r.content if b.type == "text"), "")
    log.info("Anthropic API（%s）返回：%.0f 秒，输入 %d / 输出 %d token，提示词 %d 字", model, time.monotonic() - t0,
             r.usage.input_tokens, r.usage.output_tokens, len(prompt))
    if r.stop_reason == "max_tokens":
        return None, 0.0, "输出超长被截断"
    return _loads(text)


def _ask_openai(prompt: str, system: str, schema: dict, model: str, timeout: float, api_key: str,
                base_url: str) -> tuple[dict | None, float, str]:
    if not base_url:
        return None, 0.0, "没填接口地址（控制面板 → 设置），例如 https://api.deepseek.com/v1"
    import requests
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    sys_text = (f"{system}\n\n只输出一个 JSON 对象，不要任何别的文字，必须符合这个 JSON Schema：\n"
                f"{json.dumps(schema, ensure_ascii=False)}")
    body = {"model": model, "messages": [{"role": "system", "content": sys_text}, {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    t0 = time.monotonic()
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
        if r.status_code == 400 and "response_format" in r.text:      # 有的接口不认 json_object，去掉再试一次
            body.pop("response_format")
            r = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        return None, 0.0, f"连不上 {url}：{e}"[:300]
    if r.status_code in (401, 403):
        return None, 0.0, "API Key 不对或没权限"
    if r.status_code != 200:
        return None, 0.0, f"接口报错 {r.status_code}：{r.text}"[:300]
    try:
        d = r.json()
        text = d["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        return None, 0.0, f"接口返回格式看不懂：{r.text}"[:300]
    usage = d.get("usage") or {}
    log.info("OpenAI 兼容接口（%s）返回：%.0f 秒，输入 %s / 输出 %s token，提示词 %d 字", model, time.monotonic() - t0,
             usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"), len(prompt))
    return _loads(text)


def _loads(text: str) -> tuple[dict | None, float, str]:
    """有的模型会把 JSON 包在 ```json 代码块里，剥掉再解析"""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    try:
        out = json.loads(m.group(1) if m else text)
    except json.JSONDecodeError:
        return None, 0.0, f"返回的不是 JSON：{text[:200]}"
    return (out, 0.0, "") if isinstance(out, dict) else (None, 0.0, f"返回的不是 JSON 对象：{text[:200]}")
