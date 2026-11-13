# 节点、边、方法

这个包把任何东西接成一张图并运行它。图里的节点可以是模型、函数、外部程序、设备、人，也可以是另一张图；边是写好的传递协议；方法是把节点接起来的现成结构。

```python
from indeterminate.base import Port
from indeterminate.engine import Engine
from indeterminate.methods import Chain
from indeterminate.nodes import FunctionNode
from indeterminate.protocols import DEFAULT

double = FunctionNode("double", lambda x: x * 2, inputs=[Port("x", int)], outputs=[Port("y", int)])
show = FunctionNode("show", lambda x: f"={x}", inputs=[Port("x", int)], outputs=[Port("y", str)])

graph = Chain(double, show).build(DEFAULT)
print(Engine(graph, DEFAULT).run({"in": 21}).last("out"))     # =42
```

## 分层

| 层 | 包 | 内容 |
|---|---|---|
| 抽象 | `indeterminate.base` | `Node`、`Port`、`Schema`、`Edge`、`Protocol`、`Envelope`、`Graph`、`GraphBuilder`、`Method` |
| 引擎 | `indeterminate.engine` | `Engine`、`Result`、`Journal`、`Transport`、`Stream` |
| 现成部件 | `indeterminate.nodes`、`indeterminate.protocols`、`indeterminate.methods`、`indeterminate.models` | 见下 |
| 数据格式 | `indeterminate.spec` | `dump`、`load`、`read`、`write`、`Catalog` |
| 对外接口 | `indeterminate.runs`、`indeterminate.cli` | `Run`；命令 `1ndeterminate` |

引擎只认识抽象层，不认识任何具体的节点、协议或方法。`tests/test_engine.py` 里有一个测试检查各层的引用方向。

## 节点

节点声明自己的输入口和输出口，实现一个 `run(ctx, inputs)`。它不知道自己在哪张图里，上下游是谁，输出怎么传递；它碰到的外部一切都经过 `ctx`：

- `ctx.emit(port, payload)`：从一个输出口发出，一次激活里可以发多次；
- `ctx.call(port, payload)`：从一个调用口发出请求并等待回答；
- `ctx.stopping()`：运行正在停止，长时间的激活应当尽快返回。

`run` 也可以直接返回 `{输出口: 内容}`。`open`、`close`、`snapshot`、`restore` 是可选的。

端口：`Port(name, schema, required=True, many=False, call=False)`。`schema` 描述内容的形状，建图时检查两端是否相容，运行时检查每一份内容。`many` 的输入口可以接多条边，收到的是列表。`call=True` 的输出口用来发起调用。

触发方式：`Firing.ALL`（默认）等所有必需输入到齐后运行一次；`Firing.ANY` 每到一份输入就运行一次。

现成的节点：

| 节点 | 是什么 |
|---|---|
| `FunctionNode` | 一个 Python 函数 |
| `AgentNode` | 一个模型。工具是调用口；有多个输出口时由模型用 `send_<口名>` 选择。模型是任何带 `chat` 方法的对象，`indeterminate.models` 里有现成的 |
| `ProcessNode` | 一个外部程序，输入写到标准输入，标准输出作为输出 |
| `SourceNode`、`SinkNode` | 外部读数的来源、结果的去处（相机、机械臂这类设备接在这里） |
| `HumanNode` | 人。请求交给 `ask(request)`，它返回时就是人的回答 |
| `GraphNode` | 一整张图当成一个节点，方法由此嵌套 |
| `Pass`、`Collect`、`Branch`、`Until` | 只负责流向：原样转发、汇合成列表、二选一、循环是否继续 |

## 边和协议

边从一个输出口到一个输入口，带一个协议名。协议决定什么能通过、怎么投递、到达时是否启动接收节点。

