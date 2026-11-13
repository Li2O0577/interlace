"""The command line.

    1ndeterminate check  graph.json [--allow MODULE]... [--processes]
    1ndeterminate run    graph.json --dir RUN [--input NAME=VALUE]... [--allow MODULE]... [--processes] [--workers N]
                         [--time-limit SECONDS]
    1ndeterminate resume --dir RUN
    1ndeterminate answer --dir RUN [QUESTION] (--json VALUE | --text TEXT)
    1ndeterminate status --dir RUN
    1ndeterminate serve  --runs DIRECTORY [--allow MODULE]... [--processes] [--workers N] [--time-limit SECONDS]
                         [--files DIRECTORY]

A graph file may take text (a prompt, say) from files in its own directory: ``{"file": "prompts/lead.md"}``.

Every command but ``serve`` prints JSON; ``serve`` is the MCP server in ``indeterminate.serve``. The exit
status says how a run stands: 0 done, 1 failed or refused, 3 waiting for a person, 4 stopped.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .engine import JournalError
from .runs import DONE, STOPPED, WAITING, Run, check
from .spec import SpecError

EXIT = {DONE: 0, WAITING: 3, STOPPED: 4}


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _inputs(args) -> dict:
    inputs = {}
    if args.inputs:
        inputs.update(json.loads(Path(args.inputs).read_text(encoding="utf-8")))
    for pair in args.input or ():
        name, equals, value = pair.partition("=")
        if not equals:
            raise SpecError(f"--input takes NAME=VALUE, not {pair!r}")
        inputs[name] = _value(value)
    return inputs


def _description(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SpecError(f"no such file: {path}") from None
    except ValueError as exc:
        raise SpecError(f"{path}: not JSON: {exc}") from None


def _beside(path: str) -> Path:
    return Path(path).resolve().parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="1ndeterminate", description="Check and run graphs written as data.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    def reach(command):
        command.add_argument("--allow", action="append", default=[], metavar="MODULE",
                             help="a module the graph's functions may come from (repeatable)")
        command.add_argument("--processes", action="store_true", help="let the graph start external programs")

    check = commands.add_parser("check", help="build the graph and describe it, without running anything")
    check.add_argument("graph")
    reach(check)
    run = commands.add_parser("run", help="start a run in a new directory")
    run.add_argument("graph")
    run.add_argument("--dir", required=True, help="the directory that will hold the run")
    run.add_argument("--input", action="append", metavar="NAME=VALUE", help="a graph input; VALUE is JSON, or else text")
    run.add_argument("--inputs", metavar="FILE", help="a JSON file of graph inputs")
    run.add_argument("--workers", type=int, default=0, help="threads that run nodes side by side (default: none)")
    run.add_argument("--time-limit", type=float, metavar="SECONDS", help="the longest one activation of a node may take")
    reach(run)
    for name, text in (("resume", "carry on a run that stopped, failed or was waiting"), ("status", "say where a run stands")):
        commands.add_parser(name, help=text).add_argument("--dir", required=True)
    answer = commands.add_parser("answer", help="answer a question a person in the graph was asked")
    answer.add_argument("--dir", required=True)
    answer.add_argument("question", nargs="?", help="the question's id; not needed when only one is open")
    given = answer.add_mutually_exclusive_group(required=True)
    given.add_argument("--json", metavar="VALUE", help="the answer, as JSON")
    given.add_argument("--text", help="the answer, as text")
    serve = commands.add_parser("serve", help="offer these operations to an MCP client over stdio")
    serve.add_argument("--runs", required=True, help="the directory under which the server keeps its runs")
    serve.add_argument("--workers", type=int, default=0, help="threads that run nodes side by side (default: none)")
    serve.add_argument("--time-limit", type=float, metavar="SECONDS", help="the longest one activation of a node may take")
    serve.add_argument("--files", metavar="DIRECTORY", help="a directory graphs may take text from (default: none)")
    reach(serve)
    return parser


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            state, code = check(_description(args.graph), allow=args.allow, processes=args.processes, files=_beside(args.graph)), 0
        elif args.command == "serve":
            from .serve import Server
            Server(args.runs, allow=args.allow, processes=args.processes, workers=args.workers, time_limit=args.time_limit,
                   files=args.files).serve()
            return 0
        else:
            if args.command == "run":
                inputs = _inputs(args)
                state = Run.create(args.dir, _description(args.graph), allow=args.allow, processes=args.processes,
                                   workers=args.workers, files=_beside(args.graph), time_limit=args.time_limit).start(inputs)
            elif args.command == "resume":
                state = Run(args.dir).resume()
            elif args.command == "answer":
                state = Run(args.dir).answer(json.loads(args.json) if args.json is not None else args.text, args.question)
            else:
                state = Run(args.dir).status()
            code = EXIT.get(state["status"], 1) if args.command in ("run", "resume") else 0
    except (SpecError, JournalError, ValueError) as exc:
        state, code = {"status": "refused", "error": str(exc)}, 1
    json.dump(state, out, ensure_ascii=False, indent=2)
    out.write("\n")
    return code



if __name__ == "__main__":
    sys.exit(main())
