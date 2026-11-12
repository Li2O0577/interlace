"""A node that is a model: it reads what arrives, may use tools, and sends on what it concludes.

Everything the model can do outside its own reasoning is a port of the node. A tool is a call port: the
graph decides what answers it (a function, a program, a device, a person, another agent, a whole sub-graph),
so the model's reach is exactly what the graph wires up. Where the node has several outputs, the model
chooses among them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..base import Firing, Node, NodeError, Port, canonical

_NO_PARAMETERS = {"type": "object", "properties": {}}
ANY_OUTPUT = None   # the schema of a json output: anything


@dataclass(frozen=True)
class Tool:
    """A tool the model may use. ``name`` is also the name of the call port the request leaves through;
    ``parameters`` is a JSON schema of the arguments, which arrive at the other end as one mapping."""

    name: str
    description: str = ""
    parameters: Mapping = field(default_factory=lambda: dict(_NO_PARAMETERS))

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": dict(self.parameters)}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return canonical(value)
    except (TypeError, ValueError):
        return repr(value)


class AgentNode(Node):
    """``model`` is anything with ``chat(system, messages, tools, max_tokens)`` that returns an object with
    ``text`` and ``tool_calls`` (a list of ``{"id", "name", "args"}``); messages are
    ``{"role": "user" | "assistant" | "tool", "content": ...}``.

    One activation is one turn: what arrived becomes a user message, the model is asked, every tool it calls
    is answered through the call port of that name, and it is asked again until it calls nothing more.

    * With one output, the model's closing text is sent there (parsed as JSON when ``json=True``).
    * With several, nothing is sent by itself: the model is given a tool ``send_<port>`` for each output and
      decides what goes where, and when. This is how an agent coordinates others.

    A tool that fails answers the model with the error; the model decides what to do about it. With
    ``memory`` (the default) the conversation carries over from one activation to the next and is part of the
    node's snapshot. ``usage`` adds up the tokens the model reports.
    """

    kind = "agent"

    def __init__(self, id: str, model: Any, *, system: str = "", tools: Iterable[Tool] = (),
                 inputs: Iterable[Port] | None = None, outputs: Iterable[Port] | None = None,
                 firing: Firing | None = None, memory: bool = True, json: bool = False, max_steps: int = 50,
                 max_tokens: int = 4096):
        if not callable(getattr(model, "chat", None)):
            raise NodeError(f"{id}: a model needs a chat method")
        if type(max_steps) is not int or max_steps < 1:
            raise NodeError(f"{id}: max_steps must be at least 1")
        tools = tuple(tools)
        sends = list(outputs) if outputs is not None else [Port("out", ANY_OUTPUT if json else str)]
        if any(port.call for port in sends):
            raise NodeError(f"{id}: declare what the model may call as tools, not as outputs")
        super().__init__(id, inputs=list(inputs) if inputs is not None else [Port("task")],
                         outputs=[*sends, *(Port(tool.name, call=True) for tool in tools)], firing=firing)
        self.model, self.system, self.tools = model, system, tools
        self.memory, self.json, self.max_steps, self.max_tokens = bool(memory), bool(json), max_steps, max_tokens
        self.messages: list[dict] = []
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self._sends = [port.name for port in sends]
        self._choose = len(self._sends) > 1
        self._specs = [tool.spec() for tool in tools]
        if self._choose:
            if any(tool.name.startswith("send_") for tool in tools):
                raise NodeError(f"{id}: tool names beginning with send_ are taken by the node's outputs")
            self._specs += [{"name": f"send_{name}", "description": f"Send content out of the output '{name}'.",
                             "parameters": {"type": "object", "properties": {"content": {}}, "required": ["content"]}}
                            for name in self._sends]

    def config(self) -> Mapping:
        return {"model": str(getattr(self.model, "model", "")), "system": self.system,
                "tools": [tool.spec() for tool in self.tools], "memory": self.memory, "json": self.json,
                "max_steps": self.max_steps, "max_tokens": self.max_tokens}

    def snapshot(self) -> Mapping:
        return {"messages": self.messages, "usage": self.usage} if self.memory else {"usage": self.usage}

    def restore(self, state: Mapping) -> None:
        self.messages = list(state.get("messages", []))
        self.usage = dict(state.get("usage", self.usage))

    # ---- one turn --------------------------------------------------------------------------------------

    def run(self, ctx, inputs: Mapping[str, Any]):
        if len(inputs) == 1 and len(self.inputs) == 1:
            arrived = _text(next(iter(inputs.values())))
        else:
            arrived = "\n\n".join(f"[{name}]\n{_text(value)}" for name, value in inputs.items())
        messages = self.messages if self.memory else []
        messages.append({"role": "user", "content": arrived})
        for _ in range(self.max_steps):
            if ctx.stopping():
                return None
            result = self.model.chat(self.system, messages, self._specs, self.max_tokens)
            self.usage["calls"] += 1
            self.usage["input_tokens"] += getattr(result, "input_tokens", 0) or 0
            self.usage["output_tokens"] += getattr(result, "output_tokens", 0) or 0
            text, calls = getattr(result, "text", "") or "", list(getattr(result, "tool_calls", None) or ())
            message = {"role": "assistant", "content": text}
            if calls:
                message["tool_calls"] = [{"id": call.get("id", ""), "name": call["name"], "args": call.get("args") or {}}
                                         for call in calls]
            messages.append(message)
            if not calls:
                return None if self._choose else {self._sends[0]: self._closing(text)}
            for call in message["tool_calls"]:
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                 "content": self._use(ctx, call["name"], call["args"])})
        raise NodeError(f"the model was still calling tools after {self.max_steps} steps")

    def _closing(self, text: str) -> Any:
        if not self.json:
            return text
        try:
            return json.loads(text)
        except ValueError as exc:
            raise NodeError(f"the model's answer is not JSON: {exc}") from None

    def _use(self, ctx, name: str, args: Mapping) -> str:
        """Answer one tool call; a failure becomes the answer, for the model to read."""
        try:
            if self._choose and name.startswith("send_") and name[5:] in self._sends:
                if "content" not in args:
                    return "error: content is missing"
                ctx.emit(name[5:], args["content"])
                return "sent"
            if name not in self.outputs or not self.outputs[name].call:
                return f"error: there is no tool {name}"
            return _text(ctx.call(name, dict(args)))
        except Exception as exc:   # noqa: BLE001 - whatever went wrong is the model's to read
            if ctx.stopping():
                raise
            return f"error: {exc}"

