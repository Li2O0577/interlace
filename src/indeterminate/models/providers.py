"""Model providers behind one small interface.

Messages use a provider-neutral shape:

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [{"id": "...", "name": "fs.read", "args": {...}}]}
    {"role": "tool", "tool_call_id": "...", "name": "fs.read", "content": "..."}

Tool names contain dots (``fs.read``); APIs only accept ``[a-zA-Z0-9_-]``, so
they are sent as ``fs__read`` and mapped back.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

TIMEOUT = 300


class ProviderError(Exception):
    pass


@dataclass
class ChatResult:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    input_tokens: int = 0      # the whole prompt, cached part included
    output_tokens: int = 0
    stop_reason: str = ""
    cached_tokens: int = 0     # prompt tokens served from the provider's prompt cache (billed far cheaper)
    reasoning: str = ""        # the model's visible thinking, when the provider returns it (shown folded in the UI)
    thinking_blocks: list = field(default_factory=list)   # Anthropic thinking blocks, sent back in later steps


THINKING_LEVELS = ("default", "low", "medium", "high")
THINKING_BUDGETS = {"low": 2048, "medium": 8000, "high": 24000}


def image_parts(m: dict) -> list[tuple[str, str]]:
    """(mime, base64) pairs attached to a user message for this call."""
    return m.get("image_data") or []


def api_name(name: str) -> str:
    return name.replace(".", "__")


def tool_name(api: str) -> str:
    return api.replace("__", ".")


RETRY_CODES = (429, 500, 502, 503, 504, 529)
RETRY_WAITS = (5, 15, 30, 60)   # seconds; rate limits per minute need more than a quick second try


class Busy(Exception):
    """The service is rate-limiting or overloaded (worth waiting and asking again)."""

    def __init__(self, error: "ProviderError", wait: float | None = None):
        super().__init__(str(error))
        self.error, self.wait = error, wait


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    try:
        return min(120.0, float(exc.headers.get("Retry-After")))
    except (TypeError, ValueError, AttributeError):
        return None


def _post(url: str, body: dict, headers: dict, timeout: float = TIMEOUT, attempts: int = 3) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    last_error = None
    for attempt in range(attempts):  # retry rate limits / overload with backoff
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read(3000).decode("utf-8", "replace")
            last_error = ProviderError(f"模型接口返回 {exc.code}：{detail[:500]}")
            if exc.code not in RETRY_CODES:
                raise last_error from exc
            wait = _retry_after(exc)
        except urllib.error.URLError as exc:
            last_error, wait = ProviderError(f"无法连接模型接口 {url}：{exc.reason}"), None
        except TimeoutError:
            last_error, wait = ProviderError(f"模型接口超时：{url}"), None
        except (http.client.HTTPException, ConnectionError, OSError) as exc:
            last_error, wait = ProviderError(f"与模型接口的连接中断：{type(exc).__name__}: {exc}"), None
        except ValueError as exc:
            raise ProviderError(f"模型接口返回的不是有效的 JSON：{exc}") from exc
        if attempt + 1 < attempts:
            time.sleep(wait or RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
    raise last_error


def _sse(url: str, body: dict, headers: dict):
    """POST with ``stream: true`` and yield the JSON payload of each server-sent event."""
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "text/event-stream", **headers})
    try:
        resp = urllib.request.urlopen(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        error = ProviderError(f"模型接口返回 {exc.code}：{exc.read(3000).decode('utf-8', 'replace')[:500]}")
        if exc.code in RETRY_CODES:
            raise Busy(error, _retry_after(exc)) from exc
        raise error from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise ProviderError(f"无法连接模型接口 {url}：{exc}") from exc
    try:
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    yield json.loads(data)
                except ValueError:
                    continue
    except (http.client.HTTPException, OSError) as exc:
        raise StreamCut(f"流式输出中断：{type(exc).__name__}: {exc}") from exc


class StreamCut(ProviderError):
    """The stream ended before the service said the reply was complete (a dropped connection or a gateway
    closing a long response): what arrived is partial, so it must not be used as the reply."""


class Provider:
    timeout: float = TIMEOUT   # per request; a connection test uses a short one
    attempts: int = 4          # tries on rate limits / overload (waits RETRY_WAITS between them)
    thinking: str = "default"  # default (the service's own behaviour) | low | medium | high
    thinking_budgets: dict = THINKING_BUDGETS

    def chat(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 4096,
             on_text: Callable[[str], None] | None = None) -> ChatResult:
        """Run one model call. With ``on_text``, text is streamed to it as it arrives; if streaming
        fails before anything was produced, the call falls back to a normal request."""
        if on_text is None:
            return self._complete(system, messages, tools, max_tokens)
        produced = []

        def relay(text: str) -> None:
            produced.append(text)
            on_text(text)
        cuts = 0
        for attempt in range(self.attempts + 1):
            try:
                return self._stream(system, messages, tools, max_tokens, relay)
            except Busy as busy:  # rate limit / overload before anything arrived: wait and ask again
                if attempt + 1 >= self.attempts:
                    raise busy.error from None
                time.sleep(busy.wait or RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)])
            except StreamCut:  # partial output: ask again; the full message replaces the streamed preview
                cuts += 1
                if cuts > 1:
                    break
            except ProviderError:
                if produced:
                    raise
                break
        return self._complete(system, messages, tools, max_tokens)

    def _complete(self, system, messages, tools, max_tokens) -> ChatResult:
        raise NotImplementedError

    def see(self, image: bytes, mime: str, prompt: str, system: str = "", max_tokens: int = 2000) -> ChatResult:
        """One question about one picture. Providers without image input raise ProviderError."""
        raise ProviderError("这个模型提供方不支持看图")

    def _stream(self, system, messages, tools, max_tokens, on_text) -> ChatResult:
        result = self._complete(system, messages, tools, max_tokens)
        if result.text:
            on_text(result.text)
        return result


class AnthropicProvider(Provider):
    def __init__(self, api_key: str, model: str, base_url: str = ""):
        if not api_key:
            raise ProviderError("Anthropic 提供方缺少 API Key")
        self.api_key, self.model = api_key, model
        self.url = (base_url.rstrip("/") or "https://api.anthropic.com") + "/v1/messages"

    def _body(self, system, messages, tools, max_tokens):
        # Prompt caching: breakpoints after the tools, after the stable part of the system prompt and at the end
        # of the conversation, so each step re-reads the long, unchanged prefix at the cached price.
        cache = {"type": "ephemeral"}
        static, dynamic = getattr(system, "static", ""), getattr(system, "dynamic", "")
        if static:
            sys_blocks = [{"type": "text", "text": static, "cache_control": cache}]
            if dynamic:
                sys_blocks.append({"type": "text", "text": dynamic})
        else:
            sys_blocks = [{"type": "text", "text": str(system) or "（无）", "cache_control": cache}]
        converted = self._convert(messages, keep_thinking=bool(self._thinking_budget(messages)))
        if converted and converted[-1]["content"]:
            last = converted[-1]["content"][-1]
            converted[-1]["content"][-1] = {**last, "cache_control": cache}
        body = {"model": self.model, "max_tokens": max_tokens, "system": sys_blocks, "messages": converted}
        budget = self._thinking_budget(messages)
        if budget:
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            body["max_tokens"] = max(max_tokens, budget + 4096)
        if tools:
            body["tools"] = [{"name": api_name(t["name"]), "description": t["description"][:4096],
                              "input_schema": t["parameters"]} for t in tools]
            body["tools"][-1]["cache_control"] = cache
        return body

    def _headers(self):
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    def _thinking_budget(self, messages) -> int:
        """Extended thinking budget, or 0. In the middle of a tool loop the API needs the previous step's thinking
        block back; if that step ran without thinking (the level was just switched), this step does too."""
        budget = int(self.thinking_budgets.get(self.thinking, 0) or 0) if self.thinking in ("low", "medium", "high") else 0
        if not budget:
            return 0
        last = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        if last and last.get("tool_calls") and not last.get("thinking_blocks") and messages[-1]["role"] == "tool":
            return 0
        return max(1024, budget)

    def _stream(self, system, messages, tools, max_tokens, on_text):
        result, blocks = ChatResult(), {}
        for ev in _sse(self.url, {**self._body(system, messages, tools, max_tokens), "stream": True}, self._headers()):
            kind = ev.get("type")
            if kind == "message_start":
                result.input_tokens, result.cached_tokens = _anthropic_usage((ev.get("message") or {}).get("usage", {}))
            elif kind == "content_block_start":
                b = ev.get("content_block") or {}
                blocks[ev.get("index", 0)] = {"type": b.get("type"), "id": b.get("id"), "name": b.get("name"), "json": "",
                                              "thinking": "", "signature": "", "data": b.get("data")}
            elif kind == "content_block_delta":
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta":
                    result.text += d.get("text", "")
                    on_text(d.get("text", ""))
                elif d.get("type") == "input_json_delta" and ev.get("index", 0) in blocks:
                    blocks[ev["index"]]["json"] += d.get("partial_json", "")
                elif d.get("type") == "thinking_delta" and ev.get("index", 0) in blocks:
                    blocks[ev["index"]]["thinking"] += d.get("thinking", "")
                elif d.get("type") == "signature_delta" and ev.get("index", 0) in blocks:
                    blocks[ev["index"]]["signature"] += d.get("signature", "")
            elif kind == "message_delta":
                result.stop_reason = (ev.get("delta") or {}).get("stop_reason") or ""
                result.output_tokens = (ev.get("usage") or {}).get("output_tokens", 0)
            elif kind == "error":
                raise ProviderError(f"模型接口报错：{(ev.get('error') or {}).get('message', ev)}")
        if not result.stop_reason:
            raise StreamCut("流式输出在模型说完之前断开了")
        for _, b in sorted(blocks.items()):
            if b["type"] == "tool_use":
                result.tool_calls.append({"id": b["id"], "name": tool_name(b["name"] or ""), "args": _parse_args(b["json"])})
            elif b["type"] == "thinking":
                result.thinking_blocks.append({"type": "thinking", "thinking": b["thinking"], "signature": b["signature"]})
                result.reasoning += b["thinking"]
            elif b["type"] == "redacted_thinking":
                result.thinking_blocks.append({"type": "redacted_thinking", "data": b["data"]})
        return result

    def _complete(self, system, messages, tools, max_tokens=4096):
        data = _post(self.url, self._body(system, messages, tools, max_tokens), self._headers(), self.timeout, self.attempts)
        result = ChatResult(stop_reason=data.get("stop_reason", ""))
        for block in data.get("content", []):
            if block.get("type") == "text":
                result.text += block.get("text", "")
            elif block.get("type") == "tool_use":
                result.tool_calls.append({"id": block["id"], "name": tool_name(block["name"]), "args": block.get("input") or {}})
            elif block.get("type") in ("thinking", "redacted_thinking"):
                result.thinking_blocks.append(block)
                result.reasoning += block.get("thinking", "")
        usage = data.get("usage", {})
        result.input_tokens, result.cached_tokens = _anthropic_usage(usage)
        result.output_tokens = usage.get("output_tokens", 0)
        return result

    def see(self, image, mime, prompt, system="", max_tokens=2000):
        import base64
        body = {"model": self.model, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(image).decode()}},
                    {"type": "text", "text": prompt}]}]}
        if system:
            body["system"] = system
        data = _post(self.url, body, self._headers(), self.timeout, self.attempts)
        result = ChatResult(text="".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"),
                            stop_reason=data.get("stop_reason", ""))
        result.input_tokens, result.cached_tokens = _anthropic_usage(data.get("usage", {}))
        result.output_tokens = (data.get("usage") or {}).get("output_tokens", 0)
        return result

    @staticmethod
    def _convert(messages, keep_thinking: bool = False):
        out: list[dict] = []

        def push(role, blocks):
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})

        for m in messages:
            if m["role"] == "user":
                push("user", [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
                              for mime, data in image_parts(m)] + [{"type": "text", "text": m["content"] or "（空）"}])
            elif m["role"] == "assistant":
                blocks = list(m.get("thinking_blocks") or []) if keep_thinking else []
                blocks += [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": api_name(c["name"]), "input": c["args"]}
                           for c in m.get("tool_calls", [])]
                push("assistant", blocks or [{"type": "text", "text": "（继续）"}])
            elif m["role"] == "tool":
                push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"],
                               **({"is_error": True} if m.get("error") else {})}])
        if out and out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "开始。"}]})
        return out


_NO_EFFORT: set[tuple[str, str]] = set()   # (url, model) pairs that rejected reasoning_effort


class OpenAICompatProvider(Provider):
    """Chat Completions API: OpenAI, DeepSeek, Qwen, Kimi, GLM, Ollama, vLLM …"""

    def __init__(self, api_key: str, model: str, base_url: str):
        if not base_url:
            raise ProviderError("OpenAI 兼容提供方缺少 base_url")
        self.api_key, self.model = api_key, model
        base = base_url.rstrip("/")
        self.url = base if base.endswith("/chat/completions") else base + "/chat/completions"

    def chat(self, system, messages, tools, max_tokens=4096, on_text=None):
        try:
            return super().chat(system, messages, tools, max_tokens, on_text)
        except ProviderError as exc:
            # Many compatible services reject reasoning_effort: remember that and ask again without it.
            if self._effort() and any(w in str(exc) for w in ("400", "422")) and \
                    any(w in str(exc).lower() for w in ("reasoning", "unknown", "unrecognized", "extra", "unsupported", "invalid")):
                _NO_EFFORT.add((self.url, self.model))
                return super().chat(system, messages, tools, max_tokens, on_text)
            raise

    def _effort(self) -> str:
        return self.thinking if self.thinking in ("low", "medium", "high") and (self.url, self.model) not in _NO_EFFORT else ""

    def _body(self, system, messages, tools, max_tokens):
        body = {"model": self.model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, *self._convert(messages)]}
        if self._effort():
            body["reasoning_effort"] = self._effort()
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": api_name(t["name"]), "description": t["description"],
                                                               "parameters": t["parameters"]}} for t in tools]
        return body

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _stream(self, system, messages, tools, max_tokens, on_text):
        body = {**self._body(system, messages, tools, max_tokens), "stream": True, "stream_options": {"include_usage": True}}
        result, calls = ChatResult(), {}
        for ev in _sse(self.url, body, self._headers()):
            usage = ev.get("usage") or {}
            if usage:
                result.input_tokens = usage.get("prompt_tokens", result.input_tokens)
                result.output_tokens = usage.get("completion_tokens", result.output_tokens)
                result.cached_tokens = _openai_cached(usage) or result.cached_tokens
            for choice in ev.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    result.text += delta["content"]
                    on_text(delta["content"])
                if delta.get("reasoning_content"):
                    result.reasoning += delta["reasoning_content"]
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
                if choice.get("finish_reason"):
                    result.stop_reason = choice["finish_reason"]
        if not result.stop_reason:
            raise StreamCut("流式输出在模型说完之前断开了")
        for i, (_, c) in enumerate(sorted(calls.items())):
            result.tool_calls.append({"id": c["id"] or f"call_{i}", "name": tool_name(c["name"]), "args": _parse_args(c["args"])})
        if not result.input_tokens:  # some compatible servers send no usage while streaming
            result.input_tokens = len(json.dumps(body["messages"], ensure_ascii=False)) // 3
            result.output_tokens = (len(result.text) + sum(len(c["args"]) for c in calls.values())) // 3
        return result

    def _complete(self, system, messages, tools, max_tokens=4096):
        data = _post(self.url, self._body(system, messages, tools, max_tokens), self._headers(), self.timeout, self.attempts)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        result = ChatResult(text=msg.get("content") or "", stop_reason=choice.get("finish_reason", ""),
                            reasoning=msg.get("reasoning_content") or "")
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_raw": fn.get("arguments")}
            result.tool_calls.append({"id": c.get("id") or f"call_{len(result.tool_calls)}", "name": tool_name(fn.get("name", "")),
                                      "args": args if isinstance(args, dict) else {"_raw": args}})
        usage = data.get("usage") or {}
        result.input_tokens, result.output_tokens = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        result.cached_tokens = _openai_cached(usage)
        return result

    def see(self, image, mime, prompt, system="", max_tokens=2000):
        import base64
        url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": prompt}]}]
        data = _post(self.url, {"model": self.model, "max_tokens": max_tokens, "messages": msgs}, self._headers(), self.timeout, self.attempts)
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        return ChatResult(text=(choice.get("message") or {}).get("content") or "", stop_reason=choice.get("finish_reason", ""),
                          input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0))

    @staticmethod
    def _convert(messages):
        out = []
        for m in messages:
            if m["role"] == "user":
                images = image_parts(m)
                out.append({"role": "user", "content": [*({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
                                                          for mime, data in images), {"type": "text", "text": m["content"]}]
                            if images else m["content"]})
            elif m["role"] == "assistant":
                item = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    item["tool_calls"] = [{"id": c["id"], "type": "function",
                                           "function": {"name": api_name(c["name"]), "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                                          for c in m["tool_calls"]]
                out.append(item)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out


class FakeProvider(Provider):
    """Scripted provider for tests and demos: ``policy(system, messages, tools) -> ChatResult``."""

    def __init__(self, policy: Callable[[str, list, list], ChatResult]):
        self.policy = policy

    def _complete(self, system, messages, tools, max_tokens=4096):
        return self.policy(system, messages, tools)

    def see(self, image, mime, prompt, system="", max_tokens=2000):
        """The policy sees the question with a stand-in for the picture."""
        note = f"[图片 {mime}，{len(image)} 字节]\n{prompt}"
        return self.policy(system or "评图", [{"role": "user", "content": note}], [])


def _openai_cached(usage: dict) -> int:
    """DeepSeek reports prompt_cache_hit_tokens; OpenAI-style servers prompt_tokens_details.cached_tokens."""
    hit = usage.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return int(hit or 0)


def _anthropic_usage(usage: dict) -> tuple[int, int]:
    """Anthropic's input_tokens excludes cache reads/writes; return (whole prompt, cache reads)."""
    read = int(usage.get("cache_read_input_tokens") or 0)
    return int(usage.get("input_tokens") or 0) + read + int(usage.get("cache_creation_input_tokens") or 0), read


def _parse_args(text: str) -> dict:
    try:
        args = json.loads(text or "{}")
    except ValueError:
        return {"_raw": text}
    return args if isinstance(args, dict) else {"_raw": args}


def build(model_spec: str, providers: list[dict]) -> Provider:
    """``model_spec`` is ``<provider id or name>:<model>``."""
    if not model_spec or ":" not in model_spec:
        raise ProviderError("没有指定模型，格式为 提供方:模型")
    pid, model = model_spec.split(":", 1)
    provider = next((p for p in providers if p.get("enabled", True) and pid in (p.get("id"), p.get("name"))), None)
    if provider is None:
        raise ProviderError(f"找不到已启用的模型提供方：{pid}")
    if provider["type"] == "anthropic":
        built = AnthropicProvider(provider.get("api_key", ""), model, provider.get("base_url", ""))
    elif provider["type"] == "openai_compat":
        built = OpenAICompatProvider(provider.get("api_key", ""), model, provider.get("base_url", ""))
    else:
        raise ProviderError(f"不支持的提供方类型：{provider['type']}")
    built.name = model_spec       # what a graph's description records, so the same model is found again
    return built
