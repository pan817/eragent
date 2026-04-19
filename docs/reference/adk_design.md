# Google ADK (Python) 架构设计文档

> 基于仓库 [google/adk-python](https://github.com/google/adk-python) 源码整理，面向希望理解 ADK 内部实现、进行二次开发或深度排障的工程师。
>
> 本文所有代码引用均给出 `文件:行号` 以便按图索骥。引用的代码片段为便于阅读做了最小量删节，以 `…` 标出省略之处。完整实现以仓库当前 `main` 分支为准。

---

## 目录

1. [概述与设计哲学](#1-概述与设计哲学)
2. [整体分层架构](#2-整体分层架构)
3. [Runner：无状态编排引擎](#3-runner无状态编排引擎)
4. [Agent 体系](#4-agent-体系)
5. [InvocationContext 与 Event](#5-invocationcontext-与-event)
6. [LLM Flow：交互机器](#6-llm-flow交互机器)
7. [Models 抽象层](#7-models-抽象层)
8. [工具体系](#8-工具体系)
9. [服务层：Session / Memory / Artifact](#9-服务层session--memory--artifact)
10. [Auth 子系统](#10-auth-子系统)
11. [Plugin 与 App](#11-plugin-与-app)
12. [CodeExecutor](#12-codeexecutor)
13. [CLI 与 FastAPI 表层](#13-cli-与-fastapi-表层)
14. [Evaluation 与 A2A](#14-evaluation-与-a2a)
15. [核心设计取舍与扩展点](#15-核心设计取舍与扩展点)

---

## 1. 概述与设计哲学

### 1.1 ADK 是什么

Agent Development Kit（ADK）是 Google 开源的 **code-first** Python 智能体框架，用于构建、评估和部署基于 LLM 的 agent 应用。核心定位可以概括为三句话：

- **面向工程**：agent、tool、orchestration 全部用 Python 代码定义，可版本化、可测试、可 IDE 重构，避免"拖拽式"逻辑固化到配置。
- **模型无关、部署无关**：围绕 Gemini 做最好适配，但通过 `BaseLlm` 抽象同时支持 Anthropic、LiteLLM（100+ 供应商）、本地 Gemma；一套 `agent.py` 既能本地跑、又能装进 FastAPI、又能部署到 Vertex AI Agent Engine / Cloud Run / GKE。
- **可组合**：LoopAgent / ParallelAgent / SequentialAgent 等编排器把"单个 agent"复合为"多 agent 系统"，每一层都可以拥有自己的工具、模型、记忆与生命周期回调。

ADK 的编程心智是："agent = 一段会在 reason-act 循环中被 Runner 反复唤起的配置"，并不是"agent = 一个持久运行的服务"。这一点决定了整个框架的状态管理策略：**核心组件无状态，状态通过服务层持久化**。

### 1.2 三条设计原则

这些原则在 [`contributing/adk_project_overview_and_architecture.md`](../contributing/adk_project_overview_and_architecture.md) 和 [`AGENTS.md`](../AGENTS.md) 中被多次强调，也贯穿了源码设计：

1. **Code-First**
   - 一切行为从 Python 代码推断；Agent Config（YAML）是从 Python 类派生的 schema，不是另一条平行通路。
   - 结果：IDE 跳转、单元测试、mypy 检查、git diff 等传统工程手段全部自然生效。

2. **Modularity & Composition**
   - `BaseAgent` 是一切 agent 的抽象根；`LlmAgent` 只是其中一种"会调用 LLM 的 agent"。
   - `LoopAgent` / `ParallelAgent` / `SequentialAgent` 是 `BaseAgent` 的另一类子类——它们不调用 LLM，而是**编排子 agent 执行**。这意味着"编排"和"LLM 调用"在同一抽象下可以任意嵌套。
   - `BaseTool` / `BaseToolset` / `BaseLlm` / `BaseCodeExecutor` / `BasePlugin` / `BaseSessionService` / `BaseMemoryService` / `BaseArtifactService` 都是并列的扩展点，每一个都可独立替换实现。

3. **Deployment-Agnostic**
   - 核心逻辑（agent、runner、service）不感知运行环境。
   - `get_fast_api_app(agent_dir=...)` 把 agent 目录包装为 FastAPI 应用；`adk deploy` 则把同样的目录打包到 Cloud Run / Agent Engine / GKE——两条路径共享 agent 代码。

### 1.3 核心词汇表

下表是贯穿全文的术语。第一次看到时可能觉得抽象，后面章节会逐个展开。

| 术语 | 对应代码 | 一句话说明 |
|---|---|---|
| **Agent** | [`src/google/adk/agents/base_agent.py`](../src/google/adk/agents/base_agent.py) | 声明式蓝图。定义一个 agent 的身份、指令、工具与子 agent。自身不携带运行状态。 |
| **LlmAgent** | [`src/google/adk/agents/llm_agent.py`](../src/google/adk/agents/llm_agent.py) | 最常用的 agent 类型；`Agent` 是它的别名。内部持有一个 `_llm_flow`，真正驱动 LLM 调用。 |
| **Tool** | [`src/google/adk/tools/base_tool.py`](../src/google/adk/tools/base_tool.py) | agent 可调用的能力单元。普通 Python 函数、OpenAPI 接口、MCP 服务器、甚至另一个 agent 都可以变成 tool。 |
| **Runner** | [`src/google/adk/runners.py`](../src/google/adk/runners.py) | 无状态编排引擎。接收一条新消息，驱动 agent 跑完一次 invocation，把产生的 `Event` 流回调用方并持久化到 session。 |
| **Session** | [`src/google/adk/sessions/`](../src/google/adk/sessions/) | 单次多轮对话的容器：`events`（事件流）+ `state`（键值状态）。 |
| **Memory** | [`src/google/adk/memory/`](../src/google/adk/memory/) | 跨 session 的长期记忆，通常由 session 摘要化后写入，可被搜索。 |
| **Artifact** | [`src/google/adk/artifacts/`](../src/google/adk/artifacts/) | 非文本的"文件级"内容，如音频、图片、PDF；版本化存储。 |
| **Event** | [`src/google/adk/events/event.py`](../src/google/adk/events/event.py) | agent 运行过程中产生的原子单元：一次 LLM 回复、一次 tool call、一次 tool response 都是一个 Event。Session 的 `events` 就是它们的有序列表。 |
| **Flow** | [`src/google/adk/flows/llm_flows/`](../src/google/adk/flows/llm_flows/) | LlmAgent 内部的"LLM 交互机器"。包含预处理、调用、后处理、工具派发等逻辑。`SingleFlow` 和 `AutoFlow` 是两种实现。 |
| **Plugin** | [`src/google/adk/plugins/base_plugin.py`](../src/google/adk/plugins/base_plugin.py) | 应用级回调集合。拦截 before/after agent/model/tool 等关键点，适合日志、审计、策略。 |
| **App** | [`src/google/adk/apps/app.py`](../src/google/adk/apps/app.py) | `root_agent` 之上的容器，承载应用级配置：plugins、event compaction、context cache、resumability。 |
| **InvocationContext** | [`src/google/adk/agents/invocation_context.py`](../src/google/adk/agents/invocation_context.py) | 一次 invocation 的运行时"手提箱"：当前 agent、session 引用、服务句柄、分支信息、agent 状态快照等。 |

### 1.4 一次请求的生命周期（鸟瞰）

为了让后文的细节有一个锚点，先给出端到端流程的最短版本——**一条用户消息进入 ADK 之后发生了什么**：

1. 调用方通过 `Runner.run_async(user_id, session_id, new_message=...)` 发起一次 invocation。
2. Runner 从 `SessionService` 拉取 session；构建 `InvocationContext`；创建 plugin 的 before-run 钩子。
3. Runner 调用 `root_agent.run_async(ctx)`。对 `LlmAgent` 而言，这会委托给 `_llm_flow.run_async(ctx)`。
4. `BaseLlmFlow` 进入"一次 step"循环：
   - **preprocess**：跑完 request processors（instruction 注入、contents 组装、cache 配置等），装配好 `LlmRequest`。
   - **call_llm**：通过 `BaseLlm` 发起调用，流式或一次性返回 `LlmResponse`。
   - **postprocess**：跑完 response processors（NL planning、code execution 等），生成 agent 的 `Event`。
   - **tool dispatch**：若响应里含 function call，则并发调用对应的 `BaseTool`；把 function response 作为 Event 回填到 session。
   - 如果本轮产出的是"最终回复"则跳出循环；否则用新历史再起一轮。
5. 每个 Event 被 plugin 回调包裹一次，然后通过 `SessionService.append_event` 落盘，并 yield 回调用方。
6. invocation 结束时，Runner 决定是否触发 event compaction（按 invocation 间隔 / token 阈值）。

后续章节会把这 6 步逐层展开。

---

## 2. 整体分层架构

### 2.1 分层图

ADK 的代码按"离用户输入越远、越接近基础设施"的顺序分层，依赖方向严格**自上而下**，上层可以直接调用下层，反向调用禁止（通过 Python 的包组织保证）。

```
┌────────────────────────────────────────────────────────────────────┐
│  用户入口层  (CLI / FastAPI / 调用方 Python 脚本)                  │
│  src/google/adk/cli/*                                              │
│  - adk_web_server.py  (adk web 后端)                               │
│  - cli_tools_click.py (adk run / adk api_server / adk eval ...)    │
│  - fast_api.py        (get_fast_api_app — 把 agent 包装为 HTTP)   │
└──────────────┬─────────────────────────────────────────────────────┘
               │
┌──────────────▼─────────────────────────────────────────────────────┐
│  应用容器层  (App / ResumabilityConfig / EventsCompactionConfig)   │
│  src/google/adk/apps/app.py                                        │
│  - 提供 plugins、event compaction、context cache 等应用级配置      │
└──────────────┬─────────────────────────────────────────────────────┘
               │
┌──────────────▼─────────────────────────────────────────────────────┐
│  编排层  (Runner)                                                   │
│  src/google/adk/runners.py                                          │
│  - run / run_async / run_live  — 单次 invocation 的生命周期管理    │
│  - _exec_with_plugin          — plugin 钩子的包裹                   │
│  - rewind_async                — 回滚到某次 invocation 之前         │
└──────────────┬─────────────────────────────────────────────────────┘
               │
┌──────────────▼─────────────────────────────────────────────────────┐
│  Agent 层                                                           │
│  src/google/adk/agents/                                             │
│  - base_agent.py      BaseAgent (抽象根)                            │
│  - llm_agent.py       LlmAgent  (Agent 别名，绑定 LLM + Tools)      │
│  - loop_agent.py      LoopAgent                                     │
│  - parallel_agent.py  ParallelAgent                                 │
│  - sequential_agent.py SequentialAgent                              │
│  - langgraph_agent.py / remote_a2a_agent.py (适配第三方/远程)       │
└───────┬──────────────────────────────────────┬──────────────────────┘
        │                                      │
        ▼                                      ▼
┌─────────────────────────────┐   ┌─────────────────────────────────┐
│  Flow 层 (仅 LlmAgent 使用) │   │  Tool 层                        │
│  src/google/adk/flows/      │   │  src/google/adk/tools/          │
│  llm_flows/                 │   │  - base_tool.py / base_toolset  │
│  - base_llm_flow.py         │   │  - function_tool.py             │
│  - single_flow.py           │   │  - agent_tool.py                │
│  - auto_flow.py             │   │  - mcp_tool/ openapi_tool/      │
│  - 各 processor 模块         │   │  - google_api_tool/ ...         │
└───────┬─────────────────────┘   └───────┬─────────────────────────┘
        │                                 │
        ▼                                 │
┌─────────────────────────────┐           │
│  Models 层                  │           │
│  src/google/adk/models/     │           │
│  - base_llm.py (抽象)       │           │
│  - google_llm.py (Gemini)   │           │
│  - anthropic_llm.py (Claude)│           │
│  - lite_llm.py (多供应商)   │           │
│  - registry.py              │           │
└─────────────────────────────┘           │
        │                                 │
        └─────────────┬───────────────────┘
                      ▼
┌────────────────────────────────────────────────────────────────────┐
│  服务层  (无业务逻辑，只负责持久化/检索/认证)                      │
│  src/google/adk/sessions/    - BaseSessionService (events + state) │
│  src/google/adk/memory/      - BaseMemoryService  (长期可检索记忆) │
│  src/google/adk/artifacts/   - BaseArtifactService (文件版本化)    │
│  src/google/adk/auth/        - Credential Service + 流程对象       │
│  src/google/adk/code_executors/ - BaseCodeExecutor                 │
│  src/google/adk/plugins/     - Plugin 注册与执行                   │
└────────────────────────────────────────────────────────────────────┘
```

### 2.2 包的公开导出

`src/google/adk/__init__.py` 只导出三个名字，这一决策本身就是设计信号：

```python
# src/google/adk/__init__.py
from .agents.context import Context
from .agents.llm_agent import Agent
from .runners import Runner

__all__ = ["Agent", "Context", "Runner"]
```

- **`Agent`**：`LlmAgent` 的别名，用户只需要这一个类就能写"单 agent + 工具"的最常见场景。
- **`Runner`**：把 agent 跑起来的唯一入口；所有调用方代码的起点。
- **`Context`**：即 `CallbackContext`，在 callback 和 tool 中读取/写入 session state。

其他所有类（`LoopAgent`、`SequentialAgent`、`ParallelAgent`、`App`、`BaseTool`、`BaseLlm`、`BasePlugin` 等）都要从子模块显式 import。这使得"简单场景的用户看到的 API 表面极小"，而"高级场景的用户按需深入"。

### 2.3 依赖方向与可替换性

分层的意义在于**每一层都是可替换的扩展点**。下表列出主要的扩展点及其替换方式：

| 扩展点 | 抽象基类 | 自定义方式 | 典型场景 |
|---|---|---|---|
| 模型 | `BaseLlm` | 继承 + 正则 `supported_models()` + `LLMRegistry._register` | 接入内部私有模型 |
| 工具 | `BaseTool` / `FunctionTool` | 继承 `BaseTool` 或直接 `FunctionTool(fn)` | 接内部 API |
| 工具集 | `BaseToolset` | 实现 `get_tools(readonly_context)` | 按环境/权限动态筛选工具 |
| 会话 | `BaseSessionService` | 实现 create/get/list/delete/append_event | 接入自研存储 |
| 记忆 | `BaseMemoryService` | 实现 add / search | 接入 Vector DB |
| 制品 | `BaseArtifactService` | 实现 save / load / list / delete | 接入自建对象存储 |
| 代码执行 | `BaseCodeExecutor` | 实现 `execute_code` | 企业沙箱环境 |
| 插件 | `BasePlugin` | 覆盖需要的回调方法 | 审计、限流、统一注入指令 |
| 编排 | `BaseAgent` | 继承并实现 `_run_async_impl` / `_run_live_impl` | 自定义工作流 agent |

**反向规则**：所有扩展点的实现**不应**反向 import 上层模块。例如自定义的 `BaseLlm` 子类不要 import `Runner`，自定义的 `BaseSessionService` 不要 import `LlmAgent`。这个规则在 AGENTS.md 中通过"相对 import、不从 `__init__.py` import"的风格约束间接强制。

### 2.4 两条并行的执行路径

ADK 其实存在**两套执行路径**，共享绝大部分组件但在流式层面分叉：

1. **文本路径（`run` / `run_async`）**：
   - LLM 调用走 `BaseLlm.generate_content_async(stream=True/False)`，返回若干 `LlmResponse`（SSE 式分块）。
   - 每一步 step = 一次 LLM 调用 + 可能的 tool 调用；step 之间串行。

2. **实时路径（`run_live`）**：
   - LLM 调用走 `BaseLlm.connect(llm_request) -> BaseLlmConnection`，建立双向 WebSocket。
   - 用户输入、模型输出、tool 调用都通过 `LiveRequestQueue` 以**异步消息**方式流转，支持用户中途插话（打断）、VAD、流式 tool。

两条路径在 `BaseAgent.run_async` / `BaseAgent.run_live` 处就分叉，分别调用 `_run_async_impl` / `_run_live_impl`。后者在 `LlmAgent` 中走向 `self._llm_flow.run_live(ctx)`（见 [§6.4](#64-live-模式双向流)）。

这也是为什么 `Event` 模型继承自 `LlmResponse`——文本路径和 live 路径都以"LLM 响应"为骨架，只是 live 模式上携带了更多字段（`turn_complete`、`interrupted`、`input_transcription`、`output_transcription` 等）。

---

## 3. Runner：无状态编排引擎

### 3.1 类定位与三种入口对比

`Runner`（[`runners.py:113`](../src/google/adk/runners.py)）是 ADK 的**唯一执行入口**。它本身**不持有对话状态**——所有对话状态通过 `SessionService` 读写。这使得同一个 `Runner` 实例可以被并发复用、可以跨线程使用、可以被框架按需重建。

**构造参数有两种写法**，推荐 `app` 形式：

```python
# src/google/adk/runners.py:151-197（精简）
def __init__(
    self, *,
    app: Optional[App] = None,                  # 推荐：统一承载 root_agent + plugins + cache + resumability
    app_name: Optional[str] = None,             # 若不用 app，则 app_name + agent 二选一
    agent: Optional[BaseAgent] = None,
    plugins: Optional[List[BasePlugin]] = None, # 已废弃，改由 App.plugins 提供
    artifact_service: Optional[BaseArtifactService] = None,
    session_service: BaseSessionService,        # 唯一必填服务
    memory_service: Optional[BaseMemoryService] = None,
    credential_service: Optional[BaseCredentialService] = None,
    plugin_close_timeout: float = 5.0,
    auto_create_session: bool = False,          # session 缺失时是否自动创建
):
```

关键规则：

- **`app` 与 `agent/plugins` 互斥**：提供了 `app` 后不能再传 `agent` 或 `plugins`，否则抛 `ValueError`（见 `_validate_runner_params` [`runners.py:257-275`](../src/google/adk/runners.py)）。
- **`session_service` 必填**：其他服务全部可选，缺省时内部会退化为 `InMemory*Service`。
- **`app_name` 可覆盖 `app.name`**：为 Agent Engine 等部署场景下资源名 ≠ 应用名的情况而设计。

Runner 的公共入口有三个，对应三种执行模式：

| 方法 | 形态 | 底层机制 | 典型调用方 | 能否中途插话 |
|---|---|---|---|---|
| `run(*, user_id, session_id, new_message, run_config=None)` | 同步生成器 `Generator[Event, None]` | 在后台线程里跑 `run_async`，通过 `queue.Queue` 回传 | 同步脚本、notebook 快速试验 | 否 |
| `run_async(*, user_id, session_id, invocation_id=None, new_message=None, state_delta=None, run_config=None)` | 异步生成器 `AsyncGenerator[Event, None]` | 本地实现，调用 `agent.run_async` | FastAPI、`adk run` CLI、生产调用方 | 否（但可恢复） |
| `run_live(*, live_request_queue, user_id, session_id, run_config=None)` | 异步生成器 `AsyncGenerator[Event, None]` | 调用 `agent.run_live`，用 `LiveRequestQueue` 解耦输入 | 音频/视频双向流、Web 前端 | **是**（通过 queue 发送实时输入） |

**`run` 是 `run_async` 的语法糖**——它的文档明确标注仅用于本地测试：

```python
# src/google/adk/runners.py:446-450
"""
NOTE:
  This sync interface is only for local testing and convenience purpose.
  Consider using `run_async` for production usage.
"""
```

**`run_live` 的独特签名：不接收 `new_message`**，而是接收一个预先创建好的 `LiveRequestQueue`。调用方通过 `live_request_queue.send_content(...)` / `send_realtime(...)` 持续喂入文本或音频，模型和 agent 通过同一 queue 的另一端接收。这种解耦允许：

- 用户说话时，agent 可以同时在"正在回复"——实现真正的**双向**对话；
- 流式工具（async generator）作为后台 task 持续产出，不会阻塞 LLM 响应；
- VAD（Voice Activity Detection）事件可以独立于主消息流传入。

下一节进入 `run_async` 的七步生命周期。

### 3.2 `run_async` 的七步生命周期

[`runners.py:503-633`](../src/google/adk/runners.py) 是整个框架最密集的 80 行代码。它定义了一次 invocation 从 HTTP 请求到持久化完成的全流程。拆成七步看：

```python
# src/google/adk/runners.py:542-633（精简）
async def _run_with_trace(new_message, invocation_id):
    with tracer.start_as_current_span('invocation'):                 # ① Telemetry
        session = await self._get_or_create_session(...)             # ② 拉取/创建 session

        if not invocation_id and not new_message:                    # ③ 参数校验
            raise ValueError(...)
        is_resumable = (self.resumability_config
                        and self.resumability_config.is_resumable)
        if not is_resumable and not new_message:
            raise ValueError(...)

        if not is_resumable:                                         # ④ 新 vs 恢复 分支
            invocation_context = await self._setup_context_for_new_invocation(...)
        else:
            invocation_id = self._resolve_invocation_id(...)
            if not invocation_id:
                invocation_context = await self._setup_context_for_new_invocation(...)
            else:
                invocation_context = await self._setup_context_for_resumed_invocation(...)
                if invocation_context.end_of_agents.get(invocation_context.agent.name):
                    return  # 当前 agent 已结束，无需再跑

        async def execute(ctx):                                      # ⑤ 真正的执行逻辑
            async with Aclosing(ctx.agent.run_async(ctx)) as agen:
                async for event in agen:
                    yield event

        async with Aclosing(self._exec_with_plugin(                  # ⑥ 包插件 + 持久化 + yield
                invocation_context=invocation_context,
                session=session,
                execute_fn=execute,
                is_live_call=False,
        )) as agen:
            async for event in agen:
                yield event

        if self.app and self.app.events_compaction_config:           # ⑦ invocation 末尾做 compaction
            await _run_compaction_for_sliding_window(
                self.app, session, self.session_service,
                skip_token_compaction=invocation_context.token_compaction_checked,
            )
```

逐步解读：

1. **Telemetry span**：通过 `tracer.start_as_current_span('invocation')` 开一个 OpenTelemetry span；后续所有 agent、LLM、tool 调用都挂在它的子 span 上，便于在 Cloud Trace / Jaeger 里看完整调用树。

2. **拉取 session**：`_get_or_create_session` 调用 `session_service.get_session`；若 `auto_create_session=True` 且不存在，就 `create_session`。`get_session_config` 来自 `run_config.get_session_config`，可控制只取最近 N 条事件或某时间戳之后的事件——这是**控制每次 invocation 加载开销**的关键。

3. **参数校验**：`run_async` 的参数设计很讲究——`new_message` 和 `invocation_id` 都是 `Optional`，但**必须二选一**。纯 `new_message`=新对话；纯 `invocation_id`=恢复某个被长运行工具中断的 invocation；两者都有=恢复模式下追加一条用户消息（高级用法）。若 app 未启用 `is_resumable`，则强制要求 `new_message`（无法从事件流恢复）。

4. **新 vs 恢复分支**：
   - `_setup_context_for_new_invocation`（[`runners.py:1319`](../src/google/adk/runners.py)）——把新消息 append 到 session、跑 `on_user_message_callback` 插件钩子、确定 `ctx.agent` 为 `_find_agent_to_run` 的结果。
   - `_setup_context_for_resumed_invocation`（[`runners.py:1357`](../src/google/adk/runners.py)）——从历史事件回放 agent 状态，`populate_invocation_agent_states` 把每个 agent 的 `agent_state` / `end_of_agent` 恢复到 `invocation_context.agent_states` / `end_of_agents`。
   - 如果恢复后发现当前 agent 已 `end_of_agent=True`，直接 `return`（调用方收到空流）。

5. **`execute` 闭包**：非常轻量——只是把 `ctx.agent.run_async(ctx)` 的异步生成器透传出来。重点是它被定义为函数对象传给 `_exec_with_plugin`，让**插件回调有机会早退**（step 6）。

6. **`_exec_with_plugin` 包裹**：这是事件持久化、插件钩子、顺序修正的集中地。下一节单独展开。

7. **末尾的 Event Compaction**：只有在 `app.events_compaction_config` 非空时才跑。关键约束——**compaction 只在 invocation 结束后执行，不会在中途打断事件流**。`token_compaction_checked` 标记防止一次 invocation 内被重复压缩（例如中途 LlmRequest 里已经检查过令牌超限，此处就跳过）。

**为什么 `run_async` 里有两层 `Aclosing`？** Python 的 async generator 如果没走完就被 GC，`aclose()` 不保证立即调用，可能泄漏 websocket / 线程池资源。`Aclosing` 是 `contextlib.aclosing` 的薄包装，保证异常或提前 break 时一定 cleanup。整个 ADK 流式代码都遵循这个模式。

### 3.3 `_exec_with_plugin`：三阶段包裹

[`runners.py:822-954`](../src/google/adk/runners.py) 的 `_exec_with_plugin` 是所有 invocation 都会走的"事件管道"。它的职责可以浓缩为三件事：**允许插件早退、包裹每个事件做持久化、在最终阶段执行清理**。

```python
# src/google/adk/runners.py:822-954（精简）
async def _exec_with_plugin(self, invocation_context, session, execute_fn, is_live_call=False):
    plugin_manager = invocation_context.plugin_manager

    # —— 阶段 1：before_run 钩子。若返回 Content，直接短路出一个 'model' Event ——
    early_exit_result = await plugin_manager.run_before_run_callback(
        invocation_context=invocation_context
    )
    if isinstance(early_exit_result, types.Content):
        early_exit_event = Event(
            invocation_id=invocation_context.invocation_id,
            author='model',
            content=early_exit_result,
        )
        if self._should_append_event(early_exit_event, is_live_call):
            await self.session_service.append_event(session=session, event=early_exit_event)
        yield early_exit_event
    else:
        # —— 阶段 2：真正执行 + 每个 event 过一遍 on_event 钩子 + 持久化 ——
        buffered_events: list[Event] = []
        is_transcribing: bool = False

        async with Aclosing(execute_fn(invocation_context)) as agen:
            async for event in agen:
                _apply_run_config_custom_metadata(event, invocation_context.run_config)

                modified_event = await plugin_manager.run_on_event_callback(
                    invocation_context=invocation_context, event=event
                )
                output_event = self._get_output_event(
                    original_event=event,
                    modified_event=modified_event,
                    run_config=invocation_context.run_config,
                )

                if is_live_call:
                    # ... 见 3.4 节：live 模式下的事件缓冲 ...
                else:
                    if event.partial is not True:
                        await self.session_service.append_event(
                            session=session, event=output_event
                        )

                yield output_event

    # —— 阶段 3：after_run 钩子，仅副作用（日志/指标收尾），不产生 event ——
    await plugin_manager.run_after_run_callback(invocation_context=invocation_context)
```

三个阶段的语义：

**阶段 1 — `before_run_callback`：允许插件短路整个 invocation**
- 每个注册的插件都会被顺序调用；任一插件返回非 `None`（必须是 `types.Content`），后续插件被跳过，也不再执行真正的 agent 逻辑。
- 短路出的"假事件"仍然会被持久化到 session 并 yield 给调用方——调用方无法区分这是插件短路还是真实回复。
- 典型用途：策略拦截（"这个用户被封禁，直接返回拒绝消息"）、缓存命中（"同样的 prompt 三小时内见过，直接回放上次答案"）。

**阶段 2 — 主循环：每个 event 三步走**
1. `_apply_run_config_custom_metadata`：把 `run_config.custom_metadata` 并入 `event.custom_metadata`（调用方透传业务标签用）。
2. `plugin_manager.run_on_event_callback`：所有插件按注册序跑一遍，前一个插件的返回值作为后一个的输入。任何插件返回 `None` 不算短路，它只是"我不修改"；非 `None` 返回值会替换事件。
3. `_get_output_event` 把原始 event 和修改版合并：**保留原 event 的 id / timestamp / invocation_id 等系统字段**，只允许插件改内容、actions、custom_metadata——防止插件意外污染事件身份。
4. 持久化与 yield 顺序：**先 append 到 session，再 yield 给调用方**。这保证了外部调用方看到某个事件时，session 上已经有它；即使调用方在消费过程中崩溃，session 仍然完整。
5. **`partial=True` 的事件不持久化**——流式 LLM 的中间分块只 yield 不落盘，等最后一个 `partial=False` 的完整事件到来时才写 session。

**阶段 3 — `after_run_callback`：仅做副作用**
- 文档明确标注：`This does NOT emit any event.`（[`runners.py:951`](../src/google/adk/runners.py)）
- 典型用途：把本次 invocation 的计费信息写入 BigQuery、把 `_invocation_cost_manager` 的指标 flush 到 StatsD 等。

**设计要点**：插件系统用"返回值决定是否短路"的约定（而非显式抛异常或 `should_continue=False`），让所有回调代码路径保持线性、可组合、可测试。阶段 1 / 阶段 2 / 阶段 3 的钩子互相独立，不共享状态——状态共享必须走 `invocation_context`。

### 3.4 Live 模式下的事件缓冲策略

Live（双向流式）模式比文本模式多一个棘手的排序问题：**音频转录事件的到达时机不确定**，它可能**晚于**基于这段音频产生的函数调用。如果直接按到达顺序写入 session，session 上会出现"先看到 tool call，后看到用户说的话"的倒序。

`_exec_with_plugin` 里 [`runners.py:877-940`](../src/google/adk/runners.py) 的这段缓冲逻辑就是为了修正这个：

```python
# src/google/adk/runners.py:877-940（精简）
buffered_events: list[Event] = []
is_transcribing: bool = False

async for event in agen:
    # ...（省略插件处理）...

    if is_live_call:
        # 1) 若当前是一条"部分转录"事件（仍在识别中），进入 transcribing 状态
        if event.partial and _is_transcription(event):
            is_transcribing = True

        # 2) 若处于 transcribing 状态，且来了一条 tool call / tool response，
        #    说明 agent 已经基于音频做出反应，但转录还没完成 —— 缓冲起来
        if is_transcribing and _is_tool_call_or_response(event):
            buffered_events.append(output_event)
            continue

        # 3) 非 partial 事件（partial=None 或 partial=False 都算非 partial）
        if event.partial is not True:
            if _is_transcription(event) and (
                _has_non_empty_transcription_text(event.input_transcription)
                or _has_non_empty_transcription_text(event.output_transcription)
            ):
                # 3a) 这是一条"最终转录"事件 —— 解除 transcribing 状态，
                #     先 append 这条最终转录，再把之前缓冲的 tool call / response 按序刷出
                is_transcribing = False
                if self._should_append_event(event, is_live_call):
                    await self.session_service.append_event(session=session, event=output_event)

                for buffered_event in buffered_events:
                    await self.session_service.append_event(session=session, event=buffered_event)
                    yield buffered_event  # 重新流给调用方
                buffered_events = []
            else:
                # 3b) 非转录类的非 partial 事件，正常落盘
                if self._should_append_event(event, is_live_call):
                    await self.session_service.append_event(session=session, event=output_event)
    else:
        if event.partial is not True:
            await self.session_service.append_event(session=session, event=output_event)

    yield output_event
```

这段代码的本质是一个**单状态缓冲机状态机**：

```
        +-------------------+
        |  normal (初始)    |
        +--------+----------+
                 |
   收到 partial=True 且是 transcription
                 v
        +-------------------+
        |  transcribing     |  <-- 遇到 tool call / response 时仅 buffer，不落盘
        +--------+----------+
                 |
 收到 partial=False 且是 transcription（且文本非空）
                 v
     刷出 buffered_events，回到 normal
```

关键设计：

- **缓冲只对 tool call / response 生效**——其他事件（音频 blob、usage metadata、partial 文本）仍按到达顺序持久化，不会被延迟。
- **`partial=None` 视同 `partial=False`**——这是一个容易踩坑的规则，因为 `Optional[bool]` 在 Python 里可以有三态（`None` / `False` / `True`），ADK 选择把 `None` 和 `False` 合并为"非流式中间块"。
- **`partial=False` 但转录文本为空的事件不触发刷出**——这通常是"开始转录"或"结束转录"的控制信号，不包含实质内容。
- **缓冲的事件重发到调用方**：注意 `yield buffered_event` 发生在 append 之后，所以外部消费者看到的 event 顺序和 session 里存的一致。

**为什么非 live 分支没有这个复杂度？** 文本路径的事件是"一次 LLM step 结束才统一 yield"的，tool call 和 tool response 是串行产生的，不存在转录事件插入；所以只有简单的"非 partial 才持久化"规则。

### 3.5 Event Compaction 与 `rewind_async`

Runner 还有两个**会话级写操作**，它们不是 invocation 的主循环，但直接修改 session 的事件流。

#### 3.5.1 Event Compaction：事件压缩

随着对话轮数增多，session 的事件流会膨胀，影响 LLM prompt 组装（`contents` processor 会把历史事件翻译成 `types.Content` 序列）。ADK 通过 `EventsCompactionConfig`（[`apps/app.py:62-108`](../src/google/adk/apps/app.py)）提供滑动窗口式摘要机制：

```python
# src/google/adk/apps/app.py:62-108（精简）
class EventsCompactionConfig(BaseModel):
    summarizer: Optional[BaseEventsSummarizer] = None
    compaction_interval: int           # 每累积 N 次 *用户触发的* invocation 触发一次压缩
    overlap_size: int                  # 相邻两个压缩区间重叠的 invocation 数（维持上下文连续性）
    token_threshold: Optional[int] = None          # token 阈值（可选的另一条触发路径）
    event_retention_size: Optional[int] = None     # token 压缩时保留的原始事件数
```

两条触发路径：

- **间隔触发**：每 `compaction_interval` 次用户 invocation 完成后执行一次；相邻压缩区间保留 `overlap_size` 个 invocation 的重叠，避免摘要断裂。
- **令牌阈值触发**：若 LlmRequest 实际用到的 prompt token 数 ≥ `token_threshold`，本次 invocation 末尾触发一次压缩，但保留最后 `event_retention_size` 条原始事件不压缩。

**触发点在 `run_async` 末尾**（见 3.2 节第 ⑦ 步），由 `_run_compaction_for_sliding_window`（来自 `google.adk.apps.compaction`）执行。它做的事：
1. 读取 session.events；
2. 调用 `summarizer` 把窗口内的事件摘要成一段 `Content`；
3. 创建一个 `actions.compaction=EventCompaction(start_timestamp, end_timestamp, compacted_content)` 的特殊事件 append 到 session。

后续 `contents` processor 组装 LlmRequest 历史时，会**用压缩事件替换掉被它覆盖的原始事件**。原始事件不会从存储里删除，只是不再被加载进 prompt——这保证了可追溯性。

**关键约束**：压缩永远在 invocation 结束后执行，绝不在中途触发。`token_compaction_checked` 标记 invocation 内是否已经检查过令牌，用于防止同一次 invocation 被压缩两次。

#### 3.5.2 `rewind_async`：session 回滚

[`runners.py:635-684`](../src/google/adk/runners.py) 提供了一个相对新的能力——把 session 回滚到某次 invocation 之前：

```python
# src/google/adk/runners.py:635-684（精简）
async def rewind_async(self, *, user_id, session_id, rewind_before_invocation_id, run_config=None):
    session = await self._get_or_create_session(...)

    # 找到目标 invocation 的第一个事件
    rewind_event_index = -1
    for i, event in enumerate(session.events):
        if event.invocation_id == rewind_before_invocation_id:
            rewind_event_index = i
            break
    if rewind_event_index == -1:
        raise ValueError(f'Invocation ID not found: {rewind_before_invocation_id}')

    # 计算"该点 state"与"当前 state"的差，生成反向 state_delta
    state_delta = await self._compute_state_delta_for_rewind(session, rewind_event_index)
    # 同样计算 artifact 的版本回滚
    artifact_delta = await self._compute_artifact_delta_for_rewind(session, rewind_event_index)

    # 构造一个 'user' 作者的 rewind 事件
    rewind_event = Event(
        invocation_id=new_invocation_context_id(),
        author='user',
        actions=EventActions(
            rewind_before_invocation_id=rewind_before_invocation_id,
            state_delta=state_delta,
            artifact_delta=artifact_delta,
        ),
    )
    await self.session_service.append_event(session=session, event=rewind_event)
```

实现细节值得注意：

- **不删除历史事件**——而是**追加一个带 `rewind_before_invocation_id` 的新事件**。历史事件仍在 session 里，但 ADK 的事件过滤逻辑（`_get_events`）会看到 rewind 事件后跳过被回滚的段。
- **state_delta 是差量回放**：遍历目标 invocation 之前的所有 `state_delta`，还原出"那一时刻"的 state；再和当前 state 对比，生成把当前变回去的反向 delta。`app:` 和 `user:` 前缀的状态视为应用/用户级常量，不参与回滚。
- **artifact 同理**：按 `artifact_delta` 里的版本号计算回滚目标版本。`user:` 前缀的 artifact 不回滚——这些是用户级别的持久资料（如头像），不应被 session 级回滚影响。
- **`rewind_async` 不产出 Event 流**：签名是 `-> None`，回滚只是"追加一个 marker 事件"，不触发 agent 执行。

设计哲学：session 是 append-only 日志，任何修改都通过追加事件来表达。Rewind 不例外——这让事件流始终可重放、可审计、可回退。

---

## 4. Agent 体系

### 4.1 BaseAgent：所有 agent 的抽象根

#### 4.1.1 抽象契约与门面方法

[`base_agent.py:86`](../src/google/adk/agents/base_agent.py) 的 `BaseAgent` 是 Pydantic `BaseModel` 的子类。这是一个重要的设计选择——它意味着：

- **Agent 是数据对象**：可以序列化、可以用 `model_copy` 深拷贝、可以用 `@computed_field` 派生属性。
- **校验在构造时完成**：`name` 必须是合法 Python identifier，不能叫 "user"（被用户消息占用）等规则写在 `model_validator` 里。
- **相等性与 hash 语义明确**：两个 agent 实例当且仅当所有字段相同才相等。

核心字段只有三个：

```python
# src/google/adk/agents/base_agent.py:111-137
name: str                                       # 必填，agent 树内唯一
description: str = ''                           # LLM 决定是否转移给此 agent 时读取
parent_agent: Optional[BaseAgent] = Field(
    default=None, init=False, exclude=True      # 不参与构造；Pydantic 不序列化；自动反向赋值
)
sub_agents: list[BaseAgent] = Field(default_factory=list)
```

`BaseAgent` 暴露两个 `@final` 门面方法作为**唯一运行入口**，它们的实现只有十来行：

```python
# src/google/adk/agents/base_agent.py:273-304（精简）
@final
async def run_async(self, parent_context: InvocationContext) -> AsyncGenerator[Event, None]:
    with tracer.start_as_current_span(f'invoke_agent {self.name}') as span:
        ctx = self._create_invocation_context(parent_context)    # ① 派生子上下文
        tracing.trace_agent_invocation(span, self, ctx)

        if event := await self._handle_before_agent_callback(ctx):  # ② before 回调，可短路
            yield event
        if ctx.end_invocation:
            return

        async with Aclosing(self._run_async_impl(ctx)) as agen:    # ③ 委托给子类实现
            async for event in agen:
                yield event

        if ctx.end_invocation:
            return

        if event := await self._handle_after_agent_callback(ctx):  # ④ after 回调
            yield event
```

`run_live` 是它的平行版本，结构一致，只是第三步委托给 `_run_live_impl`。

**`@final` 装饰器的意义**：子类**不应**重写 `run_async` / `run_live`，而是**必须**重写 `_run_async_impl` / `_run_live_impl`。这个约定强制了三件事：

1. **前后 callback 总是被调用**——子类无法意外绕过 `before_agent_callback` / `after_agent_callback`。
2. **子上下文总是被派生**——`_create_invocation_context` 保证每个 agent 看到的是一个干净的 context 副本，而不是父 agent 的直接引用（避免并发写坏同一个对象）。
3. **Telemetry span 总是被开**——每个 agent 调用在链路追踪里都有独立的 span，即使子类忘了加埋点。

子类契约是这两个方法：

```python
# src/google/adk/agents/base_agent.py:336-366
async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
    raise NotImplementedError(f'_run_async_impl for {type(self)} is not implemented.')
    yield  # AsyncGenerator requires having at least one yield statement

async def _run_live_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
    raise NotImplementedError(f'_run_live_impl for {type(self)} is not implemented.')
    yield
```

注意尾部那行"多余"的 `yield`——这是 Python async generator 的语法要求：**没有 `yield` 的 async 函数不会被识别为 async generator**（哪怕前面 raise 了）。这里用 `raise ... / yield` 的组合，既让不实现的子类正确抛错，又让类型系统认得它是 AsyncGenerator。

编排器（LoopAgent / ParallelAgent / SequentialAgent）和 LlmAgent 都是通过重写这两个方法来定义自己的行为的——这也是"编排"和"调 LLM"能在同一抽象下嵌套的原因。

#### 4.1.2 Before / After Agent Callback

`BaseAgent` 允许每个 agent 挂两个 callback：

```python
# src/google/adk/agents/base_agent.py:139-166
before_agent_callback: Optional[BeforeAgentCallback] = None
after_agent_callback: Optional[AfterAgentCallback] = None
```

这两个字段的值可以是**单个 callable** 或 **callable 列表**。列表模式允许多个独立关注点叠加，按注册序依次调用，**前一个返回非 None 即短路**（`canonical_before_agent_callbacks` [`base_agent.py:410-420`](../src/google/adk/agents/base_agent.py) 做归一化）。

`_handle_before_agent_callback` [`base_agent.py:434-490`](../src/google/adk/agents/base_agent.py) 的执行顺序：

1. **插件优先**：先跑 `ctx.plugin_manager.run_before_agent_callback(...)`。插件来自 App 级配置，作用于全局。
2. **插件未短路时**再跑本 agent 的 `canonical_before_agent_callbacks`，按注册序直到某个 callback 返回非 None。
3. **返回非 None 的三种语义**：
   - 返回 `types.Content`：把它包装成 Event，**设置 `ctx.end_invocation = True`**——整个 invocation 结束，用户收到这段内容作为最终回复。
   - 返回 None 但 `callback_context.state.has_delta()`：产出一个"只有 state_delta 没有 content"的事件，让 state 变化持久化。
   - 都没有：返回 None，agent 正常进入 `_run_async_impl`。

After callback 的逻辑几乎对称，但有一个关键差异：**返回 Content 不会 `end_invocation`**——它只是追加一条回复。这是因为 after callback 发生在 agent 跑完之后，设置 end_invocation 为时已晚（同层下一个 agent 不应被此影响，但同一 agent 的执行已经结束了）。

**CallbackContext 是什么？** 它是一个针对 callback 场景精简过的上下文对象（[`agents/callback_context.py`](../src/google/adk/agents/callback_context.py)），包装了 `InvocationContext` 并暴露：

- `callback_context.state`：一个 `State` 对象，支持写 delta（`state['key'] = value`），delta 在 callback 返回后被装进 event.actions.state_delta。
- `callback_context.load_artifact(name, version)` / `save_artifact(name, artifact)`：通过 `ArtifactService` 读写文件。
- `callback_context.user_content`：本次 invocation 的用户输入。

所有 callback（agent、model、tool）都接收 CallbackContext 或其变体（`ToolContext` / `ReadonlyContext` 等），这让 callback 代码的风格统一：**不直接触碰 InvocationContext，避免意外修改敏感字段**。

**设计哲学**：callback 是**声明式的"在这里插一段代码"**，不是命令式的"控制流分支"。短路用返回值表达，状态修改用 delta 表达——这让 callback 可以被独立单元测试，也可以组合。

#### 4.1.3 Sub-agents 组合与 parent_agent 反向指针

Agent 通过 `sub_agents: list[BaseAgent]` 形成**树**（不是图——每个 agent 最多一个 parent）。这个约束通过 `__set_parent_agent_for_sub_agents` 强制：

```python
# src/google/adk/agents/base_agent.py:611-620
def __set_parent_agent_for_sub_agents(self) -> BaseAgent:
    for sub_agent in self.sub_agents:
        if sub_agent.parent_agent is not None:
            raise ValueError(
                f'Agent `{sub_agent.name}` already has a parent agent, current'
                f' parent: `{sub_agent.parent_agent.name}`, trying to add:'
                f' `{self.name}`'
            )
        sub_agent.parent_agent = self
    return self
```

它在 `model_post_init`（Pydantic 的构造后钩子）里被调用，**自动**把每个子 agent 的 `parent_agent` 指回父 agent。两条规则：

- **同一个 agent 实例不能被同时挂到两棵树**：第二次设置 `parent_agent` 会直接抛错。这防止了"同一个 agent 在两个地方被触发，但它只有一个状态"的混乱语义。
- **需要复用同配置？** 明确要求使用者自己复制——或者调用 `agent.clone()`（[`base_agent.py:211-271`](../src/google/adk/agents/base_agent.py)）深拷贝一份，同时 **递归 clone** 所有 sub_agents 并重设 parent_agent，避免共享引用。

子 agent 名字在同一层**应**唯一——`validate_sub_agents_unique_names` [`base_agent.py:572-609`](../src/google/adk/agents/base_agent.py) 做了一次 warning 级别的检查（不抛错，因为 agent config 可能动态加载，名字冲突不一定是 bug）。

**树遍历方法**：`BaseAgent` 提供了几个导航属性：

- `root_agent`：沿 `parent_agent` 一直往上找，直到无父为止。
- `find_agent(name)`：在当前子树里（含自身）按名字深度优先查找。
- `find_sub_agent(name)`：只在直接子 agent 里找。

这些方法在两个场景频繁用到：

1. **agent_transfer**：`AutoFlow` 的 `agent_transfer` processor 让 LLM 通过 `transfer_to_agent(agent_name)` 函数调用切换当前 agent。切换目标必须通过名字解析——`find_agent` 负责这一步。
2. **`canonical_model` 继承**：`LlmAgent` 的 `canonical_model` 属性（[`llm_agent.py:518-533`](../src/google/adk/agents/llm_agent.py)）在自身没设 `model` 时沿 `parent_agent` 链向上找最近的 LlmAgent 的 model——这让多 agent 系统能**共享一个默认模型**，只有需要特化的 agent 才显式声明。

**`parent_agent` 为什么是 `init=False, exclude=True`？**

- `init=False`：构造函数不接这个参数。用户不应手动赋值，只有 `__set_parent_agent_for_sub_agents` 内部用。
- `exclude=True`：Pydantic 序列化时忽略它。否则 `root.model_dump()` 会因为循环引用（root → sub → parent → root）无限递归或报错。

这个双约束加在一起，让"树"从数据建模到运行时都是单向传播的。

#### 4.1.4 BaseAgentState：可恢复机制

ADK 有一个实验性能力——**在长运行工具（Long-Running Tool）调用处暂停 invocation，稍后恢复继续跑**。这需要每个 agent 能把"我跑到哪一步了"持久化到事件流，下次从该点接着跑。这就是 `BaseAgentState`。

基类提供三个方法对接这个机制：

```python
# src/google/adk/agents/base_agent.py:168-209（精简）
def _load_agent_state(self, ctx, state_type: Type[AgentState]) -> Optional[AgentState]:
    """从 ctx.agent_states 字典里加载当前 agent 的 state（若有）。"""
    if ctx.agent_states is None or self.name not in ctx.agent_states:
        return None
    return state_type.model_validate(ctx.agent_states.get(self.name))

def _create_agent_state_event(self, ctx) -> Event:
    """产出一个带 actions.agent_state / actions.end_of_agent 的事件，供 session 持久化。"""
    event_actions = EventActions()
    if (agent_state := ctx.agent_states.get(self.name)) is not None:
        event_actions.agent_state = agent_state
    if ctx.end_of_agents.get(self.name):
        event_actions.end_of_agent = True
    return Event(
        invocation_id=ctx.invocation_id,
        author=self.name,
        branch=ctx.branch,
        actions=event_actions,
    )
```

**核心想法**：把 agent 的运行状态当作 Pydantic 模型（`BaseAgentState` 的子类），在关键 checkpoint 把它塞进 `Event.actions.agent_state`，Session 落盘时一起保存。下次恢复时，`Runner._setup_context_for_resumed_invocation` 遍历事件流，把每个 agent 的最新 state 还原到 `invocation_context.agent_states`。

各编排器定义了自己的 state 类型：

| Agent | State 类 | 记录字段 |
|---|---|---|
| `LoopAgent` | `LoopAgentState` | `current_sub_agent`（本次循环跑到哪个子 agent）、`times_looped`（循环次数） |
| `SequentialAgent` | `SequentialAgentState` | `current_sub_agent`（当前在跑的子 agent） |
| `ParallelAgent` | `BaseAgentState` | 仅作为"我启动过"的标记（并行分支各自独立恢复） |

**工作流示例（LoopAgent）**：

```
初次运行：
  yield AgentStateEvent(current_sub_agent='A', times_looped=0)
  run A ... A 调用了一个 long_running_tool ... yield 长运行工具的 function_call event
  invocation 在该处暂停，agent 保留 state="A, looped=0"

几分钟后，外部系统完成了长运行操作：
  调用 runner.run_async(invocation_id=<原 id>, new_message=tool_response)
  _setup_context_for_resumed_invocation 读取事件流，重建 agent_states
  LoopAgent._run_async_impl 发现 ctx.agent_states[self.name] 非空
    → _get_start_state 解出 times_looped=0, start_index = index_of('A')
    → 从 A 继续（而不是从头跑 A，再跑 B）
```

**关键细节**：

- **只在 `ctx.is_resumable=True` 时才 yield agent_state 事件**——非恢复场景下这是纯开销，默认关闭。
- **`end_of_agent=True` 清空 agent_state**（见 [`invocation_context.py:254-256`](../src/google/adk/agents/invocation_context.py)）——agent 结束就不再需要状态快照，避免 session 膨胀。
- **`populate_invocation_agent_states`**（[`invocation_context.py:283-312`](../src/google/adk/agents/invocation_context.py)）从事件流反向重建：最后一个 `actions.agent_state` 生效；之后如果有 `end_of_agent=True` 则清除。
- **非 workflow agent（如 LlmAgent）也参与**：如果一个 agent 在事件流里有内容但没显式 agent_state，会被赋值为空 `BaseAgentState`——表示"我开始跑过，没跑完"，防止被误当作"未运行"。

**可恢复性不是所有 agent 都支持**：`ParallelAgent._run_live_impl` 明确 `raise NotImplementedError`，`LoopAgent._run_live_impl` 也是。Live 模式下的状态模型与文本路径差异太大，目前只有 SequentialAgent 支持 live 顺序模式。

### 4.2 LlmAgent：最常用的 agent 类型

`LlmAgent` 是 `Agent` 的真身（[`src/google/adk/__init__.py`](../src/google/adk/__init__.py) 把它作为 `Agent` 导出）。它绑定一个 LLM + 一组工具，提供"能思考、会调工具、可转移"的完整 reason-act 能力。整个类有 1000+ 行，分 5 个关注点来看。

#### 4.2.1 Instruction 体系：三种指令字段

LlmAgent 有**三个**指令相关字段，它们的差异是"静态 vs 动态 × 系统消息 vs 用户消息"：

```python
# src/google/adk/agents/llm_agent.py:215-291（精简）
instruction: Union[str, InstructionProvider] = ''
global_instruction: Union[str, InstructionProvider] = ''        # DEPRECATED
static_instruction: Optional[types.ContentUnion] = None
```

| 字段 | 类型 | 装配位置 | 是否做变量替换 | 主要用途 |
|---|---|---|---|---|
| `instruction` | `str` 或 `InstructionProvider` 回调 | 视 static_instruction 存在与否：**不存在时→system_instruction；存在时→user content** | ✅ `{state_var}` 占位符会被 session state 替换 | 针对当前 agent 的动态规则，可在运行时根据 state 变化 |
| `global_instruction` | 同上 | 仅 root agent 的值生效；挂到所有 agent 的 system instruction 前缀 | ✅ | **已废弃**，推荐用 `GlobalInstructionPlugin`；保留是为了向后兼容 |
| `static_instruction` | `types.ContentUnion`（str / Content / Part / Image / File / list） | 始终 → system_instruction 的最前面 | ❌ 原文发送，不替换 | **上文缓存优化**——静态前缀命中 Gemini 的 implicit/explicit cache，避免每轮重算 |

**为什么要把 `instruction` 和 `static_instruction` 分开？**

这是对**上下文缓存（Context Caching）**的适配。Gemini API 的缓存规则是：前缀越长、越稳定，命中率越高、成本越低。但 `instruction` 支持 `{state_var}` 这类动态替换，每轮都可能变化——这部分内容无法缓存。解决办法是把指令切成两段：

- `static_instruction`：永恒不变的身份、行为规范、few-shot 示例——放 prompt 最前面，整体被缓存。
- `instruction`：依赖 state 的动态规则——放在 user content 的位置，在 LLM 看来相当于用户额外提供的一段提示。

当 `static_instruction=None` 时，两者合并到 system_instruction，走普通路径；当 `static_instruction` 非空时，框架自动把 `instruction` 移到 user content，以保证 static 部分真正位于 prompt 开头。

**`InstructionProvider` 是什么？**

除了字符串，这两个字段还能传入一个 `async`/sync 回调：

```python
def dynamic_instruction(ctx: ReadonlyContext) -> str:
    # 可以根据 ctx.state、ctx.session、当前时间等计算出最合适的指令
    return f"Today is {datetime.now():%Y-%m-%d}. The user is in tier {ctx.state['user_tier']}."

agent = LlmAgent(
    name='coach',
    instruction=dynamic_instruction,
    ...
)
```

`canonical_instruction(ctx)`（[`llm_agent.py:552-574`](../src/google/adk/agents/llm_agent.py)）在每次请求装配时调用一次，返回 `(instruction_text, bypass_state_injection)`。第二个返回值决定是否跳过 `{state_var}` 替换——某些 provider 自己负责的情况下（如 MCP instruction），框架不再做二次替换。

**关键约束**：`instruction` 只影响"当前 agent"看到的指令。子 agent 有自己的 instruction，互不继承。如果需要全局统一身份，只有两条路：`global_instruction`（已废弃）或 `GlobalInstructionPlugin`。

#### 4.2.2 Model 与 Tools 的归一化

LlmAgent 的 `model` 和 `tools` 字段接受很宽松的类型，运行时需要归一化成 `BaseLlm` 和 `list[BaseTool]`。这部分逻辑在 `canonical_model` 和 `canonical_tools` 两个属性里。

**Model：字符串 → BaseLlm**

```python
# src/google/adk/agents/llm_agent.py:204-210, 517-533
model: Union[str, BaseLlm] = ''   # 默认空字符串 = 继承祖先

@property
def canonical_model(self) -> BaseLlm:
    if isinstance(self.model, BaseLlm):
        return self.model                               # ① 已经是实例，直接用
    elif self.model:                                    # ② 非空字符串，走 registry 解析
        return LLMRegistry.new_llm(self.model)
    else:                                               # ③ 空字符串，沿 parent_agent 向上找
        ancestor_agent = self.parent_agent
        while ancestor_agent is not None:
            if isinstance(ancestor_agent, LlmAgent):
                return ancestor_agent.canonical_model
            ancestor_agent = ancestor_agent.parent_agent
        return self._resolve_default_model()            # ④ 全树都没设 → 默认模型
```

三层 fallback 的设计意图：

- **实例优先**：用户显式传了 `Gemini(...)` 或自定义 `BaseLlm` 子类，就尊重它。
- **字符串次之**：常见的写法 `model='gemini-2.5-flash'`，由 `LLMRegistry` 按正则匹配到对应类。
- **继承再次**：多 agent 系统里通常 root 设模型，子 agent 省略——`canonical_model` 自动沿 `parent_agent` 链向上找。
- **默认兜底**：类变量 `_default_model`（初始为 `'gemini-2.5-flash'`）。`LlmAgent.set_default_model(...)` 可以全局改。

**Tools：ToolUnion → list[BaseTool]**

`tools` 字段的类型是 `list[ToolUnion]`，`ToolUnion` 包括普通 Python 函数、`BaseTool` 实例、`BaseToolset` 实例、甚至另一个 `BaseAgent`（自动包装为 `AgentTool`）。

`canonical_tools(ctx)`（[`llm_agent.py:610-632`](../src/google/adk/agents/llm_agent.py)）做以下事情：

1. **缓存**：结果存到 `ctx.canonical_tools_cache`，同一次 invocation 内只解析一次。
2. **按类型分发**：
   - 裸函数 → `FunctionTool(fn)`
   - `BaseTool` 实例 → 原样保留
   - `BaseToolset` 实例 → 调用 `toolset.get_tools(readonly_context)` 展开为多个 tool
   - `BaseAgent` 实例 → `AgentTool(agent)`
3. **内置工具校验**：像 `google_search`、`url_context` 这类 Gemini 原生工具在非 Gemini 模型上会被忽略或转换；`canonical_tools` 负责做兼容性检查。

**为什么动态展开？** `BaseToolset.get_tools(readonly_context)` 接收当前上下文——这意味着工具列表**可以根据 session state、user_id、分支**动态变化。比如一个 `PermissionFilteredToolset` 可以根据当前用户的角色只暴露它有权限调用的工具。

**组合示例**：

```python
agent = LlmAgent(
    name='support',
    model='gemini-2.5-flash',
    tools=[
        search_docs,                         # 普通函数
        BookingToolset(db_url=...),          # 动态产出订票相关工具
        specialist_agent,                    # 另一个 agent，自动包装
        google_search,                       # 内置工具
    ],
)
```

`canonical_tools` 返回的是一个扁平的 `list[BaseTool]`，供后续 `LlmRequest` 组装使用。

#### 4.2.3 输入输出约束：input_schema / output_schema / output_key

这三个字段用于给 LlmAgent 强加结构化约束：

```python
# src/google/adk/agents/llm_agent.py:329-351
input_schema: Optional[type[BaseModel]] = None
output_schema: Optional[SchemaType] = None
output_key: Optional[str] = None
```

**`input_schema`**：当 agent **被当作工具使用**（通过 `AgentTool` 包装）时，它的输入参数 schema。LLM 调用这个 agent 时必须提供匹配 schema 的 JSON。常见场景是"把 agent A 暴露给 agent B 调用，A 有固定的结构化输入"。

**`output_schema`**：要求 LLM 回复必须是给定 schema 的 JSON。类型可以是：

- `type[BaseModel]`：单个 Pydantic 模型
- `list[type[BaseModel]]`：模型列表
- `list[primitive]`：基础类型列表
- `dict`：原始 JSON Schema
- `Schema`：Google GenAI 的 Schema 类型

**关键约束**：当 `output_schema` 被设置，agent **不能使用任何工具**。原因很直接——LLM 此时必须把所有"输出预算"用在 schema 匹配的 JSON 上，没有余地去做 function call。框架在组装 LlmRequest 时会校验这一点：若同时有 `output_schema` 和 `tools`，走 `_output_schema_processor` 的特殊路径（把 schema 转换成一个特殊的 `set_model_response` 工具，让 LLM 通过 function call 方式返回结构化结果，见 6.3 节）。

**`output_key`**：最终回复的**文本**会被自动写入 `session.state[output_key]`。使用场景主要有两个：

1. **多 agent 协作**：上游 agent 把结果写入 `session.state['draft']`，下游 agent 在 instruction 里用 `{draft}` 引用。
2. **程序化读取**：外层代码通过 `session.state['answer']` 拿到结构化结果，而不是自己从 event 流里翻。

**实现位置**：

```python
# src/google/adk/agents/llm_agent.py:__maybe_save_output_to_state（精简）
def __maybe_save_output_to_state(self, event: Event) -> None:
    if self.output_key and event.is_final_response() and event.content:
        text = _extract_text_from_content(event.content)
        if text:
            event.actions.state_delta[self.output_key] = text
```

这段代码在 `_run_async_impl` 里被每个事件调用一次。**它不是把 state 直接改掉，而是写入 `event.actions.state_delta`**——借由 session 的 event 机制保证持久化与可回滚。

**`include_contents` 字段**（[`llm_agent.py:319-326`](../src/google/adk/agents/llm_agent.py)）：可选 `'default'` 或 `'none'`。设为 `'none'` 时，LLM 不会看到历史对话，只靠当前 instruction + user 输入工作——这对于"一次性工具"（比如 summarizer、classifier）很有用，能避免历史污染输出。

#### 4.2.4 六个 Callback：围绕 model 和 tool

除了 `BaseAgent` 继承来的 `before/after_agent_callback`，`LlmAgent` 额外定义了六个 callback，精细控制 LLM 调用和工具调用的边界：

| Callback | 时机 | 参数 | 返回值生效方式 |
|---|---|---|---|
| `before_model_callback` | LLM 调用前 | `callback_context`, `llm_request: LlmRequest` | 返回 `LlmResponse` → 跳过 LLM 调用，直接用返回值作为响应 |
| `after_model_callback` | LLM 调用后 | `callback_context`, `llm_response: LlmResponse` | 返回 `LlmResponse` → 替换原响应 |
| `on_model_error_callback` | LLM 调用抛异常 | `callback_context`, `llm_request`, `error: Exception` | 返回 `LlmResponse` → 吞掉异常，用返回值当响应 |
| `before_tool_callback` | Tool 调用前 | `tool`, `args: dict`, `tool_context` | 返回 `dict` → 跳过 tool 调用，用返回值当 tool response |
| `after_tool_callback` | Tool 调用后 | `tool`, `args`, `tool_context`, `tool_response: dict` | 返回 `dict` → 替换原 tool response |
| `on_tool_error_callback` | Tool 调用抛异常 | `tool`, `args`, `tool_context`, `error` | 返回 `dict` → 吞掉异常，用返回值当 tool response |

所有这六个字段都接受**单个 callable 或 callable 列表**，由对应的 `canonical_xxx_callbacks` 属性归一化。列表模式下按序调用到第一个返回非 None 的为止，这和 agent callback 的规则一致。

**典型用法**

- **审计 / 重写 prompt**：`before_model_callback` 修改 `llm_request.contents`，插入合规声明或屏蔽敏感字段。
- **结果后处理**：`after_model_callback` 做敏感词过滤、格式化、翻译。
- **降级策略**：`on_model_error_callback` 遇到 quota 超限时返回一段预设文案，不让用户看到 500 错误。
- **Mock 工具**：`before_tool_callback` 在测试环境里把真实 API 调用替换成固定响应，不改 agent 代码。
- **结果缓存**：`before_tool_callback` 查本地缓存命中则直接返回，跳过真实调用。
- **工具熔断**：`on_tool_error_callback` 降级到备用逻辑（例如外部搜索失败时改用本地知识库）。

**与 Plugin 的关系**：Plugin 是 **App 级**的，作用于所有 agent；Callback 是 **Agent 级**的。执行顺序是"**Plugin 先行，Agent Callback 后行**"，任一层返回非 None 都会短路剩下的钩子。这允许"应用层统一策略 + agent 层特化行为"的分层组合。

**CallbackContext / ToolContext 的角色**：

- 前三个 callback 用 `CallbackContext`，暴露 state / artifact / user_content。
- 后三个用 `ToolContext`（`CallbackContext` 的子类），额外暴露 `actions`（可以在 callback 里声明 `transfer_to_agent`、`escalate` 等动作）和 `function_call_id`。

**`on_xxx_error_callback` 的设计意义**：传统"在 callback 里 try/except"的写法无法获取 ADK 完整的上下文（比如工具名称、参数）；把错误处理作为独立 callback 注入，保持代码分层，也避免把业务 fallback 逻辑混进 tool 实现。

#### 4.2.5 `_llm_flow` 选择与 `_run_async_impl`

LlmAgent 的真正执行逻辑委托给**一个 Flow 对象**——它是 "LLM 交互机器"，把 preprocess / call LLM / postprocess / 工具派发等步骤组织成循环（下一章详细展开）。

```python
# src/google/adk/agents/llm_agent.py:716-725
@property
def _llm_flow(self) -> BaseLlmFlow:
    if (
        self.disallow_transfer_to_parent
        and self.disallow_transfer_to_peers
        and not self.sub_agents
    ):
        return SingleFlow()
    else:
        return AutoFlow()
```

选择规则很简单：

- **`SingleFlow`**：当前 agent 既**禁止向上下转移**，又**没有子 agent**——说明它是一个"孤岛 agent"，不参与多 agent 协作。只跑"LLM + 工具"的基础循环。
- **`AutoFlow`**：只要有子 agent 或允许转移，就用它。`AutoFlow = SingleFlow + agent_transfer processor`，多了一个处理器专门处理 `transfer_to_agent(...)` function call，让 LLM 能把控制权交给其他 agent。

**注意**：`_llm_flow` 是**每次访问都新建实例**的属性（不是缓存字段）。因为 Flow 内部的 `request_processors` / `response_processors` 列表是实例级的，不应在多个 agent 或多次 invocation 间共享。

**`_run_async_impl` 的三段式**：

```python
# src/google/adk/agents/llm_agent.py:466-504（精简）
async def _run_async_impl(self, ctx):
    agent_state = self._load_agent_state(ctx, BaseAgentState)

    # 段 1：恢复逻辑 —— 如果之前转移到了某个子 agent，接着跑它
    if agent_state is not None and (agent_to_transfer := self._get_subagent_to_resume(ctx)):
        async with Aclosing(agent_to_transfer.run_async(ctx)) as agen:
            async for event in agen:
                yield event
        ctx.set_agent_state(self.name, end_of_agent=True)
        yield self._create_agent_state_event(ctx)
        return

    # 段 2：主循环 —— 委托给 _llm_flow
    should_pause = False
    async with Aclosing(self._llm_flow.run_async(ctx)) as agen:
        async for event in agen:
            self.__maybe_save_output_to_state(event)     # output_key → state_delta
            yield event
            if ctx.should_pause_invocation(event):
                should_pause = True                      # 遇到长运行工具 → 暂停
    if should_pause:
        return

    # 段 3：标记 agent 结束
    if ctx.is_resumable:
        events = ctx._get_events(current_invocation=True, current_branch=True)
        if events and any(ctx.should_pause_invocation(e) for e in events[-2:]):
            return                                       # 最后两事件仍然是长运行 → 不要结束
        ctx.set_agent_state(self.name, end_of_agent=True)
        yield self._create_agent_state_event(ctx)
```

三个关键点：

1. **恢复优先**：先看上次 invocation 是否转移给了某个子 agent 没跑完——通过 `_get_subagent_to_resume`（[`llm_agent.py:727-770`](../src/google/adk/agents/llm_agent.py)）从事件流反查。找到就跑它、结束自己。
2. **主循环是 flow**：`_llm_flow.run_async(ctx)` 是 LLM agent 的"心脏"——下一章专门写。
3. **延迟暂停**：遇到 long-running tool call 时 `should_pause=True`，但**不立即 break**——让当前 flow 把 tool call event 完整 yield 完，再退出；否则会留下一个"有 call 没 response"的破碎链。

`_run_live_impl` 则简单得多——它只把 `_llm_flow.run_live(ctx)` 的事件透传，因为 live 模式下的恢复模型尚不完善（见 4.1.4 节说明）。

**为什么要把 LlmAgent 的逻辑分到 Flow 里？** 因为 "LLM 交互" 的复杂度和 "Agent 编排" 的复杂度不在一个量级——`BaseLlmFlow` 本身就有 1000+ 行（见第 6 章）。把它独立成类，便于：

- **单独测试**：Flow 可以用 MockLlm 独立测试，不依赖 agent 结构。
- **复用**：SingleFlow 被 AutoFlow 继承；未来可以再派生 CustomFlow 注入新的处理器。
- **替换**：高级用户甚至可以给 LlmAgent 传入自己的 flow 实例（虽然目前不是公开 API）。

### 4.3 编排器：LoopAgent / ParallelAgent / SequentialAgent

这三个 agent 的共同点是：**它们自己不调用 LLM**，而是通过编排子 agent 产生行为。它们的 `_run_async_impl` 里没有任何 `llm_request` 或 `call_llm`——只有 `sub_agent.run_async(ctx)` 的循环调用。

#### 4.3.1 LoopAgent：带终止条件的循环

[`loop_agent.py:52-168`](../src/google/adk/agents/loop_agent.py) 的 `LoopAgent` 把子 agent 列表当作"一轮工作"，反复执行直到满足终止条件。

```python
# src/google/adk/agents/loop_agent.py:69-124（精简）
async def _run_async_impl(self, ctx):
    if not self.sub_agents:
        return

    agent_state = self._load_agent_state(ctx, LoopAgentState)
    is_resuming_at_current_agent = agent_state is not None
    times_looped, start_index = self._get_start_state(agent_state)

    should_exit = False
    pause_invocation = False
    while (not self.max_iterations or times_looped < self.max_iterations) \
            and not (should_exit or pause_invocation):
        for i in range(start_index, len(self.sub_agents)):
            sub_agent = self.sub_agents[i]

            if ctx.is_resumable and not is_resuming_at_current_agent:
                agent_state = LoopAgentState(
                    current_sub_agent=sub_agent.name,
                    times_looped=times_looped,
                )
                ctx.set_agent_state(self.name, agent_state=agent_state)
                yield self._create_agent_state_event(ctx)

            is_resuming_at_current_agent = False

            async with Aclosing(sub_agent.run_async(ctx)) as agen:
                async for event in agen:
                    yield event
                    if event.actions.escalate:
                        should_exit = True
                    if ctx.should_pause_invocation(event):
                        pause_invocation = True

            if should_exit or pause_invocation:
                break  # 跳出当前这一轮

        if not pause_invocation:
            start_index = 0
            times_looped += 1
            ctx.reset_sub_agent_states(self.name)  # 每跑完一轮重置子 agent 状态
```

**终止条件有三个**：

1. **`event.actions.escalate=True`**：某个子 agent 通过 `exit_loop_tool`（[`tools/exit_loop_tool.py`](../src/google/adk/tools/exit_loop_tool.py)）或在 callback 里显式设置 `escalate=True`，表明"任务已完成或无需继续"。
2. **`times_looped >= max_iterations`**：若设置了 `max_iterations`，达到上限自动退出；不设则可以无限循环。
3. **`ctx.should_pause_invocation(event)`**：遇到长运行工具调用，保存状态然后暂停。

**关键设计**：每轮循环结束时**重置所有子 agent 的 state**（`ctx.reset_sub_agent_states(self.name)`）——下一轮每个子 agent 都是"新鲜"的，不会被上一轮的 agent_state 干扰。这意味着 Loop 内的 agent 默认**无状态**，任何需要跨轮保留的信息要写入 session state。

**恢复语义**：`_get_start_state(agent_state)`（[`loop_agent.py:126-147`](../src/google/adk/agents/loop_agent.py)）从 `LoopAgentState` 里解出 `times_looped` 和 `current_sub_agent` 的索引位置。一个边界情况：如果 `current_sub_agent` 在子 agent 列表里找不到（通常是代码更新后删掉了某个子 agent），logger warning 并从头开始——不抛错，保证系统向前兼容。

**典型场景**：ReAct 式智能体（Reason → Act → Observe 循环）、逐步细化答案、直到满足验收标准才出口。

**Live 模式不支持**：`_run_live_impl` 里直接 `raise NotImplementedError`。音频实时流下的"循环"语义不清晰，目前不做。

#### 4.3.2 ParallelAgent：分支隔离 + queue 合并

[`parallel_agent.py:150-216`](../src/google/adk/agents/parallel_agent.py) 让子 agent 并发运行，适合"不同角度同时分析"这类场景。

它的实现涉及两个关键机制：**分支隔离**和**事件交错合并**。

**分支隔离**：

```python
# src/google/adk/agents/parallel_agent.py:35-48
def _create_branch_ctx_for_sub_agent(agent, sub_agent, invocation_context):
    invocation_context = invocation_context.model_copy()
    branch_suffix = f'{agent.name}.{sub_agent.name}'
    invocation_context.branch = (
        f'{invocation_context.branch}.{branch_suffix}'
        if invocation_context.branch
        else branch_suffix
    )
    return invocation_context
```

每个子 agent 拿到的 `InvocationContext` 都有**独立的 branch 路径**，格式如 `parallel_1.analyst_1`、`parallel_1.analyst_2`。`branch` 字段会被塞到它们产生的每个 Event 上。后续 `contents` processor 组装 LLM 请求时，按 branch 过滤事件——**每个子 agent 只看到自己分支的历史**，看不到兄弟分支的对话。

这个设计的直接好处：两个并行跑的 `analyst_1` 和 `analyst_2` 不会互相污染，各自基于用户原始输入独立工作，然后由后续 agent 汇总两者结果。

**事件合并**（`_merge_agent_run`）：

```python
# src/google/adk/agents/parallel_agent.py:51-86（精简，Python 3.11+ 版本）
async def _merge_agent_run(agent_runs):
    sentinel = object()
    queue = asyncio.Queue()

    async def process_an_agent(events_for_one_agent):
        try:
            async for event in events_for_one_agent:
                resume_signal = asyncio.Event()
                await queue.put((event, resume_signal))
                await resume_signal.wait()         # 等 runner 消费完再继续产事件
        finally:
            await queue.put((sentinel, None))

    async with asyncio.TaskGroup() as tg:
        for events_for_one_agent in agent_runs:
            tg.create_task(process_an_agent(events_for_one_agent))

        sentinel_count = 0
        while sentinel_count < len(agent_runs):
            event, resume_signal = await queue.get()
            if event is sentinel:
                sentinel_count += 1
            else:
                yield event
                resume_signal.set()                 # 告诉生产者：可以继续产下一个
```

机制解读：

- **每个子 agent 一个 task**，通过 `asyncio.TaskGroup` 统一管理（自动级联取消、异常传播）。
- **共享一条 `asyncio.Queue`**：所有 task 把自己产的事件 `put` 进去，主协程 `get` 并 yield。
- **"产-消"同步**：每条事件携带一个 `resume_signal`（`asyncio.Event`），生产者 put 后 `await resume_signal.wait()`，直到主协程消费该事件并 `set()` 信号——生产者才继续产下一条。
  - **这个"背压"机制很关键**：防止一个快速子 agent 疯狂产事件撑爆队列，同时保证事件被消费的顺序是**严格的先来先到**，而不是最后一起冲刷。
- **`sentinel` 标记子 agent 完成**：每个 task 结束时 put 一个哨兵对象。主协程收到 N 个哨兵（N = 子 agent 数）后结束。
- **Python 3.10 版本**（`_merge_agent_run_pre_3_11`）用手写的 task 管理替代 TaskGroup，语义相同但代码更啰嗦。

**恢复**：ParallelAgent 的 agent_state 只是 `BaseAgentState`，作为"我启动过"的标记。真正的恢复由各子 agent 各自维护（通过各自分支的事件流）。`if ctx.is_resumable and all(end_of_agents.get(x) for x in sub_agents)` 时整个 ParallelAgent 才算结束。

**只跳过未完成的子 agent**：`if not sub_agent_ctx.end_of_agents.get(sub_agent.name): agent_runs.append(...)` ——已完成的子 agent 在恢复时不会被重复启动。

**典型场景**：多视角评审、A/B 模型对比、同一任务让不同 agent 竞速，后接汇总 agent。

#### 4.3.3 SequentialAgent：顺序执行与 Live 下的 task_completed

[`sequential_agent.py:48-92`](../src/google/adk/agents/sequential_agent.py) 是三个编排器里最直白的——一个接一个跑子 agent，前一个结束后一个开始。

```python
# src/google/adk/agents/sequential_agent.py:54-92（精简）
async def _run_async_impl(self, ctx):
    if not self.sub_agents:
        return

    agent_state = self._load_agent_state(ctx, SequentialAgentState)
    start_index = self._get_start_index(agent_state)   # 恢复时直接跳到目标 agent

    pause_invocation = False
    resuming_sub_agent = agent_state is not None
    for i in range(start_index, len(self.sub_agents)):
        sub_agent = self.sub_agents[i]
        if not resuming_sub_agent:
            if ctx.is_resumable:
                ctx.set_agent_state(
                    self.name,
                    agent_state=SequentialAgentState(current_sub_agent=sub_agent.name),
                )
                yield self._create_agent_state_event(ctx)

        async with Aclosing(sub_agent.run_async(ctx)) as agen:
            async for event in agen:
                yield event
                if ctx.should_pause_invocation(event):
                    pause_invocation = True

        if pause_invocation:
            return

        resuming_sub_agent = False
```

**状态与恢复**：`SequentialAgentState` 只需要记录一个字段 `current_sub_agent`——因为顺序执行没有"循环次数"这种额外状态。`_get_start_index(agent_state)`（[`sequential_agent.py:94-117`](../src/google/adk/agents/sequential_agent.py)）按名字找到在 `sub_agents` 列表中的索引。如果 `current_sub_agent` 是空字符串，说明整个流程已结束（返回 `len(sub_agents)` 让 for 循环不执行）；找不到则 warning 并从头来。

**数据串联**：Sequential 的典型用法是 agent 之间通过 `output_key` 传递结果：

```python
stage1 = LlmAgent(name='extract', instruction='...', output_key='entities')
stage2 = LlmAgent(name='verify', instruction='Check: {entities}', output_key='verified')
stage3 = LlmAgent(name='respond', instruction='Compose answer from: {verified}')

pipeline = SequentialAgent(sub_agents=[stage1, stage2, stage3])
```

每个 stage 的输出写入 session state，下一 stage 的 instruction 里用 `{entities}` 占位符引用——这是最常用的多 agent 协作模式。

**Live 模式：`task_completed` 函数注入**：

Sequential 是**唯一**实现了 `_run_live_impl` 的编排器，但方式很巧妙。因为 live 音频流没有天然的"轮次结束"信号——用户一直在说话，模型一直在听——框架需要让 agent **显式声明"我完成了"**。做法是给每个子 LlmAgent 自动注入一个工具：

```python
# src/google/adk/agents/sequential_agent.py:138-154
for sub_agent in self.sub_agents:
    def task_completed():
        """Signals that the agent has successfully completed the user's question
        or task."""
        return 'Task completion signaled.'

    if isinstance(sub_agent, LlmAgent):
        if task_completed.__name__ not in sub_agent.tools:
            sub_agent.tools.append(task_completed)
            sub_agent.instruction += f"""If you finished the user's request
            according to its description, call the {task_completed.__name__} function
            to exit so the next agents can take over. When calling this function,
            do not generate any text other than the function call."""
```

模型在 live 模式下被训练"**任务完成时调 `task_completed()`**"，ADK 检测到这个调用后主动结束当前 agent、转向下一个。这个解法虽然"土"（直接拼字符串到 instruction），但**不需要改动 LLM 协议或 Flow 核心逻辑**，是最小侵入的 live 顺序流实现。

**对比三个编排器的核心差异**：

| 编排器 | 并发度 | 分支隔离 | 终止条件 | 恢复字段 | Live 支持 |
|---|---|---|---|---|---|
| LoopAgent | 单线 | 无 | escalate / max_iterations | `times_looped` + `current_sub_agent` | ❌ |
| ParallelAgent | 多子 agent 并发 | **是**（branch 隔离） | 全部子 agent 完成 | 仅 `BaseAgentState` 标记 | ❌ |
| SequentialAgent | 单线 | 无 | 全部子 agent 完成 | `current_sub_agent` | ✅（注入 task_completed） |

三者都可以任意嵌套——用 `SequentialAgent(sub_agents=[LlmAgent1, ParallelAgent(sub_agents=[...]), LoopAgent(sub_agents=[...])])` 表达复杂工作流是完全合法的。

---

## 5. InvocationContext 与 Event

### 5.1 InvocationContext：一次调用的"手提箱"

[`invocation_context.py:100`](../src/google/adk/agents/invocation_context.py) 的 `InvocationContext` 是 ADK 最重要的内部对象之一。它**不跨 invocation 持久化**，但贯穿一次 invocation 中所有 agent / flow / tool / callback，承载它们相互传递数据所需的一切。

ADK 通过 `InvocationContext` 的 docstring（[`invocation_context.py:100-138`](../src/google/adk/agents/invocation_context.py)）给出了清晰的语义分层：

```
┌─────────────────────── invocation ──────────────────────────┐
┌──────────── llm_agent_call_1 ────────────┐ ┌─ agent_call_2 ─┐
┌──── step_1 ────────┐ ┌───── step_2 ──────┐
[call_llm] [call_tool] [call_llm] [transfer]
```

- **Invocation** 始于用户消息、终于最终响应；由 `Runner.run_async` 处理。
- **Agent call** 是某个 agent 跑完一次 `run_async`；一次 invocation 可以包含多次 agent call（当有 transfer 时）。
- **Step** 是一次 LLM 调用加上它触发的工具调用；LLM agent call 由一或多个 step 组成。

`InvocationContext` 的字段按职责分为**四类**：

**① 服务句柄（构造后只读）**

```python
# src/google/adk/agents/invocation_context.py:146-150
artifact_service: Optional[BaseArtifactService] = None
session_service: BaseSessionService                  # 唯一必填
memory_service: Optional[BaseMemoryService] = None
credential_service: Optional[BaseCredentialService] = None
context_cache_config: Optional[ContextCacheConfig] = None
```

这些服务由 Runner 构造时传入 Runner，再由 Runner 在 `_setup_context_for_*` 时赋给 ctx。Agent 和 tool 通过 ctx 读写服务，而不直接依赖 Runner——这保证了**核心逻辑与服务实现解耦**。

**② Invocation 身份与当前执行位置**

```python
# src/google/adk/agents/invocation_context.py:152-168
invocation_id: str
branch: Optional[str] = None                         # 并行分支路径
agent: BaseAgent                                     # 当前正在跑的 agent
user_content: Optional[types.Content] = None         # 启动本次 invocation 的用户输入
session: Session                                     # 当前 session（对象，非 ID）
```

`agent` 字段会随 agent 切换动态变化——每个子 agent 开始跑时，`_create_invocation_context` 会 `model_copy(update={'agent': self})` 派生一个新 ctx，父 ctx 仍指向父 agent。这是"手提箱"语义的来源：每层 agent 拿到的是自己版本的 ctx 副本，互不干扰。

`branch` 在 `ParallelAgent` 下启用（见 4.3.2），格式 `parallel.analyst_1`。`contents` processor 组装 LLM 请求时会**按 branch 过滤事件**——未设 branch 的事件对所有分支可见，设了 branch 的只对同分支或其祖先分支可见。

**③ Agent 状态与恢复控制**

```python
# src/google/adk/agents/invocation_context.py:170-180, 202-209
agent_states: dict[str, dict[str, Any]] = Field(default_factory=dict)
end_of_agents: dict[str, bool] = Field(default_factory=dict)
end_invocation: bool = False                         # callback/tool 可设置为 True 终止
resumability_config: Optional[ResumabilityConfig] = None
events_compaction_config: Optional[EventsCompactionConfig] = None
token_compaction_checked: bool = False               # 防止一次 invocation 做两次 token 检查
```

`agent_states` 和 `end_of_agents` 是可恢复机制的核心（见 4.1.4）。`end_invocation=True` 是一个"紧急终止"开关——任何 callback 或 tool 都可以设为 True，Runner 在下一个 yield 点检查并终止整个 invocation。

**④ Live 模式与实时缓存**

```python
# src/google/adk/agents/invocation_context.py:181-197
live_request_queue: Optional[LiveRequestQueue] = None
active_streaming_tools: Optional[dict[str, ActiveStreamingTool]] = None
transcription_cache: Optional[list[TranscriptionEntry]] = None
live_session_resumption_handle: Optional[str] = None
input_realtime_cache: Optional[list[RealtimeCacheEntry]] = None
output_realtime_cache: Optional[list[RealtimeCacheEntry]] = None
```

这组字段只在 `run_live` 路径里用。`live_request_queue` 是用户输入的入口、`active_streaming_tools` 记录后台运行的流式工具、几个 cache 是**在刷到 session / artifact 之前**临时攒的音频块。音频最终会被打包成 artifact（见 3.4 节的缓冲策略）。

**⑤ 其他辅助字段**

```python
# src/google/adk/agents/invocation_context.py:199-215
run_config: Optional[RunConfig] = None               # 调用方传入的运行时配置
plugin_manager: PluginManager                        # 插件管理器
canonical_tools_cache: Optional[list[BaseTool]] = None   # 工具列表缓存（本次 invocation 内有效）
_invocation_cost_manager: _InvocationCostManager     # 私有属性，追踪 LLM 调用次数 / token 成本
```

`canonical_tools_cache` 是性能优化——LlmAgent 的 `canonical_tools(ctx)` 第一次调用时展开所有 Toolset、构造所有 FunctionTool，结果存这里，同 invocation 内后续访问直接读缓存。

**设计哲学**：`InvocationContext` 是一个**仅在运行时存在的可变数据容器**，不序列化到存储（事件才序列化）。它把"服务依赖 + invocation 元数据 + agent 运行时状态 + live 缓冲"统一放到一个对象里，避免各层方法签名爆炸。代价是类很大（40+ 字段）——但这是"参数压缩"的合理结果。

### 5.2 agent_states 与恢复重建

可恢复性的实现分布在 `InvocationContext` 的三个方法里。理解它们就能理解 ADK 的"从事件流重放 agent 状态"机制。

**`set_agent_state`**：写入接口

```python
# src/google/adk/agents/invocation_context.py:232-262
def set_agent_state(
    self,
    agent_name: str,
    *,
    agent_state: Optional[BaseAgentState] = None,
    end_of_agent: bool = False,
) -> None:
    if end_of_agent:
        self.end_of_agents[agent_name] = True
        self.agent_states.pop(agent_name, None)       # agent 结束 → 清状态
    elif agent_state is not None:
        self.agent_states[agent_name] = agent_state.model_dump(mode="json")
        self.end_of_agents[agent_name] = False
    else:
        self.end_of_agents.pop(agent_name, None)
        self.agent_states.pop(agent_name, None)       # 两个都 None → 完全清空
```

三种调用模式：

- `set_agent_state(name, end_of_agent=True)` — 标记 agent 结束（清空状态）。
- `set_agent_state(name, agent_state=SomeState(...))` — 设置 checkpoint（清空 end_of_agent 标记）。
- `set_agent_state(name)` — 完全清空（允许 agent 重新跑）。

**`populate_invocation_agent_states`**：读取接口（从事件流重建）

```python
# src/google/adk/agents/invocation_context.py:283-312
def populate_invocation_agent_states(self) -> None:
    if not self.is_resumable:
        return
    for event in self._get_events(current_invocation=True):
        if event.actions.end_of_agent:
            self.end_of_agents[event.author] = True
            self.agent_states.pop(event.author, None)
        elif event.actions.agent_state is not None:
            self.agent_states[event.author] = event.actions.agent_state
            self.end_of_agents[event.author] = False
        elif (
            event.author != "user"
            and event.content
            and not self.agent_states.get(event.author)
        ):
            # 非 workflow agent（LlmAgent）没有显式 state，但产生过内容 —— 标记为"跑过"
            self.agent_states[event.author] = BaseAgentState()
            self.end_of_agents[event.author] = False
```

这段在 `_setup_context_for_resumed_invocation` 里被调用。它遍历当前 invocation 的所有事件（不只最新的），**按顺序回放**：

1. 遇到 `end_of_agent=True` 事件 → 该 agent 结束。
2. 遇到 `agent_state` 非空事件 → 更新该 agent 的状态快照。
3. 遇到非 user 且有内容的事件 → 若该 agent 没有显式状态，给一个空 state 作占位（表示"它开始跑过，但没 checkpoint 过"）。

**为什么要从事件流重建，而不是直接读 session 的某个字段？** 因为 session 本身不持有"agent states"——ADK 的**唯一真相源**就是事件流。任何 agent 级状态都必须以事件为载体。这保证了存储层的简洁：session 只需要 append_event，不需要理解"agent 状态"是什么。

**`reset_sub_agent_states`**：循环重置

```python
# src/google/adk/agents/invocation_context.py:264-281
def reset_sub_agent_states(self, agent_name: str) -> None:
    agent = self.agent.find_agent(agent_name)
    if not agent:
        return
    for sub_agent in agent.sub_agents:
        self.set_agent_state(sub_agent.name)           # 清空直接子 agent
        self.reset_sub_agent_states(sub_agent.name)    # 递归清空孙子 agent
```

只有 LoopAgent 在每轮循环间调用它。递归清空整棵子树，让下一轮所有子 agent 都以"全新状态"开始跑。

**一次恢复 invocation 的完整流程**：

```
1. 调用方：runner.run_async(session_id=..., invocation_id=OLD_ID)
2. Runner._setup_context_for_resumed_invocation:
   a. 创建 InvocationContext，agent=root_agent
   b. ctx.populate_invocation_agent_states()  ← 从事件流重建所有 agent_states
3. ctx.agent = _find_agent_to_run(...)       ← 基于 agent_states 确定从哪个 agent 恢复
4. 如果 ctx.end_of_agents[ctx.agent.name] = True，直接 return（无事可做）
5. 否则进入 agent.run_async(ctx)
   a. LoopAgent / SequentialAgent 从 ctx.agent_states 读出自己的 state
   b. 根据 state 的 current_sub_agent / times_looped 决定从哪开始
   c. LlmAgent 通过 _get_subagent_to_resume 找到曾转移过的子 agent
6. 继续执行直到遇到下一个 long-running tool 或完成整个 invocation
```

**关键约束**：ADK 官方文档强调"at-least-once"语义——如果恢复时某个 tool 被重复调用，agent 的容错应该靠 **tool 本身的幂等性**来保证，而不是 ADK 去重。这个折中让整个恢复机制保持简洁。

### 5.3 Event 与 EventActions：会话的最小原子

[`events/event.py:31`](../src/google/adk/events/event.py) 的 `Event` 是 session 事件流的基础单位。它继承 `LlmResponse`，在 LLM 响应字段的基础上加了会话级元信息。

```python
# src/google/adk/events/event.py:31-75
class Event(LlmResponse):
    invocation_id: str = ''                            # 所属 invocation
    author: str                                        # 'user' 或 agent 名
    actions: EventActions = Field(default_factory=EventActions)
    long_running_tool_ids: Optional[set[str]] = None   # 长运行 tool 的 function call ID 集合
    branch: Optional[str] = None                       # 并行分支路径
    id: str = ''                                       # 事件唯一 ID（自动生成）
    timestamp: float = Field(default_factory=lambda: platform_time.get_time())
```

**继承自 `LlmResponse` 的字段**（[`models/llm_response.py`](../src/google/adk/models/llm_response.py)）：

- `content: Optional[types.Content]`：消息内容（文本、图片、function_call、function_response 等 parts）。
- `partial: Optional[bool]`：流式中间块标记（`None`/`False` 算非 partial）。
- `turn_complete: Optional[bool]`：Live 模式下的轮次完成信号。
- `interrupted: Optional[bool]`：用户打断模型说话。
- `error_code / error_message`：LLM 或工具异常。
- `finish_reason`：STOP / MAX_TOKENS / SAFETY 等。
- `input_transcription / output_transcription`：Live 模式音频转录。
- `grounding_metadata`：Google Search grounding 等溯源信息。
- `custom_metadata: dict`：业务透传字段。
- `usage_metadata`：Token 消耗计数。

**Event 核心方法**：

```python
# src/google/adk/events/event.py:83-125
def is_final_response(self) -> bool:
    """是否最终响应（可结束本轮 LLM step）。"""
    if self.actions.skip_summarization or self.long_running_tool_ids:
        return True
    return (
        not self.get_function_calls()
        and not self.get_function_responses()
        and not self.partial
        and not self.has_trailing_code_execution_result()
    )

def get_function_calls(self) -> list[types.FunctionCall]:
    """从 content.parts 里过滤出 function_call。"""

def get_function_responses(self) -> list[types.FunctionResponse]:
    """从 content.parts 里过滤出 function_response。"""

def has_trailing_code_execution_result(self) -> bool:
    """最后一个 part 是否是代码执行结果。"""
```

`is_final_response` 的逻辑值得注意——它不是简单"有 content 就算 final"，还需要：**没有 function call / response、非 partial、尾部不是代码执行结果**。这个判定决定了 `BaseLlmFlow.run_async` 什么时候退出循环（见 6.1 节）。

**`EventActions`：事件携带的"副作用"声明**

```python
# src/google/adk/events/event_actions.py:51-114
class EventActions(BaseModel):
    skip_summarization: Optional[bool] = None            # 对 function_response 生效：不让模型总结
    state_delta: dict[str, object] = Field(default_factory=dict)
    artifact_delta: dict[str, int] = Field(default_factory=dict)   # filename -> version
    transfer_to_agent: Optional[str] = None              # 转移到哪个 agent
    escalate: Optional[bool] = None                      # 循环退出信号
    requested_auth_configs: dict[str, AuthConfig]        # 请求 OAuth / API key
    requested_tool_confirmations: dict[str, ToolConfirmation]    # 请求人工确认
    compaction: Optional[EventCompaction] = None         # 此事件是一条压缩摘要
    end_of_agent: Optional[bool] = None                  # 标记 agent 结束
    agent_state: Optional[dict[str, Any]] = None         # Agent checkpoint
    rewind_before_invocation_id: Optional[str] = None    # 回滚 marker
    render_ui_widgets: Optional[list[UiWidget]] = None   # 给 UI 的组件数据
```

`EventActions` 的每个字段都是一条**"除了内容本身之外，这个事件还要做什么"**的声明。它们在框架的不同位置被读取：

| 字段 | 谁在读 | 作用 |
|---|---|---|
| `state_delta` | `session_service.append_event` | 把 delta 合并到 `session.state`（`app:` / `user:` 前缀走全局、`temp:` 前缀只活在 invocation 内） |
| `artifact_delta` | 同上 | 记录本次事件产生了哪些 artifact 版本 |
| `transfer_to_agent` | `agent_transfer` processor | 决定下一个跑哪个 agent |
| `escalate` | `LoopAgent._run_async_impl` | 跳出循环 |
| `end_of_agent` / `agent_state` | `Runner._setup_context_for_resumed_invocation` | 恢复时重建 agent 状态 |
| `requested_auth_configs` | `Runner._exec_with_plugin` → 调用方 UI | 弹出登录窗口；用户填完后再发 function response 继续 |
| `requested_tool_confirmations` | 同上 | 弹出确认弹窗 |
| `compaction` | `contents` processor | 组装 LLM 请求时用摘要替换原始事件 |
| `skip_summarization` | `BaseLlmFlow` | 长运行 tool 的 response 不触发后续 LLM 总结 |
| `rewind_before_invocation_id` | `_get_events` 过滤 | 识别回滚 marker，跳过被回滚的事件 |

**`Event` 的 Pydantic 配置细节**：

```python
# src/google/adk/events/event.py:38-44
model_config = ConfigDict(
    extra='ignore',
    ser_json_bytes='base64',               # bytes 类型用 base64 序列化
    val_json_bytes='base64',               # 反序列化时同样
    alias_generator=alias_generators.to_camel,  # 序列化时字段名转 camelCase
    populate_by_name=True,                 # 但反序列化时接受 snake_case 也接受 camelCase
)
```

`alias_generator=to_camel` 是 ADK **wire format 为 camelCase** 的技术根源——`actions.stateDelta` 发往 FastAPI、`actions.state_delta` 在 Python 代码里用，Pydantic 自动转换。这是 ADK 明确承诺的公共 API 契约之一。

**ID 自动生成**：`model_post_init` 里如果 `id` 为空就调 `Event.new_id()` 生成一个 UUID。ID 在事件落盘时保证唯一；如果事件在 `_exec_with_plugin` 里被修改（比如插件重写了内容），`_run_one_step_async` 会刷新 id（[`base_llm_flow.py:894-896`](../src/google/adk/flows/llm_flows/base_llm_flow.py)），避免两个不同内容的事件共享同一个 ID。

**设计哲学**：Event 把"内容 + 动作"一起塞到同一个对象里——这让 ADK 能用**纯数据**表达"这个事件发生时，系统还做了什么副作用"。Runner / Flow / Agent 都不需要显式调用"设置 state"、"请求认证"这类 side-channel 操作；只要 yield 一个带正确 `actions` 的 Event，框架就会在合适的地方处理。这是整个框架最核心的解耦手段之一。

---

## 6. LLM Flow：交互机器

### 6.1 BaseLlmFlow.run_async：外层循环与终止条件

[`flows/llm_flows/base_llm_flow.py`](../src/google/adk/flows/llm_flows/base_llm_flow.py) 是整个 ADK 最密集的一个文件——1400+ 行代码把"一次 LLM 对话 + 工具调用"的全部细节集中在一起。`BaseLlmFlow` 是抽象基类，`SingleFlow` / `AutoFlow` 通过**填充 processor 列表**来特化它。

`run_async` 的外层逻辑极其精简：

```python
# src/google/adk/flows/llm_flows/base_llm_flow.py:799-812
async def run_async(self, invocation_context):
    """Runs the flow."""
    while True:
        last_event = None
        async with Aclosing(self._run_one_step_async(invocation_context)) as agen:
            async for event in agen:
                last_event = event
                yield event
        if not last_event or last_event.is_final_response() or last_event.partial:
            if last_event and last_event.partial:
                logger.warning('The last event is partial, which is not expected.')
            break
```

核心概念：**一次 step = 一次 LLM 调用 + 它触发的所有 tool 调用**。`run_async` 的外层就是"反复跑 step，直到某一步产出一个 final response"。

**终止条件（退出外层 while）**：

1. **没有事件**（`not last_event`）：step 产不出任何事件，通常意味着 end_invocation 被设置或 error，直接退出。
2. **最终响应**（`last_event.is_final_response()`）：根据 5.3 节的定义，这意味着"没有待处理的 function call / response、非 partial、尾部不是代码执行结果"。
3. **partial 异常**（`last_event.partial`）：最后一条事件如果是 partial，说明有 bug（warning 后退出）。正常情况下每个 step 的最后一条一定是完整事件。

**不退出的情况**：

- LLM 返回 function call → tool 执行完产生 function response → 下一步重新调 LLM 让它基于 tool 结果生成回复。
- LLM 返回代码执行结果（通过内置 code executor） → 结果不是"最终回复"，需要再一步让 LLM 基于执行结果总结。
- LLM 返回 `transfer_to_agent(...)` → 外层 Runner 感知后切换 agent，但当前 flow 已结束。

**一次 step 的三段结构**（`_run_one_step_async` [`base_llm_flow.py:814-897`](../src/google/adk/flows/llm_flows/base_llm_flow.py)）：

```python
# src/google/adk/flows/llm_flows/base_llm_flow.py:814-897（精简）
async def _run_one_step_async(self, invocation_context):
    llm_request = LlmRequest()

    # ① preprocess：跑所有 request processor，组装 LlmRequest
    async with Aclosing(self._preprocess_async(invocation_context, llm_request)) as agen:
        async for event in agen:
            yield event
    if invocation_context.end_invocation:
        return

    # 可恢复模式的特殊路径：最后一个事件是 function call 且已有 response
    # → 跳过 LLM 调用，直接执行剩余的 function call
    events = invocation_context._get_events(current_invocation=True, current_branch=True)
    if invocation_context.is_resumable and events and events[-1].get_function_calls():
        model_response_event = events[-1]
        async with Aclosing(
            self._postprocess_handle_function_calls_async(
                invocation_context, model_response_event, llm_request
            )
        ) as agen:
            async for event in agen:
                event.id = Event.new_id()
                yield event
            return

    # ② call_llm：构造一个"占位" model_response_event（id / invocation_id / author / branch 提前填好）
    model_response_event = Event(
        id=Event.new_id(),
        invocation_id=invocation_context.invocation_id,
        author=invocation_context.agent.name,
        branch=invocation_context.branch,
    )
    async with Aclosing(
        self._call_llm_async(invocation_context, llm_request, model_response_event)
    ) as agen:
        async for llm_response in agen:
            # ③ postprocess：跑 response processor，yield 最终事件，派发 tool 调用
            async with Aclosing(
                self._postprocess_async(
                    invocation_context, llm_request, llm_response, model_response_event
                )
            ) as agen:
                async for event in agen:
                    model_response_event.id = Event.new_id()
                    model_response_event.timestamp = platform_time.get_time()
                    yield event
```

三阶段语义：

- **Preprocess**：不调 LLM，只组装 `LlmRequest`——把 instruction、历史 content、tools 声明、cache 配置等一切模型需要的信息塞进去。具体由 **processor 责任链**完成，下节展开。
- **Call LLM**：`_call_llm_async` 调用 `invocation_context.agent.canonical_model.generate_content_async(llm_request, stream=...)`，产出一或多个 `LlmResponse`（stream=True 时多个 partial + 一个 final；stream=False 时一个）。
- **Postprocess**：response processor 可能修改响应（例如把 planning 内容标记为 thought）；然后框架构造最终 `Event`（合并 LlmResponse 字段）；若响应里有 function call，派发对应 tool（见 6.3 节）。

**`model_response_event` 为什么预先创建？** 它是一个**可变的"容器"**，在整个 step 中不断被填充——stream 时每个 partial 都复用这个 event（只换 id 和 timestamp），final 时由 `_finalize_model_response_event` 合并 LlmResponse 的内容字段到它。这种写法避免了"流式过程中每次都新建 Event 对象"的开销，同时保留了事件身份的连续性（invocation_id / branch / author 不变）。

**id 刷新**：每次 yield 前 `model_response_event.id = Event.new_id()`——这很关键。ADK 的 session 会用 id 去重/排序，如果 stream 的多个 partial 共享同一个 id，session 就会覆盖前一个；刷新 id 让每个 yield 的事件都是独立的记录（但它们的 invocation_id / timestamp seq 保证了消费方能知道"这是同一个回复的分块"）。

### 6.2 Processor 责任链：SingleFlow 与 AutoFlow

Flow 的"行为差异"都集中在 processor 列表上。`BaseLlmFlow` 本身不规定处理器，`SingleFlow` 和 `AutoFlow` 分别定义了自己的组合。

**处理器抽象**：

```python
# src/google/adk/flows/llm_flows/_base_llm_processor.py:32-53
class BaseLlmRequestProcessor(ABC):
    @abstractmethod
    async def run_async(
        self, invocation_context: InvocationContext, llm_request: LlmRequest
    ) -> AsyncGenerator[Event, None]:
        ...

class BaseLlmResponseProcessor(ABC):
    @abstractmethod
    async def run_async(
        self, invocation_context: InvocationContext, llm_response: LlmResponse
    ) -> AsyncGenerator[Event, None]:
        ...
```

每个 processor 可以：
- **修改 `llm_request` / `llm_response`**（通过直接赋值字段）
- **yield Event**（例如记录 state 变化、产生认证请求事件）
- **触发 `invocation_context.end_invocation = True`**（终止整个 invocation）

**SingleFlow 的处理器清单**：

```python
# src/google/adk/flows/llm_flows/single_flow.py:38-75
def _create_request_processors():
    return [
        basic.request_processor,
        auth_preprocessor.request_processor,
        request_confirmation.request_processor,
        instructions.request_processor,
        identity.request_processor,
        compaction.request_processor,
        contents.request_processor,
        context_cache_processor.request_processor,
        interactions_processor.request_processor,
        _nl_planning.request_processor,
        _code_execution.request_processor,
        _output_schema_processor.request_processor,
    ]

def _create_response_processors():
    return [
        _nl_planning.response_processor,
        _code_execution.response_processor,
    ]
```

**12 个 request processor 的职责**（按执行顺序）：

| # | Processor | 职责 |
|---|---|---|
| 1 | `basic` | 设置 `llm_request.model` / `config`；根据 agent 字段装配 `output_schema` / `GenerateContentConfig` |
| 2 | `auth_preprocessor` | 扫描上轮事件，若有 `requested_auth_configs` 的 function response，把凭证回填到 tool_context |
| 3 | `request_confirmation` | 检查 `requested_tool_confirmations` 是否已经被用户确认/拒绝，相应地回填到 tool 执行流程 |
| 4 | `instructions` | 把 agent 的 `instruction` / `global_instruction` 解析（替换 `{state_var}` 占位符）后塞进 `config.system_instruction` |
| 5 | `identity` | 添加 agent 身份识别指令（例如 "You are the agent named X."），让多 agent 场景下模型能自我定位 |
| 6 | `compaction` | 从 session 事件里拿到 `actions.compaction` 摘要事件，参与历史装配时替换原始事件（必须在 contents 之前跑） |
| 7 | `contents` | 组装 `llm_request.contents`——按 branch 过滤事件、合并 partial、转换成 `types.Content` 序列 |
| 8 | `context_cache_processor` | 设置 `cache_config` / 检查 `cache_metadata`，启用 Gemini 的 implicit/explicit cache |
| 9 | `interactions_processor` | 提取 `previous_interaction_id`，支持 Gemini Interactions API 的有状态对话 |
| 10 | `_nl_planning` | 如果 agent 配了 `planner`，注入规划指令；或者配置 Gemini 的 `thinking_config` |
| 11 | `_code_execution` | 若 agent 配了 `code_executor`，把历史事件里的数据文件引用重写为代码可访问的路径；必须在 contents 之后（它修改 contents） |
| 12 | `_output_schema_processor` | 若同时有 `output_schema` 和 `tools`（某些模型不支持原生 response schema），注入一个 `set_model_response` 假工具让 LLM 通过 function call 返回结构化结果 |

**2 个 response processor**：

| # | Processor | 职责 |
|---|---|---|
| 1 | `_nl_planning.response_processor` | 把 planner 生成的"思考"内容打上 `thought=True` 标记，UI 可据此分别渲染"推理"和"最终回复" |
| 2 | `_code_execution.response_processor` | 执行响应里的代码块（通过 `BaseCodeExecutor`）；把执行结果作为 part 追加到响应里；后续触发新一步 LLM 调用（让模型基于结果总结） |

**AutoFlow 的增量**：

```python
# src/google/adk/flows/llm_flows/auto_flow.py:42-44
class AutoFlow(SingleFlow):
    def __init__(self):
        super().__init__()
        self.request_processors += [agent_transfer.request_processor]
```

只多一个 `agent_transfer` processor：它给 LlmRequest 注入一个 `transfer_to_agent(agent_name: str)` 函数声明，让 LLM 通过 function call 方式切换 agent。切换方向受 LlmAgent 的 `disallow_transfer_to_parent` / `disallow_transfer_to_peers` 控制（[`auto_flow.py:23-40`](../src/google/adk/flows/llm_flows/auto_flow.py)）：

- parent → sub：始终允许。
- sub → parent：受 `disallow_transfer_to_parent` 控制。
- sub → peer：需要 parent 也是 LlmAgent，且当前 agent 未设 `disallow_transfer_to_peers`。

**处理器执行位置**：

`_preprocess_async`（[`base_llm_flow.py:899-929`](../src/google/adk/flows/llm_flows/base_llm_flow.py)）的实现：

```python
async def _preprocess_async(self, invocation_context, llm_request):
    agent = invocation_context.agent
    for processor in self.request_processors:
        async with Aclosing(processor.run_async(invocation_context, llm_request)) as agen:
            async for event in agen:
                yield event

    # 解析 toolset 的认证（放在所有 processor 之后，保证 auth 凭证已就位）
    async with Aclosing(self._resolve_toolset_auth(invocation_context, agent)) as agen:
        async for event in agen:
            yield event

    if invocation_context.end_invocation:
        return

    await _process_agent_tools(invocation_context, llm_request)  # 把 tools 装进 llm_request.config.tools
```

**关键设计**：

- **单向修改**：每个 processor 按序拿到已经被前面 processor 修改过的 `llm_request`，自己可以继续改。这是一条**可组合的管道**。
- **事件可从 processor 流出**：例如 `auth_preprocessor` 发现 tool 需要 OAuth，直接 yield 一个带 `requested_auth_configs` 的 Event——这个事件会被持久化、被 UI 捕获、用户完成授权后生成 function response，再回到下一轮 step 时 `auth_preprocessor` 看到 response 完成凭证回填。
- **顺序依赖是硬约束**：注释里明确写了 "Compaction should run before contents"、"NL Planning should be after contents"、"Code execution should be after the contents as it mutates the contents"。改 processor 顺序需要小心。
- **processor 是模块级单例**：每个 processor 模块导出一个 `request_processor` 或 `response_processor` 对象（不是工厂函数），在 Flow 实例化时被引用。因为 processor 本身是无状态的——它操作传入的 `llm_request`，不保留任何实例字段。

### 6.3 工具调用派发：并发、长运行、确认

[`flows/llm_flows/functions.py`](../src/google/adk/flows/llm_flows/functions.py) 是 ADK 处理 function call 的集中地。核心入口是 `handle_function_calls_async` / `handle_function_call_list_async`，关键特征是**全部并发执行**。

**并发派发**：

```python
# src/google/adk/flows/llm_flows/functions.py:373-433（精简）
async def handle_function_call_list_async(
    invocation_context, function_calls, tools_dict,
    filters=None, tool_confirmation_dict=None,
) -> Optional[Event]:
    filtered_calls = [fc for fc in function_calls if not filters or fc.id in filters]
    if not filtered_calls:
        return None

    # 每个 function call 一个 task，asyncio.gather 并发跑
    tasks = [
        asyncio.create_task(
            _execute_single_function_call_async(
                invocation_context, function_call, tools_dict, agent,
                tool_confirmation_dict[function_call.id]
                if tool_confirmation_dict else None,
            )
        )
        for function_call in filtered_calls
    ]
    function_response_events = await asyncio.gather(*tasks)
    function_response_events = [e for e in function_response_events if e is not None]

    # 把多个 response event 合并成一个，让 LLM 在同一轮看到所有结果
    merged_event = merge_parallel_function_response_events(function_response_events)
    return merged_event
```

**为什么要并发？** LLM 在一轮响应里经常同时返回多个 function call（比如"查询天气 + 查询汇率 + 发邮件"）。串行跑三次是线性的；并发跑的总时长等于最慢那个的时长。对于 IO 密集的 tool（HTTP 调用、数据库查询），并发是**最大的工程红利**。

**合并响应**：并发执行的多个 function response 会被 `merge_parallel_function_response_events`（[`functions.py:1169`](../src/google/adk/flows/llm_flows/functions.py)）合并为单个 Event——所有响应 part 放在同一个 `content.parts` 里，`actions.state_delta` / `actions.artifact_delta` 也合并。这让下一步 LLM 请求能一次性看到所有 tool 结果，进行统一推理。

**单个 tool 执行流程**（`_execute_single_function_call_async`，简化版）：

```python
# src/google/adk/flows/llm_flows/functions.py:436-610（概念提炼）
async def _execute_single_function_call_async(invocation_context, function_call, tools_dict, agent, tool_confirmation):
    # 1. 查找 tool
    tool = _get_tool(function_call.name, tools_dict)      # 找不到则 raise

    # 2. 构造 ToolContext（CallbackContext 的扩展版）
    tool_context = ToolContext(
        invocation_context=invocation_context,
        function_call_id=function_call.id,
        tool_confirmation=tool_confirmation,
    )

    # 3. before_tool_callback：plugin 先、agent 后
    early_return = await invocation_context.plugin_manager.run_before_tool_callback(
        tool=tool, tool_args=function_call.args, tool_context=tool_context,
    )
    if early_return is None:
        for callback in agent.canonical_before_tool_callbacks:
            early_return = maybe_await(callback(tool=tool, args=..., tool_context=...))
            if early_return is not None:
                break

    # 4. 若被短路，跳过 tool 执行；否则真正调 tool
    if early_return is not None:
        response = early_return
    else:
        try:
            response = await tool.run_async(args=function_call.args, tool_context=tool_context)
        except Exception as e:
            response = await _run_on_tool_error_callbacks(...)    # error callback 可以兜底
            if response is None:
                raise

    # 5. after_tool_callback
    modified = await invocation_context.plugin_manager.run_after_tool_callback(
        tool=tool, tool_args=..., tool_context=..., result=response,
    )
    if modified is None:
        for callback in agent.canonical_after_tool_callbacks:
            ...
    if modified is not None:
        response = modified

    # 6. 长运行 tool 特殊处理
    if tool.is_long_running and response is None:
        return None                   # 只把 function_call 持久化，不产 response

    # 7. 构造 function_response Event
    return __build_response_event(
        tool=tool, function_call_id=function_call.id,
        function_response=response, tool_context=tool_context,
    )
```

**长运行工具（LongRunningFunctionTool）**：

- 声明：`long_running_tool = LongRunningFunctionTool(fn)`，`is_long_running=True`。
- LLM 声明里带警告 "This may take a while"，避免模型在没拿到结果时重复调用。
- 调用时 `run_async` 允许返回 `None`——表示"异步启动了操作，稍后会有回调"。
- 返回 None 时，_execute 函数也返回 None；框架只把 `function_call` 事件落盘，不产 function_response。
- 上层 `ctx.should_pause_invocation` 检测到长运行 call 但无 response，触发 invocation 暂停。
- 外部系统（可能是几分钟后的 webhook）调用 `runner.run_async(invocation_id=..., new_message=function_response)` 恢复。

**工具确认（Tool Confirmation）**：

```python
# 工具声明时：FunctionTool(my_fn, require_confirmation=True)
# 第一次 LLM 调用 tool：
#   1. ADK 检查 require_confirmation=True 且 tool_confirmation 未提供
#   2. ADK 不执行 tool，而是 yield 一个带 actions.requested_tool_confirmations 的事件
#   3. UI 看到事件，弹窗"确认执行？"
#   4. 用户点击确认/拒绝，前端构造一个带 tool_confirmation 的 function response 发回
#   5. 下一步 request_confirmation processor 把确认信息塞到 tool_confirmation_dict
#   6. 再次调用 handle_function_call_list_async，这次带上 tool_confirmation，真正执行
```

`ToolConfirmation` 模型（[`tools/tool_confirmation.py`](../src/google/adk/tools/tool_confirmation.py)）可以携带用户输入——比如确认弹窗可以让用户编辑参数再确认。

**认证请求（AuthConfig）**：

类似的机制处理 tool 的 OAuth / API Key。Tool 在执行时发现凭证缺失，通过 `tool_context.actions.requested_auth_configs[function_call.id] = AuthConfig(...)` 声明"我需要这个凭证"。框架 yield 事件、UI 弹登录窗口、用户完成授权后 exchanged_credential 回填——详见 10 章。

**Live 模式工具派发**：

`handle_function_calls_live`（[`functions.py:612`](../src/google/adk/flows/llm_flows/functions.py)）是 live 模式的变体。两个主要差异：

- **流式工具**：async generator 工具产出的每个值都作为立即响应回传到 LLM；tool 在后台持续产，LLM 可以边听边调整回复。
- **线程池执行阻塞工具**：`_call_tool_in_thread_pool` 把同步阻塞的 tool 扔到 threadpool 里跑，避免卡住 event loop。可通过 `run_config.tool_thread_pool_config` 控制并发数。

### 6.4 Live 模式双向流

`BaseLlmFlow.run_live`（[`base_llm_flow.py:471-650`](../src/google/adk/flows/llm_flows/base_llm_flow.py)）是完全独立的一条路径——它不用 `run_async` 的 step 循环，而是建立一条**持久 WebSocket**，然后用两个并发 task 处理双向数据流。

**整体结构**：

```python
# src/google/adk/flows/llm_flows/base_llm_flow.py:471-650（精简）
async def run_live(self, invocation_context):
    llm_request = LlmRequest()

    # ① preprocess 复用（组装 tools / instructions / cache config）
    async with Aclosing(self._preprocess_async(invocation_context, llm_request)) as agen:
        async for event in agen:
            yield event

    llm = self.__get_llm(invocation_context)

    attempt = 1
    while True:                                          # ② 带重连的外层 while
        try:
            # ③ 若有 session_resumption_handle（重连），带上它
            if invocation_context.live_session_resumption_handle:
                llm_request.live_connect_config.session_resumption = types.SessionResumptionConfig(
                    handle=invocation_context.live_session_resumption_handle,
                    transparent=True,
                )

            async with llm.connect(llm_request) as llm_connection:
                # ④ 首次连接时发送历史；重连则跳过（server 已有 session 状态）
                if llm_request.contents and not invocation_context.live_session_resumption_handle:
                    await llm_connection.send_history(llm_request.contents)

                # ⑤ 启动"发送" task：从 live_request_queue 消费用户输入转发到 llm_connection
                send_task = asyncio.create_task(self._send_to_model(llm_connection, invocation_context))

                try:
                    # ⑥ "接收" task 在主协程里跑，从 llm_connection 读取响应，yield 为 Event
                    async for event in self._receive_from_model(
                        llm_connection, event_id, invocation_context, llm_request
                    ):
                        if not event:
                            break
                        yield event

                        # ⑦ function response 回写给 LLM（让它基于结果继续说话）
                        if event.get_function_responses():
                            invocation_context.live_request_queue.send_content(event.content)

                        # ⑧ transfer_to_agent：关闭当前连接，切换到子 agent 的 live
                        if _is_transfer_to_agent(event):
                            await asyncio.sleep(DEFAULT_TRANSFER_AGENT_DELAY)
                            send_task.cancel()
                            await llm_connection.close()
                            transfer_to_agent = event.actions.transfer_to_agent
                            agent_to_run = self._get_agent_to_run(invocation_context, transfer_to_agent)
                            async for item in agent_to_run.run_live(invocation_context):
                                yield item

                        # ⑨ task_completed：SequentialAgent 下一个 agent 接手信号
                        if _is_task_completed(event):
                            await asyncio.sleep(DEFAULT_TASK_COMPLETION_DELAY)
                            send_task.cancel()
                            return
                finally:
                    send_task.cancel()
                    await send_task     # 吞掉 CancelledError

        except (ConnectionClosed, ConnectionClosedOK) as e:
            if invocation_context.live_session_resumption_handle and attempt <= MAX_RECONNECT_ATTEMPTS:
                continue    # 带会话 handle 重连
            raise
```

**两个并发 task 的分工**：

**`_send_to_model`**（[`base_llm_flow.py:652-713`](../src/google/adk/flows/llm_flows/base_llm_flow.py)）：

```python
async def _send_to_model(self, llm_connection, invocation_context):
    while True:
        live_request = await invocation_context.live_request_queue.get()  # 阻塞直到调用方 put

        # 转发到所有活跃流式 tool（让它们也能感知用户输入）
        if invocation_context.active_streaming_tools:
            for tool in invocation_context.active_streaming_tools.values():
                if tool.stream:
                    tool.stream.send(live_request)

        # 分发到不同的 connection 方法
        if live_request.close:
            await llm_connection.close()
            return
        if live_request.activity_start:
            await llm_connection.send_realtime(types.ActivityStart())
        elif live_request.activity_end:
            await llm_connection.send_realtime(types.ActivityEnd())
        elif live_request.blob:
            await llm_connection.send_realtime(live_request.blob)   # 音频块
        elif live_request.content:
            await llm_connection.send_content(live_request.content) # 文本 / function_response
```

职责是**消费 `live_request_queue` 并转发给连接**。调用方（Web 前端 / VAD 组件）通过 `live_request_queue.send_content(...)` 或 `send_realtime(blob)` 喂数据。这里同时把输入广播给所有 **流式工具**，让它们可以基于用户实时输入调整自己的 stream 输出。

**`_receive_from_model`**（[`base_llm_flow.py:714-797`](../src/google/adk/flows/llm_flows/base_llm_flow.py)）：

- 从 `llm_connection.receive()` 拿 `LlmResponse` 流。
- 跑 `_postprocess_live`（比 `_postprocess_async` 多处理 turn_complete / transcription / usage_metadata 等 live 专属字段）。
- 对于 function call 响应，调用 `handle_function_calls_live` 并发执行；tool 结果通过 `live_request_queue.send_content` 再喂回 LLM。
- 维护 `live_session_resumption_handle`：每次 LLM 返回更新 handle 就更新 ctx，断线时用它重连。
- 管理音频缓存：`audio_cache_manager.cache_audio(...)` 在 turn_complete / interrupted 时把缓冲的音频块刷到 artifact，event 里只存 file_data 引用。

**断线重连机制**：

- Gemini Live API 支持**透明会话恢复（transparent session resumption）**——服务端维护会话状态，客户端凭 `session_resumption.handle` 重连后从中断处继续，不需要重新发历史。
- 每次服务端下发响应时可能带新的 handle；`_receive_from_model` 保存到 `invocation_context.live_session_resumption_handle`。
- 断线异常（`ConnectionClosed` / `APIError` with code 1000/1006）被外层 while 捕获；若有 handle 就 `continue` 重连，最多 `DEFAULT_MAX_RECONNECT_ATTEMPTS=5` 次。

**特殊事件处理**：

- **`transfer_to_agent`**：当前连接关闭、`send_task` 取消，控制交给目标子 agent 的 `run_live`。注意这**在 `run_live` 里处理，而不是在 `_postprocess_live`**——避免 function response 被父 / 子 agent 各处理一次造成重复。
- **`task_completed`**：SequentialAgent 用来标记子 agent 结束的假函数（见 4.3.3）；收到后直接 return，让 Sequential 的 for 循环进入下一个子 agent。

**与文本路径的对比**：

| 维度 | `run_async`（文本） | `run_live`（双向流） |
|---|---|---|
| LLM 交互 | `generate_content_async(stream=True/False)` | `llm.connect()` 返回 `BaseLlmConnection` |
| 主循环 | step 为单位，串行 | 两个 task 并发，send / receive 分离 |
| 用户输入 | 事先在 session 里 | 运行中通过 `live_request_queue` 实时喂 |
| 中途打断 | 不支持 | ✅ interrupted 标志 + 用户可随时说新话 |
| 断线恢复 | 无需（单次请求） | `session_resumption_handle` 自动重连 |
| tool 执行 | step 内串行 + function response 触发下步 | 并发，response 直接喂回 live queue |
| 音频 | 不处理 | 输入/输出都被缓存到 artifact |

---

## 7. Models 抽象层

### 7.1 BaseLlm 接口与 LLMRegistry 解析

[`models/base_llm.py:32`](../src/google/adk/models/base_llm.py) 的 `BaseLlm` 是所有模型适配器的抽象根。它是 Pydantic 模型，只要求一个字段 + 三个方法：

```python
# src/google/adk/models/base_llm.py:32-206（精简）
class BaseLlm(BaseModel):
    model: str                                    # 唯一必填字段

    @classmethod
    def supported_models(cls) -> list[str]:
        """返回此类能处理的模型名正则，供 LLMRegistry 匹配。"""
        return []

    @abstractmethod
    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """文本路径的核心方法。stream=True 时多个 partial + 一个 final；stream=False 时一个完整响应。"""

    def connect(self, llm_request: LlmRequest) -> BaseLlmConnection:
        """Live 路径。不实现的子类会在被调用时抛 NotImplementedError。"""
        raise NotImplementedError(...)
```

**解析流程**：当 LlmAgent 的 `model` 字段是字符串时，`canonical_model` 调用 `LLMRegistry.new_llm(model)`（[`models/registry.py:41-52`](../src/google/adk/models/registry.py)）：

```python
# src/google/adk/models/registry.py:41-124（精简）
class LLMRegistry:
    @staticmethod
    def new_llm(model: str) -> BaseLlm:
        return LLMRegistry.resolve(model)(model=model)     # 先解析类，再构造实例

    @staticmethod
    @lru_cache(maxsize=32)
    def resolve(model: str) -> type[BaseLlm]:
        for regex, llm_class in _llm_registry_dict.items():
            if re.compile(regex).fullmatch(model):         # 完整匹配
                return llm_class

        # 找不到时给友好的错误提示，告诉用户缺哪个 pip 包
        if re.match(r'^claude-', model):
            raise ValueError('...Install it with: pip install google-adk[extensions]...')
        elif '/' in model:
            raise ValueError('...pip install litellm>=1.75.5...')
        raise ValueError(f'Model {model} not found.')

    @staticmethod
    def register(llm_cls: type[BaseLlm]):
        for regex in llm_cls.supported_models():
            LLMRegistry._register(regex, llm_cls)
```

**正则驱动 + 懒加载**：

- 注册表 `_llm_registry_dict` 是 `dict[regex_str, type[BaseLlm]]`。
- 每个子类自己声明 `supported_models()` 返回一组正则。
- 模块首次 import 时通过 `LLMRegistry.register(cls)` 把自己注册进去。
- `resolve` 用 `lru_cache(32)` 缓存解析结果——同一个模型名不会重复走正则。
- 找不到时根据名字模式给**安装提示**（claude- 提示 anthropic、带 `/` 的提示 litellm），降低新手上手门槛。

**设计哲学**：模型解析走"数据驱动"而非"硬编码 if/elif"。自定义模型只需要：

```python
class MyCompanyLlm(BaseLlm):
    @classmethod
    def supported_models(cls) -> list[str]:
        return [r'mycompany-.*']

    async def generate_content_async(self, llm_request, stream=False):
        ...

LLMRegistry.register(MyCompanyLlm)
```

之后所有 `Agent(model='mycompany-v2')` 就自动使用这个类。

### 7.2 内置模型实现对比

| 模型族 | 类 | 文件 | `supported_models()` | 流式 | Live | 备注 |
|---|---|---|---|---|---|---|
| Gemini | `Gemini` | [`google_llm.py`](../src/google/adk/models/google_llm.py) | `gemini-.*`, endpoint 模式, `model-optimizer-*` | ✅ | ✅ | 最完整的实现。支持 Interactions API、context cache、retry |
| Claude | `Claude` / `AnthropicLlm` | [`anthropic_llm.py`](../src/google/adk/models/anthropic_llm.py) | `claude-.*` | ✅ | ❌ | 需 `anthropic>=0.43.0`。支持 vision、thinking、refusal messages |
| LiteLLM | `LiteLlm` | [`lite_llm.py`](../src/google/adk/models/lite_llm.py) | `.+/.+` (任意 provider/model) | ✅ | ❌ | 100+ 供应商适配层；结构化输出转换；`openai/gpt-4o` 之类 |
| Gemma | `Gemma` | [`gemma_llm.py`](../src/google/adk/models/gemma_llm.py) | `gemma-.*` | ✅ | ❌ | 本地 Ollama 或自建推理服务 |
| Apigee | `ApigeeLlm` | [`apigee_llm.py`](../src/google/adk/models/apigee_llm.py) | 企业 Apigee 网关路径 | ✅ | ❌ | Google Apigee 托管的私有 LLM 网关 |

**各实现的关键差异**：

- **Gemini** 直接用官方 `google-genai` SDK，字段一一映射。
- **Claude** 把 ADK 的 `LlmRequest.contents` 转成 Anthropic 的 `MessageParam[]`，把 `function_call` 转为 `tool_use` content block。
- **LiteLLM** 转成 OpenAI chat completions 格式，function call 用 OpenAI 的 tool_calls 结构；LiteLLM 本身再把它转成具体 provider 的格式。
- **Gemma / Apigee** 是相对轻量的封装，通常只支持文本，不支持 tool calling 或高级特性。

### 7.3 LlmRequest 与 LlmResponse 字段

这两个 Pydantic 模型是 Flow ↔ Model 之间的契约。所有处理器操作它们、所有实现接收/返回它们。

**LlmRequest 核心字段**（[`models/llm_request.py`](../src/google/adk/models/llm_request.py)）：

```python
class LlmRequest(BaseModel):
    model: Optional[str] = None                                    # 模型名
    contents: list[types.Content] = Field(default_factory=list)    # 对话历史
    config: Optional[types.GenerateContentConfig] = None           # 生成参数（temperature, max_tokens, tools, system_instruction, safety_settings, response_mime_type, response_schema）
    live_connect_config: Optional[LiveConnectConfig] = None        # Live API 参数（session_resumption, input_audio_transcription, output_audio_transcription）
    tools_dict: dict[str, BaseTool] = Field(default_factory=dict)  # 内部工具映射（由 name 查 BaseTool 实例）
    cache_config: Optional[ContextCacheConfig] = None              # Context cache 配置
    cache_metadata: Optional[CacheMetadata] = None                 # 缓存命中的指纹/cache_name
    previous_interaction_id: Optional[str] = None                  # Interactions API 链式会话 ID
```

**`tools_dict` 的作用**：`config.tools` 里的工具声明只有 name 和 schema（给 LLM 看的），当 LLM 返回 function call 时 ADK 需要找到对应的 `BaseTool` 实例去执行——`tools_dict[call.name]` 就是这个映射。

**LlmResponse 核心字段**（[`models/llm_response.py`](../src/google/adk/models/llm_response.py)）：

```python
class LlmResponse(BaseModel):
    content: Optional[types.Content] = None               # 模型输出（文本、function_call、file_data）
    partial: Optional[bool] = None                        # 流式中间块标志
    turn_complete: Optional[bool] = None                  # Live 模式：本轮结束
    interrupted: Optional[bool] = None                    # Live 模式：用户打断
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    finish_reason: Optional[FinishReason] = None          # STOP / MAX_TOKENS / SAFETY / ...
    input_transcription: Optional[Transcription] = None   # 用户说话的转录
    output_transcription: Optional[Transcription] = None  # 模型说话的转录
    grounding_metadata: Optional[GroundingMetadata] = None  # Search grounding 的引用
    usage_metadata: Optional[UsageMetadata] = None        # prompt/output token 计数
    custom_metadata: Optional[dict[str, Any]] = None      # 业务透传
    live_session_resumption_update: Optional[str] = None  # Gemini Live 动态下发的 resumption handle
```

`Event` 继承 `LlmResponse`，在其基础上加会话字段（invocation_id / author / actions / branch / id / timestamp）——这是整个 ADK 事件系统的类型链。

### 7.4 BaseLlmConnection 与 GeminiLlmConnection

Live 路径的双向流通过 `BaseLlmConnection` 抽象。

```python
# src/google/adk/models/base_llm_connection.py:25-82
class BaseLlmConnection(ABC):
    @abstractmethod
    async def send_history(self, contents: list[types.Content]) -> None:
        """首次连接时发送对话历史。"""

    @abstractmethod
    async def send_content(self, content: types.Content) -> None:
        """发送文本消息或 function response。"""

    @abstractmethod
    async def send_realtime(self, input) -> None:
        """发送音频 blob、ActivityStart/End 等实时控制消息。"""

    @abstractmethod
    def receive(self) -> AsyncGenerator[LlmResponse, None]:
        """接收服务端响应流。"""

    @abstractmethod
    async def close(self) -> None:
        ...
```

**`GeminiLlmConnection`**（[`models/gemini_llm_connection.py`](../src/google/adk/models/gemini_llm_connection.py)）是唯一实现：

- 内部封装 `google.genai.live.AsyncSession`。
- `send_history` 会**过滤掉 audio parts**——因为音频已经被转成转录文本（`input_transcription`），再发一次音频会让 server 重复处理。
- `send_realtime` 支持三种形态：`types.Blob`（音频数据）、`types.ActivityStart/End`（VAD 控制信号）。
- `receive` 把 GenAI SDK 的 `LiveServerMessage` 转换成 ADK 的 `LlmResponse`，涉及字段映射：
  - `server_content.model_turn.parts` → `LlmResponse.content.parts`
  - `server_content.turn_complete` → `turn_complete`
  - `server_content.interrupted` → `interrupted`
  - `session_resumption_update.new_handle` → `live_session_resumption_update`
  - `usage_metadata` / `input_transcription` / `output_transcription` → 同名字段

**设计哲学**：让 Live API 的"消息驱动"模型（客户端和服务器都在持续发/收）通过 `BaseLlmConnection` 的对称方法表达。文本路径的单次请求-响应和 Live 的长连接，在 ADK 里用两个**独立的**抽象表达，不强行统一——这避免了把两种语义硬塞进一个接口导致的复杂度。

---

## 8. 工具体系

### 8.1 BaseTool 与 BaseToolset

[`tools/base_tool.py:47`](../src/google/adk/tools/base_tool.py) 的 `BaseTool` 是所有 ADK 工具的抽象根。与 Agent 一样，它通过**约定极简 + 扩展点明确**来保证可定制性。

```python
# src/google/adk/tools/base_tool.py:47-130（精简）
class BaseTool(ABC):
    name: str
    description: str
    is_long_running: bool = False
    custom_metadata: Optional[dict[str, Any]] = None

    def _get_declaration(self) -> Optional[types.FunctionDeclaration]:
        """返回 OpenAPI 式的 function declaration（给 LLM 看的工具 schema）。"""
        return None                       # None 表示这个工具不需要加到 LlmRequest（如 Gemini 内置工具）

    async def run_async(self, *, args: dict, tool_context: ToolContext) -> Any:
        """LLM 调用此工具时真正执行的代码。"""
        raise NotImplementedError(f"{type(self)} is not implemented")

    async def process_llm_request(self, *, tool_context: ToolContext, llm_request: LlmRequest) -> None:
        """默认实现：把自己的声明加到 llm_request.config.tools。子类可重写做更复杂的预处理。"""
        llm_request.append_tools([self])
```

**三个核心方法的分工**：

| 方法 | 职责 | 何时调用 | 典型重写场景 |
|---|---|---|---|
| `_get_declaration()` | 返回 JSON Schema 形式的工具声明 | `process_llm_request` 内部；装配 LlmRequest 时 | 所有需要被 LLM 显式调用的工具 |
| `run_async(args, tool_context)` | 工具执行体 | LLM 返回 function call 后 | 几乎所有工具 |
| `process_llm_request(tool_context, llm_request)` | 把工具注入到 LlmRequest | 每次 LLM 调用前 | 内置工具（如 `google_search`）需要设置 `config.tools=[types.Tool(google_search=...)]` 而非 function_declarations |

**为什么有 `process_llm_request`？** 因为有些 Gemini 原生工具（`google_search`、`url_context`、`code_executor`）不是通过 function declaration 暴露的——它们是 `types.Tool` 的特殊字段。`process_llm_request` 让工具自己决定如何被"挂到请求上"，而不是框架统一处理。

**`from_config`**（[`base_tool.py:136-220`](../src/google/adk/tools/base_tool.py)）：一个通用的**从配置文件构造工具实例**的 classmethod。它通过 `inspect` 读取子类 `__init__` 的类型提示，自动把 YAML/JSON 字段映射到构造参数，支持：`int/str/bool/float`、Pydantic BaseModel、Callable（按全限定名解析）、`list[X]`。这使得 agent config（YAML）可以不写 Python 代码就实例化常见工具。

**`BaseToolset`**：[`tools/base_toolset.py:63`](../src/google/adk/tools/base_toolset.py) 的工具集合抽象。核心语义是"一组**可能随上下文变化的**工具"：

```python
# src/google/adk/tools/base_toolset.py:63-150（精简）
class BaseToolset(ABC):
    def __init__(
        self, *,
        tool_filter: Optional[Union[ToolPredicate, List[str]]] = None,
        tool_name_prefix: Optional[str] = None,
    ):
        ...
        self._cached_invocation_id: Optional[str] = None
        self._cached_prefixed_tools: Optional[list[BaseTool]] = None

    @abstractmethod
    async def get_tools(self, readonly_context: Optional[ReadonlyContext] = None) -> list[BaseTool]:
        """根据上下文返回当前可用的工具列表。"""

    @final
    async def get_tools_with_prefix(self, readonly_context=None) -> list[BaseTool]:
        """带 invocation 缓存的带前缀版本。同一 invocation 内，相同 toolset 只解析一次。"""
```

**三个关键能力**：

1. **动态产出工具**：`get_tools(readonly_context)` 接收当前上下文，可以根据 `ctx.state['role']` 等动态返回不同工具。例如 `BookingToolset` 里，普通用户只看到 `search_flights`，admin 看到 `search_flights + cancel_booking + refund`。
2. **工具过滤**：`tool_filter` 可以是列表（只保留名字在列表中的工具）或 `ToolPredicate` 回调（`(tool, readonly_ctx) -> bool`）。让使用方在不修改 toolset 源码的前提下定制子集。
3. **前缀隔离**：`tool_name_prefix='booking_'` 会给每个工具名加前缀，避免多个 toolset 产生同名工具冲突。常用于多 MCP server 场景——不同 server 的 `search` 工具加上各自的 prefix 就能共存。

**`get_auth_config`**（可选重写）：返回 toolset 需要的 OAuth / API Key 配置。ADK 在调用 `get_tools` 前先帮 toolset 交换凭证（通过 `credential_service`），然后把 `exchanged_auth_credential` 写入 toolset——toolset 内部的 tool 不需要自己处理认证。

### 8.2 FunctionTool：把 Python 函数变成工具

[`tools/function_tool.py:39`](../src/google/adk/tools/function_tool.py) 是最常用的工具类。它的目标是"**用户写一个普通 Python 函数，ADK 自动把它变成 LLM 可调用的工具**"，无需任何手动声明 schema。

**核心流程**：

```python
# src/google/adk/tools/function_tool.py:39-101（精简）
class FunctionTool(BaseTool):
    def __init__(self, func, *, require_confirmation=False):
        name = func.__name__
        doc = inspect.cleandoc(func.__doc__ or '')
        super().__init__(name=name, description=doc)
        self.func = func
        self._context_param_name = find_context_parameter(func) or 'tool_context'
        self._ignore_params = [self._context_param_name, 'input_stream']
        self._require_confirmation = require_confirmation

    def _get_declaration(self) -> Optional[types.FunctionDeclaration]:
        # 用 inspect + type hints + Pydantic 自动生成 JSON Schema
        return types.FunctionDeclaration.model_validate(
            build_function_declaration(
                func=self.func,
                ignore_params=self._ignore_params,
                variant=self._api_variant,
            )
        )
```

`build_function_declaration`（[`tools/_automatic_function_calling_util.py`](../src/google/adk/tools/_automatic_function_calling_util.py) 和 `_function_tool_declarations.py`）做的事：

1. **读取函数签名**：`inspect.signature(func)` 拿到所有参数及其类型注解。
2. **过滤特殊参数**：`tool_context`、`input_stream` 被跳过——它们是 ADK 注入的框架参数，不应暴露给 LLM。
3. **类型到 Schema 转换**：用 `pydantic.create_model(...)` 动态构造一个 Pydantic 模型，然后调 `model.model_json_schema()` 生成 JSON Schema。支持：
   - 基础类型（str / int / bool / float）
   - Optional / Union
   - List[X] / Dict[str, X]
   - 嵌套的 Pydantic BaseModel
   - 前向引用（用 `get_type_hints` 解析）
4. **描述**：函数 docstring 作为工具 description，参数级描述从 Google-style docstring（`Args:` 段）里提取。

**`run_async` 的执行流程**：

```python
# src/google/adk/tools/function_tool.py:159-221（精简）
async def run_async(self, *, args, tool_context):
    # 1. 预处理：把 dict 参数转成 Pydantic 实例（如果函数签名要 Pydantic 模型）
    args_to_call = self._preprocess_args(args)

    # 2. 注入 tool_context（如果函数签名要）
    if self._context_param_name in valid_params:
        args_to_call[self._context_param_name] = tool_context
    args_to_call = {k: v for k, v in args_to_call.items() if k in valid_params}

    # 3. 必填参数校验
    missing = [arg for arg in self._get_mandatory_args() if arg not in args_to_call]
    if missing:
        return {'error': f'missing mandatory parameters: {missing}'}

    # 4. 确认流程
    require_confirmation = (
        await self._invoke_callable(self._require_confirmation, args_to_call)
        if callable(self._require_confirmation)
        else bool(self._require_confirmation)
    )
    if require_confirmation:
        if not tool_context.tool_confirmation:
            # 第一次：请求确认，立刻返回一个占位错误
            tool_context.request_confirmation(hint='Please approve or reject...')
            tool_context.actions.skip_summarization = True
            return {'error': 'This tool call requires confirmation, please approve or reject.'}
        elif not tool_context.tool_confirmation.confirmed:
            return {'error': 'This tool call is rejected.'}

    # 5. 真正调用函数（支持 async 和 sync）
    return await self._invoke_callable(self.func, args_to_call)
```

**值得关注的细节**：

- **参数校验在框架侧**：如果 LLM 漏填必填参数，ADK **不调用函数**，而是返回结构化的 error。LLM 在下一步能看到"缺少 X、Y 参数"的提示，通常会自动补齐并重试——这大幅提升了 LLM 调用 tool 的稳定性。
- **Pydantic 模型自动转换**：如果函数签名是 `def my_fn(item: MyPydantic)`，LLM 返回的 JSON 会被 `MyPydantic.model_validate(...)` 转换成实例。失败时 warning + 保留原始 dict，让 tool 自己决定怎么处理。
- **`tool_context` 可选**：函数如果需要访问 state / artifact / 请求认证，就在签名里声明 `tool_context: ToolContext`——ADK 会自动注入，且不把它暴露给 LLM。检测方式是**优先看类型注解**（`find_context_parameter`），退而求其次看名字叫 `tool_context`。
- **sync/async 统一**：`_invoke_callable` 用 `inspect.iscoroutinefunction` 检测，async 就 `await target(...)`，sync 就直接调——用户不需要区分。

**`require_confirmation` 的两种形式**：

- **静态布尔**：`FunctionTool(delete_file, require_confirmation=True)` — 所有调用都要确认。
- **动态谓词**：`FunctionTool(transfer_money, require_confirmation=lambda amount, **kw: amount > 1000)` — 只有金额超过 1000 才要确认。

动态谓词签名和被包装函数一致，可以基于 LLM 填的参数决定是否要人工兜底。

**`LongRunningFunctionTool`**（[`tools/long_running_tool.py`](../src/google/adk/tools/long_running_tool.py)）是 `FunctionTool` 的子类：

```python
class LongRunningFunctionTool(FunctionTool):
    def __init__(self, func):
        super().__init__(func)
        self.is_long_running = True
```

短短几行，但意义重大——`is_long_running=True` 会让框架在把工具声明发给 LLM 时加上警告，也让 `_execute_single_function_call_async` 允许工具返回 None（见 6.3 节）。

### 8.3 多 Agent 组合工具：AgentTool / transfer_to_agent / exit_loop

这三个工具都和"多 agent 协作"相关，但语义完全不同。理解它们的边界是写多 agent 系统的关键。

**`AgentTool`**（[`tools/agent_tool.py:94`](../src/google/adk/tools/agent_tool.py)）：把一个 agent **作为工具**嵌入另一个 agent。

```python
# src/google/adk/tools/agent_tool.py:94-170（精简）
class AgentTool(BaseTool):
    def __init__(
        self,
        agent: BaseAgent,
        skip_summarization: bool = False,
        *,
        include_plugins: bool = True,
        propagate_grounding_metadata: bool = False,
    ):
        self.agent = agent
        self.skip_summarization = skip_summarization
        self.include_plugins = include_plugins
        super().__init__(name=agent.name, description=agent.description)

    def _get_declaration(self) -> types.FunctionDeclaration:
        input_schema = _get_input_schema(self.agent)       # 递归找到 LlmAgent 的 input_schema
        if input_schema:
            result = build_function_declaration(func=input_schema, variant=self._api_variant)
            result.description = self.agent.description
        else:
            # 没指定 schema 时，默认 {"request": str}
            result = types.FunctionDeclaration(
                parameters=types.Schema(properties={'request': types.Schema(type='STRING')}),
            )
        return result
```

**语义**：父 agent 调用 `child_agent_name(args)` → ADK **把 child_agent 当作一个完整的 agent 跑一遍**（新建嵌套的 Runner 语义），拿到它的最终回复作为 tool response 返回给父 agent。父 agent 继续基于这个回复思考。

**关键特征**：

- **child agent 独立 session 副本**：通过 `ForwardingArtifactService` 让 artifact 可以在父子 agent 间共享，其他状态隔离。
- **`include_plugins=True`**（默认）：child agent 继承父 agent 的 plugins。设为 False 可以让 child 运行在隔离的 plugin 环境（比如测试 child 时不跑父的审计插件）。
- **`skip_summarization=True`**：父 agent 拿到 tool response 后不再让 LLM 总结一遍；把 child 的输出原样作为父 agent 的回复。
- **`input_schema` / `output_schema` 继承**：从 child agent 自动提取；composite agent 递归找第一个 sub_agent 的 input_schema / 最后一个 sub_agent 的 output_schema。

**用途**：把一个专业 agent 封装成工具。例如 `writer_agent` 被 `reviewer_agent` 调用——reviewer 像调普通 tool 一样让 writer 改写内容，但 writer 内部有自己的 LLM + 工具循环。

**`transfer_to_agent`**（[`tools/transfer_to_agent_tool.py`](../src/google/adk/tools/transfer_to_agent_tool.py)）：**转移控制权**而非调用。

```python
# src/google/adk/tools/transfer_to_agent_tool.py:26-40
def transfer_to_agent(agent_name: str, tool_context: ToolContext) -> None:
    """Transfer the question to another agent."""
    tool_context.actions.transfer_to_agent = agent_name

class TransferToAgentTool(FunctionTool):
    def __init__(self, agent_names: list[str]):
        super().__init__(func=transfer_to_agent)
        self._agent_names = agent_names

    def _get_declaration(self):
        function_decl = super()._get_declaration()
        # 把 agent_name 参数加上 enum 约束，防止 LLM 幻觉不存在的名字
        function_decl.parameters.properties['agent_name'].enum = self._agent_names
        return function_decl
```

**语义**：当前 agent 说"我不处理这个请求了，交给 `agent_name` 处理"。框架：

1. 通过 `tool_context.actions.transfer_to_agent = agent_name` 在事件里记录目标。
2. 当前 agent 的 flow 退出（`is_final_response()` 为 True，因为 transfer_to_agent 是特殊 function_response）。
3. Runner 的 `_find_agent_to_run` 在下一次 invocation 或同一 invocation 的下一步用目标 agent 继续。

**AgentTool vs transfer_to_agent 的对比**：

| 维度 | `AgentTool` | `transfer_to_agent` |
|---|---|---|
| 控制流 | **父 agent 调用 child，拿到结果继续** | **当前 agent 让位，目标 agent 接手** |
| 结构 | 父子关系，child 是工具 | 平级或父子，通过 agent_tree 的 transfer 规则限制方向 |
| 结果处理 | Child 输出回到父 agent | 目标 agent 直接回复用户 |
| 典型比喻 | "我雇人做一个子任务，拿到结果自己判断" | "这个请求我搞不定，交给专家" |
| LLM 感知 | 像普通 function call | 特殊 function call（`transfer_to_agent`），触发框架控制流 |
| 启用方式 | `tools=[AgentTool(child)]` | `AutoFlow`（有子 agent 或未禁止转移时自动） |

**`exit_loop`**（[`tools/exit_loop_tool.py`](../src/google/adk/tools/exit_loop_tool.py)）：一行函数触发 LoopAgent 退出。

```python
# src/google/adk/tools/exit_loop_tool.py:20-26
def exit_loop(tool_context: ToolContext):
    """Exits the loop."""
    tool_context.actions.escalate = True
    tool_context.actions.skip_summarization = True
```

只做两件事：

- 设置 `escalate=True` → LoopAgent 看到后跳出循环（见 4.3.1）。
- 设置 `skip_summarization=True` → LLM 不再对这个工具响应做总结。

**典型用法**：在 ReAct 式循环里，子 agent 判断"任务已完成"时调用 `exit_loop()`——LoopAgent 立即终止迭代，控制流回到父 agent。

**三个工具的设计哲学**：ADK 选择用**工具语义统一表达多 agent 控制流**。不需要专门的"Agent Graph"或"State Machine"概念——工具 + `actions` 字段已经足够：

- 组合 → `AgentTool`
- 单向转移 → `transfer_to_agent` + enum 约束
- 循环控制 → `exit_loop` + `escalate`

LLM 只需要理解"调用某个函数可以完成某件事"这一件事，不需要理解框架内部的编排概念——这让多 agent 对 LLM 的要求非常低，甚至小模型也能跑起来。

### 8.4 MCP 与 OpenAPI 集成

ADK 原生支持两种大规模外部工具生态：**MCP（Model Context Protocol）** 和 **OpenAPI** 规范。两者都通过 `BaseToolset` 抽象接入，agent 代码对底层协议无感。

#### 8.4.1 MCP Toolset

[`tools/mcp_tool/mcp_toolset.py:66`](../src/google/adk/tools/mcp_tool/mcp_toolset.py) 的 `McpToolset` 连接到 MCP Server，动态发现它暴露的工具。

**三种连接参数**：

- **`StdioConnectionParams`**：启动一个子进程作为 MCP server，通过 stdin/stdout 通信。适合本地工具（如 filesystem、git）。
- **`SseConnectionParams`**：HTTP Server-Sent Events 协议。适合云端 MCP 服务。
- **`StreamableHTTPConnectionParams`**：MCP 新协议版本，支持 HTTP 流式双向通信。

**核心类**：

- **`MCPSessionManager`**（[`mcp_session_manager.py`](../src/google/adk/tools/mcp_tool/mcp_session_manager.py)）：管理 MCP 会话生命周期。使用连接池、超时、重试（`retry_on_errors` 装饰器）。多 agent 场景下复用同一个底层连接。
- **`MCPTool`**（[`mcp_tool.py`](../src/google/adk/tools/mcp_tool/mcp_tool.py)）：单个 MCP 工具的 `BaseTool` 实现。把 MCP 的 `types.Tool` 转换成 ADK 的工具声明；调用时把 ADK 的 args 转成 MCP 的 CallToolRequest。
- **`LoadMcpResourceTool`**（[`load_mcp_resource_tool.py`](../src/google/adk/tools/load_mcp_resource_tool.py)）：MCP 除了 tools 还有 resources 概念（可访问的数据，如文件内容、数据库表）。ADK 把 resource 访问包装成另一个工具。

**McpToolset 工作流程**：

```
1. agent 初次 canonical_tools(ctx) → toolset.get_tools(readonly_context)
2. McpToolset 检查会话是否已建立，若无则：
   a. MCPSessionManager 根据 connection_params 启动子进程 / 建立 SSE 连接
   b. 调用 MCP session.list_tools() 拿到 server 支持的工具列表
3. 把每个 MCP Tool 包装成 MCPTool，返回给 agent
4. agent 把这些 MCPTool 装进 LlmRequest.config.tools
5. LLM 返回 function call → MCPTool.run_async → MCP session.call_tool(...) → 回传结果
6. invocation 结束时框架自动关闭 toolset (close())，释放 MCP 连接
```

**动态 header（认证）**：`McpToolset` 支持 `header_provider: Callable[[ReadonlyContext], dict]`——每次调用 MCP 前动态生成认证 header。典型用途：多租户系统里，header 包含当前用户的 JWT。

**进度回调**：MCP 支持工具进度通知（`ProgressCallbackFactory`），McpToolset 可以把这些通知转成 ADK 的事件流——长运行 MCP tool 能向用户实时反馈进度。

#### 8.4.2 OpenAPI Toolset

[`tools/openapi_tool/`](../src/google/adk/tools/openapi_tool/) 从 OpenAPI（Swagger）规范动态生成工具。

- **`OpenAPIToolset`**：输入 OpenAPI spec（JSON / YAML dict），输出一组 `RestApiTool`。
- **`RestApiTool`**：每个 OpenAPI operation 对应一个 `BaseTool`，`_get_declaration` 从 spec 的 `parameters` / `requestBody` 转换而来，`run_async` 发送 HTTP 请求（通过 `httpx`）并解析响应。

**能力**：

- 支持 OpenAPI 3.x 所有参数类型（path、query、header、body）。
- 处理安全方案：API Key（header/query）、OAuth2、HTTP Bearer、HTTP Basic——与 ADK 的 `AuthConfig` 对接，凭证通过 `credential_service` 注入。
- 处理响应 schema：解析 200 响应的 schema，把嵌套对象转成工具的结构化返回。
- 处理 `$ref` 引用：自动解析 OpenAPI 里的引用链，展开成 flat schema 发给 LLM。

**用途**：把任何已有 REST API（内部微服务、SaaS 供应商的 API）一键变成 agent 可调用的工具，无需手写 FunctionTool。

**对比总结**：

| 维度 | `FunctionTool` | `AgentTool` | `McpToolset` | `OpenAPIToolset` |
|---|---|---|---|---|
| 来源 | 用户 Python 函数 | 另一个 agent | MCP server | OpenAPI spec |
| 工具数量 | 1 个 | 1 个 | N 个（server 决定） | N 个（spec 决定） |
| Schema 生成 | inspect + Pydantic | 从 agent.input_schema 提取 | MCP 声明直译 | OpenAPI parameters → JSON Schema |
| 执行 | 直接调函数 | 跑一次 sub-agent | MCP session.call_tool | httpx HTTP 请求 |
| 认证 | 用户自处理 | agent 内部 | header_provider / AuthConfig | OpenAPI security → AuthConfig |
| 典型场景 | 业务内函数 | agent 编排 | 开源工具生态 | 已有 REST API |

---

## 9. 服务层：Session / Memory / Artifact

服务层是 ADK 所有持久化操作的出口。三个服务各负其责，**没有重叠**——理解它们的分工是理解 ADK 数据模型的关键。

### 9.1 BaseSessionService：事件流 + 状态

`Session`（[`sessions/session.py`](../src/google/adk/sessions/session.py)）是一次多轮对话的容器，只有两个核心字段：

- `events: list[Event]` — 有序事件流（包含 user 消息、agent 回复、tool call、tool response 等）。
- `state: dict[str, Any]` — 键值状态快照（当前所有 `state_delta` 的合并结果）。

`BaseSessionService`（[`sessions/base_session_service.py:54`](../src/google/adk/sessions/base_session_service.py)）的契约：

```python
# src/google/adk/sessions/base_session_service.py:54-125（精简）
class BaseSessionService(abc.ABC):
    @abstractmethod
    async def create_session(self, *, app_name, user_id, state=None, session_id=None) -> Session: ...
    @abstractmethod
    async def get_session(self, *, app_name, user_id, session_id, config=None) -> Optional[Session]: ...
    @abstractmethod
    async def list_sessions(self, *, app_name, user_id=None) -> ListSessionsResponse: ...
    @abstractmethod
    async def delete_session(self, *, app_name, user_id, session_id) -> None: ...

    async def append_event(self, session: Session, event: Event) -> Event:
        """基类提供默认实现 —— 具体后端只需覆盖前四个方法。"""
        if event.partial:
            return event
        self._apply_temp_state(session, event)      # ① temp:X → in-memory session.state
        event = self._trim_temp_delta_state(event)  # ② 清掉 temp:X delta，避免持久化
        self._update_session_state(session, event)  # ③ 其他 state_delta → session.state
        session.events.append(event)
        return event
```

**关键巧思：temp state 的两步处理**

ADK 允许给 state key 加前缀表达作用域：

- `app:X` — 应用级（所有 session 共享，归属 app_name）
- `user:X` — 用户级（该用户的所有 session 共享）
- `temp:X` — **只在当前 invocation 内有效**，不持久化
- 无前缀 — 当前 session 内持久化

`temp:X` 的实现巧妙：先把 `temp:X` 的 delta 应用到**内存中的** `session.state`（供同 invocation 内后续 agent 读取），再从 event 的 `state_delta` 里移除 temp 键（避免落盘）。结果：

- SequentialAgent 里 stage1 写 `temp:intermediate=X`，stage2 能读到它。
- invocation 结束、存储刷盘时，temp 数据不会留痕。
- 下一次 invocation 开始时，temp 键消失。

**`GetSessionConfig`** 允许客户端按需加载：

```python
class GetSessionConfig(BaseModel):
    num_recent_events: Optional[int] = None   # 只取最近 N 条事件
    after_timestamp: Optional[float] = None   # 只取某时间戳之后的事件
```

不设时全量加载。这是 ADK 控制**每次 invocation 的事件装配成本**的关键——长 session 不必每次把所有历史都塞进 LLM。

**内置实现**：

| 实现 | 文件 | 存储后端 |
|---|---|---|
| `InMemorySessionService` | [`in_memory_session_service.py`](../src/google/adk/sessions/in_memory_session_service.py) | Python dict（进程内，重启丢失） |
| `DatabaseSessionService` | [`database_session_service.py`](../src/google/adk/sessions/database_session_service.py) | SQLAlchemy（PostgreSQL / SQLite / MySQL / Spanner） |
| `VertexAiSessionService` | [`vertex_ai_session_service.py`](../src/google/adk/sessions/vertex_ai_session_service.py) | Vertex AI Agent Engine Session 托管 |

### 9.2 BaseMemoryService：跨 session 的长期记忆

`Session` 是一次对话的完整历史；**`Memory` 是从多个 session 提炼出的可检索知识**。两者分工明确：

- Session：append-only 事件流，侧重**完整性**（不丢）。
- Memory：摘要化后的条目，侧重**可搜索性**（相关性查询）。

`BaseMemoryService`（[`memory/base_memory_service.py:44`](../src/google/adk/memory/base_memory_service.py)）：

```python
class BaseMemoryService(ABC):
    @abstractmethod
    async def add_session_to_memory(self, session: Session) -> None:
        """摄取整个 session（可以多次调用，实现应幂等或追加）。"""

    async def add_events_to_memory(
        self, *, app_name, user_id, events, session_id=None, custom_metadata=None
    ) -> None:
        """增量摄取事件子集（默认抛 NotImplementedError —— 子类可选实现）。"""

    async def add_memory(
        self, *, app_name, user_id, memories, custom_metadata=None
    ) -> None:
        """直接写 MemoryEntry（某些服务支持直接写，不需要先有 session）。"""

    @abstractmethod
    async def search_memory(
        self, *, app_name, user_id, query: str
    ) -> SearchMemoryResponse: ...
```

**三种写入模式**：

- `add_session_to_memory(session)` — 最常用，通常在 session 结束后调用一次，把整个对话摘要化入库。
- `add_events_to_memory(events=...)` — 增量，每轮结束后只摄取最新事件。适合流式场景、或内存服务支持"增量摘要"时。
- `add_memory(memories=[...])` — 直接写，绕过事件。适合从外部系统（知识库、文档）导入。

**`search_memory` 只要求返回 `MemoryEntry` 列表**——不规定排序或相关度算分，具体由实现决定。

**内置实现**：

| 实现 | 文件 | 后端 |
|---|---|---|
| `InMemoryMemoryService` | [`in_memory_memory_service.py`](../src/google/adk/memory/in_memory_memory_service.py) | 内存列表 + 关键词匹配 |
| `VertexAiMemoryBankService` | [`vertex_ai_memory_bank_service.py`](../src/google/adk/memory/vertex_ai_memory_bank_service.py) | Vertex AI Agent Engine Memory Bank（向量检索） |

**Agent 如何使用 Memory**：

通过两个内置工具：

- **`preload_memory_tool`**：自动在每次 LLM 调用前搜索 memory，把相关条目塞进 prompt。无需 LLM 显式调用。
- **`load_memory_tool`**：让 LLM 通过 function call 主动查询 memory（`load_memory(query=...)`）。

### 9.3 BaseArtifactService：文件级版本化存储

`Artifact` = 一个命名的、版本化的二进制资源（或 Content 对象）。典型使用场景：用户上传的图片、音频、PDF；agent 生成的报告、音频回复。

`BaseArtifactService`（[`artifacts/base_artifact_service.py`](../src/google/adk/artifacts/base_artifact_service.py)）：

```python
class BaseArtifactService(ABC):
    @abstractmethod
    async def save_artifact(self, *, app_name, user_id, session_id, filename, artifact) -> int:
        """保存一个 artifact，返回新版本号（从 0 递增）。"""

    @abstractmethod
    async def load_artifact(self, *, app_name, user_id, session_id, filename, version=None) -> Optional[types.Part]:
        """不指定 version 时返回最新版。"""

    @abstractmethod
    async def list_artifact_keys(self, *, app_name, user_id, session_id) -> list[str]: ...
    @abstractmethod
    async def delete_artifact(self, *, app_name, user_id, session_id, filename) -> None: ...
    @abstractmethod
    async def list_versions(self, *, app_name, user_id, session_id, filename) -> list[int]: ...
```

**两个作用域**：

- `filename='doc.pdf'` — session 级，只属于当前会话。
- `filename='user:profile.jpg'` — **用户级**，跨 session 共享（与 state 的 `user:` 前缀语义一致）。

**版本号单调递增**：每次 `save_artifact` 返回新版本号（0, 1, 2, ...）；`load_artifact(version=1)` 读历史版本。版本信息通过 `Event.actions.artifact_delta: dict[str, int]`（filename → version）记录在事件流里——配合 rewind 机制可以把 artifact 也一起回滚。

**Agent 读写**：通过 `CallbackContext.save_artifact` / `load_artifact` 或 `tool_context` 同名方法。内置工具 `load_artifacts_tool` 让 LLM 能显式加载指定 artifact 到 prompt。

**内置实现**：

| 实现 | 存储 |
|---|---|
| `InMemoryArtifactService` | dict + bytes（进程内） |
| `GcsArtifactService` | Google Cloud Storage（推荐生产用） |

**三个服务的分工总结**：

| 服务 | 存什么 | 粒度 | 可搜索 | 跨 session |
|---|---|---|---|---|
| Session | 事件流 + 键值状态 | 一次对话 | 否（线性读） | 否（user_id + session_id 定位） |
| Memory | 摘要化知识 | 多次对话提炼 | **是**（语义/关键词） | 是（同 user 共享） |
| Artifact | 文件（版本化） | 单个文件 | 否（按 filename） | 看前缀（`user:` 共享） |

---

## 10. Auth 子系统

[`auth/`](../src/google/adk/auth/) 子目录处理一个棘手的问题：**LLM agent 在运行时可能需要用户的凭证**——OAuth token、API Key、Service Account 等。ADK 把这套流程做成一个**两段式的 dance**，让 tool 声明需求、ADK 协调、客户端 UI 完成交互。

### 10.1 核心模型：AuthConfig

[`auth/auth_tool.py:51`](../src/google/adk/auth/auth_tool.py) 的 `AuthConfig` 是这个流程的中心数据结构：

```python
# src/google/adk/auth/auth_tool.py:51-95（精简）
class AuthConfig(BaseModelWithConfig):
    auth_scheme: AuthScheme                              # 描述"需要什么类型的凭证"
    raw_auth_credential: Optional[AuthCredential] = None # 原始凭证（如 OAuth client_id/secret）
    exchanged_auth_credential: Optional[AuthCredential] = None  # 交换后的凭证（如 access_token）
    credential_key: Optional[str] = None                 # 在 credential service 中的存取 key
```

三个字段对应**两段式流程的三个时刻**：

- `auth_scheme`：**谁都要填**——描述"我需要什么"。`AuthScheme` 是 OpenAPI security scheme 的 Python 版（HTTP Bearer / API Key in header / OAuth2 / OIDC 等）。
- `raw_auth_credential`：**工具/开发者填**——提供"能去换 token 的底子"（如 OAuth 的 client_id / client_secret）。某些方案（API Key、Service Account）不需要这个字段——直接填 `exchanged_auth_credential` 就行。
- `exchanged_auth_credential`：**ADK + 客户端合作填**——最终可用的凭证（access_token / 已配置好的 API key）。

### 10.2 两段式流程

**场景：agent 调用一个需要 Google OAuth2 的工具（例如 Calendar API）**

```
① 工具声明需求：
    @tool.run_async 里发现当前 user 没有 Calendar 的 exchanged_auth_credential。
    调用 tool_context.request_credential(AuthConfig(
        auth_scheme=OAuth2(...),
        raw_auth_credential=AuthCredential(oauth2=OAuth2Credential(
            client_id=..., client_secret=..., scopes=[...]
        )),
    ))
    → 把 AuthConfig 写入 event.actions.requested_auth_configs[function_call_id]
    → tool 返回一个临时"需要认证"的 response，告诉 LLM 等凭证就绪再重试

② ADK 加工 AuthConfig：
    auth_preprocessor（request processor）或 tool 内部的 AuthHandler 拿到 raw_auth_credential，
    如果是 OAuth2 且缺少 authorization_uri，ADK 构造完整的 OAuth URL + state，
    填入 exchanged_auth_credential.oauth2.authorization_uri / state。

③ 客户端接收事件，弹出浏览器：
    UI 收到带 requested_auth_configs 的 Event，引导用户访问 authorization_uri、同意授权。
    回调拿到 authorization_code。

④ 客户端把回调信息写回：
    构造一个 function response：{
      function_call_id: ...,
      response: AuthConfig(
        auth_scheme=...,
        raw_auth_credential=...,
        exchanged_auth_credential=AuthCredential(oauth2=OAuth2Credential(
            authorization_response=".../callback?code=xxx&state=yyy"
        )),
      ),
    }
    → 通过 runner.run_async(new_message=...) 把这个 response 发回。

⑤ ADK 交换 token：
    auth_preprocessor 看到 function response 里的 authorization_response，
    调用 OAuth provider 的 token endpoint，拿到 access_token / refresh_token，
    把最终 exchanged_auth_credential 存到 credential_service（按 credential_key 索引）。

⑥ Tool 重新执行：
    agent 的下一步 LLM 调用再调这个工具，tool_context.get_credential(...) 从 service 拿到 access_token，
    调 Calendar API 成功，把结果作为 tool response 返回。
```

对于**不需要交换**的方案（API Key、Service Account）：

- 客户端直接填 `exchanged_auth_credential`（带 API key 或 service account JSON）。
- ADK 跳过步骤 ② 和 ⑤，直接走 ⑥。

### 10.3 Credential Service

`BaseCredentialService`（[`auth/credential_service/base_credential_service.py`](../src/google/adk/auth/credential_service/base_credential_service.py)）是凭证的**存储与检索**抽象：

```python
class BaseCredentialService(ABC):
    async def save_credential(self, *, auth_config: AuthConfig, callback_context): ...
    async def load_credential(self, *, auth_config: AuthConfig, callback_context): ...
```

通过 `credential_key` 作为索引。内置实现：

- **`InMemoryCredentialService`**：进程内 dict，重启丢失（开发用）。
- **`SessionStateCredentialService`**：凭证存到 `session.state`（有 `user:` 前缀，跨 session 持久）。

**关键约束**：ADK **不内置** Google Secret Manager / Vault 集成，但扩展点明确——自建安全凭证服务只需要继承 `BaseCredentialService` 并实现 save/load。

### 10.4 `credential_key` 的生成

默认实现（`get_credential_key`）根据 `auth_scheme` + `raw_auth_credential` 的稳定摘要生成 key。这意味着：

- 同一个 OAuth 配置（相同 client_id + scopes）产生相同 key — 凭证跨 session 复用。
- 用户显式传 `credential_key='my_gmail'` 可以覆盖默认——便于精细控制凭证作用域。

**设计哲学**：Auth 子系统的核心是**把"凭证交换"这件事变成事件流中的一轮对话**。LLM 不需要理解 OAuth；工具只需要声明需求；ADK 只需要协调；客户端只需要在事件里检测 `requested_auth_configs` 并弹窗。这套抽象让**动态凭证**（不同用户用不同 token 的场景）成为框架层自然支持的能力，而不是每个工具自己实现一套。

---

## 11. Plugin 与 App

### 11.1 BasePlugin：全部钩子与短路语义

[`plugins/base_plugin.py:41`](../src/google/adk/plugins/base_plugin.py) 的 `BasePlugin` 是 App 级拦截器。Plugin 与 Agent callback 的关键差异：

- **Agent callback** 挂在单个 agent 上，只对该 agent 生效。
- **Plugin** 挂在 App 上，作用于**所有 agent**、**所有 tool**、**整个 invocation**。

钩子方法齐全，覆盖 invocation / agent / model / tool 四层：

| 钩子 | 位置 | 签名（精简） | 返回值生效方式 |
|---|---|---|---|
| `on_user_message_callback` | Invocation 开始前 | `invocation_context, user_message` | 返回 `Content` → 替换用户消息 |
| `before_run_callback` | `_exec_with_plugin` 阶段 1 | `invocation_context` | 返回 `Content` → 短路整个 invocation |
| `on_event_callback` | 每个 event yield 前 | `invocation_context, event` | 返回 `Event` → 替换事件 |
| `after_run_callback` | `_exec_with_plugin` 阶段 3 | `invocation_context` | 无返回（仅副作用） |
| `before_agent_callback` | Agent `_run_async_impl` 前 | `agent, callback_context` | 返回 `Content` → 短路 agent 执行 |
| `after_agent_callback` | Agent 结束后 | 同上 | 返回 `Content` → 追加响应 |
| `before_model_callback` | LLM 调用前 | `callback_context, llm_request` | 返回 `LlmResponse` → 跳过 LLM |
| `after_model_callback` | LLM 调用后 | `callback_context, llm_response` | 返回 `LlmResponse` → 替换响应 |
| `on_model_error_callback` | LLM 异常 | `callback_context, llm_request, error` | 返回 `LlmResponse` → 吞掉异常 |
| `before_tool_callback` | Tool 调用前 | `tool, tool_args, tool_context` | 返回 `dict` → 跳过 tool |
| `after_tool_callback` | Tool 调用后 | `tool, tool_args, tool_context, result` | 返回 `dict` → 替换结果 |
| `on_tool_error_callback` | Tool 异常 | `tool, tool_args, tool_context, error` | 返回 `dict` → 吞掉异常 |
| `close` | Runner 关闭时 | 无 | 仅清理资源（关连接、刷 buffer） |

**执行顺序（`PluginManager.run_xxx_callback`）**：

1. **多个插件按注册序依次调用**，前一个插件的返回值作为后一个的输入（修改后的 llm_request 传给下一个 plugin 的 before_model_callback）。
2. **任一插件返回非 None** → 立即停止剩余插件链，也**跳过 agent 级别的 callback**。
3. **所有插件都返回 None** → 走 agent callback（same pattern）。

**典型插件实例**：

- **ToolLoggerPlugin**（在 BasePlugin docstring 里给的例子）：`before_tool_callback` 打印 tool 名和参数、`after_tool_callback` 打印结果。用于全局审计。
- **GlobalInstructionPlugin**：`before_model_callback` 给 `llm_request.config.system_instruction` 加上应用统一的"身份"前缀。取代已废弃的 `LlmAgent.global_instruction`。
- **RateLimitPlugin**：`before_run_callback` 检查用户配额，超限时返回一段 `Content` 告知用户"请稍后再试"。
- **ObservabilityPlugin**：`after_model_callback` 把 token 消耗写入 Prometheus 指标；`after_run_callback` 把 invocation 时长推到 Honeycomb。

**Plugin 的生命周期**：构造时传入 `App.plugins=[...]`，Runner 构造时从 App 取出，装配到 `PluginManager`。Runner 的 `close()` 会调用每个 plugin 的 `close()`，带 `plugin_close_timeout`（默认 5s）——即使某个 plugin 卡住，也不会无限阻塞 Runner 关闭。

### 11.2 App：应用级配置容器

[`apps/app.py:111`](../src/google/adk/apps/app.py) 的 `App` 是 `root_agent` 之上的容器。它集中管理应用级配置：

```python
# src/google/adk/apps/app.py:111-146
class App(BaseModel):
    name: str                                                   # 合法 Python identifier，不能叫 'user'
    root_agent: BaseAgent                                       # 应用根 agent
    plugins: list[BasePlugin] = Field(default_factory=list)     # 应用级插件
    events_compaction_config: Optional[EventsCompactionConfig] = None
    context_cache_config: Optional[ContextCacheConfig] = None   # Gemini 上下文缓存配置
    resumability_config: Optional[ResumabilityConfig] = None    # 可恢复性配置
```

**四个配置字段的作用**：

1. **`plugins`**：见上节。
2. **`events_compaction_config`**（[`apps/app.py:62`](../src/google/adk/apps/app.py)）：
   - `compaction_interval`：每 N 次 user invocation 触发一次滑动窗口压缩。
   - `overlap_size`：相邻压缩区间重叠的 invocation 数，维持上下文连续性。
   - `token_threshold` + `event_retention_size`：token 阈值触发压缩，保留最后若干条原始事件。
3. **`context_cache_config`**（[`agents/context_cache_config.py`](../src/google/adk/agents/context_cache_config.py)）：Gemini 上下文缓存的全局配置（TTL、命名前缀等）。作用于 app 下所有 LLM agent。
4. **`resumability_config`**（[`apps/app.py:42`](../src/google/adk/apps/app.py)）：一个布尔开关 `is_resumable`。启用后整个 app 的 agent 都会在长运行工具处 checkpoint，支持 `runner.run_async(invocation_id=...)` 恢复。

**App vs 裸 `root_agent` 的对比**：

| 特性 | 裸 `Runner(agent=..., ...)` | `Runner(app=App(root_agent=...))` |
|---|---|---|
| 启动成本 | 最少（只要一个 agent） | 稍多（需要命名 app） |
| Plugins | ❌（废弃的 `plugins=[...]` 参数） | ✅ `App.plugins` |
| Event Compaction | ❌ | ✅ |
| Context Cache | ❌ | ✅ |
| 可恢复性 | ❌ | ✅ |
| 推荐程度 | 仅用于最简单 demo | **生产推荐** |

文档明确说 "Providing `app` is the recommended way to create a runner."（[`runners.py:172`](../src/google/adk/runners.py)）。这是 ADK 逐步演进的结果——早期 `Runner(agent=...)` 是主流，后来高级能力都挂到 App 上，裸 agent 的用法仅保留向后兼容。

**App 的 name 约束**：

```python
# src/google/adk/apps/app.py:30-38
def validate_app_name(name: str) -> None:
    if not name.isidentifier():
        raise ValueError(f"Invalid app name '{name}'...")
    if name == "user":
        raise ValueError("App name cannot be 'user'; reserved for end-user input.")
```

name 会出现在 session / memory / artifact 的作用域 key 里，所以要求是 Python identifier。`"user"` 被 event.author 占用，不能作为 app name。

**设计哲学**：App 是**"应用 = root_agent + 运行时政策"**的体现——agent 描述"是什么"，App 描述"怎么跑"。这种分离让同一个 agent 可以被不同的 App 配置使用（例如生产环境启用 compaction、开发环境不启用），而无需改 agent 代码。

---

## 12. CodeExecutor

LLM 经常需要**动态执行代码**来完成复杂任务（数据分析、数学计算、生成图表）。ADK 把这个能力抽象成 `BaseCodeExecutor`，提供多种沙箱实现。

### 12.1 BaseCodeExecutor 抽象

[`code_executors/base_code_executor.py:28`](../src/google/adk/code_executors/base_code_executor.py)：

```python
# src/google/adk/code_executors/base_code_executor.py:28-97（精简）
class BaseCodeExecutor(BaseModel):
    optimize_data_file: bool = False               # 是否从 LLM request 提取 CSV 等数据文件
    stateful: bool = False                          # 执行环境是否跨调用保留变量
    error_retry_attempts: int = 2                   # 连续失败时重试次数
    code_block_delimiters: list[tuple[str, str]] = [
        ('```tool_code\n', '\n```'),
        ('```python\n', '\n```'),
    ]
    execution_result_delimiters: tuple[str, str] = ('```tool_output\n', '\n```')
    timeout_seconds: Optional[int] = None

    @abc.abstractmethod
    def execute_code(
        self,
        invocation_context: InvocationContext,
        code_execution_input: CodeExecutionInput,
    ) -> CodeExecutionResult:
        ...
```

**核心接口只有 `execute_code`**。其余字段是"元属性"——`_code_execution` processor 根据它们决定如何从响应里提取代码块、如何格式化执行结果、失败时重试多少次。

**和工具的差异**：CodeExecutor 不是 tool。LLM 不需要通过 function call 触发它；它是 LlmAgent 的字段（`code_executor=...`），由 `_code_execution` processor 在 response 处理时自动识别代码块并执行。执行结果作为**新的 part** 追加到同一响应里，触发 LLM 基于结果再说一轮。

### 12.2 五种内置后端

| 实现 | 文件 | 沙箱机制 | 状态性 | 适用场景 |
|---|---|---|---|---|
| **`UnsafeLocalCodeExecutor`** | [`unsafe_local_code_executor.py`](../src/google/adk/code_executors/unsafe_local_code_executor.py) | ❌ 无——直接 `exec()` 在当前进程里 | 有限 | 开发调试；信任的代码；**绝不用于生产** |
| **`ContainerCodeExecutor`** | [`container_code_executor.py`](../src/google/adk/code_executors/container_code_executor.py) | Docker 容器，每次执行创建独立容器 | 可配置 | 本地开发的安全沙箱 |
| **`VertexAiCodeExecutor`** | [`vertex_ai_code_executor.py`](../src/google/adk/code_executors/vertex_ai_code_executor.py) | Google Vertex AI Code Execution API | 有限 | GCP 环境下托管的沙箱 |
| **`GkeCodeExecutor`** | [`gke_code_executor.py`](../src/google/adk/code_executors/gke_code_executor.py) | Kubernetes Pod，每次创建 + `k8s-agent-sandbox` 库 | 可选 | 企业级 K8s 集群里的统一沙箱 |
| **`AgentEngineSandboxCodeExecutor`** | [`agent_engine_sandbox_code_executor.py`](../src/google/adk/code_executors/agent_engine_sandbox_code_executor.py) | Vertex AI Agent Engine Sandbox API | ✅ 高 | 部署到 Agent Engine 时首选 |
| **`BuiltInCodeExecutor`** | [`built_in_code_executor.py`](../src/google/adk/code_executors/built_in_code_executor.py) | ❌（本身不执行代码）| — | **占位符**——让 Gemini 使用自己的原生代码执行工具 |

**关键区分**：

- **`BuiltInCodeExecutor` 很特殊**：它**不在本地执行代码**，而是告诉 `_code_execution` processor "让 Gemini 的 built-in code interpreter 来跑"——把代码执行直接下沉到模型侧。Gemini 2.5 系列原生支持，不占用自己的计算资源。
- **`stateful=True`** 的执行器（如 `AgentEngineSandboxCodeExecutor`）会保留变量。前一轮定义的 `df = pd.read_csv(...)` 在后一轮仍然可用。这对"探索式数据分析"场景很重要。
- **`optimize_data_file=True`** 让 `_code_execution` request processor 扫描历史事件里的 CSV 文件引用，自动挂载到执行环境，LLM 可以直接 `pd.read_csv('input.csv')`。

**配置示例**：

```python
# Vertex AI Agent Engine 场景
agent = LlmAgent(
    name='data_analyst',
    model='gemini-2.5-flash',
    instruction='...',
    code_executor=AgentEngineSandboxCodeExecutor(
        resource_name='projects/.../agentExecutionSandbox/...',
        timeout_seconds=60,
        stateful=True,
    ),
)
```

**设计哲学**：把"代码执行"作为 LlmAgent 的独立字段，而不是 tool。两个好处：

1. **代码提取逻辑统一**：`_code_execution` processor 集中处理"从响应里找代码块、执行、把结果贴回"，所有后端共用。
2. **后端可替换**：本地测试用 `UnsafeLocalCodeExecutor`，生产切到 `VertexAiCodeExecutor`，只改一行代码，agent 逻辑不变。

---

## 13. CLI 与 FastAPI 表层

ADK 的 `cli/` 子目录既是**用户入口**，也是**部署适配层**。核心产物有两个：`adk` CLI 工具和 `get_fast_api_app` HTTP 适配器。

### 13.1 `adk` CLI：单一 entry point

[`src/google/adk/cli/cli_tools_click.py`](../src/google/adk/cli/cli_tools_click.py) 用 `click` 框架定义了所有子命令。`pyproject.toml` 里声明 `adk = "google.adk.cli:main"`，装包后 `adk` 命令就可用。

| 子命令 | 职责 | 对应文件 |
|---|---|---|
| `adk run <agent_dir>` | 启动交互式 CLI，用户在终端里和 agent 对话 | [`cli.py`](../src/google/adk/cli/cli.py) |
| `adk web <agents_dir>` | 启动 FastAPI + Angular Web UI（开发调试主力） | [`adk_web_server.py`](../src/google/adk/cli/adk_web_server.py) |
| `adk api_server <agents_dir>` | 启动 FastAPI 服务，不带 UI——供前端或服务调用 | [`fast_api.py`](../src/google/adk/cli/fast_api.py) |
| `adk eval <agent_dir> <eval_set>` | 运行评测集，生成评估报告 | [`cli_eval.py`](../src/google/adk/cli/cli_eval.py) |
| `adk deploy <agent_dir>` | 部署到 Vertex AI Agent Engine / Cloud Run / GKE | [`cli_deploy.py`](../src/google/adk/cli/cli_deploy.py) |
| `adk create <name>` | 脚手架：生成一个最小 agent 目录 | [`cli_create.py`](../src/google/adk/cli/cli_create.py) |

**所有子命令共享的设计**：它们都接收一个 **agent 目录路径**，按 agent 发现约定（[`contributing/adk_project_overview_and_architecture.md`](../contributing/adk_project_overview_and_architecture.md)）从目录里加载 `root_agent` 或 `app`。

### 13.2 Agent 发现约定

这是 ADK 约定中**最关键也最硬性**的一条——任何被 CLI 加载的 agent 必须遵守：

```
my_agent/
├── __init__.py          # 必须包含: from . import agent
└── agent.py             # 必须定义: root_agent = Agent(...)     或     app = App(...)
```

两个硬性要求：

1. `__init__.py` 的 `from . import agent` — 让 Python 的包导入机制能识别 `my_agent.agent` 子模块。
2. `agent.py` 导出 `root_agent` **或** `app` 作为模块级变量。

**发现流程**（简化）：

```python
# 伪代码 —— cli 里 AgentLoader 做的事
module = importlib.import_module(f'{package_name}.agent')
if hasattr(module, 'app'):
    return Runner(app=module.app, ...)
elif hasattr(module, 'root_agent'):
    return Runner(app_name=package_name, agent=module.root_agent, ...)
else:
    raise ValueError('Agent module must define root_agent or app')
```

**对 Web UI**：`adk web` 扫描指定目录下的所有子目录，每个符合结构的就是一个 app，UI 左侧列出所有 app 供选择切换。这就是为什么 `contributing/samples/` 下可以有上百个 agent 放在一起——它们都被 `adk web ./contributing/samples/` 统一托管。

### 13.3 `get_fast_api_app`：agent → HTTP

[`cli/fast_api.py`](../src/google/adk/cli/fast_api.py) 的 `get_fast_api_app(agent_dir=...)` 把一个 agent 目录包装成 FastAPI 应用：

```python
from google.adk.cli.fast_api import get_fast_api_app

app = get_fast_api_app(agent_dir='./agents')

@app.get('/health')
async def health():
    return {'status': 'ok'}
```

生成的 app 自带以下路由：

- `GET /list-apps` — 列出所有发现的 agent app。
- `POST /apps/{app_name}/users/{user_id}/sessions` — 创建 session。
- `GET /apps/{app_name}/users/{user_id}/sessions/{session_id}` — 读 session。
- `DELETE /apps/{app_name}/users/{user_id}/sessions/{session_id}` — 删 session。
- `POST /run` — 同步运行一次 invocation，返回全部事件。
- `POST /run_sse` — **Server-Sent Events 流式**运行，逐个 yield 事件。
- `POST /run_live` — WebSocket 双向流（对应 `Runner.run_live`）。
- `GET /trace/{trace_id}` — 读取 OpenTelemetry span 数据，用于调试。

**Wire format 是 camelCase**——Event 和 LlmResponse 的 Pydantic 配置里 `alias_generator=to_camel`，Python 代码用 snake_case，JSON 里是 camelCase。这是 ADK 明确承诺的公共 API 契约，修改需要 major 版本 bump（见 AGENTS.md 的 "Public API Surface Definition"）。

### 13.4 自定义端点 + Service Registry

用户可以在 `get_fast_api_app` 返回的 `app` 上**继续加路由**（上例的 `/health`）——这让"把 ADK 嵌入现有 FastAPI 服务"成为可能。

[`cli/service_registry.py`](../src/google/adk/cli/service_registry.py) 提供 **Service Registry**——允许用户注册自己的 `SessionService` / `MemoryService` / `ArtifactService` / `CredentialService` 实现，FastAPI 服务启动时按名字实例化。这是给高级部署场景留的后门：不需要改 ADK 源码，也能换存储后端。

**设计哲学**：`get_fast_api_app` 体现"**Agent = Library, Deployment = Integration**"——同一个 agent 代码可以被 `adk run` 本地跑、被 `adk web` 暴露 UI、被 FastAPI 包装成服务、被 `adk deploy` 打包到 Cloud Run。每层切换只需要换外壳，agent 本身不动。

---

## 14. Evaluation 与 A2A

### 14.1 Evaluation 框架

[`src/google/adk/evaluation/`](../src/google/adk/evaluation/) 是 ADK 的**评测子系统**——47 个文件的子目录，独立于核心 agent 逻辑，提供系统化的 agent 质量评估。

**评测的基本单位是 `EvalSet`**：一个 JSON 文件，包含若干 `EvalCase`。每个 case 描述：

- **输入**：`user_content`（测试消息）和初始 `session.state`。
- **参考答案**：`expected_tool_calls`（期望 agent 调用了什么工具）和 `expected_final_response`（期望的最终文本/结构）。
- **元数据**：`name`、`description`、`tags`。

**两大类内置评估指标**：

| 指标 | 评分逻辑 |
|---|---|
| **`tool_trajectory_avg_score`** | agent 调用的 tool 序列和 expected_tool_calls 的重合度。衡量"是否选对了工具、顺序是否合理"。 |
| **`response_match_score`** | 最终回复与参考答案的匹配度（ROUGE-L 或语义相似度）。衡量"答案是否正确"。 |

**Rubric 评测（较新）**：近期新增的能力（见 [`CHANGELOG`](../CHANGELOG.md) 里的 rubric-based evaluation commit）。用户定义一组**自然语言评分标准**（rubric），由 LLM 判分。`evaluate_full_response` 参数控制是只评最终回复还是全程轨迹。这比硬匹配更灵活，适合开放式回答。

**三种运行入口**：

- **UI**：`adk web` 里切到 "Eval" 标签，可视化运行并查看每个 case 的得分、trace。
- **CLI**：`adk eval <agent_dir> <eval_set.json>`，适合 CI/CD 流水线。
- **Pytest 集成**：`evaluation/agent_evaluator.py` 提供 `AgentEvaluator.evaluate(agent_module, eval_set)`，可以写在单测里——commit 前自动跑。

**为什么 eval 是独立子系统？** 因为评测的需求和 agent 运行时完全不同：
- Agent 运行关注**单次交互的正确性**；eval 关注**统计意义的质量**。
- Agent 只读当前 session；eval 要跨 case 聚合指标。
- Agent 面向最终用户；eval 面向开发者。

把它抽离到 `evaluation/` 下，核心 agent 代码不背 evaluation 的复杂度。

### 14.2 A2A：Agent-to-Agent 协议

[`src/google/adk/a2a/`](../src/google/adk/a2a/) 实现 [A2A 协议](https://github.com/google-a2a/A2A/) 的 ADK 适配层。A2A 的核心想法：**让 agent 之间像 HTTP 服务一样通信**——每个 agent 有独立的 URL，按标准协议收发消息。

**ADK 里的 A2A 支持**：

- **`RemoteA2aAgent`**（[`agents/remote_a2a_agent.py`](../src/google/adk/agents/remote_a2a_agent.py)）：一个特殊的 `BaseAgent` 子类。它**不在本地执行**，而是通过 HTTP/gRPC 把 invocation 转发到远端 A2A 服务，收到响应后转换为 Event 流 yield 出来。在 agent 树里看起来和普通子 agent 一样。
- **`a2a/converters/`**：ADK 的 Event / LlmRequest / LlmResponse 与 A2A 协议消息格式的双向转换。
- **A2A server**：ADK 可以反过来**把自己暴露为 A2A 服务**——其他（非 ADK 的）agent 框架可以通过 A2A 协议调用 ADK 跑的 agent。

**典型使用场景**：

```python
# 本地 agent
local_router = LlmAgent(
    name='router',
    sub_agents=[
        RemoteA2aAgent(name='finance_agent', url='https://finance-service/a2a'),
        RemoteA2aAgent(name='hr_agent', url='https://hr-service/a2a'),
        local_support_agent,
    ],
)
```

`router` 通过 `transfer_to_agent` 把请求路由给三个 agent 中的一个；前两个是部署在其他服务上的远程 agent，`router` 无需关心它们内部用什么框架、什么模型。

**为什么 A2A 重要**？

- **团队/组织解耦**：金融团队的 agent、HR 团队的 agent、客服团队的 agent 可以各自独立开发、部署、升级。
- **异构栈整合**：ADK agent 调 LangChain agent、LangChain agent 调 AutoGen agent——统一走 A2A 协议。
- **租户隔离**：A2A agent 可以运行在独立的网络环境里，便于合规隔离。

A2A 是比"多 agent 系统"更上一层的抽象——前者是同一进程内的 agent 协作，A2A 是跨进程、跨组织的 agent 通信标准。ADK 通过 `RemoteA2aAgent` 把两者**同构化**——对父 agent 而言，本地子 agent 和远程 A2A agent 看起来完全一样。

---

## 15. 核心设计取舍与扩展点

读完前 14 章再回头看，会发现 ADK 的代码虽然覆盖面很广，但**底层设计决策高度一致**。本章总结这些跨模块的共性决策。

### 15.1 事件流是唯一真相源（Single Source of Truth: Event Stream）

**决策**：所有状态变化都通过**追加事件**表达；从不存在"侧 channel"直接改 state / artifact。

**体现**：

- `state_delta` / `artifact_delta` 是 event 的字段，`session_service.append_event` 统一处理。
- `agent_state` 也是 event 字段——恢复机制就是从事件流重放。
- Rewind 不删事件，而是**追加一个带 rewind marker 的事件**。
- Event compaction 不替换事件，而是**在事件流里插入一个摘要事件**，读取时替换。

**代价**：事件流会膨胀——于是有 compaction；恢复时需要重放事件——于是有 `populate_invocation_agent_states`；某些字段需要"当场应用但不持久化"——于是有 `temp:` 前缀的两步处理。

**好处**：所有会话级行为**可重放、可审计、可回滚**，没有"状态和日志不一致"的 bug 空间。

### 15.2 异步生成器 + Aclosing 的流式骨架

**决策**：从 Runner 到 Agent 到 Flow 到 Model 连接，**全链路都是 `AsyncGenerator[Event, None]`**。

**体现**：

- `Runner.run_async / run_live` 返回 AsyncGenerator。
- `BaseAgent.run_async / run_live` 返回 AsyncGenerator。
- `BaseLlmFlow.run_async` 返回 AsyncGenerator。
- `BaseLlm.generate_content_async` 返回 AsyncGenerator。
- Tool 执行的每一层都返回 event 流。

**代价**：每层都要用 `async with Aclosing(...)` 保证清理，代码看起来啰嗦。

**好处**：
- **天然支持流式**：partial 事件可以立即被 UI 渲染，不用等全部生成完。
- **背压自动传播**：消费方慢则生产方自然阻塞（`asyncio.Queue` / Python async 协议保证）。
- **短路和异常不泄漏**：任一层提前 return 或抛错，整条链自动清理。

### 15.3 插件短路语义：返回值而非控制流

**决策**：所有回调（plugin、agent callback、model callback、tool callback）都用**返回值**决定"短路还是继续"——非 None 即短路。

**体现**：

- `before_run_callback` 返回 Content → 整个 invocation 短路。
- `before_model_callback` 返回 LlmResponse → 跳过 LLM 调用。
- `before_tool_callback` 返回 dict → 跳过 tool 执行。

**好处**：
- **代码路径线性**：callback 不需要 `raise` 特殊异常或返回 `(should_continue, result)` 元组。
- **可组合**：多个 callback 链式调用，前一个的返回值作为后一个的输入——形成一个**单向管道**。
- **可测试**：每个 callback 是纯函数（通常），单独 mock 掉就能测。

### 15.4 Pydantic-First：类型即约束

**决策**：几乎所有数据模型都是 `pydantic.BaseModel` 子类，用它做**构造校验、序列化、Schema 生成**三位一体。

**体现**：

- Agent / LlmAgent / Event / EventActions / LlmRequest / LlmResponse / App / AuthConfig... 全是 Pydantic。
- `FunctionTool._get_declaration` 用 `pydantic.create_model(...)` 生成 JSON Schema。
- Wire format 通过 `alias_generator=to_camel` 自动做 snake_case ↔ camelCase 转换。
- `model_config = ConfigDict(extra='forbid')` 拒绝未知字段——Config schema 严格。

**代价**：Pydantic 的学习曲线；运行时校验开销。

**好处**：
- **IDE 自动补全、类型检查**：每个字段都有精确类型。
- **自动化 Schema**：工具声明、API 文档、配置文件 schema 全部自动生成，不用手写。
- **反序列化免费**：从 YAML / JSON 构造对象不需要额外代码。

### 15.5 关注点分离：Processor / Callback / Plugin 三层

**决策**：对"插入代码到流程某个点"这件事，ADK 区分了**三种机制**，职责互不重叠。

| 机制 | 作用域 | 时机 | 典型用途 |
|---|---|---|---|
| **Processor** | Flow 内部 | 装配 LlmRequest / 处理 LlmResponse | 框架级别的"必做事项"——instruction 注入、contents 组装、cache 配置 |
| **Callback** | 单个 agent / tool | before/after 关键操作 | agent 特化的业务逻辑——这个 agent 在调 tool 前要做鉴权 |
| **Plugin** | 整个 App | 同 callback 但作用于所有 agent/tool | 跨 agent 的横切关注点——审计、限流、统一日志 |

**代价**：学习成本——用户要先理解"我该用哪个"。

**好处**：
- **Processor 不被用户扩展**——保持框架行为的确定性。
- **Callback 专注于"这一个 agent 的特殊需求"**——代码就近在 agent 定义里。
- **Plugin 把"应用级策略"提取出去**——不污染 agent 代码。

### 15.6 抽象在"接口极简 + 实现丰富"上取平衡

**决策**：所有扩展点（BaseLlm / BaseTool / BaseSessionService / BasePlugin / BaseCodeExecutor / BaseAgent）的抽象基类都**只要求一两个方法**，其他行为通过默认实现或可选方法提供。

**体现**：

- `BaseLlm` 只强制 `supported_models()` 和 `generate_content_async`，`connect` 可选（不支持 Live 的模型不实现即可）。
- `BaseTool` 只强制 `name` / `description`，`_get_declaration` 和 `run_async` 默认抛 NotImplementedError；`process_llm_request` 有默认实现。
- `BaseSessionService` 的 `append_event` 基类已提供完整实现（包括 temp state 处理），子类只需要实现 create/get/list/delete。
- `BaseToolset` 只强制 `get_tools`，前缀和缓存由基类处理。

**代价**：读代码时要跳进基类看默认行为。

**好处**：**扩展门槛极低**。写一个自定义 LLM 适配器 ~30 行代码；自定义 SessionService ~50 行；自定义 Plugin 只覆盖需要的钩子即可。

### 15.7 扩展点一览与参考实现

给一个表格收尾，便于二次开发时快速定位扩展点：

| 扩展点 | 基类 | 参考实现 | 典型自定义场景 |
|---|---|---|---|
| LLM 适配器 | `BaseLlm` | `google_llm.py` | 接内部私有模型 / 第三方 API |
| 工具 | `BaseTool` / `FunctionTool` | `function_tool.py`, `agent_tool.py` | 业务 API 封装 |
| 工具集 | `BaseToolset` | `mcp_toolset.py` | 动态发现工具源 |
| 会话存储 | `BaseSessionService` | `database_session_service.py` | 接自研存储 |
| 记忆存储 | `BaseMemoryService` | `vertex_ai_memory_bank_service.py` | 接 Vector DB |
| 制品存储 | `BaseArtifactService` | `gcs_artifact_service.py` | 接对象存储 |
| 凭证服务 | `BaseCredentialService` | `session_state_credential_service.py` | 接 Vault / Secret Manager |
| 代码执行 | `BaseCodeExecutor` | `container_code_executor.py` | 企业沙箱 |
| 编排 Agent | `BaseAgent` | `loop_agent.py`, `parallel_agent.py` | 自定义工作流 |
| 插件 | `BasePlugin` | 无（用户编写） | 审计 / 限流 / 策略 |
| Planner | `BasePlanner` | `plan_re_act_planner.py` | 自定义规划策略 |
| Events Summarizer | `BaseEventsSummarizer` | LLM-based 实现 | 自定义压缩摘要逻辑 |

**结语**

ADK 的复杂度**不在单点**，而在**层与层之间的边界设计**。理解了 Runner → Agent → Flow → Model/Tool → Service 的分层依赖，理解了 Event 作为"数据 + 动作"的双重角色，理解了 Processor / Callback / Plugin 的三层拦截机制，框架的其他细节（Live 双向流、可恢复性、auth dance、compaction）都是在这些原语上搭建的具体能力。

读源码顺序建议：
1. `runners.py:run_async` 走一遍 invocation 生命周期。
2. `base_agent.py` + `llm_agent.py` 看 agent 抽象。
3. `base_llm_flow.py:run_async / _run_one_step_async` 看 Flow 主循环。
4. `single_flow.py` 的 processor 列表，结合每个 processor 文件看职责。
5. 按需深入具体子系统：tools / sessions / memory / plugins。

祝你玩得开心。


