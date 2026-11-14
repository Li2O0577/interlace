# interlacing

[English](README.md) · **中文**

把任何东西连成一张图，然后运行它。

- **节点**是任何接收信息并给出信息的东西：模型、函数、外部程序、设备、人，或者另一张图。
- **边**按一份写明的协议把信息从一个节点带到另一个节点：排队的值、只留最新值、状态、信号、流、有应答的调用、按路径传递的文件。
- **方法**是把节点连起来的现成方式：链、环、分发再汇合、树、星形、需要人同意的树、成员在被叫到时才出现的池。

引擎运行这张图并照看这次运行：输出要经过检查，用量按预算计数，每次激活都计时，所有事情写入日志，被打断的运行可以接着跑，观察者在事情发生的当时就被告知。这个包是一个内核，用来在它上面搭框架。它只依赖 Python 标准库。

## 安装

```sh
pip install interlacing
pip install -e .            # 或者从代码检出安装
```

需要 Python 3.11 或更高版本，支持 Linux、macOS 和 Windows。

## 第一张图

```python
from interlacing import DEFAULT, Chain, Engine, FunctionNode, Port

double = FunctionNode("double", lambda x: x * 2, inputs=[Port("x", int)], outputs=[Port("y", int)])
show = FunctionNode("show", lambda x: f"={x}", inputs=[Port("x", int)], outputs=[Port("y", str)])

graph = Chain(double, show).build(DEFAULT)
print(Engine(graph, DEFAULT).run({"in": 21}).last("out"))     # =42
```

## 能做什么

**带工具的模型。** 工具是一条通向普通节点的调用边，所以一本词典、一个程序和一条机械臂的应答方式完全一样。

```python
from interlacing import AgentNode, GraphBuilder, SinkNode, Tool

agent = AgentNode("agent", model, system="...", tools=[Tool("lookup", "查一个词"), Tool("move", "移动机械臂")])
graph = (GraphBuilder().add(agent, dictionary, SinkNode("arm", arm.move, answers=True))
         .connect("agent.lookup", "dictionary.x", "call")
         .connect("agent.move", "arm.x", "call")
         .input("in", "agent.task").output("out", "agent.out").compile(DEFAULT))
```

`model` 是任何带有 `chat(system, messages, tools, max_tokens)` 的对象。`interlacing.models` 里有 Anthropic 接口、OpenAI 兼容接口，以及测试用的 `FakeProvider`。

**经过检查的输出。** 端口写明什么可以从它出去，以及出去的是别的东西时怎么办：节点失败，或者带着警告继续走，或者告诉节点哪里不对并允许它重发。

```python
Port("plan", dict, check=valid_plan, repairs=2)      # 模型会被告知计划为什么被拒，最多两次
```

**按路径传递的文件。** 节点发送的是相对路径，不是内容。文件在发出时检查一次，到达时再检查一次。

```python
protocols = with_files(DEFAULT, "work")
Port("chart", FileOf(".png", max_size=5_000_000), repairs=2)
builder.connect("draw.chart", "report.chart", "file")
```

**把设计当作单元使用。** 一张已经想清楚的图成为一个块，可以多次使用，也可以和别的块拼接。

```python
review = Block(lambda: Loop(writer(), critic(), max_rounds=3), name="review")
study = seq(clean, par(review, review, review), publish)       # 顺排、并排、嵌套
graph = Chain(intake, study("study"), report).build(DEFAULT)
```

**块门口的收发台。** `Desk` 接收从多处到来的东西，整理之后分发到多处。放在块的入口，它把工作分给块内的成员；放在块的出口，它把成员给出的结果整合成一份答复。整理需要判断力时，换成一个输入输出相同的模型节点站在同一个位置。

```python
team = Fan(analyst, modeller,                                   # 每个成员按名字拿到自己的那一份
           split=Desk("door", takes=["brief", "data"], gives=["analyst", "modeller"], sort=share),
           join=Desk("exit", takes=["analyst", "modeller"], sort=report))
```

**不事先固定的图。** `Pool` 为成员留好位置，成员在被叫到之前什么也不做。`SpawnNode` 构建并运行一张由节点在运行中写出的图，范围由创建这个节点的人限定。

```python
grow = SpawnNode("grow", Catalog(allow=["my_steps"]), max_nodes=20)
graph = Chain(planner, grow).build(DEFAULT)                    # planner 发出 {"graph": ..., "inputs": ...}
```

**被照看的运行。**

```python
engine = Engine(graph, DEFAULT, workers=4,
                journal=Journal("run.jsonl"),                  # 被打断的运行用 resume() 接着跑
                time_limit=600,                                # 一次激活最长可以用多久
                budget={"tokens": 2_000_000},                  # 节点上报用量；超出则运行停止
                observe=print)                                 # 每个事件，在发生的当时
```

不能在图外把同一件事做两遍的节点使用 `ctx.key`，被打断的激活重跑时它保持不变。

