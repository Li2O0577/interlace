"""A run kept in a directory: the graph, its journal, the questions put to people and their answers.

Everything about a run is in its directory, so a run can be started by one process, answered from another
and continued by a third::

    graph.json      the graph, as data
    options.json    what the graph may reach (modules, processes), how many workers, the time limit
    journal.jsonl   what has happened so far
    questions/      one file per question that a person in the graph has been asked
    answers/        one file per answer given
    files/          the working directory of the graph's nodes: what the ``file`` protocol's paths are relative to
    state.json      where the run stands: done, waiting, stopped or failed

A person is never waited for inside a running process. When a ``human`` node needs an answer that is not
there yet, the run stops with the state ``waiting``; once the answer has been given, ``resume`` carries on
from the journal. Nothing that had finished runs again.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .engine import Engine, Journal, RunError
from .models import ChatResult, FakeProvider, ProviderError, build
from .spec import Catalog, SpecError, dump, load

DONE, WAITING, STOPPED, FAILED, NEW = "done", "waiting", "stopped", "failed", "new"


class Pending(BaseException):
    """Raised inside a ``human`` node whose answer is not there yet. It is not an error: the engine lets it
    pass, the journal stays open, and the run is continued later."""

    def __init__(self, question: Mapping):
        super().__init__(question["id"])
        self.question = dict(question)


def models_from_environment(name: str) -> Any:
    """The model ``provider:model`` stands for, with keys taken from the environment and from nowhere else.

    ``anthropic:<model>`` uses ``ANTHROPIC_API_KEY`` (and ``ANTHROPIC_BASE_URL`` when set);
    ``openai:<model>`` uses ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL`` (default ``https://api.openai.com/v1``);
    ``fake:echo`` calls no service and repeats what it was told, for trying a graph out.
    """
    provider, _, model = name.partition(":")
    if provider == "fake":
        echo = FakeProvider(lambda system, messages, tools: ChatResult(text=str(messages[-1].get("content", ""))))
        echo.name = name
        return echo
    env = os.environ
    known = [{"id": "anthropic", "type": "anthropic", "api_key": env.get("ANTHROPIC_API_KEY", ""),
              "base_url": env.get("ANTHROPIC_BASE_URL", "")},
             {"id": "openai", "type": "openai_compat", "api_key": env.get("OPENAI_API_KEY", ""),
              "base_url": env.get("OPENAI_BASE_URL", "https://api.openai.com/v1")}]
    key = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider)
    if key and not env.get(key):
        raise SpecError(f"the model {name!r} needs {key} in the environment")
    try:
        return build(name, known)
    except ProviderError as exc:
        raise SpecError(f"the model {name!r} cannot be built: {exc} (known providers: anthropic, openai, fake)") from None


class _Unbuilt:
    """Stands in for a model while a graph is only being checked: no key is needed and nothing is called."""

    def __init__(self, name: str):
        self.name = name

    def chat(self, *args, **kwargs):
        raise RuntimeError("a graph that is only being checked does not run")


def check(description: Mapping, *, allow=(), processes: bool = False, files=None) -> dict:
    """Build the graph a description stands for, without running anything, and say what it is."""
    graph = load(description, Catalog(allow=allow, processes=processes, models=_Unbuilt, files=files, workspace="."))
    return {"digest": graph.digest, "nodes": {node.id: type(node).kind for node in graph.nodes},
            "edges": len(graph.edges), "inputs": sorted(graph.inputs), "outputs": sorted(graph.outputs)}


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


class Run:
    """``observe`` is told what happens while the run is going, as the engine's ``observe`` is."""

    def __init__(self, directory, *, models: Callable[[str], Any] | None = models_from_environment,
                 observe: Callable[[Mapping], None] | None = None):
        self.directory = Path(directory)
        self.models, self.observe = models, observe

    # ---- creating --------------------------------------------------------------------------------------

    @classmethod
    def create(cls, directory, description: Mapping, *, allow=(), processes: bool = False, workers: int = 0,
               models: Callable[[str], Any] | None = models_from_environment, files=None,
               time_limit: float | None = None, observe: Callable[[Mapping], None] | None = None,
               workspace=None) -> "Run":
        """Check the description and set up a directory for one run of it. Nothing runs yet. ``files`` is
        the directory the description may take text from; the text is kept in the run, the files are not
        needed again. ``time_limit`` is the longest one activation may take, in seconds. ``workspace`` is the
        directory the nodes work in and send file paths relative to; it is ``files`` in the run's directory
        unless another is given."""
        run = cls(directory, models=models, observe=observe)
        if (run.directory / "graph.json").exists():
            raise SpecError(f"{run.directory} already holds a run")
        if time_limit is not None and not time_limit > 0:
            raise SpecError("the time limit must be a positive number of seconds")
        options = {"allow": list(allow), "processes": bool(processes), "workers": int(workers), "time_limit": time_limit,
                   "workspace": str(Path(workspace).resolve()) if workspace is not None else None}
        run.workspace(options).mkdir(parents=True, exist_ok=True)
        graph = load(description, run._catalog(options, files))
        _write(run.directory / "graph.json", dump(graph))
        _write(run.directory / "options.json", options)
        run._state(NEW, graph=graph.digest)
        return run

    def workspace(self, options: Mapping | None = None) -> Path:
        """The directory the graph's nodes work in."""
        options = options if options is not None else _read(self.directory / "options.json", {})
        return Path(options["workspace"]) if options.get("workspace") else self.directory / "files"

    def _catalog(self, options: Mapping, files=None) -> Catalog:
        return Catalog(allow=options["allow"], processes=options["processes"], models=self.models, ask=self._ask, files=files,
                       workspace=self.workspace(options))

    def _engine(self) -> Engine:
        description = _read(self.directory / "graph.json")
        if description is None:
            raise SpecError(f"{self.directory} holds no run")
        options = _read(self.directory / "options.json")
        catalog = self._catalog(options)
        return Engine(load(description, catalog), catalog.protocols, workers=options["workers"],
                      journal=Journal(self.directory / "journal.jsonl"), time_limit=options.get("time_limit"),
                      observe=self.observe)

    # ---- running ---------------------------------------------------------------------------------------

    def start(self, inputs: Mapping[str, Any] | None = None) -> dict:
        """Run from the beginning until the run is done, needs a person, or fails. Returns the state."""
        self._held()
        if (self.directory / "journal.jsonl").exists():
            raise SpecError(f"{self.directory} has already been started: resume it")
        return self._go(lambda engine: engine.run(inputs))

    def resume(self) -> dict:
        """Carry on from the journal."""
        self._held()
        if not (self.directory / "journal.jsonl").exists():
            raise SpecError(f"{self.directory} has not been started")
        return self._go(lambda engine: engine.resume())

    def _held(self) -> None:
        if not (self.directory / "graph.json").exists():
            raise SpecError(f"{self.directory} holds no run")

    def _go(self, action) -> dict:
        engine = self._engine()
        try:
            result = action(engine)
        except Pending:
            return self._state(WAITING)
        except RunError as exc:
            return self._state(FAILED, error=str(exc), node=exc.node, problems=exc.problems)
        return self._state(DONE if result.complete else STOPPED, outputs=result.outputs, rounds=result.rounds,
                           limited=list(result.limited), problems=result.problems)

    # ---- people ----------------------------------------------------------------------------------------

    def _ask(self, node: str, round: int, request: Any) -> Any:
        id = f"{node}-{round}".replace("/", "__")       # a person inside a block: block.round__node-round
        answer = _read(self.directory / "answers" / f"{id}.json")
        if answer is not None:
            return answer["answer"]
        question = {"id": id, "node": node, "round": round, "request": request}
        _write(self.directory / "questions" / f"{id}.json", question)
        raise Pending(question)

    def questions(self) -> list[dict]:
        """The questions that have been asked and not yet answered, oldest first."""
        asked = sorted((self.directory / "questions").glob("*.json"), key=lambda path: path.stat().st_mtime)
        return [_read(path) for path in asked if not (self.directory / "answers" / path.name).exists()]

    def answer(self, answer: Any, question: str | None = None) -> dict:
        """Give the answer to a question (to the only open one when none is named). The run is not resumed."""
        waiting = self.questions()
        if question is None:
            if len(waiting) != 1:
                raise SpecError(f"{len(waiting)} questions are open: name the one this answers"
                                if waiting else "no question is open")
            question = waiting[0]["id"]
        elif question not in {q["id"] for q in waiting}:
            raise SpecError(f"no open question {question!r}")
        _write(self.directory / "answers" / f"{question}.json", {"answer": answer})
        return self.status()

    # ---- state -----------------------------------------------------------------------------------------

    def _state(self, status: str, **more) -> dict:
        previous = _read(self.directory / "state.json", {})
        state = {"status": status, "graph": more.pop("graph", previous.get("graph")),
                 "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **more}
        _write(self.directory / "state.json", state)
        return {**state, "questions": self.questions()}

    def status(self) -> dict:
        state = _read(self.directory / "state.json")
        if state is None:
            raise SpecError(f"{self.directory} holds no run")
        return {**state, "questions": self.questions()}
