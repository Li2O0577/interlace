"""CLI agent runners: Claude Code and Codex drive the agent loop themselves,
while every action goes through the workbench's tools (MCP bridge).

A runner starts one CLI process per turn (resuming the CLI's own session on
later turns), streams its JSON events, and reports text, tool activity and
usage back to the runtime. The CLIs' built-in tools are switched off where the
CLI allows it, so files, code and experiments are handled by the workbench —
with its permissions, approvals, layout rules and timeline.
"""

from __future__ import annotations

import json
import re
import os
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .providers import ProviderError

MCP_PREFIX = "mcp__mch__"


@dataclass
class CLIOutcome:
    session: str | None = None
    text: str = ""
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    turns: int = 0
    error: str | None = None
    live: dict = field(default_factory=dict)   # per assistant-message usage seen mid-turn (message id → usage)


@dataclass
class MCPServer:
    """How the CLI starts the workbench MCP server for this agent."""
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)


def bridge_server(url: str, project: str, agent: str, token: str) -> MCPServer:
    backend = str(Path(__file__).resolve().parents[2])
    env={'MCH_BRIDGE_TOKEN':token,'PYTHONPATH':backend,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    if os.name=='nt' and os.environ.get('SystemRoot'):
        env['SystemRoot']=os.environ['SystemRoot']  # Required by Windows SSL initialization, not a credential.
    return MCPServer(sys.executable, ["-m", "indeterminate.mcp_bridge", "--url", url, "--project", project, "--agent", agent],
                     env)


# When the workbench itself runs inside a Claude Code session (a terminal in Claude Code, a cloud session), the CLIs
# it starts would inherit that session: its memory stores, extra directories, skill sync, background-task behaviour
# (a long MCP call moved to the background after 120 s ends the agent's turn while the tool keeps running) and, in a
# cloud session, its session id (CLAUDE_CODE_REMOTE_SESSION_ID): every agent then resumes one shared conversation and
# reads the others' work. None of that belongs to an agent, so everything of the enclosing session goes, except the
# settings a person sets to choose how Claude Code signs in and which backend it uses.
INHERITED_PREFIXES = ("CLAUDE_COWORK_", "CLAUDE_MEMORY_", "CLAUDE_CODE_", "CCR_", "SESSION_INGRESS", "CLAUDE_SESSION_INGRESS")
INHERITED_NAMES = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_ADDITIONAL_DIRECTORIES", "CLAUDE_AUTO_BACKGROUND_TASKS",
                   "CLAUDE_AFTER_LAST_COMPACT", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_ENABLE_STREAM_WATCHDOG", "CLAUDE_EFFORT"}
KEPT_NAMES = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
              "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CODE_MAX_OUTPUT_TOKENS"}


def agent_env(base: dict, extra: dict) -> dict:
    """The environment for a CLI agent: the workbench's own, minus what belongs to an enclosing Claude session."""
    env = {k: v for k, v in base.items()
           if k in KEPT_NAMES or (k not in INHERITED_NAMES and not k.startswith(INHERITED_PREFIXES))}
    return {**env, 'PYTHONUTF8':'1', 'PYTHONIOENCODING':'utf-8', **extra}


LIMIT_TEXT = re.compile(r"hit your .{0,40}limit|usage limit|limit reached|rate limit", re.I)


def usage_limited(out: "CLIOutcome") -> str:
    """The CLI stopped because the account's usage limit was reached: its message, or "" otherwise. Claude Code
    reports this as the turn's result text, which must not be taken for the agent's answer or report."""
    for text in (out.error or "", out.text or ""):
        if text and len(text) < 400 and LIMIT_TEXT.search(text):
            return text.strip()
    return ""


RESET_TEXT = re.compile(r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?", re.I)
LIMIT_RETRY = 30 * 60    # seconds to wait when the message does not say when the limit resets


def limit_reset(text: str, now: float) -> float:
    """When the usage limit in Claude Code's message ("… resets 4:30am (UTC)") lifts, as a timestamp a minute
    after the stated time; without a readable time, LIMIT_RETRY from now (a still-limited retry just waits again)."""
    import datetime as dt
    m = RESET_TEXT.search(text or "")
    if not m:
        return now + LIMIT_RETRY
    hour, minute, half, zone = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), (m.group(4) or "UTC").strip()
    if half:
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return now + LIMIT_RETRY
    try:
        from zoneinfo import ZoneInfo
        tz = dt.timezone.utc if zone.upper() in ("UTC", "GMT") else ZoneInfo(zone)
    except Exception:  # noqa: BLE001 — an unknown zone name: take the time as UTC
        tz = dt.timezone.utc
    current = dt.datetime.fromtimestamp(now, tz)
    reset = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= current:
        reset += dt.timedelta(days=1)
    return reset.timestamp() + 60


