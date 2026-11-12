# stdio MCP 服务

`1ndeterminate` 自带一个 stdio MCP 服务，外部客户端通过它创建和查看图项目。内核的分层、类层次和节点协议见 [架构说明](ARCHITECTURE.md)，可运行示例在 `examples/extension/`。

本页描述当前实现的默认做法：进程内 stdio、只在本地命令行审批、一个工作区一个引擎。这些都可以替换。

## 启动与配置

在源码目录可直接运行：

```sh
python -m pip install -e .
1ndeterminate --workspace ./workspace --home ./.indeterminate serve
# 或先构建 wheel：uv build -o dist，再在干净环境安装
```

这是 MCP stdio 端点，应由兼容客户端启动并连接 stdin/stdout。外部连接不会成为主控。诊断只写 stderr。当前实现 MCP **2025-11-25** 的初始化、ping、tools/list、tools/call、stdio 换行 JSON；没有实现 2026-07-28 新生命周期，也没有 resources/prompts/elicitation/HTTP 外部传输。[初始化约定](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)、[stdio 约定](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)、[工具协议](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。

客户端需要的启动参数如下；各客户端的配置文件位置和外层字段不同，下面是启动描述，不声称已经验证某个客户端的专用配置格式：

```json
{
  "command": "/absolute/path/to/venv/bin/1ndeterminate",
  "args": ["--workspace", "/absolute/path/to/workspace", "--home", "/absolute/path/to/settings", "serve"]
}
```

Windows 使用虚拟环境的 `Scripts/1ndeterminate.exe`。本服务不会自动写宿主的 MCP 配置。

`--home` 默认 `INDETERMINATE_HOME` 或 `~/.indeterminate`；`--workspace` 默认 `INDETERMINATE_WORKSPACE` 或该 home 下的 `workspace/`。操作系统文件锁保证同一工作区同时只有一个引擎；它不是多进程协作数据库。

真实运行读取设置目录下 `settings.json` 中的 `providers` 和 `defaults.agent_model`。例如（占位值须自行替换）：

```json
{
  "providers": [{"id": "api", "name": "My provider", "type": "openai_compat", "base_url": "https://YOUR-SERVICE/v1", "api_key": "YOUR-KEY", "models": ["YOUR-MODEL"]}],
  "defaults": {"agent_model": "api:YOUR-MODEL"}
}
```

可用类型为 `openai_compat`、`anthropic`、`claude_code`、`codex`。命令行类型需要本机已配置好相应命令；引擎为它们启动一个只监听 127.0.0.1 随机端口的桥接，每轮使用一次性 token 调用引擎的工具。它不是对外的 MCP HTTP 服务。密钥只写在设置目录，不写进项目。尚未用真实模型做过完整运行。

## 外部 MCP 工具

| 工具 | 最小参数 | 行为 |
|---|---|---|
| `project.create` | `request_id,name,task`；可选 `automatic_files` | 创建带主控、执行、检查和整理节点的两块草稿；不启动 |
| `project.list` | 无 | 当前模式的图项目 |
| `project.open` | `project` | 接回既有项目，不替换内部 lead |
| `graph.view` | `project` | 计划、`plan_hash`、实例/块轮次、提交、请求与批准记录 |
| `operation.request` | `project,request_id,instruction`；可选 `proposal` 对象 | 把外部意图作为数据交内部 lead，不伪装为操作者批准 |
| `events.read` | `project`；可选 `after=0,limit=100`（最大 500） | 按 seq 升序返回，`cursor` 用于继续读取 |
| `artifact.list` | `project` | 当前有效、摘要一致的正式交付清单 |
| `artifact.read` | `project,path` | 仅该清单中的 UTF-8 文件，最多 512 KiB |

没有通用工具执行器，没有对外的 `graph.confirm/approve/release` 或 `approval.resolve`。申请返工、结束、唤醒、改图等意图统一用 `operation.request`；内部主控判断后调用自己的受控工具。`proposal` 不会自动写入计划。二进制/大文件留给操作者本地查看。

图定义支持每节点 `model` 与 `thinking`（default/low/medium/high），任务块支持 `coordinator` 指向图内次主控。内部协调者获得 `graph.block_status` 和 `graph.request_review`，外部 MCP 工具清单仍为上表 8 个；不会因外部连接而取得协调者或总主控身份。计划图的数据结构见 [架构说明](ARCHITECTURE.md)。

参数长度上限写在工具的 `inputSchema` 里：`name` 200 字符，`task` 与 `instruction` 50000 字符，`request_id` 256 字符，`project` 128 字符，`path` 1024 字符，`proposal` 序列化后 256 KiB；含无法编码字符（孤立代理项）的参数直接拒绝。一行消息最多 4 MiB。超限、非 UTF-8、无法解析或嵌套过深的消息都只得到 JSON-RPC 错误，连接保持。

`request_id` 是调用方提供的稳定 ID（最长 256 字符）。同 ID、同参数重试返回已持久化结果；同 ID 不同参数拒绝。执行前预留、返回前记录结果；中断时尚无确定结果的预留拒绝自动重放，先检查现有项目与 `.engine-requests/` 记录。此机制防止自动重试重复创建，**不承诺跨文件事务、模型消息恰好执行一次或在外部命令崩溃后自动回滚**。

## 操作者确认和审批

另开终端，使用与服务相同的 `--workspace`。以下命令不暴露为 MCP 工具：

```sh
1ndeterminate --workspace /absolute/workspace pending
1ndeterminate --workspace /absolute/workspace confirm PROJECT PLAN_SHA256
1ndeterminate --workspace /absolute/workspace approve-change PROJECT CHANGE_ID NEW_PLAN_SHA256
1ndeterminate --workspace /absolute/workspace decide-tool PROJECT APPROVAL_ID approve --reason "已核对"
1ndeterminate --workspace /absolute/workspace decide-tool PROJECT APPROVAL_ID deny --reason "请改方案"
1ndeterminate --workspace /absolute/workspace message PROJECT "操作者对主控问题的回答"
1ndeterminate --workspace /absolute/workspace resume PROJECT AGENT_ID --reason "暂停原因已解决"
```

`pending` 展示草稿或改图的前后定义、原因、受影响节点和成果处理，并列出工具审批。确认必须匹配当前 SHA256。写命令返回本地记录 `id`、`status=pending`；随后用 `result ID` 查看 `done/failed/uncertain`，**入队不等于批准已经生效**。服务未运行时保持待处理。`message` 只回复内部 lead；`resume` 只恢复已有暂停会话，不能复活已结束图实例。

自动模式（当前映射 autonomy=full）的一般工具操作自动执行，不需要操作者逐次用 CLI 处理。助手批次由内部总主控批准；额外文件由只读 keeper 独立决定。初始计划确认和改图批准仍由操作者完成，安装软件等事项仍须操作者逐次决定。块协调者申请验收不等于最终放行。

改图批准后仍由内部 lead 应用；块结束仍须 lead 的独立 `graph.release(block,round,note)`。操作者 CLI 也没有“代替 lead 放行”命令。遇到中断的本地操作标记 uncertain，不自动重试。记录最小格式：

```json
{"schema":1,"id":"24位十六进制ID","command":"confirm","args":{"project":"PROJECT","hash":"SHA256"},"status":"pending","created":0}
```

结果在同一记录增加 `result` 或 `error`。无法读取的队列记录不会阻止引擎启动：原文件改名为 `*.json.corrupt` 保留，原位置写入 `failed` 记录。队列位于工作区 `.engine-control/`，不在节点目录。拥有同等 shell/磁盘权限的程序仍可能写此队列；这不是技术上证明“只有人能批准”的身份隔离。独立操作者认证尚未接入。目录/prompt/代码扫描也不提供 OS 级文件隔离。

## 一次完整模拟演示

使用独立目录，不与真实提供器项目混用：

```sh
1ndeterminate --workspace ./demo/workspace --home ./demo/home serve --demo
```

通过 MCP 客户端按协议初始化后：

1. `project.create({"request_id":"demo-create-1","name":"资料审查","task":"整理来源并记录限制"})`，取得项目 ID。
2. `graph.view({"project":"ID"})` 核对草稿及 `plan_hash`。此时没有工作实例。
3. 操作者在另一终端执行 `confirm ID SHA256`，用返回的队列 ID 查询 `result`。
4. 轮询 `graph.view` 或 `events.read`。执行节点和检查员依次提交；整理块仍不启动。
5. `operation.request({"project":"ID","request_id":"demo-release-1","instruction":"DEMO_REVIEW_AND_RELEASE work"})`。仅 `--demo` 下的确定性**模拟内部主控**明确调用真实 `graph.release`；没有绕过核心门禁，不代表模型质量检查。
6. 整理节点提交后用 `artifact.list/read` 查看带 `SIMULATION` 的成果。
7. 关闭 stdin，再用相同目录重连；同 ID 请求返回原结果，不新建项目或同轮节点。

这是 FakeProvider 加真实 AgentManager 线程和文件工具的模拟，不是真实多模型执行。`runtime_mode=simulation`、成果和放行注记均作标记。模拟模式与真实提供器模式互相拒绝接管同一工作区，演示工作区应保持独立。模拟策略只验证这条标准演示流程，不提供任意任务推理、审查判断或自动返工策略。

可直接复跑验证：

```sh
python -m pytest -q tests
uv build -o dist
# 在只装了该 wheel 的干净环境中：
python scripts/kernel_package_smoke.py --example examples/extension
```

## 生命周期与恢复

stdin EOF/宿主关闭会停止本进程内工作，不是后台常驻服务。已发出的 API 请求可能等返回，外部脱离进程的程序不保证随之停止。项目/会话/审批落盘；新连接按引擎的恢复策略续接，丢失的工具结果标记中断，不假定旧模型还存活。已由主控明确结束的图实例不复用旧会话，需要 `graph.wake` 新轮次，并核查 state 与现行授权。进程被强制终止（SIGKILL）后用同一工作区重启即可续接，已启动的轮次不会重复启动。

某个项目的状态文件损坏（`state/graph.json`、`project.json`、`state/engine-mode.json`）时只停止该项目，并给出损坏文件名；`project.list` 仍列出它并带 `error`。`request_id` 的记录损坏时拒绝重放，需要人工核对。

正式文件更新、已结束节点重启、获批局部改图的最小处理保留旧历史并阻止下游新启动，待 human/lead 复核；这一处理尚待真实任务验证。自动文件模式暂映射 `autonomy=full`，只读授权沿用 `graph.runtime.DEFAULT_AUTH`；非自动模式不创建 keeper。keeper 按具体文件决策，由程序记录，不进图。

CI 构建 wheel，在只装该 wheel 的干净环境中运行 stdio 服务和第三方示例。运行数据写到使用者的 home 和 workspace，不写入安装目录。
