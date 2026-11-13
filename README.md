# 1ndeterminate

把任何东西接成一张图并运行它。

- **节点**是接收信息、给出信息的任何东西：模型、函数、外部程序、设备、人，或者另一张图。
- **边**按写好的协议把信息从一个节点带到另一个节点：排队的数据、只留最新值、状态、信号、流、有回答的调用、按引用传递的文件。
- **方法**是把节点接起来的现成结构：链、循环、扇出汇合、树、星形、需要人同意的树。

引擎按图运行：事件驱动调度，可并行，可写日志并在中断后继续，可以在改过的图下继续。只依赖 Python 标准库。

## 安装

```sh
pip install -e .            # 在源码目录安装（尚未发布到公共索引）
uv build -o dist            # 构建 wheel
```

需要 Python 3.11 及以上。发布名是 `1ndeterminate`，导入名是 `indeterminate`（Python 模块名不能以数字开头）。

## 用法

```python
from indeterminate import DEFAULT, Chain, Engine, FunctionNode, Port

double = FunctionNode("double", lambda x: x * 2, inputs=[Port("x", int)], outputs=[Port("y", int)])
show = FunctionNode("show", lambda x: f"={x}", inputs=[Port("x", int)], outputs=[Port("y", str)])

graph = Chain(double, show).build(DEFAULT)
print(Engine(graph, DEFAULT).run({"in": 21}).last("out"))     # =42
```

一个模型带两个工具，工具由图上的节点回答：

```python
from indeterminate import AgentNode, GraphBuilder, SinkNode, Tool

agent = AgentNode("agent", model, system="...", tools=[Tool("lookup", "查词"), Tool("move", "移动机械臂")])
graph = (GraphBuilder().add(agent, dictionary, SinkNode("arm", arm.move, answers=True))
         .connect("agent.lookup", "dictionary.x", "call")
         .connect("agent.move", "arm.x", "call")
         .input("in", "agent.task").output("out", "agent.out").compile(DEFAULT))
```

`model` 是任何带 `chat(system, messages, tools, max_tokens)` 的对象；`indeterminate.models` 里有 Anthropic 接口、OpenAI 兼容接口和用于测试的 `FakeProvider`。

完整说明见 [docs/GRAPH.md](docs/GRAPH.md)。四个可以直接运行的示例在 [examples/graphs/](examples/graphs/)：链里接一个外部程序、写作和评审的循环、需要人同意的树、相机到机械臂。

## 组成

| 层 | 包 | 主要名称 |
|---|---|---|
| 抽象 | `indeterminate.base` | `Node`、`Port`、`Schema`、`Edge`、`Protocol`、`Envelope`、`Graph`、`GraphBuilder`、`Method` |
| 引擎 | `indeterminate.engine` | `Engine`、`Result`、`Journal`、`Transport` |
| 现成部件 | `indeterminate.nodes`、`.protocols`、`.methods`、`.models` | `FunctionNode`、`AgentNode`、`ProcessNode`、`SourceNode`、`SinkNode`、`HumanNode`、`GraphNode`；`Chain`、`Loop`、`Fan`、`Tree`、`Star`、`Governed` |

每层只引用下面的层，引擎不认识任何具体的节点、协议或方法；`tests/test_engine.py` 里有测试检查这条规则。

## 测试

```sh
python -m pip install -e . pytest
python -m pytest -q tests
python scripts/bench_engine.py       # 每个信封的调度开销
```

测试只用脚本化的假模型和假设备，不调用真实模型。

## 限制

- 中断后的恢复是“至少一次”：被打断的节点会重跑，它对图外做过的事会再做一次。
- 目前只有进程内的传递；`Transport` 是留给跨进程、跨机器的接口。
- 设备节点没有接过真实设备，智能体节点没有用真实模型跑过。
- 还没有命令行和 MCP 服务这类对外接口。

## 许可

MIT
