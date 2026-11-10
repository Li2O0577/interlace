"""One plain question to a model, no tools: for short checks (formulation fidelity, grading)."""

from __future__ import annotations

import json
import os
import subprocess

from . import cli
from .providers import ProviderError


def ask(runner, text: str, timeout: float = 900, max_tokens: int = 4000) -> str:
    """``runner`` is what ``providers.build`` returns: an API provider or a CLI runner."""
    if isinstance(runner, cli.ClaudeCodeRunner):
        argv = [*runner.command, "-p", text, "--output-format", "json", "--tools", "", "--setting-sources", "",
                "--strict-mcp-config", "--max-turns", "1"]
        if runner.model and runner.model not in ("default", "-"):
            argv += ["--model", runner.model]
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                                  env=cli.agent_env(os.environ, {}), encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(f"模型调用失败：{exc}") from exc
        try:
            data = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise ProviderError(f"模型调用失败：{(done.stderr or done.stdout)[-400:]}") from None
        if data.get("is_error"):
            raise ProviderError(f"模型调用失败：{data.get('result') or data.get('subtype')}")
        return str(data.get("result") or "")
    if isinstance(runner, cli.CLIRunner):
        raise ProviderError(f"{runner.kind} 暂不支持这种单次调用")
    return runner.chat("", [{"role": "user", "content": text}], [], max_tokens=max_tokens).text