| 协议 | 投递 |
|---|---|
| `value` | 每份内容一个信封，按顺序排队，一次激活取一个 |
| `latest` | 只保留最新的一个，旧的未读即被替换 |
| `state` | 最新值随时可读，但到达不会启动节点，读取也不消耗 |
| `signal` | 没有内容，只有到达这件事 |
| `stream` | 发送方一次激活发出的内容是一个序列，接收方边写边读 |
| `call` | 请求和回答：发送方等待，接收节点为这次请求运行，返回值是回答 |
| `artifact` | 内容存进内容库，信封里只有引用（哈希、大小、类型） |

`DEFAULT` 是前六种的注册表；`standard(store)` 另外带上 `artifact`。自己的协议继承 `Protocol`，实现 `check`、`encode`、`decode`，注册进 `Protocols`。

回边：`connect(..., back=True, max_rounds=N)`。去掉回边后图必须无环，每条回边必须真的闭合一个环，并且带轮数上限，所以任何图都不会无限运行。

## 图

`GraphBuilder` 收集节点和边，`compile(protocols)` 检查整体并返回只读的 `Graph`。图的 `digest` 是其定义的 SHA-256，定义相同则摘要相同。改图是从原图得到一张新图：

```python
new = GraphBuilder.of(graph).remove("review").add(check).connect("draft.y", "check.x", "value").compile(DEFAULT)
```

## 方法

方法接受节点，返回一张图。运行规则已经在图里：每个节点何时触发，每条回边最多几轮。

| 方法 | 结构 |
|---|---|
| `Chain(a, b, c)` | a → b → c |
| `Loop(a, b, until=f, max_rounds=N)` | 链的末端回到开头，直到 `f` 成立或轮数用完 |
| `Fan(a, b, c)` | 同一输入给各分支，结果汇合 |
| `Tree(lead, a, Tree(b, b1, b2), max_rounds=N)` | 协调者把任务交给下级，下级的回复回到协调者，可以多层 |
| `Star(hub, a, b, c, max_rounds=N)` | 一层的树 |
| `Governed(lead, a, b, operator=person, max_rounds=N)` | 树，加上协调者放行前向操作者节点请求同意 |

自己的方法继承 `Method`，实现 `build(protocols)`。

## 引擎

`Engine(graph, protocols, workers=0, journal=None, transport=None).run(inputs)` 返回 `Result`：每个图输出收到的全部内容、各节点运行次数、达到上限的回边、是否做完。

调度是事件驱动的。每条边有自己的信箱，每个节点记着还缺几个必需输入；一个信封到达只改动一个节点的计数，所以每条消息的调度开销与图的大小无关（实测 10 到 10,000 个节点都是每个信封约 9 微秒）。`workers=0` 在调用线程里按固定顺序运行；更多工作线程时节点并行运行，同一个节点不会同时运行两次。

`journal=Journal(path)` 让运行可恢复：每次完成的激活整条写入日志，新引擎用同一个日志 `resume()` 接着运行。做完的不重做，被打断的重跑。`resume(change=True)` 让运行在另一张图下继续。

`transport` 是发送和接收之间的接口，进程内不需要它；跨进程或跨机器的传递方式放在这里。

## 把图写成数据

`indeterminate.spec` 把图写成 JSON，再从 JSON 建回同一张图：

```python
from indeterminate.spec import Catalog, dump, load, read, write

write("graph.json", graph)                                   # 图的定义，加上嵌套子图的定义
again = read("graph.json", Catalog(allow=["mypkg"]))         # again.digest == graph.digest
```

写出来的就是图自己的描述（摘要正是由它算出的）：每个节点的种类、端口、配置，每条边，图的输入输出。函数用带模块的名字指明（`mypkg.steps.clean`），所以必须是模块顶层的函数，`lambda` 写不出去。模型用名字指明。

描述里只有名字，建图时要把名字变成真东西，这等于让描述决定运行哪些代码。`Catalog` 是宿主划定范围的地方，默认什么都够不到：

| 参数 | 含义 |
|---|---|
| `allow` | 函数可以来自哪些模块，`"mypkg"` 包括它下面的所有模块 |
| `processes` | 是否允许 `process` 节点，它会在本机启动程序 |
| `models` | `models(name)` 返回智能体用的模型；不给就建不了智能体 |
| `ask` | `ask(node_id, round, request)` 替图里没有自带函数的 `human` 节点回答 |
| `protocols` | 编译图用的协议表 |