**写成数据的图。** 图文件里只有结构和与默认值不同的部分；提示词是一个被引用的文件。

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
interlacing check  release.json --allow steps
interlacing run    release.json --dir /tmp/run --allow steps --input topic=tides    # 在等人回答，退出码 3
interlacing answer --dir /tmp/run --json '{"approved": true}'
interlacing resume --dir /tmp/run                                                   # 完成，退出码 0
```

一次运行就是一个目录。图里的人还没有回答时，不会有进程一直等着：命令退出，等回答给出后运行再继续。同一个目录同一时间只由一个进程运行。

**作为 MCP 服务。** 同样的操作可以提供给另一个应用里的模型。它的图能触及什么、能用多少、一个节点能跑多久，由启动服务的人在命令行上设定，客户端改不了。

```json
{"mcpServers": {"interlacing": {"command": "interlacing",
  "args": ["serve", "--runs", "/path/to/runs", "--allow", "mypkg", "--budget", "tokens=500000"]}}}
```

模型密钥从环境变量读取（`ANTHROPIC_API_KEY`、`OPENAI_API_KEY` 以及对应的 `_BASE_URL`），不会写入图文件、运行目录或日志。

## 用来做什么

三个展开讲的案例在 [docs/CASES.md](docs/CASES.md)。简要如下：

| 案例 | 怎么搭 |
|---|---|
| 具身智能体：多模态模型把夹爪位姿规划成空间中的点，控制器执行，下一次观测决定接下来做什么 | `Loop(observe, planning, act)`；模型的测量工具是调用边上的节点；动作约定是计划端口上的检查，所以坏计划带着原因回到模型手里，到不了控制器；计划次数和秒数是预算。用替身可以直接运行：[examples/graphs/planner_and_arm.py](examples/graphs/planner_and_arm.py) |
| 建模助手：一个组长，每个问题一个负责人，求解者和质疑者争论到答案被证明为止，人可以在任何地方介入 | 每个问题是一个装着 `Loop(solve, critic)` 的 `Block`；“没有证明就不算完成”是一个端口检查；问题用 `seq` 依次进行；人是 `Governed` 树的操作者；运行目录在重启后仍然有效 |
| 数学家：组长并行派出探索者，每个探索者把成果交给证明检查器，通过后才汇报 | `Pool(lead, explorer, size=10)` 加 `workers`；每个探索者是一个 `Loop(prove, check)`；给出三个方向，就只存在三个探索者 |

内核负责传递信息和照看运行。模型怎么想、机械臂怎么动、论文怎么排版，是节点自己的事。

## 组成

| 层 | 包 | 主要名字 |
|---|---|---|
| 抽象 | `interlacing.base` | `Node`、`Port`、`Schema`、`Edge`、`Protocol`、`Envelope`、`Graph`、`GraphBuilder`、`Method`、`Context` |
| 引擎 | `interlacing.engine` | `Engine`、`Result`、`RunError`、`Journal`、`Transport` |
| 部件 | `interlacing.nodes`、`.protocols`、`.methods`、`.models` | `FunctionNode`、`AgentNode`、`ProcessNode`、`SourceNode`、`SinkNode`、`HumanNode`、`GraphNode`、`Desk`；`Chain`、`Loop`、`Fan`、`Tree`、`Star`、`Governed`、`Pool` |
| 块 | `interlacing.blocks` | `Block`、`seq`、`par` |
| 写成数据的图 | `interlacing.spec` | `dump`、`load`、`read`、`write`、`Catalog`、`SpawnNode` |
| 对外 | `interlacing.runs`、`interlacing.cli`、`interlacing.serve` | `Run`；命令 `interlacing`；MCP 服务 |

每一层只使用它下面的层。引擎不认识任何具体的节点、协议或方法，`tests/test_engine.py` 里有一个测试守着这一点。

完整说明见 [docs/GRAPH.md](docs/GRAPH.md)。九个可以直接运行的例子在 [examples/graphs/](examples/graphs/)，写成 JSON 的图在 [examples/data/](examples/data/)。

## 测试

```sh
python -m pip install -e . pytest
python -m pytest -q tests
python scripts/bench_engine.py       # 引擎给每个信封增加的开销（约 11 微秒）
```

测试在 Linux、macOS 和 Windows 上运行，使用按脚本应答的模型和替身设备。其中包括在随机位置打断的运行，日志像进程突然死掉时那样被截断，这些运行必须和没被打断的运行得到同样的结局。这个包通过 `mypy` 检查。

## 限制

- 接着跑是“至少一次”：被打断的节点会重跑，它在图外做过的事会再做一遍，除非节点用 `ctx.key` 防止。
- 投递只在进程内。`Transport` 是在进程或机器之间传递信封的位置，目前那里什么都没有。
- 两种模型接口各用很小的请求在一个兼容服务上试过一次。思考输出、图片输入和限流重试还没有在真实服务上试过。设备节点只接触过替身。
- 超时的节点是被放弃，而不是被杀掉：线程无法从外部停止。`ProcessNode` 启动的程序会被杀掉。
- `allow` 是一份模块清单，图可以点名其中的函数。它不是沙箱。
- MCP 服务只走 stdio，一次处理一个调用，还没有接过真实客户端。
- 没有异步接口；引擎使用线程。

## 许可

MIT
