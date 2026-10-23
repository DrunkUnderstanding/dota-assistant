"""无头调用 Claude Code（玩家现有订阅，不另外花 API 钱），返回按 JSON schema 约束的结构化结果。

选人盲测（aibacktest.py）和本局打法（gameplan.py）共用。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)


def claude_exe() -> str:
    """直接调 claude.exe：经过 npm 的 claude.cmd 时，空参数（--tools ""）和多行参数会被 cmd.exe 弄丢 / 截断"""
    shim = shutil.which("claude")
    if shim:
        exe = Path(shim).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)
    return shim or "claude"


def ask_json(prompt: str, system: str, schema: dict, model: str, timeout: float = 900) -> tuple[dict | None, float, str]:
    """返回 (结构化结果, 折合 API 价格, 错误信息)"""
    out, cost, err = _ask_json(prompt, system, schema, model, timeout)
    if err:
        log.warning("Claude Code 调用失败（%s）：%s", model, err)
    return out, cost, err


def _ask_json(prompt: str, system: str, schema: dict, model: str, timeout: float) -> tuple[dict | None, float, str]:
    t0 = time.monotonic()
    cmd = [claude_exe(), "-p", "--model", model, "--safe-mode", "--tools", "", "--system-prompt", system,
           "--no-session-persistence", "--json-schema", json.dumps(schema), "--output-format", "json"]
    try:
        # --safe-mode：不加载 CLAUDE.md / MCP / 技能；空目录里跑，避免读到项目文件
        with tempfile.TemporaryDirectory() as cwd:
            r = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, cwd=cwd)
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