def workbench_tool(name: str) -> str:
    """mcp__mch__fs__read → fs.read"""
    return (name[len(MCP_PREFIX):] if name.startswith(MCP_PREFIX) else name).replace("__", ".")


def split_command(command: str, *, windows: bool | None = None) -> list[str]:
    """Preserve a pasted executable path and Windows backslashes; never use a shell."""
    if Path(command).is_file():
        return [command]
    windows=os.name=='nt' if windows is None else windows
    if windows and not command.startswith(('\"',"'")):
        # Existing configurations often paste two absolute paths without quotes.
        # Recognize real executable prefixes without guessing non-existent paths.
        for match in reversed(list(re.finditer(r'\s+',command))):
            executable=command[:match.start()]
            if Path(executable).is_file():
                rest=command[match.end():].strip()
                return [executable,*([rest] if Path(rest).is_file() else split_command(rest,windows=True))]
    parts=shlex.split(command,posix=not windows)
    if windows:
        parts=[p[1:-1] if len(p)>=2 and p[0]==p[-1] and p[0] in ('\"',"'") else p for p in parts]
    return parts


class CLIRunner:
    kind = "cli"
    default_command = ""

    thinking = "default"          # set per turn from the agent's thinking depth (low/medium/high)

    def __init__(self, command: str, model: str):
        self.command = split_command(command) if command else [self.default_command]
        self.model = model
        if not shutil.which(self.command[0]) and not Path(self.command[0]).exists():
            raise ProviderError(f"找不到 {self.command[0]}：请先安装并登录，或在设置中填写它的完整路径")

    # -- subclass hooks ------------------------------------------------------------------
    def argv(self, prompt: str, system: str, session: str | None, mcp: MCPServer, workdir: Path) -> list[str]:
        raise NotImplementedError

    def parse(self, event: dict, out: CLIOutcome, emit: Callable[[str, dict], None]) -> None:
        raise NotImplementedError

    def env(self) -> dict:
        return {}

    def save_session(self, workdir: Path, session: str | None, folder: Path) -> None:
        """Keep a copy of the CLI's own conversation with the agent (no-op for CLIs that do not need it)."""

    def restore_session(self, workdir: Path, session: str | None, folder: Path) -> None:
        """Put the kept copy back where the CLI looks for it, if the CLI's own file is gone."""

    def see(self, image: bytes, mime: str, prompt: str, system: str = "", max_tokens: int = 2000):
        raise ProviderError(f"{self.kind} 不能直接看图：请在设置里另配一个评图模型")

    # -- driver ---------------------------------------------------------------------------
    def run(self, workdir: Path, prompt: str, system: str, session: str | None, mcp: MCPServer,
            emit: Callable[[str, dict], None], should_stop: Callable[[], bool]) -> CLIOutcome:
        out = CLIOutcome(session=session)
        argv = self.argv(prompt, system, session, mcp, workdir)
        try:
            proc = subprocess.Popen(argv, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=agent_env(os.environ, self.env()), text=True,
                                    encoding="utf-8", errors="replace", start_new_session=(os.name == "posix"))
        except OSError as exc:
            raise ProviderError(f"无法启动 {self.command[0]}：{exc}") from exc
        err_lines: list[str] = []
        threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True).start()
        watcher_done = threading.Event()

        def watch():
            while not watcher_done.wait(0.5):
                if should_stop() and proc.poll() is None:
                    proc.terminate()

        threading.Thread(target=watch, daemon=True).start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                self.parse(event, out, emit)
        finally:
            proc.wait()
            watcher_done.set()
        if proc.returncode and not should_stop() and not out.text and not out.turns:
            tail = "".join(err_lines)[-800:].strip()
            raise ProviderError(f"{self.command[0]} 退出码 {proc.returncode}" + (f"：{tail}" if tail else "") +
                                (f"；{out.error}" if out.error else ""))
        return out


