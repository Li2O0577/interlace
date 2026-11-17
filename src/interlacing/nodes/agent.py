"""A node that is a model: it reads what arrives, may use tools, and sends on what it concludes.

Everything the model can do outside its own reasoning is a port of the node. A tool is a call port: the
graph decides what answers it (a function, a program, a device, a person, another agent, a whole sub-graph),
so the model's reach is exactly what the graph wires up. Where the node has several outputs, the model
chooses among them.
"""
from __future__ import annotations

import base64
import copy
import inspect
import json
import typing
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from ..base import Firing, Invalid, Node, NodeError, Port, canonical
from .function import _hints, named

_NO_PARAMETERS = {"type": "object", "properties": {}}
ANY_OUTPUT = None   # the schema of a json output: anything


@dataclass(frozen=True)
class Tool:
    """A tool the model may use. ``name`` is also the name of the call port the request leaves through;
    ``parameters`` is a JSON schema of the arguments, which arrive at the other end as one mapping.

    A tool with ``run`` is the node's own: it has no port and no edge, the node answers it itself by calling
    ``run(arguments, ctx)``. That is what a tool taken from a tool pool is (``file`` is where in the pool),
    what the file tools of a working area are, and what a plain function becomes (``Tool.of``)."""

    name: str
    description: str = ""
    parameters: Mapping = field(default_factory=lambda: dict(_NO_PARAMETERS))
    run: Callable[[Mapping, Any], Any] | None = field(default=None, compare=False)
    file: str = ""
    function: Callable | None = field(default=None, compare=False)

    @classmethod
    def of(cls, function: Callable, name: str | None = None, description: str | None = None) -> "Tool":
        """A plain function as a tool the agent answers itself, by calling it. The name is the function's,
        the description its docstring, and the arguments its parameters, typed by their annotations
        (``int``, ``float``, ``str``, ``bool``, ``list[...]``, ``dict``); those without a default are required.
        A parameter called ``ctx`` is not the model's to give: it is the agent's context."""
        if not callable(function):
            raise NodeError(f"a tool is a function, not {function!r}")
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):
            raise NodeError(f"the parameters of {function!r} cannot be read") from None
        hints = _hints(function)
        properties: dict[str, Any] = {}
        required: list[str] = []
        takes_ctx = False
        for param in signature.parameters.values():
            if param.name == "ctx":
                takes_ctx = True
                continue
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            properties[param.name] = _json_schema(hints.get(param.name))
            if param.default is param.empty:
                required.append(param.name)
        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required

        def run(arguments: Mapping, ctx: Any) -> Any:
            unknown = set(arguments) - set(properties)
            if unknown:
                raise NodeError(f"no argument {', '.join(sorted(unknown))}")
            return function(**arguments, **({"ctx": ctx} if takes_ctx else {}))
        title = name or getattr(function, "__name__", "")
        if not title or title == "<lambda>":
            raise NodeError("a tool made of a function needs a name: give one")
        about = description if description is not None else inspect.cleandoc(function.__doc__ or "")
        return cls(title, about, parameters, run=run, function=function)

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": dict(self.parameters)}

    def describe(self) -> dict:
        """The tool as a graph's description records it."""
        if self.function is not None:
            return {**self.spec(), "function": named(self.function)}
        return {**self.spec(), **({"file": self.file} if self.file else {"own": True} if self.run is not None else {})}


def _json_schema(hint: Any) -> dict:
    """The JSON schema of an argument, from its annotation; anything when it says nothing plain."""
    simple = {int: "integer", float: "number", str: "string", bool: "boolean", dict: "object", list: "array"}
    if hint in simple:
        return {"type": simple[hint]}
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is list:
        return {"type": "array", **({"items": _json_schema(args[0])} if args else {})}
    if origin is dict:
        return {"type": "object"}
    return {}


def time_left(ctx) -> str:
    """How much time you have left before your time limit, in seconds."""
    left = ctx.time_left()
    return "you have no time limit" if left is None else f"{left:.0f} seconds"


