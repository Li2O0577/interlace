# Changelog

Versions before 1.0 may change any interface; each change is listed here. From 1.0 on, the names exported
by `interlacing` and the formats of graph files, run directories and journals change incompatibly only
with a new major version.

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
