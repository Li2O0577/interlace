# Changelog

Versions before 1.0 may change any interface; each change is listed here. From 1.0 on, the names exported
by `interlacing` and the formats of graph files, run directories and journals change incompatibly only
with a new major version.

## Unreleased

- `ctx.receive(ports=None, timeout=None)` (and `await ctx.areceive(...)`): a running node waits for its next
  message and takes it, in the order messages came. While it waits it holds none of the run's workers, so
  `workers=0` or `1` cannot be blocked by it, and its time limit does not run. It gets None when the time is
  up, when it is stopped or the run is ending, or when nothing more can come because every other activation
  has ended or waits too: two nodes waiting for each other both get None and can finish. What it takes is
  the activation's, journaled with it and put back if the activation is cut. Observers are told
  `received`. A node told to end no longer sends what its `run` returns.
- `ctx.commit()`: a long activation saves its progress (what it took, sent and holds) to the journal and goes
  on. Taken up after the process ended, or switched on again after a stop, it is not run again from the start:
  the node gets back its saved state and runs once more with `ctx.resumed` true and no inputs. A restart
  starts it again from what it first had.

## 0.14.0

- Shorter ways to write a graph. A plain function is a node: its name is the id, its parameters the inputs,
  its annotations the schemas, and what it returns leaves through `out` (`as_node`, `FunctionNode.of`).
  `@node(...)` writes only what differs. `Chain`, `Loop`, `Fan` and `Tree` take functions and other methods
  where they take nodes. `interlacing.run(thing, inputs)` builds and runs in one step.
- Protocols may be left out: a compiled graph keeps the protocols it was built with, and `build()`,
  `compile()`, `Engine(graph)`, `GraphNode(id, graph)` and `ctx.engine(graph)` use them, or `DEFAULT`.
  `connect(a, b)` is a `value` edge.
- A port that says nothing of what it produces may feed a port that asks for a schema; what arrives is
  checked there, and fails or warns as the receiving port says. Ports whose schemas disagree are still refused.
- `FolderNode` (`"kind": "folder"`): a whole application in a folder, described by its `node.json`, as one
  node. Inputs go in as one JSON object and the answer comes back as one, by file or on standard output; a
  text format suits plain command-line programs. Graph files reach only the folders under `--apps`
  (`Catalog(apps=)`, `Run.create(apps=)`, the server).
- A graph changes while it runs through one explicit operation: `ctx.change(Change().add(...).connect(...)
  .remove(...).disconnect(...).output(...))`. The change is checked by every rule a graph is built by and is
  refused whole (`GraphError`) or made at once; observers are told (`changed`); with a journal it is kept with
  the activation and made again on resume, the added nodes rebuilt from their descriptions
  (`Engine(rebuild=)`, done by `Run` itself; `spec.load_node`). `Engine(max_nodes=)` bounds the growth, and
  `Engine.graph` is the graph as it stands.
- `RemoteNode` (`"kind": "remote"`): a node that lives in another process or on another machine and stays
  up between activations. It is served there with `serve_node(node)` or `interlacing node-serve
  module:name`; one JSON object per line over TCP carries the inputs, what the node emits, what it uses and a
  stop. A server listening beyond 127.0.0.1 needs a token; graph files read the token from an environment
  variable (`token_env`) and reach only the addresses allowed (`Catalog(remotes=)`, `Run.create(remotes=)`,
  `--remote` on `check`, `run` and `serve`).
- Nodes may be coroutines: `run` (and `open`, `close`) may be `async def`, and so may the function of a
  `FunctionNode` or `@node`. A graph that has such nodes runs on an event loop: they are awaited there, without
  a thread each, and the others go to threads; `workers` still bounds how many activations are under way,
  `workers=0` still runs one at a time in a fixed order. One that overruns its time is cancelled. `await
  ctx.acall(port, payload)` calls from such a node. `arun`, `aresume` and `ago` run such a graph on the
  caller's loop; `run` starts a loop of its own. `Node.asynchronous` says which kind a node is.
