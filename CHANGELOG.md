# Changelog

Versions before 1.0 may change any interface; each change is listed here. From 1.0 on, the names exported
by `inter1ace` and the formats of graph files, run directories and journals change incompatibly only
with a new major version.

## 0.11.0

First release on the package index.

- Nodes (`FunctionNode`, `AgentNode`, `ProcessNode`, `SourceNode`, `SinkNode`, `HumanNode`, `GraphNode`),
  protocols (`value`, `latest`, `state`, `signal`, `stream`, `call`, `artifact`, `file`) and methods (`Chain`, `Loop`,
  `Fan`, `Tree`, `Star`, `Governed`, `Pool`).
- The engine: workers, journal and `resume`, time limits, budgets, an observer, output checks with repairs.
- `Block`, `seq`, `par`; graphs as JSON with `Catalog`; `SpawnNode` for graphs written during a run.
- `Desk`: a node that takes in from several places, sorts, and hands out to several; `Fan` joins a split and a
  join to its branches by name, and shows everything they take and give.
- Run directories, the `inter1ace` command, and an MCP server over stdio.
- Graphs that are not trusted are held to the run's time limit, node count and workers (`firm`, `strict`).
