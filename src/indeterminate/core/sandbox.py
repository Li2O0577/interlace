"""Limits for code that agents run on this machine (code.run and the processes it starts).

This is a guard rail, not a container: it keeps ordinary analysis code from
reaching outside the project by accident or by a model's mistake.

- **Environment**: the child process does not inherit secrets (variables whose
  names look like keys, tokens or passwords), so code cannot read the API keys
  the engine was started with.
- **Writes**: Python processes start with an audit hook (``sitecustomize``)
  that refuses to create, change or delete files outside the project folder
  and the temporary directory. Python child processes inherit it.
- **Shell commands**: obviously destructive commands (sudo, rm on paths
  outside the project, mkfs, dd, shutdown, piping a download into a shell)
  are refused before they start.
- **Optional**: block network connections from the code, cap its memory.

Commands that run on SSH hosts are not affected: those machines are the
user's own servers.

Settings live under ``sandbox`` in the global settings.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

DEFAULTS = {"enabled": True, "extra_write_dirs": [], "block_network": False, "max_memory_mb": 0}
GUARD_DIR = Path(__file__).resolve().parent / "_sandbox"
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|COOKIE|SESSION|AUTH)", re.I)
# Variables that look like secrets by name but are needed and harmless.
KEEP = {"SSH_AUTH_SOCK", "XAUTHORITY", "KRB5CCNAME"}


class SandboxError(Exception):
    pass


def config(settings: dict | None) -> dict:
    cfg = {**DEFAULTS, **((settings or {}).get("sandbox") or {})}
    cfg["extra_write_dirs"] = [str(d) for d in cfg.get("extra_write_dirs") or [] if str(d).strip()]
    try:
        cfg["max_memory_mb"] = max(0, int(cfg.get("max_memory_mb") or 0))
    except (TypeError, ValueError):
        cfg["max_memory_mb"] = 0
    return cfg


def scrub(env: dict) -> dict:
    """Drop variables that look like credentials."""
    return {k: v for k, v in env.items() if k in KEEP or not SECRET_NAME.search(k)}


def env_for(root: Path, cfg: dict, base: dict | None = None, extra: dict | None = None) -> dict:
    """Environment for a child process that runs project code."""
    env = dict(os.environ if base is None else base)
    env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
    # `python` in a shell command is the engine's own interpreter, with its packages, not whatever is first on PATH.
    bindir = str(Path(sys.executable).parent)
    if env.get("PATH", "").split(os.pathsep)[0] != bindir:
        env["PATH"] = os.pathsep.join([bindir, *([env["PATH"]] if env.get("PATH") else [])])
    if not cfg.get("enabled", True):
        return {**env, **(extra or {})}
    env = scrub(env)
    root = Path(root).resolve()
    cache = root / "agents" / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    allowed = [str(root), tempfile.gettempdir(), *cfg.get("extra_write_dirs", [])]
    env.update({
        "INDETERMINATE_SANDBOX_ROOTS": os.pathsep.join(allowed),
        "INDETERMINATE_SANDBOX_NET": "0" if cfg.get("block_network") else "1",
        "PYTHONPATH": os.pathsep.join([str(GUARD_DIR), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]),
        "PYTHONDONTWRITEBYTECODE": "1",
        # tools that keep caches in the home folder write them inside the project instead
        "MPLCONFIGDIR": str(cache / "matplotlib"), "XDG_CACHE_HOME": str(cache),
        "JUPYTER_RUNTIME_DIR": str(cache / "jupyter"), "IPYTHONDIR": str(cache / "ipython"),
    })
    env.update(extra or {})
    return env


def shell_command(command: str) -> str:
    """Quote a leading current interpreter on cmd.exe; retain shell syntax.

    Never guess other executable names or rewrite the command's arguments.
    """
    if os.name=='nt' and command.startswith(sys.executable+' '):
        return '"'+sys.executable+'"'+command[len(sys.executable):]
    return command


def preexec(cfg: dict):
    """A ``preexec_fn`` that caps memory (POSIX only), or None."""
    limit = cfg.get("max_memory_mb", 0) if cfg.get("enabled", True) else 0
    if not limit or os.name != "posix":
        return None

    def apply():
        import resource
        size = int(limit) * 2**20
        resource.setrlimit(resource.RLIMIT_AS, (size, size))
    return apply


_DANGER = [
    (r"(^|[;&|`(]\s*)sudo\b", "不允许使用 sudo"),
    (r"(^|[;&|`(]\s*)su\s", "不允许切换用户"),
    (r"\bmkfs(\.\w+)?\b", "不允许格式化磁盘"),
    (r"(^|[;&|`(]\s*)dd\s+.*\bof=", "不允许用 dd 写设备或文件"),
    (r"\b(shutdown|reboot|poweroff|halt)\b", "不允许关机或重启"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k|da)?sh\b", "不允许把下载的内容直接交给 shell 执行"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "不允许 fork 炸弹"),
    (r">\s*/dev/(sd|nvme|disk|hd)", "不允许写磁盘设备"),
]
_WRITERS = {"rm", "rmdir", "mv", "cp", "chmod", "chown", "ln", "truncate", "shred", "tee", "touch", "mkdir", "install", "rsync"}


def check_command(command: str, root: Path, cfg: dict) -> None:
    """Refuse shell commands that are destructive or write outside the allowed folders."""
    if not cfg.get("enabled", True):
        return
    for pattern, reason in _DANGER:
        if re.search(pattern, command):
            raise SandboxError(f"命令被拒绝：{reason}")
    allowed = [Path(root).resolve(), Path(tempfile.gettempdir()).resolve(),
               *(Path(d).expanduser().resolve() for d in cfg.get("extra_write_dirs", []))]
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    targets: list[str] = []
    prog = ""
    for i, w in enumerate(words):
        if i == 0 or words[i - 1] in (";", "&&", "||", "|", "&"):
            prog = os.path.basename(w)
            continue
        if prog in _WRITERS and not w.startswith("-"):
            targets.append(w)
    targets += re.findall(r">>?\s*([^\s;&|]+)", command)
    for t in targets:
        if t.startswith("&") or t in ("/dev/null", "/dev/stdout", "/dev/stderr"):
            continue
        if not (t.startswith("/") or t.startswith("~") or ".." in Path(t).parts or re.match(r"^[A-Za-z]:[\\/]", t)):
            continue
        path = Path(os.path.expanduser(t))
        path = (Path(root) / path).resolve() if not path.is_absolute() else path.resolve()
        if not any(path == a or a in path.parents for a in allowed):
            raise SandboxError(f"命令被拒绝：不能修改项目文件夹以外的位置（{t}）。如确有需要，请在 设置 → 代码执行 中添加允许写入的目录")


def describe(cfg: dict) -> dict:
    return {"enabled": cfg.get("enabled", True), "python": sys.executable, "guard": str(GUARD_DIR)}