`catalog.register(kind, loader)` 加入自己的节点种类。

手写时可以更短：节点的设置直接写在 `id`、`kind` 旁边，端口只写名字，边只写 `from` 和 `to`；也可以不写节点和边，直接指定方法：

```json
{"method": "loop", "max_rounds": 4, "until": "mypkg.checks.good_enough", "nodes": [
  {"id": "draft", "kind": "agent", "model": "anthropic:some-model", "system": "..."},
  {"id": "tidy", "kind": "function", "function": "mypkg.steps.tidy", "inputs": ["x"], "outputs": ["y"]}
]}
```

## 命令行和运行目录

安装后有命令 `1ndeterminate`（也可以用 `python -m indeterminate.cli`）。每个命令都输出 JSON。

| 命令 | 作用 |
|---|---|
| `check graph.json` | 只建图不运行，输出摘要、节点、输入输出；不需要密钥 |
| `run graph.json --dir RUN` | 在新目录里开始一次运行；`--input 名=值`（值按 JSON 读，读不成就当文字）或 `--inputs 文件` |
| `resume --dir RUN` | 从日志接着跑：等人回答之后、失败修好之后、被中断之后都用它 |
| `answer --dir RUN [问题] --json 值` 或 `--text 文字` | 回答图里的人被问到的问题；只有一个待答问题时不用写问题的名字 |
| `status --dir RUN` | 输出运行的状态和待答的问题 |

`check` 和 `run` 用 `--allow 模块`（可以重复）和 `--processes` 划定图能够到的范围，和 `Catalog` 的含义相同；不给就什么都够不到。范围在 `run` 时写进运行目录，`resume` 沿用它。`--workers N` 是并行运行节点的线程数。

`run` 和 `resume` 的退出码说明运行停在哪里：0 完成，1 失败或被拒绝，3 在等人回答，4 因为轮数或停止条件而停下。

一次运行的全部内容都在它的目录里，所以可以由一个进程开始、另一个进程回答、第三个进程继续：

```
graph.json      图的定义
options.json    图能够到的范围和线程数
journal.jsonl   已经发生的事
questions/      图里的人被问到的问题，一个问题一个文件，名字是 <节点>-<轮次>
answers/        已经给出的回答
state.json      运行的状态：done、waiting、stopped、failed
```

图里没有自带函数的 `human` 节点由运行目录代答：回答还没有时，运行停在 `waiting`，进程退出，不会有进程挂着等人；`answer` 写下回答，`resume` 继续，已经完成的节点不会重跑。

模型的名字写成 `提供方:模型`，密钥只从环境变量读取，不写进图文件、运行目录或日志：`anthropic:` 用 `ANTHROPIC_API_KEY`（可选 `ANTHROPIC_BASE_URL`），`openai:` 用 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`（任何兼容接口），`fake:echo` 不调用任何服务，原样返回收到的话，用来试图的接法。

在 Python 里用 `indeterminate.runs.Run` 做同样的事：`Run.create(目录, 描述, allow=...)`、`start(inputs)`、`resume()`、`questions()`、`answer(值)`、`status()`。

## 示例

[examples/data/](../examples/data/) 是一张写成 JSON 的图和它用到的函数，用命令行运行：起草、等编辑同意、发布。


[examples/graphs/](../examples/graphs/) 下有四个可以直接运行的示例：链里接一个外部程序、写作和评审的循环、需要人同意的树、相机到机械臂。

## 限制

- 恢复是“至少一次”：被打断的节点会重跑，它对图外做过的事会再做一次。
- 流和调用还不经过传输接口；目前只有进程内的实现。
- 设备节点只用假设备测试过，智能体节点只用脚本化的假模型测试过。
- 一个运行目录同一时间只能由一个进程运行或继续，目前没有加锁。
