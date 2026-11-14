"""A node that is a model: it reads what arrives, may use tools, and sends on what it concludes.

Everything the model can do outside its own reasoning is a port of the node. A tool is a call port: the
graph decides what answers it (a function, a program, a device, a person, another agent, a whole sub-graph),
so the model's reach is exactly what the graph wires up. Where the node has several outputs, the model
chooses among them.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from ..base import Firing, Invalid, Node, NodeError, Port, canonical
from .function import named

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


def is_image(value: Any) -> bool:
    """A picture, as it travels on an edge: ``{"mime": "image/png", "data": <base64 text>}``."""
    return (isinstance(value, Mapping) and isinstance(value.get("data"), str)
            and isinstance(value.get("mime"), str) and value["mime"].startswith("image/"))


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
    ``{"role": "user" | "assistant" | "tool", "content": ...}``. Its ``name`` (or else its ``model``) attribute is
    what the node's description records as the model, and what a catalog is asked for when the graph is loaded.

    One activation is one turn: what arrived becomes a user message, the model is asked, every tool it calls
    is answered through the call port of that name, and it is asked again until it calls nothing more.

    * With one output, the model's closing text is sent there (parsed as JSON when ``json=True``).
    * With several, nothing is sent by itself: the model is given a tool ``send_<port>`` for each output and
      decides what goes where, and when. This is how an agent coordinates others.

    A tool that fails answers the model with the error; the model decides what to do about it. With
    ``memory`` (the default) the conversation carries over from one activation to the next and is part of the
    node's snapshot. ``memory`` may also be a function ``memory(messages) -> messages``: it is called before
    every request to the model and what it returns becomes the conversation, so it is where a long
    conversation is shortened, summarised or otherwise kept in hand. ``usage`` adds up the tokens the model
    reports.

    A picture that arrives (``{"mime": "image/...", "data": <base64>}``, alone or inside what arrived) is
    shown to the model as a picture, where the model's interface takes pictures. With ``files`` (a
    directory), a picture that arrives as a file reference (``{"path": "plots/a.png", "media": "image/png"}``,
    as the ``file`` protocol delivers it) is read from there and shown too.

    What the model sends is checked like any node's output. When a check finds a problem and the port has
    repairs left (an agent's ports have two unless they say otherwise), the model is told what is wrong, in
    the port's own words, and answers again; it still has its tools, so it can write the file that was
    missing. The same goes for an answer that should have been JSON and is not.
    """

    kind = "agent"
    repairs = 2

    def __init__(self, id: str, model: Any, *, system: str = "", tools: Iterable[Tool] = (),
                 inputs: Iterable[Port] | None = None, outputs: Iterable[Port] | None = None,
                 firing: Firing | None = None, memory: bool | Callable[[list], list] = True, json: bool = False, max_steps: int = 50,
                 max_tokens: int = 4096, files=None, repairs: int | None = None):
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
        self.keep = memory if callable(memory) else None       # what is done to the conversation before each request
        self.memory, self.json, self.max_steps, self.max_tokens = bool(memory), bool(json), max_steps, max_tokens
        self.files = files
        if repairs is not None:
            if type(repairs) is not int or repairs < 0:
                raise NodeError(f"{id}: repairs must be a whole number, 0 or more")
            self.repairs = repairs
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
        return {"model": str(getattr(self.model, "name", "") or getattr(self.model, "model", "")), "system": self.system,
                "tools": [tool.spec() for tool in self.tools], "memory": named(self.keep) if self.keep is not None else self.memory, "json": self.json,
                "max_steps": self.max_steps, "max_tokens": self.max_tokens, "repairs": self.repairs}

    def snapshot(self) -> Mapping:
        return {"messages": self.messages, "usage": self.usage} if self.memory else {"usage": self.usage}

    def restore(self, state: Mapping) -> None:
        self.messages = list(state.get("messages", []))
        self.usage = dict(state.get("usage", self.usage))

    # ---- one turn --------------------------------------------------------------------------------------

    def run(self, ctx, inputs: Mapping[str, Any]):
        pictures: list = []
        inputs = {name: self._pictures(value, pictures) for name, value in inputs.items()}
        if len(inputs) == 1 and len(self.inputs) == 1:
            arrived = _text(next(iter(inputs.values())))
        else:
            arrived = "\n\n".join(f"[{name}]\n{_text(value)}" for name, value in inputs.items())
        # the conversation is taken up on a copy and kept only when the turn ends well: a turn that fails or is
        # stopped half way leaves nothing behind, so running it again starts from where the last good turn ended
        messages = list(self.messages) if self.memory else []
        messages.append({"role": "user", "content": arrived, **({"image_data": pictures} if pictures else {})})
        for problem in ctx.problems:
            messages.append({"role": "user", "content": f"What you sent was not accepted: {problem}"})
        unparsed = self.repairs
        for _ in range(self.max_steps):
            if ctx.stopping():
                return None
            if self.keep is not None:
                kept = self.keep(messages)
                if not isinstance(kept, list) or not kept:
                    raise NodeError("memory must return the conversation: a list of messages that is not empty")
                messages[:] = kept
            result = self.model.chat(self.system, messages, self._specs, self.max_tokens)
            spent_in, spent_out = getattr(result, "input_tokens", 0) or 0, getattr(result, "output_tokens", 0) or 0
            self.usage["calls"] += 1
            self.usage["input_tokens"] += spent_in
            self.usage["output_tokens"] += spent_out
            ctx.use(calls=1, input_tokens=spent_in, output_tokens=spent_out, tokens=spent_in + spent_out)
            text, calls = getattr(result, "text", "") or "", list(getattr(result, "tool_calls", None) or ())
            message: dict[str, Any] = {"role": "assistant", "content": text}
            if calls:
                message["tool_calls"] = [{"id": call.get("id", ""), "name": call["name"], "args": call.get("args") or {}}
                                         for call in calls]
            messages.append(message)
            if not calls:
                if self._choose:
                    self.messages = messages if self.memory else []
                    return None
                try:
                    try:
                        answer = self._closing(text)
                    except NodeError as exc:
                        if unparsed <= 0:
                            raise
                        unparsed -= 1
                        raise Invalid(self._sends[0], str(exc), again=True) from None
                    ctx.emit(self._sends[0], answer)
                    self.messages = messages if self.memory else []
                    return None
                except Invalid as exc:
                    if not exc.again:
                        raise
                    messages.append({"role": "user", "content": f"Your answer was not accepted: {exc.problem}\n"
                                                                f"Put that right and give your answer again."})
                    continue
            for call in message["tool_calls"]:
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                 "content": self._use(ctx, call["name"], call["args"])})
        raise NodeError(f"the model was still calling tools after {self.max_steps} steps")

    def _pictures(self, value: Any, found: list) -> Any:
        """Take the pictures out of what arrived, leaving a note where each one was."""
        if is_image(value):
            found.append([value["mime"], value["data"]])
            return f"[image {len(found)}]"
        if (self.files is not None and isinstance(value, Mapping) and isinstance(value.get("path"), str)
                and str(value.get("media", "")).startswith("image/")):
            root = Path(self.files).resolve()
            path = (root / value["path"]).resolve()
            if root in path.parents and path.is_file():
                found.append([value["media"], base64.b64encode(path.read_bytes()).decode("ascii")])
                return {**value, "shown": f"[image {len(found)}]"}
            return value
        if isinstance(value, (list, tuple)):
            return [self._pictures(item, found) for item in value]
        if isinstance(value, Mapping):
            return {key: self._pictures(item, found) for key, item in value.items()}
        return value

    def _closing(self, text: str) -> Any:
        if not self.json:
            return text
        text = text.strip()
        if text.startswith("```"):          # many models put their JSON in a fenced block
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
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
        except Invalid as exc:     # what it sent did not pass a check: its to correct while the port allows
            if not exc.again:
                raise
            return f"not accepted: {exc.problem}. Put that right and send it again."
        except Exception as exc:   # noqa: BLE001 - whatever went wrong is the model's to read
            if ctx.stopping():
                raise
            return f"error: {exc}"

