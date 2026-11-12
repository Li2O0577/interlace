# 1ndeterminate

通用的多智能体编排内核。操作者确认一张计划图，内核按图确定性地调度节点，检查每个节点的文件边界，处理审批和放行，保存可恢复的状态和审计日志。

内核只约束边界：谁能启动、读、写、通信、放行。它不规定模型怎么推理，也不包含任何具体任务的方法。只依赖 Python 标准库，唯一用到的外部程序是 git。

## 安装

```sh
pip install -e .            # 在源码目录安装（尚未发布到公共索引）
uv build -o dist            # 构建 wheel
```

需要 Python 3.11 及以上。发布名和命令是 `1ndeterminate`，导入名是 `indeterminate`（Python 模块名不能以数字开头）。

## 组成

| 层 | 内容 | 主要名称 |
|---|---|---|
| 4 接口 | 嵌入式内核、操作者动作、扩展注册、stdio MCP 服务 | `Kernel`、`Operator`、`Extension`、`TaskType`、`Contract` |
| 3 编排 | 调度、轮次、审批、放行、改图、恢复；智能体运行时 | `graph.*`、`agents.runtime` |
| 2 实体与协议 | 计划图实体；智能体类；节点协议 | `Plan`、`Block`、`Node`、`Edge`；`Agent` 及子类；`NodeKind` |
| 1 基础 | 事件、文件与锁、沙箱、宿主适配、模型提供方 | `core.*`、`HostAdapters`、`Provider` |

每层只引用下面的层，`tests/test_layers.py` 检查这条规则。类层次、计划图的数据结构和节点协议见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 节点、边、方法

仓库里正在建立新的一层：通用的节点、边（传递协议）、方法（连接结构）和运行它们的引擎。节点可以是模型、函数、外部程序、设备、人或另一张图。说明见 [docs/GRAPH.md](docs/GRAPH.md)，示例见 [examples/graphs/](examples/graphs/)。下文描述的是在它之前的那一层，两者目前并存。

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

完整可运行示例见 [examples/extension/](examples/extension/)：自定义智能体类、带路径契约的工具和任务类型，只引用本包。

扩展工具只有声明了读写哪些路径参数（`reads=`、`writes=`）才会出现在图节点里，节点边界按声明检查这些路径。这是路径检查，不是操作系统级沙箱：处理函数在声明之外做的事由宿主负责。

## stdio MCP 服务

```sh
1ndeterminate --workspace /abs/workspace --home /abs/settings serve           # 真实提供方
1ndeterminate --workspace /abs/demo --home /abs/demo-home serve --demo         # 模拟，不调用模型
```

对外 8 个工具：`project.create`、`project.list`、`project.open`、`graph.view`、`operation.request`、`events.read`、`artifact.list`、`artifact.read`。确认计划、批准改图、处理工具审批只能由操作者在另一终端用 `1ndeterminate confirm/approve-change/decide-tool/...` 完成，外部连接拿不到主控身份。细节见 [docs/MCP.md](docs/MCP.md)。

设置目录默认 `~/.indeterminate`（环境变量 `INDETERMINATE_HOME`），工作区默认其下的 `workspace/`（`INDETERMINATE_WORKSPACE`）。

## 测试

```sh
python -m pip install -e . pytest
python -m pytest -q tests
```

测试只用脚本化的假模型（`FakeProvider`），不调用真实模型。

## 限制

- 文件边界是程序层面的路径与权限检查，不是操作系统隔离。
- 操作者队列在工作区里，同等磁盘权限的程序也能写；尚无独立的操作者身份认证。
- 尚未用真实模型做过完整运行。

## 许可

MIT