- A switch on every node: `engine.control(nodes, "pause" | "resume" | "stop" | "restart", team=False)`, one
  node or several. A running node acts on it at its next step (`ctx.checkpoint()`, called by `AgentNode`
  before each request to its model and each tool): paused, it waits there; stopped, it ends and gives back
  what it took; restarted, it also forgets what it held (`Node.reset`). Who manages whom is part of the graph
  (`GraphBuilder.manage`, `"manages"` in a graph file, `Graph.team`), and `ctx.control` switches only nodes
  under the caller. Switches are journaled and kept on resume; `Run.control`, `interlacing control` and the
  MCP tool `control` set them, between goes too. A run whose remaining work is all paused ends, not complete,
  with `Result.held`, to be taken up later. Time spent paused does not count against a time limit.
  A run going in another process is switched through its directory (`controls/`), and `changed` names only
  the nodes that changed. Pausing reaches work handed outside: a `RemoteNode`'s far side waits at its own
  next step, and a program a node runs is suspended with it (on POSIX), its own timeout not counting the
  pause. `ctx.paused()` tells a node it is paused. `Engine.going` says whether a run is under way.
- The engine is split into parts that each do one thing (`wiring`, `scheduler`, `checks`, `ledger`,
  `recorder`, `clock`, `context`, `result`). Nothing exported changed, and it is as fast as before.
  `Engine.used` says what the run has used so far.

## 0.13.0

- `Workspace` and `Area`: a run's files laid out by node (`<node>/`, `shared/`), one more layer for each block;
  each node reads and writes within its bounds (`"reads"`, `"writes"` in a graph file); who wrote each file is
  recorded. `Run.files()` and `interlacing files --dir RUN`.
- `WorkerNode` (`"kind": "worker"`): an agent with file tools for its area and a tool that runs commands in its
  own directory; standard inputs and outputs.
- `ToolPool`: a directory of tool files (a Python function, or a JSON file naming a program). Agents take tools
  by path; each use runs in a process of its own. `Catalog(tools=)` and `--tools DIRECTORY` on the command line
  and the server.
- `Tool(run=...)`: a tool the node answers itself, with no port and no edge.
- An agent with an area may only send paths it can read; a path outside goes back to the model with the reason.

## 0.12.0

- `await engine.arun()`, `aresume()` and `ago()`; cancelling the wait stops the run. A `FunctionNode`'s function
  may be `async def`.
- `ctx.once(name, do)`: what a node does outside the graph is kept in the journal and is not done again when
  its activation runs again.
- `IsolatedNode`: a function in a process of its own, killed when it overruns or the run stops (`"kind":
  "isolated"` in a graph file, where processes are allowed).
- The MCP server answers tool calls side by side. `start` and `resume` take `wait: false`; a new tool, `stop`;
  a cancelled call stops its run; a client that goes away leaves nothing running. Tried with the reference
  client.
- `Run.status()` says `running` while a process holds the run directory; `Run.stop()`.
- The model interfaces are tested against a stand-in service. Fixed: a `Retry-After` of 0 became a wait of five
  seconds; an agent did not give a model its thinking back between tool calls, so it stopped thinking there.

## 0.11.0

First release on the package index.

- Nodes (`FunctionNode`, `AgentNode`, `ProcessNode`, `SourceNode`, `SinkNode`, `HumanNode`, `GraphNode`),
  protocols (`value`, `latest`, `state`, `signal`, `stream`, `call`, `artifact`, `file`) and methods (`Chain`, `Loop`,
  `Fan`, `Tree`, `Star`, `Governed`, `Pool`).
- The engine: workers, journal and `resume`, time limits, budgets, an observer, output checks with repairs.
- `Block`, `seq`, `par`; graphs as JSON with `Catalog`; `SpawnNode` for graphs written during a run.
- `Desk`: a node that takes in from several places, sorts, and hands out to several; `Fan` joins a split and a
  join to its branches by name, and shows everything they take and give.
- Run directories, the `interlacing` command, and an MCP server over stdio.
- Graphs that are not trusted are held to the run's time limit, node count and workers (`firm`, `strict`).