class ClaudeCodeRunner(CLIRunner):
    kind = "claude_code"
    default_command = "claude"
    SESSION_COPY = "cli-session.jsonl"

    # Claude Code keeps a conversation in <config>/projects/<working folder, non-alphanumerics as "->/<id>.jsonl.
    # That folder can be lost (a new machine, a reset container, a cleaned home), and --resume then fails: the agent
    # keeps a copy next to its own files and puts it back before resuming.
    @staticmethod
    def _session_file(workdir: Path, session: str) -> Path:
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(workdir).resolve())) / f"{session}.jsonl"

    def save_session(self, workdir, session, folder):
        if not session or not re.fullmatch(r"[\w-]+", session):
            return
        src = self._session_file(workdir, session)
        if src.is_file():
            try:
                folder.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, folder / self.SESSION_COPY)
            except OSError:
                pass

    def restore_session(self, workdir, session, folder):
        if not session or not re.fullmatch(r"[\w-]+", session):
            return
        dst, copy = self._session_file(workdir, session), folder / self.SESSION_COPY
        if not dst.exists() and copy.is_file():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(copy, dst)
            except OSError:
                pass

    def argv(self, prompt, system, session, mcp, workdir):
        config = {"mcpServers": {"mch": {"type": "stdio", "command": mcp.command, "args": mcp.args, "env": mcp.env}}}
        argv = [*self.command, "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--tools", "",                         # no built-in tools: everything goes through the workbench
                "--mcp-config", json.dumps(config, ensure_ascii=False), "--strict-mcp-config",
                "--allowedTools", "mcp__mch",          # the workbench does its own permission checks and approvals
                "--setting-sources", "",
                "--append-system-prompt", system]
        if self.model and self.model not in ("default", "-"):
            argv += ["--model", self.model]
        if self.thinking in ("low", "medium", "high"):
            argv += ["--effort", self.thinking]
        argv += ["--resume", session] if session else []
        return argv

    def env(self):
        return {"MCP_TOOL_TIMEOUT": str(6 * 3600 * 1000)}  # tool calls may wait for the user's approval

    def see(self, image, mime, prompt, system="", max_tokens=2000):
        """Look at one picture: a short CLI run whose only tool is Read, on a temp copy of the image."""
        import tempfile
        from .providers import ChatResult
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(mime, ".png")
        with tempfile.TemporaryDirectory(prefix="mch-see-") as tmp:
            (Path(tmp) / f"image{ext}").write_bytes(image)
            argv = [*self.command, "-p", f"先用 Read 打开当前目录下的 image{ext}，再回答：\n\n{prompt}",
                    "--output-format", "json", "--tools", "Read", "--allowedTools", "Read",
                    "--setting-sources", "", "--strict-mcp-config", "--max-turns", "4"]
            if system:
                argv += ["--append-system-prompt", system]
            if self.model and self.model not in ("default", "-"):
                argv += ["--model", self.model]
            try:
                done = subprocess.run(argv, cwd=tmp, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                      env=agent_env(os.environ, self.env()), encoding="utf-8", errors="replace", timeout=600)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ProviderError(f"看图失败：{exc}") from exc
        try:
            data = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise ProviderError(f"看图失败：{(done.stderr or done.stdout)[-400:]}") from None
        if data.get("is_error"):
            raise ProviderError(f"看图失败：{data.get('result') or data.get('subtype')}")
        usage = data.get("usage") or {}
        read = int(usage.get("cache_read_input_tokens") or 0)
        return ChatResult(text=str(data.get("result") or ""), output_tokens=int(usage.get("output_tokens") or 0),
                          input_tokens=int(usage.get("input_tokens") or 0) + read + int(usage.get("cache_creation_input_tokens") or 0),
                          cached_tokens=read)

    def parse(self, event, out, emit):
        t = event.get("type")
        if t == "system" and event.get("session_id"):
            out.session = event["session_id"]
        elif t == "assistant":
            msg = event.get("message") or {}
            text = "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")
            calls = [{"id": b.get("id"), "name": workbench_tool(b.get("name", "")), "args": b.get("input") or {}}
                     for b in msg.get("content", []) if b.get("type") == "tool_use"]
            if text or calls:
                emit("assistant", {"text": text, "tool_calls": calls})
            out.text = text or out.text
            usage = msg.get("usage")
            if usage and msg.get("id"):           # live usage: one entry per API message, reconciled at "result"
                read = int(usage.get("cache_read_input_tokens") or 0)
                out.live[msg["id"]] = {"input_tokens": int(usage.get("input_tokens") or 0) + read
                                       + int(usage.get("cache_creation_input_tokens") or 0),
                                       "cached_tokens": read, "output_tokens": int(usage.get("output_tokens") or 0)}
                emit("usage", {"input_tokens": sum(u["input_tokens"] for u in out.live.values()),
                               "cached_tokens": sum(u["cached_tokens"] for u in out.live.values()),
                               "output_tokens": sum(u["output_tokens"] for u in out.live.values())})
        elif t == "user":
            for b in (event.get("message") or {}).get("content", []) if isinstance((event.get("message") or {}).get("content"), list) else []:
                if b.get("type") == "tool_result":
                    content = b.get("content")
                    if isinstance(content, list):
                        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                    emit("tool_result", {"id": b.get("tool_use_id"), "content": str(content or ""), "error": bool(b.get("is_error"))})
        elif t == "result":
            out.session = event.get("session_id") or out.session
            usage = event.get("usage") or {}
            read = int(usage.get("cache_read_input_tokens") or 0)
            out.input_tokens = int(usage.get("input_tokens") or 0) + read + int(usage.get("cache_creation_input_tokens") or 0)
            out.cached_tokens = read
            out.output_tokens = int(usage.get("output_tokens") or 0)
            out.cost_usd = event.get("total_cost_usd")
            out.turns = int(event.get("num_turns") or 1)
            if event.get("is_error") or str(event.get("subtype", "")).startswith("error"):
                out.error = str(event.get("result") or event.get("subtype"))
            elif event.get("result"):
                out.text = event["result"]


