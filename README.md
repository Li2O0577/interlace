# interlace

**English** · [中文](https://github.com/Li2O0577/interlace/blob/main/README.zh-CN.md)

Join anything into a graph and run it.

- A **node** is anything that takes information in and gives information out: a model, a function, an external program, a device, a person, or another graph.
- An **edge** carries information from one node to another under a written protocol: queued values, latest value only, state, signals, streams, calls that are answered, files passed by path.
- A **method** is a ready-made way of joining nodes: chain, loop, fan-out and join, tree, star, a tree that needs a person's consent, a pool of members that appear when called.

The engine runs the graph and looks after the run: outputs are checked, usage is counted against a budget, activations are timed, everything is journaled so an interrupted run carries on, and an observer is told what happens as it happens. The package is a kernel to build frameworks on. It depends on the Python standard library and nothing else.

## Install

```sh
pip install interlace
pip install -e .            # or from a checkout
```

Python 3.11 or later, on Linux, macOS and Windows.

## A first graph

```python
from interlace import DEFAULT, Chain, Engine, FunctionNode, Port

double = FunctionNode("double", lambda x: x * 2, inputs=[Port("x", int)], outputs=[Port("y", int)])
show = FunctionNode("show", lambda x: f"={x}", inputs=[Port("x", int)], outputs=[Port("y", str)])

graph = Chain(double, show).build(DEFAULT)
print(Engine(graph, DEFAULT).run({"in": 21}).last("out"))     # =42
```

## What it does

**A model with tools.** A tool is a call edge to an ordinary node, so a dictionary, a program and a robot arm are all answered the same way.

```python
from interlace import AgentNode, GraphBuilder, SinkNode, Tool

agent = AgentNode("agent", model, system="...", tools=[Tool("lookup", "Look a word up"), Tool("move", "Move the arm")])
graph = (GraphBuilder().add(agent, dictionary, SinkNode("arm", arm.move, answers=True))
         .connect("agent.lookup", "dictionary.x", "call")
         .connect("agent.move", "arm.x", "call")
         .input("in", "agent.task").output("out", "agent.out").compile(DEFAULT))
```

`model` is any object with `chat(system, messages, tools, max_tokens)`. `interlace.models` has an Anthropic interface, an OpenAI-compatible interface, and `FakeProvider` for tests.

**Outputs that are checked.** A port says what may leave through it and what happens when something else does: the node fails, the payload goes on with a warning, or the node is told what is wrong and may send again.

```python
Port("plan", dict, check=valid_plan, repairs=2)      # a model is told why its plan was refused, twice at most
```

**Files passed by path.** Nodes send relative paths, not contents. The file is checked when it is sent and again when it arrives.

```python
protocols = with_files(DEFAULT, "work")
Port("chart", FileOf(".png", max_size=5_000_000), repairs=2)
builder.connect("draw.chart", "report.chart", "file")
```

**Designs used as units.** A graph that has been worked out becomes a block, used many times and joined with others.

```python
review = Block(lambda: Loop(writer(), critic(), max_rounds=3), name="review")
study = seq(clean, par(review, review, review), publish)       # in a row, side by side, nested
graph = Chain(intake, study("study"), report).build(DEFAULT)
```

**Graphs that are not fixed in advance.** `Pool` keeps places for members that do nothing until they are called for. `SpawnNode` builds and runs a graph that a node wrote during the run, within limits set by whoever made the node.

```python
grow = SpawnNode("grow", Catalog(allow=["my_steps"]), max_nodes=20)
graph = Chain(planner, grow).build(DEFAULT)                    # planner sends {"graph": ..., "inputs": ...}
```

**Runs that are looked after.**

```python
engine = Engine(graph, DEFAULT, workers=4,
                journal=Journal("run.jsonl"),                  # an interrupted run is taken up with resume()
                time_limit=600,                                # the longest one activation may take
                budget={"tokens": 2_000_000},                  # nodes report what they use; over it, the run stops
                observe=print)                                 # every event, as it happens
```

A node that must not do something twice outside the graph uses `ctx.key`, which stays the same when an interrupted activation runs again.

**Graphs written as data.** A graph file holds the structure and whatever differs from the defaults; a prompt is a file that is referred to.

```json
{
  "defaults": {"agent": {"model": "anthropic:some-model", "time_limit": 600}},
  "method": "pool", "size": 10, "max_rounds": 3,
  "root": {"id": "lead", "kind": "agent", "system": {"file": "prompts/lead.md"}, "firing": "any",
           "inputs": ["task", {"name": "reply", "required": false}], "outputs": ["out", "send"]},
  "member": {"kind": "agent", "system": {"file": "prompts/scout.md"}}
}
```

```sh
cd examples/data && export PYTHONPATH=.
interlace check  release.json --allow steps
interlace run    release.json --dir /tmp/run --allow steps --input topic=tides    # waiting for a person, exit code 3
interlace answer --dir /tmp/run --json '{"approved": true}'
interlace resume --dir /tmp/run                                                   # done, exit code 0
```

A run is a directory. A person in the graph who has not answered yet does not keep a process waiting: the command exits, and the run goes on once the answer has been given. One process at a time runs a directory.

**As an MCP server.** The same operations can be offered to a model in another application. What its graphs may reach, how much they may use and how long a node may take are set on the command line by whoever starts the server, and the client cannot change them.

```json
{"mcpServers": {"interlace": {"command": "interlace",
  "args": ["serve", "--runs", "/path/to/runs", "--allow", "mypkg", "--budget", "tokens=500000"]}}}
```

Model keys are read from the environment (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and their `_BASE_URL`), and are never written to a graph file, a run directory or a journal.

## What it is for

Three worked cases are in [docs/CASES.md](https://github.com/Li2O0577/interlace/blob/main/docs/CASES.md) (in Chinese). In short:

| Case | How it is built |
|---|---|
| A physical agent: a multimodal model plans gripper poses as points in space, a controller executes, and the next observation decides what follows | `Loop(observe, planning, act)`; the model's measuring tools are nodes on call edges; the action contract is a check on the plan port, so a bad plan goes back to the model with the reason and never reaches the controller; plans and seconds are budgets. Runnable with stand-ins: [examples/graphs/planner_and_arm.py](https://github.com/Li2O0577/interlace/blob/main/examples/graphs/planner_and_arm.py) |
| A modelling assistant: a lead, one owner per question, a solver and a critic who argue until the answer is proved, a person who may step in anywhere | each question is a `Block` holding a `Loop(solve, critic)`; "no proof, not done" is a port check; questions run in `seq`; the person is the operator of a `Governed` tree; the run directory survives restarts |
| A mathematician: a lead sends out explorers in parallel, each hands its work to a proof checker and reports back when it passes | `Pool(lead, explorer, size=10)` with `workers`; each explorer is a `Loop(prove, check)`; three directions given, three explorers exist |

The kernel carries information and looks after the run. How a model thinks, how an arm moves and how a paper is typeset are the business of the nodes.

## Layout

| Layer | Package | Main names |
|---|---|---|
| Abstractions | `interlace.base` | `Node`, `Port`, `Schema`, `Edge`, `Protocol`, `Envelope`, `Graph`, `GraphBuilder`, `Method`, `Context` |
| Engine | `interlace.engine` | `Engine`, `Result`, `RunError`, `Journal`, `Transport` |
| Parts | `interlace.nodes`, `.protocols`, `.methods`, `.models` | `FunctionNode`, `AgentNode`, `ProcessNode`, `SourceNode`, `SinkNode`, `HumanNode`, `GraphNode`; `Chain`, `Loop`, `Fan`, `Tree`, `Star`, `Governed`, `Pool` |
| Blocks | `interlace.blocks` | `Block`, `seq`, `par` |
| Graphs as data | `interlace.spec` | `dump`, `load`, `read`, `write`, `Catalog`, `SpawnNode` |
| Outside | `interlace.runs`, `interlace.cli`, `interlace.serve` | `Run`; the command `interlace`; the MCP server |

Each layer uses only the layers below it. The engine knows no particular node, protocol or method; a test in `tests/test_engine.py` holds it to that.

The full description is [docs/GRAPH.md](https://github.com/Li2O0577/interlace/blob/main/docs/GRAPH.md) (in Chinese). Eight examples that run as they stand are in [examples/graphs/](https://github.com/Li2O0577/interlace/tree/main/examples/graphs), and graphs written as JSON in [examples/data/](https://github.com/Li2O0577/interlace/tree/main/examples/data).

## Tests

```sh
python -m pip install -e . pytest
python -m pytest -q tests
python scripts/bench_engine.py       # what the engine adds per envelope (about 11 microseconds)
```

The tests run on Linux, macOS and Windows, with scripted models and stand-in devices. They include runs broken at random places, with the journal cut short as a dying process leaves it, which must end as unbroken runs do. The package passes `mypy`.

## Limits

- Taking a run up again is at-least-once: an interrupted node runs again, and what it did outside the graph is done again unless the node uses `ctx.key` to prevent it.
- Delivery is in-process only. `Transport` is the place for carrying envelopes between processes or machines, and nothing is there yet.
- Both model interfaces have been tried once, with very small requests, against one compatible service. Thinking output, picture input and retries on rate limits have not been tried against a real service. Device nodes have only met stand-ins.
- A node that overruns its time is abandoned, not killed: a thread cannot be stopped from outside. A program started by `ProcessNode` is killed.
- `allow` is a list of modules whose functions a graph may name. It is not a sandbox.
- The MCP server speaks over stdio only, handles one call at a time, and has not been tried with a real client.
- There is no asynchronous interface; the engine uses threads.

## License

MIT
