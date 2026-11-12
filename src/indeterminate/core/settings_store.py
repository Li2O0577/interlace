"""Global user settings (model providers, defaults), stored in ``~/.indeterminate``.

API keys never leave the server in clear text: reads return a masked value,
and writes that send the masked value back keep the stored key unchanged.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
from pathlib import Path
from typing import Any
from .jsonfile import replace_file

PROVIDER_TYPES = {"anthropic", "openai_compat", "claude_code", "codex"}
MASK_PREFIX = "••••"

DEFAULT_SETTINGS: dict[str, Any] = {
    "providers": [],
    "defaults": {"autonomy": "checkpoints", "language": "zh", "max_parallel_agents": 4, "local_max_jobs": 2,
                 "agent_model": "", "vision_model": "", "assistant_model": "", "max_agent_depth": 3, "context_tokens": 100_000, "max_output_tokens": 16_000,
                 "lead_budget_tokens": 3_000_000, "sub_budget_tokens": 400_000,
                 "auto_resume_agents": True, "stream": True,
                 # thinking depth: default (the service decides) | low | medium | high; budgets are Anthropic thinking tokens
                 "thinking_lead": "default", "thinking_sub": "default", "thinking_worker": "high",
                 "thinking_budgets": {"low": 2048, "medium": 8000, "high": 24000}},
    "github": {"token": "", "user": ""},
    "hosts": [],
    "research": {"provider": "", "api_key": "", "base_url": "", "semantic_key": "", "mailto": "",
                 "allow_private_urls": False},
    # limits for code that agents run on this machine (see core/sandbox.py)
    "sandbox": {"enabled": True, "extra_write_dirs": [], "block_network": False, "max_memory_mb": 0},
}


def mask_key(key: str) -> str:
    if not key:
        return ""
    return MASK_PREFIX + key[-4:] if len(key) > 8 else MASK_PREFIX


class SettingsError(Exception):
    pass


class SettingsStore:
    def __init__(self, home: Path):
        self.path = home / "settings.json"
        self._cache: tuple[tuple, dict] | None = None   # (file signature, parsed settings)

    def _load(self) -> dict:
        """Read on every call by agents (defaults, model prices …): parsed once per file version, copied out."""
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return json.loads(json.dumps(DEFAULT_SETTINGS))
        if self._cache is None or self._cache[0] != sig:
            self._cache = (sig, self._parse())
        return copy.deepcopy(self._cache[1])

    def _parse(self) -> dict:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for key, value in DEFAULT_SETTINGS.items():
            data.setdefault(key, json.loads(json.dumps(value)))
        for section in ("defaults", "research", "sandbox"):
            for key, value in DEFAULT_SETTINGS[section].items():
                data[section].setdefault(key, value)
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        replace_file(tmp,self.path)
        self._cache = None

    def public(self) -> dict:
        data = self._load()
        for provider in data["providers"]:
            provider["api_key"] = mask_key(provider.get("api_key", ""))
        data["github"]["token"] = mask_key(data["github"].get("token", ""))
        for key in ("api_key", "semantic_key"):
            data["research"][key] = mask_key(data["research"].get(key, ""))
        return data

    def set_github_user(self, user: str) -> None:
        current = self._load()
        current["github"]["user"] = user
        self._save(current)

    def update(self, incoming: dict) -> dict:
        current = self._load()
        stored_keys = {p["id"]: p.get("api_key", "") for p in current["providers"]}

        providers = []
        for raw in incoming.get("providers", current["providers"]):
            ptype = raw.get("type")
            if ptype not in PROVIDER_TYPES:
                raise SettingsError(f"未知的提供方类型: {ptype}")
            name = str(raw.get("name", "")).strip()
            if not name:
                raise SettingsError("提供方名称不能为空")
            pid = raw.get("id") or secrets.token_hex(4)
            key = raw.get("api_key", "")
            if key.startswith(MASK_PREFIX):
                key = stored_keys.get(pid, "")
            models = raw.get("models", [])
            if isinstance(models, str):
                models = [m.strip() for m in models.split(",")]
            providers.append({
                "prices": _clean_prices(raw.get("prices", {})),
                "currency": str(raw.get("currency", "¥")).strip()[:3] or "¥",
                "id": pid,
                "name": name,
                "type": ptype,
                "base_url": str(raw.get("base_url", "")).strip(),
                "api_key": key,
                "command": str(raw.get("command", "")).strip(),
                "models": [m for m in models if m],
                "enabled": bool(raw.get("enabled", True)),
            })
        current["providers"] = providers
        if "defaults" in incoming:
            current["defaults"].update(incoming["defaults"])
            current["defaults"]["role_models"] = _clean_role_models(current["defaults"].get("role_models") or {})
        if "research" in incoming:
            r = incoming["research"] or {}
            old = current["research"]
            provider = str(r.get("provider", old.get("provider", ""))).strip()
            if provider not in ("", "tavily", "brave", "searxng"):
                raise SettingsError(f"未知的网页搜索服务: {provider}")
            secret = lambda k: (old.get(k, "") if str(r.get(k, "")).startswith(MASK_PREFIX)  # noqa: E731
                                else str(r.get(k, old.get(k, ""))).strip())
            current["research"] = {
                "provider": provider, "api_key": secret("api_key"), "semantic_key": secret("semantic_key"),
                "base_url": str(r.get("base_url", old.get("base_url", ""))).strip(),
                "mailto": str(r.get("mailto", old.get("mailto", ""))).strip(),
                "allow_private_urls": bool(r.get("allow_private_urls", old.get("allow_private_urls", False))),
            }
        if "sandbox" in incoming:
            sb = incoming["sandbox"] or {}
            dirs = sb.get("extra_write_dirs", current["sandbox"].get("extra_write_dirs", []))
            if isinstance(dirs, str):
                dirs = dirs.splitlines()
            try:
                memory = max(0, int(sb.get("max_memory_mb", current["sandbox"].get("max_memory_mb", 0)) or 0))
            except (TypeError, ValueError) as exc:
                raise SettingsError("内存上限必须是整数（MB，0 表示不限制）") from exc
            current["sandbox"] = {
                "enabled": bool(sb.get("enabled", current["sandbox"].get("enabled", True))),
                "extra_write_dirs": [str(d).strip() for d in dirs if str(d).strip()],
                "block_network": bool(sb.get("block_network", current["sandbox"].get("block_network", False))),
                "max_memory_mb": memory,
            }
        if "hosts" in incoming:
            current["hosts"] = [_clean_host(h) for h in incoming["hosts"] or []]
        if "github" in incoming:
            gh = incoming["github"] or {}
            token = str(gh.get("token", current["github"].get("token", ""))).strip()
            if token.startswith(MASK_PREFIX):
                token = current["github"].get("token", "")
            current["github"] = {"token": token, "user": str(gh.get("user", current["github"].get("user", ""))).strip()}
        self._save(current)
        return self.public()

    def raw(self) -> dict:
        """Full settings including secrets; for server-side use only."""
        return self._load()

    def model_catalog(self) -> dict:
        """Configured API/CLI choices only; never provider credentials or commands."""
        data=self._load()
        models=[{'id':f'{p["id"]}:{model}','label':f'{p.get("name",p["id"])} · {model}'}
                for p in data.get('providers',[]) if p.get('enabled',True) and p.get('type') in PROVIDER_TYPES
                for model in p.get('models',[])]
        return {'models':models,'default':data.get('defaults',{}).get('agent_model','')}


def _clean_host(raw: dict) -> dict:
    name = str(raw.get("name", "")).strip()
    host = str(raw.get("host", "")).strip()
    if not name or not host:
        raise SettingsError("SSH 主机需要填写名称和地址（或 ~/.ssh/config 中的别名）")
    if any(c.isspace() for c in host) or host.startswith("-"):
        raise SettingsError(f"主机地址不合法: {host}")
    user = str(raw.get("user", "")).strip()
    if user.startswith("-") or any(c.isspace() for c in user):
        raise SettingsError(f"用户名不合法: {user}")
    try:
        port = int(raw.get("port") or 22)
        max_jobs = int(raw.get("max_jobs") or 2)
    except (TypeError, ValueError) as exc:
        raise SettingsError("端口和并行数必须是整数") from exc
    if not 0 < port < 65536 or not 0 < max_jobs <= 64:
        raise SettingsError("端口或并行数超出范围")
    return {
        "id": str(raw.get("id") or secrets.token_hex(4)),
        "name": name, "host": host, "user": user, "port": port,
        "identity_file": str(raw.get("identity_file", "")).strip(),
        "remote_root": str(raw.get("remote_root", "")).strip() or "~/indeterminate-runs",
        "max_jobs": max_jobs,
        "ssh_command": str(raw.get("ssh_command", "")).strip() or "ssh",
    }


def _clean_prices(raw) -> dict:
    """{model: {input, cached, output}} in currency per million tokens. Also accepts the text form
    used by the settings page: one ``model = input, cached, output`` per line."""
    if isinstance(raw, str):
        parsed = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise SettingsError(f"价格格式应为 模型 = 输入, 缓存命中, 输出：{line}")
            model, nums = line.split("=", 1)
            parts = [x.strip() for x in nums.replace("，", ",").split(",")]
            if len(parts) == 2:
                parts = [parts[0], parts[0], parts[1]]
            if len(parts) != 3:
                raise SettingsError(f"价格需要 输入, 缓存命中, 输出 三个数（或 输入, 输出 两个数）：{line}")
            parsed[model.strip()] = dict(zip(("input", "cached", "output"), parts))
        raw = parsed
    clean = {}
    for model, p in (raw or {}).items():
        try:
            clean[str(model)] = {k: float(p.get(k, p.get("input", 0) if k == "cached" else 0)) for k in ("input", "cached", "output")}
        except (TypeError, ValueError, AttributeError) as exc:
            raise SettingsError(f"模型 {model} 的价格不是数字") from exc
        if any(v < 0 for v in clean[str(model)].values()):
            raise SettingsError(f"模型 {model} 的价格不能为负")
    return clean


def _clean_role_models(raw) -> dict:
    """{role: "provider:model"}; also accepts lines of ``role = provider:model``."""
    if isinstance(raw, str):
        pairs = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise SettingsError(f"格式应为 角色 = 提供方:模型：{line}")
            role, model = (x.strip() for x in line.split("=", 1))
            pairs[role] = model
        raw = pairs
    from ..agents.kinds import DEFAULT  # local import: settings must not depend on the agent runtime at import time
    assignable = DEFAULT.assignable()
    clean = {}
    for role, model in raw.items():
        if role not in assignable:
            raise SettingsError(f"未知角色：{role}（可用：{', '.join(assignable)}）")
        if model and ":" not in str(model):
            raise SettingsError(f"{role} 的模型格式应为 提供方:模型")
        if model:
            clean[role] = str(model)
    return clean