class CodexRunner(CLIRunner):
    """OpenAI Codex CLI (``codex exec --json``). Experimental: Codex keeps a read-only sandbox for its own shell."""
    kind = "codex"
    default_command = "codex"

    def argv(self, prompt, system, session, mcp, workdir):
        def toml(v):
            return json.dumps(v, ensure_ascii=False)
        cfg = ["-c", f"mcp_servers.mch.command={toml(mcp.command)}", "-c", f"mcp_servers.mch.args={toml(mcp.args)}",
               "-c", "mcp_servers.mch.env={" + ", ".join(f"{k} = {toml(v)}" for k, v in mcp.env.items()) + "}",
               "-c", "mcp_servers.mch.tool_timeout_sec=21600", "-c", 'approval_policy="never"']
        model = ["-m", self.model] if self.model and self.model not in ("default", "-") else []
        if session:
            return [*self.command, "exec", "resume", session, "--json", *cfg, *model, prompt]
        full = f"{system}\n\n---\n\n{prompt}"  # codex exec has no separate system-prompt flag
        return [*self.command, "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", *cfg, *model, full]

    def parse(self, event, out, emit):
        t = event.get("type")
        if t == "thread.started":
            out.session = event.get("thread_id") or out.session
        elif t == "item.completed":
            item = event.get("item") or {}
            kind = item.get("type") or item.get("item_type")
            if kind in ("agent_message", "assistant_message"):
                out.text = item.get("text", "")
                emit("assistant", {"text": out.text, "tool_calls": []})
            elif kind == "mcp_tool_call":
                call = {"id": item.get("id"), "name": workbench_tool(str(item.get("tool", ""))),
                        "args": item.get("arguments") or {}}
                emit("assistant", {"text": "", "tool_calls": [call]})
                result = item.get("result") or {}
                text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)) \
                    if isinstance(result, dict) else str(result)
                emit("tool_result", {"id": call["id"], "content": text or str(item.get("error") or ""),
                                     "error": item.get("status") == "failed"})
        elif t == "turn.completed":
            usage = event.get("usage") or {}
            out.input_tokens += int(usage.get("input_tokens") or 0)
            out.cached_tokens += int(usage.get("cached_input_tokens") or 0)
            out.output_tokens += int(usage.get("output_tokens") or 0)
            out.turns += 1
        elif t in ("turn.failed", "error"):
            err = event.get("error") or {}
            out.error = err.get("message") if isinstance(err, dict) else str(event.get("message") or err)


RUNNERS = {"claude_code": ClaudeCodeRunner, "codex": CodexRunner}


def build(provider: dict, model: str) -> CLIRunner:
    return RUNNERS[provider["type"]](provider.get("command", ""), model)


CLI_NOTE = """## 你通过工作台的工具工作
你运行在 Claude Code / Codex 中，但这里只开放了工作台的工具（名字形如 mcp__mch__fs__read，对应工作台的 fs.read）。
读写文件、运行代码和实验、推进流程、派生子 Agent、汇报，都用这些工具完成；不要尝试别的途径。
- 你写下的每一段文字用户都能看到，一律用中文，包括调用工具前顺手写的说明。
- 不要在代码里 sleep 等待：长任务用 exp.run（或后台实验）跑，等结果时直接结束本轮，结果会作为通知送回来。"""
