"""Connection checks for model providers, run from the settings page before (or after) saving:
list the models an endpoint offers, and make one tiny call (optionally with a picture) to prove that the
address, key and model name work."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request

from ..core.settings_store import MASK_PREFIX
from . import providers as P

TEST_TIMEOUT = 40

# A 32×32 red square: enough for a vision model to answer "what colour is it".
RED_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKklEQVR4nGO4pq5BU8QwasGoBaMWjFowasGoBaMWjFowasGoBaMWDBULAKSrlD1AXfa5AAAAAElFTkSuQmCC")

# Common services: picking one fills in the type and address; models are fetched from the endpoint.
PRESETS = [
    {"id": "deepseek", "name": "DeepSeek", "type": "openai_compat", "base_url": "https://api.deepseek.com"},
    {"id": "anthropic", "name": "Anthropic", "type": "anthropic", "base_url": ""},
    {"id": "openai", "name": "OpenAI", "type": "openai_compat", "base_url": "https://api.openai.com/v1"},
    {"id": "qwen", "name": "通义千问", "type": "openai_compat", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"id": "kimi", "name": "Kimi", "type": "openai_compat", "base_url": "https://api.moonshot.cn/v1"},
    {"id": "glm", "name": "智谱 GLM", "type": "openai_compat", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    {"id": "siliconflow", "name": "硅基流动", "type": "openai_compat", "base_url": "https://api.siliconflow.cn/v1"},
    {"id": "openrouter", "name": "OpenRouter", "type": "openai_compat", "base_url": "https://openrouter.ai/api/v1"},
    {"id": "ollama", "name": "Ollama（本机）", "type": "openai_compat", "base_url": "http://127.0.0.1:11434/v1"},
]


def resolve_key(provider: dict, stored: list[dict]) -> dict:
    """The form shows a masked key; use the saved one for the same provider."""
    key = str(provider.get("api_key", ""))
    if key.startswith(MASK_PREFIX):
        key = next((p.get("api_key", "") for p in stored if p.get("id") == provider.get("id")), "")
    return {**provider, "api_key": key, "enabled": True, "id": provider.get("id") or "probe"}


def _get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise P.ProviderError(f"{exc.code}：{exc.read(800).decode('utf-8', 'replace')[:300]}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise P.ProviderError(f"无法连接 {url}：{getattr(exc, 'reason', exc)}") from exc
    except ValueError as exc:
        raise P.ProviderError(f"{url} 返回的不是 JSON") from exc


def list_models(provider: dict) -> list[str]:
    ptype, key = provider.get("type"), provider.get("api_key", "")
    if ptype == "anthropic":
        base = (provider.get("base_url") or "https://api.anthropic.com").rstrip("/")
        if not key:
            raise P.ProviderError("缺少 API Key")
        data = _get(base + "/v1/models?limit=100", {"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif ptype == "openai_compat":
        base = (provider.get("base_url") or "").rstrip("/")
        if not base:
            raise P.ProviderError("缺少接口地址 base_url")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        data = _get(base + "/models", {"Authorization": f"Bearer {key}"} if key else {})
    else:
        raise P.ProviderError("命令行提供方（Claude Code / Codex）没有模型列表，直接填模型名")
    ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
    if not ids:
        raise P.ProviderError("接口没有返回模型列表，请手动填写模型名")
    return sorted(ids)


def test(provider: dict, model: str, vision: bool = False) -> dict:
    """One short call. Returns ok, latency, the reply and token counts, or the error in plain words."""
    if not model:
        return {"ok": False, "error": "先选一个模型"}
    try:
        client = P.build(f"{provider['id']}:{model}", [provider])
    except P.ProviderError as exc:
        return {"ok": False, "error": str(exc)}
    client.timeout, client.attempts = TEST_TIMEOUT, 1
    start = time.time()
    try:
        if vision:
            r = client.see(RED_PNG, "image/png", "这张图片是什么颜色？只回答颜色。", max_tokens=50)
        else:
            r = client.chat("你是连接测试。", [{"role": "user", "content": "只回复两个字：收到"}], [], max_tokens=50)
    except P.ProviderError as exc:
        return {"ok": False, "error": str(exc), "hint": _hint(str(exc), vision)}
    except Exception as exc:  # noqa: BLE001 — CLI providers may fail in their own ways
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out = {"ok": True, "seconds": round(time.time() - start, 1), "reply": (r.text or "").strip()[:200],
           "input_tokens": r.input_tokens, "output_tokens": r.output_tokens}
    if vision:
        out["sees"] = any(w in out["reply"].lower() for w in ("红", "red"))
        if not out["sees"]:
            out["hint"] = "模型有回复，但没认出图片颜色：它可能不支持看图，评图模型请换一个支持图片输入的。"
    return out


def _hint(error: str, vision: bool) -> str:
    e = error.lower()
    if "401" in e or "403" in e or "unauthorized" in e or "invalid api key" in e or "authentication" in e:
        return "API Key 不对或没有权限：检查是否复制完整、是否已开通这个模型。"
    if "404" in e or "model_not_found" in e or "does not exist" in e or "not found" in e:
        return "接口地址或模型名不对：点“获取模型列表”选一个，或核对 base_url（很多服务需要以 /v1 结尾）。"
    if "无法连接" in error or "timed out" in e or "超时" in error:
        return "连不上接口：检查网络、代理和 base_url；国内服务一般不需要代理，海外服务可能需要。"
    if "429" in e:
        return "请求太频繁或余额不足：稍后再试，或检查账户余额。"
    if vision and ("image" in e or "vision" in e or "不支持看图" in error):
        return "这个模型不支持图片输入，评图模型请换一个支持看图的。"
    return ""
