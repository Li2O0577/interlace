# 1ndeterminate

通用多智能体编排内核：操作者确认的计划图、确定性调度、节点文件边界、审批、可恢复状态和审计日志。内核只约束边界（谁能启动、读、写、通信、放行），不约束模型如何推理。只依赖 Python 标准库，外部程序只用 git。

建模工作台（`mch` 包，在另一个仓库）是建在这个内核上的一个应用；内核本身不含网页、比赛预设或任何应用依赖。

## 安装

```sh
pip install -e .            # 在源码目录安装（尚未发布到公共索引）
uv build -o dist            # 构建 wheel
```

需要 Python 3.11 及以上。发布名和命令是 `1ndeterminate`，导入名是 `indeterminate`（Python 模块名不能以数字开头）。

## 分层

每层只引用下面的层，`tests/test_layers.py` 检查这条规则。

| 层 | 内容 | 主要名称 |
|---|---|---|
| 4 接口 | 嵌入式内核、操作者动作、扩展注册、stdio MCP 服务 | `Kernel`、`Operator`、`Extension`、`TaskType`、`Contract` |
| 3 编排 | 调度、轮次、审批、放行、改图、恢复；智能体运行时 | `graph.*`、`agents.runtime` |
| 2 实体与协议 | 计划实体；智能体类；节点协议 | `Plan`、`Block`、`Node`、`Edge`；`Agent` 及子类；`NodeKind` |
| 1 基础 | 事件、文件与锁、沙箱、宿主适配、模型提供方 | `core.*`、`HostAdapters`、`Provider` |

智能体类层次：

```text
Agent
├─ Coordinator          可以派生和跟进下属
│  ├─ Lead              项目主控：放行任务块、申请改图、批准助手
│  └─ SubLead           协调一个任务块，不能放行
├─ Worker               在自己的工作目录完成工作，可运行代码
│  └─ Coder、Analyst、Researcher、Writer
├─ Reviewer             检查交付并写出意见，不运行代码
└─ Keeper               图外只读，只决定额外文件申请
```

类只声明能力和边界（默认工具、必需工具、是否协调、是否主控等），不规定模型怎么思考。运行时和图引擎通过询问能力做判断，不比较角色名。主控和文件处理两种能力由内核保留，扩展注册的子类不能声明。

节点协议：调度器决定节点何时可以运行，并为它预留一个轮次；节点种类决定运行什么（`prepare`、`activate`、`rework`、`stop`）。内核只内置 `agent` 一种（模型实例）。宿主可以继承 `NodeKind` 注册其他种类，计划用节点的可选字段 `kind` 指定；不是模型的运行者通过 `GraphRuntime.deliver` 交付，与模型节点经过同样的状态与提交检查。交付从不放行任务块，放行只由主控做。本引擎未注册的种类不会启动。

## 最小用法

```python
from indeterminate import Kernel, Extension, TaskType, Worker

ext = Extension("translation")

class Translator(Worker):
    role = "translator"
    title = "翻译"
    tools = ("fs.list", "fs.read", "fs.write", "fs.edit")

ext.kinds.append(Translator)
ext.task_types.append(TaskType("translate", build_plan=my_plan_builder))

with Kernel("./workspace", "./settings", extensions=[ext]) as kernel:
    created = kernel.create_project("说明书翻译", "把 material/ 译成英文", task_type="translate", request_id="t-1")
    # 操作者核对 created["plan_hash"] 后确认；内核不会自动确认
    kernel.operator.confirm(created["project"]["id"], created["plan_hash"])
```

完整可运行示例见 [examples/harness_extension/](examples/harness_extension/)：自定义智能体类、带路径契约的工具和任务类型，只引用本包。

扩展工具只有声明了读写哪些路径参数（`reads=`、`writes=`）才会出现在图节点里，节点边界按声明检查这些路径。这是路径检查，不是操作系统级沙箱：处理函数在声明之外做的事由宿主负责。

## stdio MCP 服务

```sh
1ndeterminate --workspace /abs/workspace --home /abs/settings serve           # 真实提供方
1ndeterminate --workspace /abs/demo --home /abs/demo-home serve --demo         # 模拟，不调用模型
```

对外 8 个工具：`project.create`、`project.list`、`project.open`、`graph.view`、`operation.request`、`events.read`、`artifact.list`、`artifact.read`。确认计划、批准改图、处理工具审批只能由操作者在另一终端用 `1ndeterminate confirm/approve-change/decide-tool/...` 完成，外部连接拿不到主控身份。协议与命令细节见 [docs/MCP.md](docs/MCP.md)，图引擎设计见 [docs/GRAPH.md](docs/GRAPH.md)。

## 测试

```sh
python -m pip install -e . pytest
python -m pytest -q tests
```

测试只用脚本化的假模型（`FakeProvider`），不调用真实模型。`tests/test_layers.py` 检查分层引用规则。

## 限制

- 文件边界是程序层面的路径与权限检查，不是操作系统隔离。
- 操作者队列在工作区里，同等磁盘权限的程序也能写；尚无独立的操作者身份认证。
- 测试只用脚本化的假模型；尚未用真实模型做过完整运行。