_RESIDENT = {
    "wait": {"description": "Wait for the next message from the others (they reach you through your inputs). "
                            "Returns what came, with your task board; or says that nothing more can come.",
             "parameters": {"type": "object", "properties": {"seconds": {"type": "number", "description":
                            "give up after this long; leave out to wait as long as it takes"}}}},
    "note": {"description": "Write down where you stand, for yourself. It is kept, and your progress is saved.",
             "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    "plan": {"description": "Add items to your task board.",
             "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}},
                            "required": ["items"]}},
    "done": {"description": "Tick off an item of your task board, by its number or its text.",
             "parameters": {"type": "object", "properties": {"item": {"type": ["string", "integer"]}}, "required": ["item"]}},
    "correct": {"description": "Correct a message you sent: give its id (what sending it answered) and the content "
                               "that is to take its place. Whoever has not read it yet gets only the correction; "
                               "whoever has, is told.",
                "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "content": {}},
                               "required": ["id", "content"]}},
    "finish": {"description": "Say that your work is done. Send everything first: what you have not sent is lost.",
               "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}}},
}


_CORRECTS = ": this corrects what came here before"


def _corrected(ctx, got: Mapping[str, Any]) -> set:
    """The ports among ``got`` whose message corrects one read before."""
    replaces = getattr(ctx, "replaces", None)
    return {name for name in got if replaces(name)} if replaces is not None else set()


def _step(ctx) -> bool:
    """A step is done: wait while paused; False when the activation is to end now."""
    checkpoint = getattr(ctx, "checkpoint", None)
    return checkpoint() if checkpoint is not None else not ctx.stopping()


def _save(ctx) -> None:
    commit = getattr(ctx, "commit", None)
    if commit is not None:
        commit()


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

    With ``area`` (an ``Area`` of a ``Workspace``), a file the node sends out of a port that carries files
    must be one the area lets it read; one that is not goes back to the model with the reason.

    With ``resident=True`` the agent stays for as long as it has work, and decides itself when it is done:
    one activation, not one per message. It sends each output when it is ready (``send_<port>``, even with
    one output), waits for messages from the others (``wait``, which holds none of the run's workers),
    keeps a task board (one line per output, plus what it adds with ``plan`` and ticks off with ``done``),
    writes down where it stands (``note``, which also saves its progress to the journal) and ends with
    ``finish``. ``wait`` says so when nothing more can come, because everyone else has finished or waits
    too. Taken up after the process ended, it goes on from its last saved progress.
    """

    kind = "agent"
    repairs = 2
    on_correction = "tell"           # a model reads the correction and decides what to do about it

    def __init__(self, id: str, model: Any, *, system: str = "", tools: Iterable[Tool] = (),
                 inputs: Iterable[Port] | None = None, outputs: Iterable[Port] | None = None,
                 firing: Firing | None = None, memory: bool | Callable[[list], list] = True, json: bool = False, max_steps: int = 50,
                 max_tokens: int = 4096, files=None, repairs: int | None = None, area=None, resident: bool = False):
        if not callable(getattr(model, "chat", None)):
            raise NodeError(f"{id}: a model needs a chat method")
        if type(max_steps) is not int or max_steps < 1:
            raise NodeError(f"{id}: max_steps must be at least 1")
        tools = tuple(tool if isinstance(tool, Tool) else Tool.of(tool) for tool in tools)
        if len({tool.name for tool in tools}) != len(tools):
            raise NodeError(f"{id}: two tools have the same name")
        sends = list(outputs) if outputs is not None else [Port("out", ANY_OUTPUT if json else str)]
        if any(port.call for port in sends):
            raise NodeError(f"{id}: declare what the model may call as tools, not as outputs")
        super().__init__(id, inputs=list(inputs) if inputs is not None else [Port("task")],
                         outputs=[*sends, *(Port(tool.name, call=True) for tool in tools if tool.run is None)], firing=firing)
        self.model, self.system, self.tools = model, system, tools
        self.keep = memory if callable(memory) else None       # what is done to the conversation before each request
        self.memory, self.json, self.max_steps, self.max_tokens = bool(memory), bool(json), max_steps, max_tokens
        self.files, self.area = files, area
        self._own = {tool.name: tool for tool in tools if tool.run is not None}
        self._paths = {port.name for port in sends if isinstance(port.schema.describe(), Mapping) and "file" in port.schema.describe()}
        if repairs is not None:
            if type(repairs) is not int or repairs < 0:
                raise NodeError(f"{id}: repairs must be a whole number, 0 or more")
            self.repairs = repairs
        self.messages: list[dict] = []
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self._sends = [port.name for port in sends]
        self.resident = bool(resident)
        self._choose = len(self._sends) > 1 or self.resident
        self._specs = [tool.spec() for tool in tools]
        self.board: dict = self._fresh_board()
        if self.resident:
            taken = {tool.name for tool in tools} & set(_RESIDENT)
            if taken:
                raise NodeError(f"{id}: the tools {', '.join(sorted(taken))} are a resident agent's own")
            self._specs += [{"name": name, **spec} for name, spec in _RESIDENT.items()]
        if self._choose:
            if any(tool.name.startswith("send_") for tool in tools):
                raise NodeError(f"{id}: tool names beginning with send_ are taken by the node's outputs")
            self._specs += [{"name": f"send_{name}", "description": f"Send content out of the output '{name}'.",
                             "parameters": {"type": "object", "properties": {"content": {}}, "required": ["content"]}}
                            for name in self._sends]

    def config(self) -> Mapping:
        return {"model": str(getattr(self.model, "name", "") or getattr(self.model, "model", "")), "system": self.system,
                "tools": [tool.describe() if tool.run is not None else tool.spec() for tool in self.tools], "memory": named(self.keep) if self.keep is not None else self.memory, "json": self.json,
                "max_steps": self.max_steps, "max_tokens": self.max_tokens, "repairs": self.repairs,
                **({"resident": True} if self.resident else {}),
                **(self.area.given if self.area is not None else {})}

    def reset(self) -> None:
        self.messages = []
        self.board = self._fresh_board()

    def snapshot(self) -> Mapping:
        kept = {"messages": self.messages, "usage": self.usage} if self.memory else {"usage": self.usage}
        return {**kept, "board": self.board} if self.resident else kept

    def restore(self, state: Mapping) -> None:
        self.messages = list(state.get("messages", []))
        self.usage = dict(state.get("usage", self.usage))
        if "board" in state:
            self.board = {**self._fresh_board(), **copy.deepcopy(dict(state["board"]))}

    def _fresh_board(self) -> dict:
        return {"sent": {name: 0 for name in self._sends}, "fixed": {}, "items": [], "notes": [], "warned": False}

    # ---- one turn --------------------------------------------------------------------------------------

    def run(self, ctx, inputs: Mapping[str, Any]):
        if self.resident:
            return self._stay(ctx, inputs)
        pictures: list = []
        inputs = {name: self._pictures(value, pictures) for name, value in inputs.items()}
        if len(inputs) == 1 and len(self.inputs) == 1 and not _corrected(ctx, inputs):
            arrived = _text(next(iter(inputs.values())))
        else:
            arrived = self._heard(inputs, ctx)
        # the conversation is taken up on a copy and kept only when the turn ends well: a turn that fails or is
        # stopped half way leaves nothing behind, so running it again starts from where the last good turn ended
        messages = list(self.messages) if self.memory else []
        messages.append({"role": "user", "content": arrived, **({"image_data": pictures} if pictures else {})})
        for problem in ctx.problems:
            messages.append({"role": "user", "content": f"What you sent was not accepted: {problem}"})
        unparsed = self.repairs
        for _ in range(self.max_steps):
            if not _step(ctx):             # paused: waits here; stopped, restarted or the run is ending: leaves
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
            thought = getattr(result, "thinking_blocks", None)
            if thought:     # a service that thinks between tool calls wants its thinking back with the next request
                message["thinking_blocks"] = list(thought)
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
                    self._send(ctx, self._sends[0], answer)
                    self.messages = messages if self.memory else []
                    return None
                except Invalid as exc:
                    if not exc.again:
                        raise
                    messages.append({"role": "user", "content": f"Your answer was not accepted: {exc.problem}\n"
                                                                f"Put that right and give your answer again."})
                    continue
            for call in message["tool_calls"]:
                if not _step(ctx):
                    return None
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                 "content": self._use(ctx, call["name"], call["args"])})
        raise NodeError(f"the model was still calling tools after {self.max_steps} steps")

    # ---- a resident agent -------------------------------------------------------------------------------

    def _stay(self, ctx, inputs: Mapping[str, Any]):
        """One activation for as long as the agent has work: it ends when the agent says it has finished."""
        messages = list(self.messages) if self.memory else []
        if getattr(ctx, "resumed", False):
            messages.append({"role": "user", "content": "The run was taken up again after an interruption. "
                                                        "Here is where you stood:\n" + self._shown_board()})
        else:
            self.board = self._fresh_board()
            messages.append(self._arrival(inputs, ctx) if inputs else
                            {"role": "user", "content": "You have started. " + self._shown_board()})
        steps = 0
        while True:
            if not _step(ctx):
                self.messages = messages if self.memory else []
                return None
            if steps >= self.max_steps:
                raise NodeError(f"the model went {self.max_steps} steps without waiting or finishing")
            steps += 1
            if self.keep is not None:
                kept = self.keep(messages)
                if not isinstance(kept, list) or not kept:
                    raise NodeError("memory must return the conversation: a list of messages that is not empty")
                messages[:] = kept
            message, calls = self._ask(ctx, messages)
            if not calls:
                messages.append({"role": "user", "content": "Use your tools: wait for messages, send what is "
                                                            "ready, or finish when your work is done."})
                continue
            for call in message["tool_calls"]:
                if not _step(ctx):
                    self.messages = messages if self.memory else []
                    return None
                name, args = call["name"], call["args"]
                if name == "finish":
                    unsent = [port for port, count in self.board["sent"].items() if not count]
                    if unsent and not self.board["warned"]:
                        self.board["warned"] = True
                        answer = (f"Nothing has been sent out of {', '.join(unsent)}. Send it, or call finish "
                                  f"again to end without it.")
                    else:
                        messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": "finished"})
                        self.messages = messages if self.memory else []
                        return None
                elif name == "wait":
                    self.messages = list(messages) if self.memory else []
                    _save(ctx)                   # what it has done so far is kept while it waits
                    got = ctx.receive(timeout=args.get("seconds"))
                    if got is not None:
                        steps = 0
                        answer = self._heard(got, ctx) + "\n\n" + self._shown_board()
                    elif ctx.stopping():
                        answer = "The run is ending."
                    elif args.get("seconds") is not None:
                        answer = "Nothing came in that time.\n\n" + self._shown_board()
                    else:
                        answer = ("Nothing more can come: everyone else has finished or is waiting too. Send "
                                  "what is left, then finish.\n\n" + self._shown_board())
                elif name == "note":
                    self.board["notes"].append(str(args.get("text", "")))
                    messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": "noted"})
                    self.messages = list(messages) if self.memory else []
                    _save(ctx)
                    continue
                elif name == "plan":
                    items = args.get("items") or []
                    self.board["items"] += [[str(item), False] for item in (items if isinstance(items, list) else [items])]
                    answer = self._shown_board()
                elif name == "done":
                    answer = self._tick(args.get("item"))
                elif name == "correct":
                    answer = self._fix(ctx, args)
                else:
                    answer = self._use(ctx, name, args)
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": answer})

    def _ask(self, ctx, messages: list) -> tuple[dict, list]:
        """One request to the model; its answer is added to ``messages``."""
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
        thought = getattr(result, "thinking_blocks", None)
        if thought:
            message["thinking_blocks"] = list(thought)
        messages.append(message)
        return message, calls

    def _arrival(self, inputs: Mapping[str, Any], ctx=None) -> dict:
        pictures: list = []
        inputs = {name: self._pictures(value, pictures) for name, value in inputs.items()}
        if len(inputs) == 1 and len(self.inputs) == 1 and not _corrected(ctx, inputs):
            arrived = _text(next(iter(inputs.values())))
        else:
            arrived = self._heard(inputs, ctx)
        return {"role": "user", "content": arrived, **({"image_data": pictures} if pictures else {})}

    def _heard(self, got: Mapping[str, Any], ctx=None) -> str:
        return "\n\n".join(f"[{name}{_CORRECTS if name in _corrected(ctx, got) else ''}]\n{_text(value)}"
                             for name, value in got.items())

    def _fix(self, ctx, args: Mapping) -> str:
        """The ``correct`` tool: put new content in the place of a message the agent sent."""
        sent, port = str(args.get("id", "")), str(args.get("id", "")).rpartition("#")[0]
        if "content" not in args:
            return "error: content is missing"
        if port not in self._sends:
            return f"error: {sent!r} is not the id of a message you sent"
        try:
            if self.area is not None and port in self._paths:
                problem = self.area.sends(args["content"])
                if problem:
                    return f"not accepted: {problem}. Put that right and correct it again."
            ctx.correct(sent, args["content"])
        except Invalid as exc:
            if not exc.again:
                raise
            return f"not accepted: {exc.problem}. Put that right and correct it again."
        except Exception as exc:   # noqa: BLE001 - whatever went wrong is the model's to read
            if ctx.stopping():
                raise
            return f"error: {exc}"
        fixed = self.board.setdefault("fixed", {})
        fixed[port] = fixed.get(port, 0) + 1
        return f"corrected {sent}"

    def _shown_board(self) -> str:
        board = self.board
        lines = ["Your task board:"]
        fixed = board.get("fixed", {})
        lines += [f"- output {port}: " + (f"sent {count} time{'s' if count > 1 else ''}" if count else "not sent yet")
                  + (f", {fixed[port]} correction{'s' if fixed[port] > 1 else ''}" if fixed.get(port) else "")
                  for port, count in board["sent"].items()]
        lines += [f"{i}. [{'x' if done else ' '}] {text}" for i, (text, done) in enumerate(board["items"], 1)]
        if board["notes"]:
            lines.append(f"Your last note: {board['notes'][-1]}")
        return "\n".join(lines)

    def _tick(self, item: Any) -> str:
        items = self.board["items"]
        for i, entry in enumerate(items, 1):
            if item == i or str(item).strip() in (str(i), entry[0]):
                entry[1] = True
                return self._shown_board()
        return f"error: there is no item {item!r} on your board\n\n" + self._shown_board()

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

    def _send(self, ctx, port: str, payload: Any) -> Any:
        if self.area is not None and port in self._paths:
            problem = self.area.sends(payload)
            if problem:
                raise Invalid(port, problem, again=True)
        return ctx.emit(port, payload)

    def _use(self, ctx, name: str, args: Mapping) -> str:
        """Answer one tool call; a failure becomes the answer, for the model to read."""
        try:
            if self._choose and name.startswith("send_") and name[5:] in self._sends:
                if "content" not in args:
                    return "error: content is missing"
                sent = self._send(ctx, name[5:], args["content"])
                if self.resident:
                    self.board["sent"][name[5:]] = self.board["sent"].get(name[5:], 0) + 1
                    if isinstance(sent, str):
                        return f"sent, as {sent}"
                return "sent"
            if name in self._own:
                return _text(self._own[name].run(dict(args), ctx))      # type: ignore[misc]
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

