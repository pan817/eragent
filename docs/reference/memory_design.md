# Google ADK Memory 子系统设计文档

> 本文深入解析 ADK 的 Memory 子系统：总体架构、核心设计思想、关键实现细节、完整读写流程。
>
> 所有代码引用标注了 `文件:行号`。引用片段为便于阅读做了最小删节（以 `…` 标出）。

---

## 目录

1. [概述与定位](#1-概述与定位)
2. [核心设计思想与哲学](#2-核心设计思想与哲学)
3. [总体架构分层](#3-总体架构分层)
4. [核心数据模型](#4-核心数据模型)
5. [BaseMemoryService 抽象契约](#5-basememoryservice-抽象契约)
6. [内置实现](#6-内置实现)
7. [接入点：Context / InvocationContext / Runner](#7-接入点context--invocationcontext--runner)
8. [两个内置工具：PreloadMemoryTool 与 LoadMemoryTool](#8-两个内置工具preloadmemorytool-与-loadmemorytool)
9. [完整流程：写入 + 读取](#9-完整流程写入--读取)
10. [关键设计点解读](#10-关键设计点解读)
11. [扩展指南](#11-扩展指南)
12. [设计取舍总结](#12-设计取舍总结)

---

## 1. 概述与定位

### 1.1 Memory 是什么

ADK 的 Memory 子系统（[`src/google/adk/memory/`](../src/google/adk/memory/)）提供 **跨 session 的长期记忆能力**。它的核心想法是：把用户过往对话中产生的关键信息"离线"地沉淀下来，在后续任意 session 里可以通过**查询**的方式检索回来，作为 prompt 的一部分喂给 LLM。

**一句话定位**：Session 是对话的**"完整日志"**，Memory 是从日志里提炼出的**"可搜索知识"**。

### 1.2 与 Session 的分工

这是理解 Memory 的起点。ADK 有三套持久化服务（Session / Memory / Artifact），三者完全不重叠：

| 服务 | 存什么 | 粒度 | 读取模式 | 跨 session | 典型实例 |
|---|---|---|---|---|---|
| **SessionService** | `events`（完整事件流）+ `state`（键值状态） | 一次完整对话 | 线性遍历 | 否（按 session_id 定位） | InMemory / Database / VertexAI |
| **MemoryService** | `MemoryEntry`（带元信息的 Content） | 多次对话提炼 | **按 query 检索** | **是**（同 user 共享） | InMemory / VertexAIMemoryBank / VertexAIRag / Firestore |
| **ArtifactService** | 二进制文件（版本化） | 单个文件 | 按 filename | 看前缀（`user:` 共享） | InMemory / GCS |

两个关键差异需要强调：

- **Session 侧重完整性、Memory 侧重可搜索性**。Session 是 append-only 日志，任何 event 都不会丢；Memory 允许服务端做摘要、压缩、向量化——只要**检索相关性**好就行。
- **Session 按 `(app_name, user_id, session_id)` 定位；Memory 按 `(app_name, user_id)` + `query` 定位**。也就是 Memory 天然是**用户级**的，同一用户跨多次对话的历史可以被统一检索。

### 1.3 典型使用场景

- **长期用户画像**：agent 在多轮对话中零散获取到"用户喜欢 XX"、"用户所在时区 YY"等信息，存入 Memory；下次对话自动调取。
- **跨会话问答**："上次我们聊到的那个项目怎么样了？"——需要从历史 session 里检索相关内容。
- **个性化知识库**：用户上传文档/笔记；agent 把它们写入 Memory；后续可以按问题检索。
- **多 agent 协同**：多个 agent 共享同一 Memory Bank，一个 agent 收集的信息能被另一个 agent 读取。

### 1.4 本文档覆盖范围

本文档分析的代码范围：

- **核心模块**：`src/google/adk/memory/`（7 个文件 + 测试）。
- **接入点**：`agents/context.py`（`add_session_to_memory` / `search_memory` 等方法）、`agents/invocation_context.py`（`memory_service` 字段）。
- **内置工具**：`tools/load_memory_tool.py`、`tools/preload_memory_tool.py`。
- **集成层实现**：`integrations/firestore/firestore_memory_service.py`。
- **CLI / Web 接入**：`cli/utils/service_factory.py`、`cli/adk_web_server.py` 的 memory 相关路由。

下一章进入设计哲学。

---

## 2. 核心设计思想与哲学

ADK Memory 子系统有**五条贯穿始终的设计原则**，它们在各个实现类里反复出现。理解这五条，后续看代码就会有"意料之中"的感觉。

### 2.1 事件流是真相源，Memory 是派生视图

**原则**：Session 的 `events` 是**唯一真相源**（Single Source of Truth），Memory 永远是从 events 派生出来的可搜索视图。

**体现**：

- `BaseMemoryService` 的主写入方法叫 `add_session_to_memory(session)`——直接把**整个 session 对象**作为输入。语义是"把这段对话全部摄取进来，自己决定怎么处理"。
- 所有内置实现都从 `session.events` 里提取 `event.content`（过滤掉无内容的事件），不关心 `session.state`。**state 属于 session 的私有属性，不会进入 Memory**。
- 恢复/修复场景下，**Memory 可以被完全重建**——只要事件流还在，跑一遍 `add_session_to_memory` 就能重建索引。

**代价**：事件格式改变时，Memory 里的旧数据可能需要重新摄取。

**好处**：
- 两套存储**不会出现不一致的状态**。
- 备份/迁移/审计只需处理 session，Memory 是可选的加速层。
- 换 Memory 后端（比如从关键词匹配换到向量检索）不影响 agent 代码。

### 2.2 服务抽象，后端可替换

**原则**：Memory 访问全部走 `BaseMemoryService` 抽象，具体后端（内存、Vertex AI、Firestore、自研 Vector DB）都是可插拔的。

**体现**：

- 抽象类 [`base_memory_service.py:44`](../src/google/adk/memory/base_memory_service.py) 只定义 4 个方法（2 抽象 + 2 可选）。
- 内置 4 个实现，每个都是独立文件；各自的依赖（vertexai / firestore SDK）通过 **lazy import** 和 try/except 隔离——未安装对应 SDK 时不会阻塞导入。
- Runner 只认 `BaseMemoryService`；Agent 通过 `ctx.memory_service` 访问，永远不直接引用实现类。
- CLI 通过 **URI scheme 驱动**自动选择实现：`--memory_service_uri=rag://my-corpus` → `VertexAiRagMemoryService`，`--memory_service_uri=agentengine://456` → `VertexAiMemoryBankService`。

**关键代码**（[`cli/utils/service_factory.py:242-269`](../src/google/adk/cli/utils/service_factory.py)）：

```python
def create_memory_service_from_options(
    *, base_dir: Path | str, memory_service_uri: Optional[str] = None,
) -> BaseMemoryService:
    registry = get_service_registry()
    if memory_service_uri:
        service = registry.create_memory_service(memory_service_uri, agents_dir=str(base_dir))
        if service is None:
            raise ValueError(f"Unsupported memory service URI: {memory_service_uri}")
        return service
    return InMemoryMemoryService()
```

### 2.3 写入双通路：事件摄取 + 直接写

**原则**：Memory 的写入有**两条语义层级**——"丢进去让服务自己摘要"和"我已经提炼好了直接存"。

**体现**：`BaseMemoryService` 定义了三个写入方法，对应两种通路：

| 方法 | 语义 | 输入 | 典型使用者 |
|---|---|---|---|
| `add_session_to_memory(session)` | **从 session 摄取** | 完整 Session 对象 | Web UI 的 `Update memory` 按钮 / 框架默认路径 |
| `add_events_to_memory(events, ...)` | **从事件子集增量摄取** | Event 序列 | 流式 / 增量场景 |
| `add_memory(memories, ...)` | **直接写入预先构造的 MemoryEntry** | MemoryEntry 序列 | 外部知识库导入 / 业务特化提炼 |

前两种把"摘要/提取/向量化"责任推给服务端；第三种把这个责任留给调用方，让用户可以完全自定义存什么。

**`VertexAiMemoryBankService` 同时支持两条通路**：

- 默认走 `memories.ingest_events`（Vertex 端异步处理、自动触发生成）。
- 若 `custom_metadata` 含 `ttl`/`metadata`/`wait_for_completion` 等"仅 generate 支持的键"，自动切换到 `memories.generate`。
- `add_memory` 默认走 `memories.create`；若 `custom_metadata['enable_consolidation']=True`，切到 `memories.generate` + `direct_memories_source`，让 Vertex 对客户端提供的 memory 做二次合并。

这种多路径设计让**同一个 MemoryService 能兼容 Vertex 的多种后端 API**，但对调用者保持统一的 `BaseMemoryService` 接口。

### 2.4 读取双接入：隐式预加载 + 显式工具调用

**原则**：Memory 对 LLM 的暴露有两种模式——**帮它做**（隐式）和**让它自己做**（显式）。

**体现**：ADK 内置了两个工具，分别代表两种模式：

| 工具 | 触发方式 | LLM 是否可见 | 适合场景 |
|---|---|---|---|
| `PreloadMemoryTool` | **每次 LLM 调用前自动执行**，把"用户当前输入"作为 query 去搜 memory，结果塞进 system instruction | ❌ 对 LLM 透明 | 大多数场景——无需模型智力参与，开销可控 |
| `LoadMemoryTool` | **LLM 通过 function call 主动调用** `load_memory(query=...)` | ✅ 作为一个工具声明 | 需要 agent 根据上下文判断"需不需要查"、用什么 query 查——开放式推理场景 |

两个工具可以**共存**——一个做兜底的相关性预注入，一个给 LLM 保留主动权。

### 2.5 Scope 统一：`app_name + user_id`

**原则**：Memory 的隔离单位是 `(app_name, user_id)`。同一应用下的同一用户，看到的 Memory 是共享的；跨应用、跨用户天然隔离。

**体现**：

- `BaseMemoryService` 的 `search_memory` / `add_events_to_memory` / `add_memory` 全部接受 `app_name` 和 `user_id` 两个参数。
- `add_session_to_memory(session)` 从 `session.app_name` / `session.user_id` 自动取。
- `session_id` 是**可选的**第三级 scope——对 InMemory 实现有意义（用作内部分区键），对 Vertex 实现通常忽略（服务端按 user 维度检索）。
- `InMemoryMemoryService` 的内部 key 就是 `f'{app_name}/{user_id}'`：

```python
# src/google/adk/memory/in_memory_memory_service.py:36-37
def _user_key(app_name: str, user_id: str) -> str:
    return f'{app_name}/{user_id}'
```

**关键约束**：`app_name` 和 `user_id` 是隐私边界——Memory 服务实现**绝不能**返回别的 user 的内容。Vertex 实现通过 `scope={'app_name': ..., 'user_id': ...}` 下放给服务端过滤。

---

## 3. 总体架构分层

### 3.1 分层图

```
┌─────────────────────────────────────────────────────────────────────┐
│  用户入口层                                                          │
│  - CLI: adk web / adk api_server  通过 --memory_service_uri 注入    │
│  - 自定义调用：Runner(app=..., memory_service=...)                  │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Runner 层                                                           │
│  - Runner 构造时接收 memory_service                                  │
│  - _setup_context_for_new_invocation 把它塞进 InvocationContext      │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Context 层（InvocationContext / CallbackContext / ToolContext）     │
│  - invocation_context.memory_service                                │
│  - context.add_session_to_memory() / add_events_to_memory()          │
│  - context.add_memory() / search_memory(query)                       │
└──────┬──────────────────────────────┬───────────────────────────────┘
       │                              │
       │  "显式" 访问                  │  "隐式" 访问
       ▼                              ▼
┌──────────────────────────┐  ┌────────────────────────────────────────┐
│  Agent callback          │  │  内置 Memory 工具                       │
│  - after_agent_callback  │  │  - PreloadMemoryTool                   │
│    中 await              │  │    （process_llm_request 自动查询）    │
│    ctx.add_session_      │  │  - LoadMemoryTool                      │
│    to_memory()           │  │    （LLM function call 主动查询）      │
└──────────────────────────┘  └───────────────┬────────────────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  BaseMemoryService 抽象                                              │
│  - add_session_to_memory(session)            [abstract]              │
│  - add_events_to_memory(app, user, events)   [optional]              │
│  - add_memory(app, user, memories)           [optional]              │
│  - search_memory(app, user, query)           [abstract]              │
└──────┬──────────┬──────────┬─────────────┬──────────────────────────┘
       │          │          │             │
       ▼          ▼          ▼             ▼
┌─────────┐ ┌────────────┐ ┌─────────┐ ┌──────────────┐
│InMemory │ │VertexAI    │ │VertexAI │ │Firestore     │
│Memory   │ │MemoryBank  │ │RagMemory│ │MemoryService │
│Service  │ │Service     │ │Service  │ │(integrations)│
└─────────┘ └────────────┘ └─────────┘ └──────────────┘
  dict         Vertex        RAG          Firestore
  关键词       Agent         Corpus       + 关键词
               Engine        + 向量
```

### 3.2 关键包与文件

```
src/google/adk/memory/
├── __init__.py                          # 导出 BaseMemoryService / InMemoryMemoryService / VertexAiMemoryBankService
│                                        # VertexAiRagMemoryService 懒导入（vertexai 可选）
├── _utils.py                            # format_timestamp 辅助函数
├── base_memory_service.py               # 核心抽象：BaseMemoryService + SearchMemoryResponse
├── memory_entry.py                      # MemoryEntry 数据模型
├── in_memory_memory_service.py          # 进程内实现，关键词匹配
├── vertex_ai_memory_bank_service.py     # Vertex AI Agent Engine Memory Bank（最复杂，31K）
└── vertex_ai_rag_memory_service.py      # Vertex AI RAG Corpus

src/google/adk/integrations/firestore/
└── firestore_memory_service.py          # Firestore 集成

src/google/adk/tools/
├── load_memory_tool.py                  # LLM 显式调用的 Memory 工具
├── preload_memory_tool.py               # 每轮 LLM 前自动预加载
└── _memory_entry_utils.py               # MemoryEntry → 纯文本提取

src/google/adk/agents/
├── invocation_context.py:148            # memory_service 字段
└── context.py:314-413                   # 四个 memory 方法（add_session / add_events / add / search）
```

### 3.3 依赖关系单向性

所有依赖都是**自上而下**的：

- **Tool** 依赖 **ToolContext**，ToolContext 通过 `_invocation_context` 访问 **MemoryService**。
- **MemoryService** 依赖 **MemoryEntry** 和 **Event**（读 session.events）。
- **MemoryService 实现类** 依赖各自的外部 SDK（vertexai / firestore）。

**反向约束**：
- `base_memory_service.py` **只**从 `..events.event` 和 `..sessions.session` 通过 `TYPE_CHECKING` 导入——避免运行时循环依赖。
- 实现类把 `vertexai` / `google.cloud.firestore` 等重型 SDK 放在**方法内部** import 或 lazy 导入——未安装对应依赖时整个 `memory` 包仍然可用。

### 3.4 三种使用姿势

根据部署方式的不同，Memory 有三种典型启用姿势：

**姿势 1：代码里显式构造（测试/脚本）**
```python
from google.adk.memory import InMemoryMemoryService
from google.adk import Runner, Agent

runner = Runner(
    app_name='demo',
    agent=my_agent,
    session_service=InMemorySessionService(),
    memory_service=InMemoryMemoryService(),
)
```

**姿势 2：CLI 参数驱动（生产部署）**
```bash
adk api_server my_agents/ \
    --session_service_uri=sqlite:///sessions.db \
    --memory_service_uri=agentengine://456
```
URI 前缀自动映射到对应的 MemoryService 实现。

**姿势 3：通过 ServiceRegistry 自定义**
```python
from google.adk.cli.service_registry import get_service_registry

registry = get_service_registry()
registry.register_memory_service(
    scheme='myvector',
    factory=lambda uri, agents_dir: MyVectorDbMemoryService(uri),
)
# 之后可以通过 --memory_service_uri=myvector://... 使用
```

---

## 4. 核心数据模型

Memory 子系统只定义了**两个**公开数据模型。它们刻意保持精简——把复杂度推给具体实现。

### 4.1 `MemoryEntry`：一条记忆

[`memory/memory_entry.py:26`](../src/google/adk/memory/memory_entry.py) 的定义极其简洁：

```python
# src/google/adk/memory/memory_entry.py:26-45
class MemoryEntry(BaseModel):
    content: types.Content
    """The main content of the memory."""

    custom_metadata: dict[str, Any] = Field(default_factory=dict)
    """Optional custom metadata associated with the memory."""

    id: Optional[str] = None
    """The unique identifier of the memory."""

    author: Optional[str] = None
    """The author of the memory."""

    timestamp: Optional[str] = None
    """The timestamp when the original content of this memory happened.

    This string will be forwarded to LLM. Preferred format is ISO 8601 format.
    """
```

字段解读：

| 字段 | 类型 | 含义 | 备注 |
|---|---|---|---|
| `content` | `types.Content` | 记忆的主体（由若干 `Part` 组成——可以是纯文本、图片、file_data 等） | **唯一必填**字段 |
| `custom_metadata` | `dict[str, Any]` | 任意业务字段（标签、分类、权重…） | 透传，不被框架解释 |
| `id` | `Optional[str]` | 记忆的唯一 ID | 服务端生成；某些后端不暴露 |
| `author` | `Optional[str]` | 来源（agent 名 或 `'user'`） | 读取时会被格式化进 prompt |
| `timestamp` | `Optional[str]` | 原始内容发生的时间（**字符串格式**，推荐 ISO 8601） | 会被 PreloadMemoryTool 注入到 prompt |

**关键设计点**：

- **`content` 沿用 Gemini 的 `types.Content`**——和 Event、LlmRequest / LlmResponse 保持一致。这让 event → memory 的转换几乎零代价（只需复制引用）。
- **`timestamp` 是字符串而不是 float/datetime**。这是刻意的——不同 Memory 后端的时间粒度差异很大（数据库可能到毫秒、RAG 检索结果可能只有日期）；统一用字符串并推荐 ISO 8601，既兼容又避免类型转换错误。`_utils.format_timestamp` 提供了从 float 到 ISO 字符串的转换：
  ```python
  # src/google/adk/memory/_utils.py:21-23
  def format_timestamp(timestamp: float) -> str:
      return datetime.fromtimestamp(timestamp).isoformat()
  ```
- **没有 `app_name` / `user_id` 字段**——这些属于 scope，通过调用方法的参数传递，不应**内嵌**到 entry 里。这样同一个 MemoryEntry 可以在不同 scope 间被复用/迁移。
- **`custom_metadata` 与 `revision_labels`**：某些 Vertex 实现会根据 `author` / `timestamp` 自动生成 revision labels，同时把 `custom_metadata` 向 Vertex 的 `config.metadata` 映射。这让 MemoryEntry 的通用字段和后端专有字段都能被同一个模型承载。

### 4.2 `SearchMemoryResponse`：检索结果

[`base_memory_service.py:34`](../src/google/adk/memory/base_memory_service.py) 定义：

```python
class SearchMemoryResponse(BaseModel):
    memories: list[MemoryEntry] = Field(default_factory=list)
```

**只有一个字段**——命中的 `MemoryEntry` 列表。没有"相关度得分"、"总数"、"游标"等字段。

这个极简设计背后的判断：

- **相关度已由服务端排序**，调用方信任顺序即可（最相关的在前）。
- **分页暂不需要**——一次 Memory 查询的目标是"挑出几条最相关的塞进 prompt"，不是"浏览全部结果"。prompt 长度有限，10 条封顶的相关结果通常已足够。
- **如果后端确实需要透传得分/元信息**，可以放进每个 `MemoryEntry.custom_metadata`。

**命中结果的语义**：框架**不保证**同一个 MemoryEntry 不重复出现——例如 `VertexAiRagMemoryService` 会合并跨 session 的相同时间戳事件。后端实现决定去重策略。

### 4.3 与 Event 的转换关系

Memory 与 Session 的 Event 是紧密联系但不相同的两种数据：

| 维度 | `Event` | `MemoryEntry` |
|---|---|---|
| 定义位置 | `events/event.py` | `memory/memory_entry.py` |
| 继承自 | `LlmResponse` | `BaseModel` 直接 |
| 核心字段 | `invocation_id`、`author`、`actions`、`content`、`partial`、`branch`、`id`、`timestamp`... | `content`、`custom_metadata`、`id`、`author`、`timestamp` |
| 时间 | `float`（秒） | `str`（ISO 8601） |
| 场景 | 完整对话日志 | 可检索摘要 |
| 持久化 | 一定持久化 | 可能经过摘要 / 向量化 |

**转换方向**：

- **Event → MemoryEntry**：发生在 `add_session_to_memory`。各实现的做法不同：
  - `InMemoryMemoryService` 直接存 `Event` 对象（构造 MemoryEntry 是在 `search_memory` 时现场完成的）。
  - `VertexAiMemoryBankService` 把 `event.content` 封装成 `GenerateMemoriesRequestDirectContentsSourceEvent` 发给服务端。
  - `VertexAiRagMemoryService` 把每个 event 序列化成 JSON 行，写入临时文件，再上传到 RAG corpus。
- **MemoryEntry → prompt**：`_memory_entry_utils.extract_text(memory)` 把 `memory.content.parts` 里的文本 part 拼成字符串；配合 `memory.author` / `memory.timestamp` 生成 "Time: ... / author: text" 的多行格式（见 `PreloadMemoryTool`）。

**注意**：当前 Memory 工具**只使用文本 part**（见 `PreloadMemoryTool` 源码的注释 "Currently this tool only uses text part from the memory"）。图片 / 音频等非文本内容存在 Memory 里但不会进 prompt——这是一个可扩展点。

---

## 5. BaseMemoryService 抽象契约

### 5.1 完整接口定义

[`base_memory_service.py:44-141`](../src/google/adk/memory/base_memory_service.py) 的 `BaseMemoryService` 类总共声明了 4 个方法。它们被**刻意分成"必填"和"可选"两档**——这是 ADK 所有扩展点的一贯风格：门槛低、按需深入。

```python
# src/google/adk/memory/base_memory_service.py:44-141（精简）
class BaseMemoryService(ABC):
    # ① 必填：从 session 摄取记忆
    @abstractmethod
    async def add_session_to_memory(self, session: Session) -> None:
        """Adds a session to the memory service.
        A session may be added multiple times during its lifetime."""

    # ② 可选：从指定 event 子集摄取（默认抛 NotImplementedError）
    async def add_events_to_memory(
        self, *, app_name: str, user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        raise NotImplementedError(
            "This memory service does not support adding event deltas. "
            "Call add_session_to_memory(session) to ingest the full session."
        )

    # ③ 可选:直接写入预构造的 MemoryEntry（默认抛 NotImplementedError）
    async def add_memory(
        self, *, app_name: str, user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        raise NotImplementedError(
            "This memory service does not support direct memory writes. "
            "Call add_events_to_memory(...) or add_session_to_memory(session) instead."
        )

    # ④ 必填：按 query 检索
    @abstractmethod
    async def search_memory(
        self, *, app_name: str, user_id: str, query: str,
    ) -> SearchMemoryResponse:
        ...
```

### 5.2 必填 vs 可选的判断

**为什么只强制 2 个方法？**

因为"摄取 session + 检索"构成了 Memory 的最小闭环。任何 MemoryService 不能没有它们。其余两个是**便利方法**：

- `add_events_to_memory`：如果后端支持增量摄取（例如流式写入 Vector DB），实现它会更高效；否则用户总能退化为"收集事件 → 构造一个临时 Session 对象 → 调 add_session_to_memory"。
- `add_memory`：适合"我已经提炼好了，别再服务端摘要了"的场景。如果后端不支持"跳过摘要直接写"，不实现它合情合理。

两个可选方法的**默认实现都是抛 `NotImplementedError`**（不是 `pass` 也不是返回空）。这是明确的信号："我支持调用这个方法" != "我吞掉调用"。调用方需要在 try/except 里判断。

### 5.3 签名细节与隐含约束

**`add_session_to_memory(session)` 只接收 `session` 对象**
- 隐含约束：实现类必须从 `session.app_name` / `session.user_id` 自行提取 scope。
- 好处：调用方无需额外拼接参数，传 Session 对象即可；scope 不会不一致。
- 代价：Session 对象必须是完整的——如果只拿到 `events` 序列就必须用 `add_events_to_memory`。

**`add_events_to_memory` 要求传 `app_name` / `user_id`**
- 因为 event 本身不携带 scope（Event 里只有 `invocation_id` / `author`），增量摄取时 scope 必须显式传。
- `session_id` 可选——InMemory 实现用它做内部分区，Vertex 实现目前忽略（未来可能用作 stream_id）。
- `custom_metadata` 是**实现专属字段的逃生舱**——参见下节。

**`search_memory` 的签名最简单**
- 只需 `(app_name, user_id, query)`——没有过滤器、没有分页、没有时间范围。
- 高级查询可以通过返回结果在调用方自行过滤；或者通过**自定义 MemoryService 实现**扩展签名（但那就不符合 `BaseMemoryService` 契约了）。
- `query: str` 而不是 `query: types.Content`——Memory 检索的入参目前只支持文本；多模态检索是未来可扩展方向。

### 5.4 `custom_metadata` 的作用

这是个贯穿三个写入方法的参数，作用是**给具体实现"留后门"**，传递后端专属配置而不污染基础签名。

**`VertexAiMemoryBankService` 的用法**（[`vertex_ai_memory_bank_service.py:46-99`](../src/google/adk/memory/vertex_ai_memory_bank_service.py)）：

```python
_GENERATE_MEMORIES_CONFIG_FALLBACK_KEYS = frozenset({
    'disable_consolidation', 'disable_memory_revisions', 'http_options',
    'metadata', 'metadata_merge_strategy', 'revision_expire_time',
    'revision_labels', 'revision_ttl', 'ttl', 'wait_for_completion',
})

_INGEST_EVENTS_CONFIG_FALLBACK_KEYS = frozenset({
    'force_flush', 'generation_trigger_config', 'stream_id',
})

def _should_use_generate_memories(custom_metadata):
    """如果 custom_metadata 含有仅 generate 支持的 key，切换到 generate API 路径。"""
    if not custom_metadata:
        return False
    for key in custom_metadata:
        if key not in _INGEST_EVENTS_CONFIG_FALLBACK_KEYS and key in _GENERATE_MEMORIES_CONFIG_FALLBACK_KEYS:
            return True
    return False
```

调用方通过 `custom_metadata` **间接选择 API 路径**。这很巧妙：用户不需要知道 Vertex 有两条 API，只要知道"我需要 TTL 功能"就行——传 `custom_metadata={'ttl': '6000s'}`，实现自动走 generate 路径。

**命名约定**：`custom_metadata` 里的 key 是**实现专属的**，换一个 MemoryService 实现可能完全不认识这些 key。这不是 bug——契约就是"不保证兼容"。

### 5.5 与 Session 的最小依赖

`BaseMemoryService` 只依赖 `Session` 和 `Event` 两个类，而且都是 TYPE_CHECKING 导入：

```python
# src/google/adk/memory/base_memory_service.py:22-31
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from .memory_entry import MemoryEntry

if TYPE_CHECKING:
    from ..events.event import Event
    from ..sessions.session import Session
```

这保证了 Memory 子系统**不在导入时拉起整个 session 子系统**——自定义 Memory 实现如果只处理 `MemoryEntry`（通过 `add_memory` + `search_memory`），可以完全不关心 Session / Event。

---

## 6. 内置实现

ADK 在 `memory/` 目录下内置了 3 个 Memory 实现，外加 `integrations/firestore/` 里一个集成层实现。每个都针对不同场景设计。

### 6.1 InMemoryMemoryService：开发与测试

[`in_memory_memory_service.py:45`](../src/google/adk/memory/in_memory_memory_service.py) 是最简单的实现——全部数据放在进程内存，用**关键词匹配**做检索。

**存储结构**：

```python
# src/google/adk/memory/in_memory_memory_service.py:54-60
def __init__(self):
    self._lock = threading.Lock()
    self._session_events: dict[str, dict[str, list[Event]]] = {}
    """Keys are "{app_name}/{user_id}". Values are dicts of session_id to
    session event lists."""
```

嵌套三层 dict：`{app_name}/{user_id}` → `session_id` → `list[Event]`。用 `threading.Lock` 保证并发安全。

**写入**（`add_session_to_memory`）：

```python
# src/google/adk/memory/in_memory_memory_service.py:62-72
async def add_session_to_memory(self, session: Session) -> None:
    user_key = _user_key(session.app_name, session.user_id)
    with self._lock:
        self._session_events[user_key] = self._session_events.get(user_key, {})
        self._session_events[user_key][session.id] = [
            event
            for event in session.events
            if event.content and event.content.parts
        ]
```

**要点**：
- **过滤掉无内容的事件**——只保留 `event.content` 非空、`parts` 非空的事件。纯动作事件（如 `actions.state_delta`-only）不进 Memory。
- **完全覆盖写**：`self._session_events[user_key][session.id] = [...]`——如果同一个 session 多次调用 `add_session_to_memory`，后者完全覆盖前者。这是幂等行为。

**增量写入**（`add_events_to_memory`）：

```python
# src/google/adk/memory/in_memory_memory_service.py:74-101
async def add_events_to_memory(
    self, *, app_name, user_id, events,
    session_id: str | None = None,
    custom_metadata: Mapping[str, object] | None = None,
) -> None:
    _ = custom_metadata                                          # InMemory 实现忽略 custom_metadata
    user_key = _user_key(app_name, user_id)
    scoped_session_id = session_id or _UNKNOWN_SESSION_ID        # 缺失时用固定占位符
    events_to_add = [e for e in events if e.content and e.content.parts]

    with self._lock:
        self._session_events[user_key] = self._session_events.get(user_key, {})
        existing_events = self._session_events[user_key].get(scoped_session_id, [])
        existing_ids = {event.id for event in existing_events}
        for event in events_to_add:
            if event.id not in existing_ids:                     # 按 id 去重
                existing_events.append(event)
                existing_ids.add(event.id)
        self._session_events[user_key][scoped_session_id] = existing_events
```

- 追加而非覆盖——幂等靠 `event.id` 集合判断。
- `session_id=None` 时所有增量事件归到固定的 `__unknown_session_id__` 分区。
- 忽略 `custom_metadata`——InMemory 没有后端配置。

**检索**（`search_memory`）：

```python
# src/google/adk/memory/in_memory_memory_service.py:103-134
async def search_memory(self, *, app_name, user_id, query):
    user_key = _user_key(app_name, user_id)
    with self._lock:
        session_event_lists = self._session_events.get(user_key, {})

    words_in_query = _extract_words_lower(query)
    response = SearchMemoryResponse()

    for session_events in session_event_lists.values():
        for event in session_events:
            if not event.content or not event.content.parts:
                continue
            words_in_event = _extract_words_lower(
                ' '.join([part.text for part in event.content.parts if part.text])
            )
            if not words_in_event:
                continue
            if any(query_word in words_in_event for query_word in words_in_query):
                response.memories.append(MemoryEntry(
                    content=event.content,
                    author=event.author,
                    timestamp=_utils.format_timestamp(event.timestamp),
                ))
    return response
```

**关键词匹配算法**：
1. `_extract_words_lower` 用 `re.findall(r'[A-Za-z]+', text)` 提取英文单词并转小写。
2. query 和 event 各自提词，做**集合交集**——任一 query 词在 event 中出现就命中。
3. 命中事件构造 `MemoryEntry` 加入响应。

**局限**：
- 只支持英文（正则只匹配 A-Z / a-z）；中文、emoji、数字全部被忽略。
- 没有相关度排序——命中即返回，顺序是遍历顺序。
- 单线程扫描，线性复杂度。

**定位**：**开发与测试专用**。docstring 明确写了 "should be used for testing and development only"。

### 6.2 另外三个实现在接下来分节介绍

下面三节分别展开：

- **§6.2 VertexAiMemoryBankService** — Vertex AI Agent Engine Memory Bank，最完整的生产级实现。
- **§6.3 VertexAiRagMemoryService** — 基于 Vertex AI RAG Corpus 的向量检索方案。
- **§6.4 FirestoreMemoryService** — 集成层里的 Firestore 方案，展示如何接入自定义数据库。

### 6.2 VertexAiMemoryBankService：生产级主推方案

[`vertex_ai_memory_bank_service.py:174`](../src/google/adk/memory/vertex_ai_memory_bank_service.py) 是 4 个实现里**最复杂**的——700+ 行代码，因为它需要同时对接 Vertex AI Agent Engine 的**两套 API**（`ingest_events` 和 `generate`），并根据调用方的 `custom_metadata` 自动路由。

#### 6.2.1 初始化

```python
# src/google/adk/memory/vertex_ai_memory_bank_service.py:177-220（精简）
class VertexAiMemoryBankService(BaseMemoryService):
    def __init__(
        self,
        project: Optional[str] = None,
        location: Optional[str] = None,
        agent_engine_id: Optional[str] = None,
        *,
        express_mode_api_key: Optional[str] = None,
    ):
        if not agent_engine_id:
            raise ValueError('agent_engine_id is required for VertexAiMemoryBankService.')

        self._project = project
        self._location = location
        self._agent_engine_id = agent_engine_id
        self._express_mode_api_key = get_express_mode_api_key(
            project, location, express_mode_api_key
        )

        # 用户常见错误：传了完整 resource path 而不是 ID
        if agent_engine_id and '/' in agent_engine_id:
            logger.warning(
                "agent_engine_id appears to be a full resource path: '%s'. "
                "Expected just the ID (e.g., '456')."
            )
```

关键点：

- **`agent_engine_id` 是必填**——它标识 Vertex AI 里的 Agent Engine 资源。
- 支持两种认证：标准的 GCP project/location 或 Express Mode API key（个人开发场景）。
- 主动检测常见错误（把完整 resource path 传为 ID）并 warning。

#### 6.2.2 两套写入 API 的路由

**核心路由函数**（[`vertex_ai_memory_bank_service.py:83-99`](../src/google/adk/memory/vertex_ai_memory_bank_service.py)）：

```python
def _should_use_generate_memories(custom_metadata):
    """If custom_metadata contains keys that only GenerateMemories supports,
    use the generate_memories API path. Otherwise ingest_events is default."""
    if not custom_metadata:
        return False
    ingest_keys = _INGEST_EVENTS_CONFIG_FALLBACK_KEYS
    generate_keys = _GENERATE_MEMORIES_CONFIG_FALLBACK_KEYS
    for key in custom_metadata:
        if key not in ingest_keys and key in generate_keys:
            return True
    return False
```

**两套 API 对比**：

| 特性 | `memories.ingest_events` | `memories.generate` |
|---|---|---|
| 适合 | 流式/增量摄取，服务端按 `generation_trigger_config` 决定何时摘要 | 一次性摄取 + 立即摘要 |
| 触发时机 | 异步（默认 idle timeout 触发） | 立即生成 |
| 支持的 config | `stream_id` / `force_flush` / `generation_trigger_config` | `ttl` / `metadata` / `wait_for_completion` / `disable_consolidation` / `metadata_merge_strategy` 等 |
| 延迟 | ~800ms 触发 | 同步等待 |
| ADK 默认 | ✅ 是 | 仅在 `custom_metadata` 含 generate 专属 key 时自动切换 |

**`add_events_to_memory` 走 ingest 路径**（[`vertex_ai_memory_bank_service.py:362-449`](../src/google/adk/memory/vertex_ai_memory_bank_service.py)）：

```python
async def _add_events_to_memory_via_ingest(self, *, app_name, user_id, events_to_process, custom_metadata):
    import vertexai

    direct_events = []
    for event in events_to_process:
        if _should_filter_out_event(event.content):
            continue
        if event.content:
            event_time = None
            if event.timestamp is not None:
                event_time = datetime.datetime.fromtimestamp(
                    event.timestamp, tz=datetime.timezone.utc
                )
            direct_events.append(
                vertexai.types.IngestionDirectContentsSourceEvent(
                    content=event.content,
                    event_id=event.id,
                    event_time=event_time,
                )
            )

    api_client = self._get_api_client()
    request_kwargs = {
        'name': 'reasoningEngines/' + self._agent_engine_id,
        'scope': {'app_name': app_name, 'user_id': user_id},
    }
    # …（从 custom_metadata 里提取 stream_id / force_flush / generation_trigger_config）…

    # Fire-and-forget ingest: 触发延迟 ~800ms，不值得 await
    task = asyncio.create_task(
        api_client.agent_engines.memories.ingest_events(**request_kwargs)
    )
    _background_tasks.add(task)                           # 强引用防 GC
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_ingest_task_error)        # 失败只记 log，不传播
```

**关键设计**：
- **Fire-and-forget 策略**：ingest 请求不阻塞调用方。Vertex ingest 有约 800ms 延迟，await 它会严重拖慢 invocation 结束速度。
- **强引用集合 `_background_tasks`**：`asyncio.create_task` 的结果必须被引用，否则 Python 可能 GC 掉 task 导致请求丢失。ADK 用模块级 `set` 保存，done 时自动 discard。
- **`_log_ingest_task_error` 错误处理**：失败只记 ERROR 日志，不向调用栈传播——Memory 写入失败不应阻塞主流程。

#### 6.2.3 事件过滤规则

`_should_filter_out_event`（[`vertex_ai_memory_bank_service.py:577-594`](../src/google/adk/memory/vertex_ai_memory_bank_service.py)）决定哪些事件进 Memory：

```python
def _should_filter_out_event(content: types.Content) -> bool:
    if not content or not content.parts:
        return True
    for part in content.parts:
        if (part.text or part.inline_data or part.file_data
            or part.function_call or part.function_response
            or part.executable_code or part.code_execution_result
            or part.tool_call or part.tool_response):
            return False
    return True
```

**保留**包含任一实质内容的 event：文本、媒体、函数调用/响应、代码、工具调用。
**过滤掉**空 content 或只有 thought / grounding metadata 等"元"内容的 event。

这个规则比 `InMemoryMemoryService` 的"有 parts 就保留"更严格——Vertex 会计费，要避免把无意义内容发过去。

#### 6.2.4 `add_memory` 的两条路径

```python
# src/google/adk/memory/vertex_ai_memory_bank_service.py:277-307
async def add_memory(self, *, app_name, user_id, memories, custom_metadata=None):
    if _is_consolidation_enabled(custom_metadata):
        # custom_metadata['enable_consolidation']=True → 走 generate + direct_memories_source
        # 让服务端对客户端提供的 memory 做二次合并
        await self._add_memories_via_generate_direct_memories_source(...)
        return

    # 默认：memories.create 直接写入，不经过服务端摘要
    await self._add_memories_via_create(...)
```

两条路径的选择基于一个开关键：`enable_consolidation`。默认**不合并**（直接写，用户 100% 控制写入内容）；显式开启则让服务端合并去重、相似度合并。

**`memories.create` 内部把每个 MemoryEntry 转成 fact 文本**（[`vertex_ai_memory_bank_service.py:782-807`](../src/google/adk/memory/vertex_ai_memory_bank_service.py)）：

```python
def _memory_entry_to_fact(memory: MemoryEntry, *, index: int) -> str:
    if _should_filter_out_event(memory.content):
        raise ValueError(f'memories[{index}] must include text.')

    text_parts = []
    for part in memory.content.parts:
        if part.inline_data or part.file_data:
            raise ValueError(
                f'memories[{index}] must include text only; inline_data and file_data are not supported.'
            )
        if not part.text:
            continue
        stripped_text = part.text.strip()
        if stripped_text:
            text_parts.append(stripped_text)

    if not text_parts:
        raise ValueError(f'memories[{index}] must include non-whitespace text.')
    return '\n'.join(text_parts)
```

`memories.create` **只支持纯文本 fact**——inline_data / file_data 会被直接拒绝。这与 Memory Bank 的 API 契约一致。

#### 6.2.5 检索

```python
# src/google/adk/memory/vertex_ai_memory_bank_service.py:518-550
async def search_memory(self, *, app_name, user_id, query):
    api_client = self._get_api_client()
    retrieved_memories_iterator = await api_client.agent_engines.memories.retrieve(
        name='reasoningEngines/' + self._agent_engine_id,
        scope={'app_name': app_name, 'user_id': user_id},
        similarity_search_params={'search_query': query},
    )

    memory_events: list[MemoryEntry] = []
    async for retrieved_memory in retrieved_memories_iterator:
        memory_events.append(MemoryEntry(
            author='user',                                       # 固定为 'user'
            content=types.Content(
                parts=[types.Part(text=retrieved_memory.memory.fact)],
                role='user',
            ),
            timestamp=retrieved_memory.memory.update_time.isoformat(),
        ))
    return SearchMemoryResponse(memories=memory_events)
```

服务端负责**向量相似度搜索**（Memory Bank 内部用嵌入模型）。返回的 `retrieved_memory.memory.fact` 是纯文本 fact。

**返回的 `author='user'` 是固定的**——因为 Memory Bank 不区分原始 author，所有 memory 都是从用户视角总结出来的"知识点"。这一点和 InMemory 实现不同（后者保留原始 author）。

#### 6.2.6 `_supports_*` 动态适配

`VertexAiMemoryBankService` 有一个有趣的细节——它**运行时探测**当前安装的 `vertexai` SDK 版本是否支持某些字段：

```python
# src/google/adk/memory/vertex_ai_memory_bank_service.py:107-125
def _supports_generate_memories_metadata() -> bool:
    try:
        from vertexai._genai.types import common as vertex_common_types
    except ImportError:
        return False
    return 'metadata' in vertex_common_types.GenerateAgentEngineMemoriesConfig.model_fields

def _supports_create_memory_metadata() -> bool:
    try:
        from vertexai._genai.types import common as vertex_common_types
    except ImportError:
        return False
    return 'metadata' in vertex_common_types.AgentEngineMemoryConfig.model_fields
```

用 `pydantic.BaseModel.model_fields` 判断 SDK 的数据类是否含某个字段。如果不支持就 log warning 并忽略相应的 config——让同一份 ADK 代码能兼容不同版本的 vertexai SDK。

### 6.3 VertexAiRagMemoryService：基于 RAG Corpus

[`vertex_ai_rag_memory_service.py:38`](../src/google/adk/memory/vertex_ai_rag_memory_service.py) 把 session 当作**文档**上传到 Vertex AI RAG Corpus，检索时通过 RAG 的向量语义搜索。

#### 6.3.1 初始化

```python
# src/google/adk/memory/vertex_ai_rag_memory_service.py:41-63
class VertexAiRagMemoryService(BaseMemoryService):
    def __init__(
        self,
        rag_corpus: Optional[str] = None,
        similarity_top_k: Optional[int] = None,
        vector_distance_threshold: float = 10,
    ):
        self._vertex_rag_store = types.VertexRagStore(
            rag_resources=[
                types.VertexRagStoreRagResource(rag_corpus=rag_corpus),
            ],
            similarity_top_k=similarity_top_k,
            vector_distance_threshold=vector_distance_threshold,
        )
```

三个参数：
- `rag_corpus`: RAG corpus 资源名（完整路径或仅 ID）。
- `similarity_top_k`: 每次检索返回的 top-K 候选。
- `vector_distance_threshold`: 向量距离阈值，超过此值的候选被过滤。

#### 6.3.2 写入：session → JSON Lines → 临时文件 → RAG upload

这是最"曲折"的写入路径——RAG corpus 的 API 只接受**文件上传**，不直接接受结构化数据，所以 ADK 把 session 序列化成 JSON Lines 文件：

```python
# src/google/adk/memory/vertex_ai_rag_memory_service.py:65-106
async def add_session_to_memory(self, session: Session):
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as temp_file:
        output_lines = []
        for event in session.events:
            if not event.content or not event.content.parts:
                continue
            text_parts = [
                part.text.replace("\n", " ")
                for part in event.content.parts
                if part.text
            ]
            if text_parts:
                output_lines.append(json.dumps({
                    "author": event.author,
                    "timestamp": event.timestamp,
                    "text": ".".join(text_parts),              # "." 而不是 "\n"：避免 RAG 按行分割出错
                }))
        output_string = "\n".join(output_lines)
        temp_file.write(output_string)
        temp_file_path = temp_file.name

    from ..dependencies.vertexai import rag
    for rag_resource in self._vertex_rag_store.rag_resources:
        rag.upload_file(
            corpus_name=rag_resource.rag_corpus,
            path=temp_file_path,
            # 临时 workaround：RAG upload 不支持 metadata，用 display_name 传 session 信息
            display_name=f"{session.app_name}.{session.user_id}.{session.id}",
        )

    os.remove(temp_file_path)
```

**关键设计巧思**：

- **`display_name=f"{app_name}.{user_id}.{session_id}"`**：RAG 的 `upload_file` 不支持自定义 metadata，ADK 把 scope 信息**编码到 display_name 里**。检索时解析 display_name 来做 app/user 过滤。这是当前 Vertex RAG API 限制下的 workaround。
- **只取 text parts**：图片、音频、function call 都被丢弃——RAG 只索引纯文本。
- **用 "." 连接 parts 而不是换行**：因为 RAG 按行分块索引，保留换行会导致同一个 event 的内容被分成多块，相关性评分被稀释。
- **临时文件用完立即删**：`os.remove(temp_file_path)`——不留磁盘垃圾。

#### 6.3.3 检索：向量搜索 + scope 过滤 + 事件合并

```python
# src/google/adk/memory/vertex_ai_rag_memory_service.py:108-175
async def search_memory(self, *, app_name, user_id, query):
    from ..dependencies.vertexai import rag
    from ..events.event import Event

    response = rag.retrieval_query(
        text=query,
        rag_resources=self._vertex_rag_store.rag_resources,
        rag_corpora=self._vertex_rag_store.rag_corpora,
        similarity_top_k=self._vertex_rag_store.similarity_top_k,
        vector_distance_threshold=self._vertex_rag_store.vector_distance_threshold,
    )

    memory_results = []
    session_events_map = OrderedDict()
    for context in response.contexts.contexts:
        # ① 客户端 scope 过滤（因为 display_name 里编码了 scope）
        if not context.source_display_name.startswith(f"{app_name}.{user_id}."):
            continue
        session_id = context.source_display_name.split(".")[-1]

        # ② 把返回的文本按行解析回 Event 对象
        events = []
        if context.text:
            for line in context.text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    event_data = json.loads(line)
                    content = types.Content(parts=[types.Part(text=event_data.get("text", ""))])
                    event = Event(
                        author=event_data.get("author", ""),
                        timestamp=float(event_data.get("timestamp", 0)),
                        content=content,
                    )
                    events.append(event)
                except json.JSONDecodeError:
                    continue                                 # 非 JSON 行跳过

        # ③ 按 session_id 分组
        if session_id in session_events_map:
            session_events_map[session_id].append(events)
        else:
            session_events_map[session_id] = [events]

    # ④ 合并相同 session 下重叠的 event 列表
    for session_id, event_lists in session_events_map.items():
        for events in _merge_event_lists(event_lists):
            sorted_events = sorted(events, key=lambda e: e.timestamp)
            memory_results.extend([
                MemoryEntry(
                    author=event.author,
                    content=event.content,
                    timestamp=_utils.format_timestamp(event.timestamp),
                )
                for event in sorted_events if event.content
            ])
    return SearchMemoryResponse(memories=memory_results)
```

**四步流程**：
1. **Scope 过滤**：RAG 返回的是"整个 corpus"的相关块——ADK 在客户端用 `display_name` 前缀过滤掉其他用户的数据。注意代码里的 `TODO: Add server side filtering by app_name and user_id`——这是 RAG API 功能未齐备下的 workaround。
2. **反序列化**：把 RAG 返回的文本块按行拆开、JSON 解析回 Event 对象。
3. **按 session 分组**：一次检索可能命中同一 session 的多个片段，先聚类。
4. **事件合并去重**：`_merge_event_lists` 基于 `timestamp` 集合判断两个事件列表是否有重叠，有重叠则合并。这处理了"同一 event 出现在多个检索块里"的情况。

**事件合并算法**（[`vertex_ai_rag_memory_service.py:178-202`](../src/google/adk/memory/vertex_ai_rag_memory_service.py)）：

```python
def _merge_event_lists(event_lists: list[list[Event]]) -> list[list[Event]]:
    """Merge event lists that have overlapping timestamps."""
    merged = []
    while event_lists:
        current = event_lists.pop(0)
        current_ts = {event.timestamp for event in current}
        merge_found = True

        while merge_found:
            merge_found = False
            remaining = []
            for other in event_lists:
                other_ts = {event.timestamp for event in other}
                if current_ts & other_ts:                    # 时间戳有交集 → 合并
                    new_events = [e for e in other if e.timestamp not in current_ts]
                    current.extend(new_events)
                    current_ts.update(e.timestamp for e in new_events)
                    merge_found = True
                else:
                    remaining.append(other)
            event_lists = remaining
        merged.append(current)
    return merged
```

用 **timestamp 作为事件身份**——两个列表有任一 timestamp 重叠就认为是同一段对话的不同切片，合并它们。

#### 6.3.4 RAG 方案的特点

| 维度 | VertexAiRagMemoryService |
|---|---|
| 检索质量 | ✅ 语义向量搜索，比关键词好很多 |
| 写入成本 | 每次 session 上传一个文件到 RAG corpus |
| 跨 session 聚合 | ✅ 天然跨所有历史 session 检索 |
| 数据管理 | 依赖 corpus 管理（删除/TTL 由用户控制） |
| 隐私边界 | 客户端过滤（通过 display_name 前缀） |
| 限制 | 不支持 `add_events_to_memory` / `add_memory`（只实现了两个抽象方法） |
| 适合 | 知识密集型 agent；用户允许数据存在 RAG corpus 的场景 |

**局限**：只实现了 `add_session_to_memory` 和 `search_memory`——不支持增量写入、不支持直接写 MemoryEntry。要写"提炼好的 fact"请用 Memory Bank。

### 6.4 FirestoreMemoryService：集成层的关键词方案

[`integrations/firestore/firestore_memory_service.py:46`](../src/google/adk/integrations/firestore/firestore_memory_service.py) 是 ADK **`integrations/` 子目录下的实现**——技术上它和 `memory/` 里的三个实现地位平等，但被放在集成层因为依赖 `google.cloud.firestore`。

**核心想法**：用 Firestore 的两级 collection 存事件和"关键词索引"，检索时通过 Firestore 的 array_contains 查询命中相关事件。

#### 6.4.1 数据模型

Firestore 里存什么？**`memories` collection**，每个文档对应一个 event，字段如下：

```python
# src/google/adk/integrations/firestore/firestore_memory_service.py:103-117
batch.set(
    doc_ref,
    {
        "appName": session.app_name,
        "userId": session.user_id,
        "keywords": list(keywords),                  # 从 event 文本提取的关键词集合
        "author": event.author,
        "content": event.content.model_dump(exclude_none=True, mode="json"),
        "timestamp": event.timestamp,
    },
)
```

- **`keywords`** 是核心索引字段——Firestore 的 `array_contains` 操作符可以高效检索"含某关键词"的文档。
- `content` 是 `types.Content` 的完整 JSON——检索时反序列化回 `types.Content`。

#### 6.4.2 关键词提取（带 stop words）

```python
# src/google/adk/integrations/firestore/firestore_memory_service.py:126-129
def _extract_keywords(self, text: str) -> set[str]:
    """Extracts keywords from text, ignoring stop words."""
    words = re.findall(r"[A-Za-z]+", text.lower())
    return {word for word in words if word not in self.stop_words}
```

比 `InMemoryMemoryService` 多一步**停用词过滤**——`_stop_words.py` 提供标准英文 stop words 列表，避免 `the` / `is` / `and` 等词污染索引。

#### 6.4.3 批量写入

```python
# src/google/adk/integrations/firestore/firestore_memory_service.py:86-124
async def add_session_to_memory(self, session: Session) -> None:
    batch = self.client.batch()
    count = 0

    for event in session.events:
        if not event.content or not event.content.parts:
            continue
        text = " ".join([part.text for part in event.content.parts if part.text])
        if not text:
            continue
        keywords = self._extract_keywords(text)
        if not keywords:
            continue

        doc_ref = self.client.collection(self.memories_collection).document()
        batch.set(doc_ref, {...})
        count += 1
        if count >= 500:
            await batch.commit()                    # Firestore 限制每 batch ≤500 ops
            batch = self.client.batch()
            count = 0

    if count > 0:
        await batch.commit()
```

**Firestore 批量操作限制**：每个 batch 最多 500 次写入，超出要提交后新开 batch。ADK 做了正确的分批处理。

#### 6.4.4 并发关键词检索

```python
# src/google/adk/integrations/firestore/firestore_memory_service.py:163-195
async def search_memory(self, *, app_name, user_id, query):
    keywords = self._extract_keywords(query)
    if not keywords:
        return SearchMemoryResponse()

    # 每个关键词独立查 Firestore，并发
    tasks = [
        self._search_by_keyword(app_name, user_id, keyword)
        for keyword in keywords
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 合并 + 去重
    seen = set()
    memories = []
    for result_list in results:
        if isinstance(result_list, BaseException):
            logger.warning(f"Memory keyword search partial failure: {result_list}")
            continue
        for entry in result_list:
            content_text = " ".join([p.text for p in entry.content.parts if p.text])
            key = (entry.author, content_text, entry.timestamp)
            if key not in seen:
                seen.add(key)
                memories.append(entry)
    return SearchMemoryResponse(memories=memories)
```

**并发查询**：query 里每个关键词独立查 Firestore，`asyncio.gather(..., return_exceptions=True)` 允许部分失败——任一查询出错只 warning，不影响其他关键词的结果。

**去重 key = (author, content_text, timestamp)**：用三元组作为身份，避免同一个 event 因为命中多个关键词被重复返回。

#### 6.4.5 `_search_by_keyword` 单次查询

```python
# src/google/adk/integrations/firestore/firestore_memory_service.py:131-161
async def _search_by_keyword(self, app_name, user_id, keyword):
    query = (
        self.client.collection(self.memories_collection)
        .where(filter=FieldFilter("appName", "==", app_name))
        .where(filter=FieldFilter("userId", "==", user_id))
        .where(filter=FieldFilter("keywords", "array_contains", keyword))
    )
    docs = await query.get()
    entries = []
    for doc in docs:
        data = doc.to_dict()
        if data and "content" in data:
            try:
                content = types.Content.model_validate(data["content"])
                entries.append(MemoryEntry(
                    content=content,
                    author=data.get("author", ""),
                    timestamp=_utils.format_timestamp(data.get("timestamp", 0.0)),
                ))
            except Exception as e:
                logger.warning(f"Failed to parse memory entry: {e}")
    return entries
```

**三个 WHERE 条件**：
- `appName == app_name` — 应用隔离
- `userId == user_id` — 用户隔离（隐私边界）
- `keywords array_contains keyword` — 关键词命中

Firestore 对这种复合查询需要**复合索引**——使用前要在 Firestore 控制台创建 `appName` + `userId` + `keywords` 的索引。

#### 6.4.6 与其他实现的对比

| 维度 | InMemory | Firestore | Vertex AI Memory Bank | Vertex AI RAG |
|---|---|---|---|---|
| 检索方式 | 关键词 | 关键词 | 向量 + 服务端合并 | 向量（RAG） |
| 持久化 | ❌（进程内） | ✅ | ✅ | ✅ |
| 摘要能力 | 无 | 无 | ✅ 服务端自动 | 无（存完整文本） |
| 并发 | Lock 保护 | Firestore 并发 | Vertex 并发 | RAG 并发 |
| 写入延迟 | 即时 | 批量 commit | Fire-and-forget ~800ms | 文件上传 |
| scope 过滤 | 内部 dict key | Firestore WHERE | 服务端 scope | 客户端 display_name 前缀 |
| 依赖 SDK | 无 | `google-cloud-firestore` | `vertexai` | `vertexai` |
| 适合场景 | 开发测试 | 自建 GCP / 已用 Firestore | GCP 首选生产方案 | 知识库 RAG 场景 |

### 6.5 小结：四个实现的选择建议

- **本地开发 / 单元测试** → `InMemoryMemoryService`（零依赖）。
- **生产环境在 GCP** → `VertexAiMemoryBankService`（官方推荐，自动摘要 + 向量检索）。
- **RAG / 知识库场景** → `VertexAiRagMemoryService`（数据存在 RAG corpus 里，方便其他工具复用）。
- **已使用 Firestore 的技术栈** → `FirestoreMemoryService`（无需额外引入 Vertex 服务）。
- **自建向量数据库** → 自定义 `BaseMemoryService` 实现（见 §11）。

---

## 7. 接入点：Context / InvocationContext / Runner

MemoryService 的实例**如何从 Runner 传到 agent 代码里**？这一章把这条链路讲清楚。

### 7.1 链路总览

```
Runner.__init__(memory_service=...)
    │
    ▼
Runner._setup_context_for_*_invocation()
    │
    ▼
InvocationContext.memory_service    ← ① Memory 服务句柄
    │
    ├──▶ Context(invocation_context)           ← ② Agent callback / Tool 访问层
    │      ├── add_session_to_memory()
    │      ├── add_events_to_memory()
    │      ├── add_memory()
    │      └── search_memory(query)
    │
    └──▶ BaseLlmRequestProcessor / Tool
           └── tool_context.search_memory(query)   ← ③ 工具通过 Context 间接访问
```

三层依次展开。

### 7.2 Runner 层：构造注入

Runner 接受 `memory_service` 作为构造参数：

```python
# src/google/adk/runners.py:151-164（精简）
def __init__(
    self, *,
    app: Optional[App] = None,
    app_name: Optional[str] = None,
    agent: Optional[BaseAgent] = None,
    plugins: Optional[List[BasePlugin]] = None,
    artifact_service: Optional[BaseArtifactService] = None,
    session_service: BaseSessionService,
    memory_service: Optional[BaseMemoryService] = None,         # 可选
    credential_service: Optional[BaseCredentialService] = None,
    ...
):
    self.session_service = session_service
    self.memory_service = memory_service
    ...
```

**关键点**：

- `memory_service` 是 `Optional` 的——不传就是 `None`。这意味着整个 agent 系统可以不配置 Memory 服务，agent 代码里调 `ctx.search_memory` 会抛 ValueError。
- 与 `session_service` 不同——`session_service` 是强制必填的。这反映了 ADK 的**"会话存储是必要的，Memory 是锦上添花"**的判断。

### 7.3 InvocationContext：运行时注入点

`InvocationContext` 是一次 invocation 的"手提箱"，它携带所有服务句柄：

```python
# src/google/adk/agents/invocation_context.py:146-150
artifact_service: Optional[BaseArtifactService] = None
session_service: BaseSessionService
memory_service: Optional[BaseMemoryService] = None
credential_service: Optional[BaseCredentialService] = None
context_cache_config: Optional[ContextCacheConfig] = None
```

**Runner 创建 InvocationContext 时把 memory_service 塞进去**（在 `_setup_context_for_new_invocation` / `_setup_context_for_resumed_invocation` 里）。此后所有 agent、tool、callback 通过 `ctx.memory_service` 访问同一个实例——**它在 invocation 结束前保持不变**。

### 7.4 Context：面向用户代码的访问层

`agents/context.py` 里的 `Context`（即 `CallbackContext`）是 callback / tool 代码看到的"友好版 API"。它包装 InvocationContext，暴露专门的 memory 方法：

```python
# src/google/adk/agents/context.py:314-413（精简）
class Context(ReadonlyContext):
    # === Memory methods ===

    async def add_session_to_memory(self) -> None:
        """触发 Memory 生成当前 session。"""
        if self._invocation_context.memory_service is None:
            raise ValueError("Cannot add session to memory: memory service is not available.")
        await self._invocation_context.memory_service.add_session_to_memory(
            self._invocation_context.session
        )

    async def add_events_to_memory(self, *, events: Sequence[Event], custom_metadata=None) -> None:
        if self._invocation_context.memory_service is None:
            raise ValueError("Cannot add events to memory: memory service is not available.")
        await self._invocation_context.memory_service.add_events_to_memory(
            app_name=self._invocation_context.session.app_name,
            user_id=self._invocation_context.session.user_id,
            session_id=self._invocation_context.session.id,
            events=events,
            custom_metadata=custom_metadata,
        )

    async def add_memory(self, *, memories: Sequence[MemoryEntry], custom_metadata=None) -> None:
        if self._invocation_context.memory_service is None:
            raise ValueError("Cannot add memory: memory service is not available.")
        await self._invocation_context.memory_service.add_memory(
            app_name=self._invocation_context.session.app_name,
            user_id=self._invocation_context.session.user_id,
            memories=memories,
            custom_metadata=custom_metadata,
        )

    async def search_memory(self, query: str) -> SearchMemoryResponse:
        if self._invocation_context.memory_service is None:
            raise ValueError("Memory service is not available.")
        return await self._invocation_context.memory_service.search_memory(
            app_name=self._invocation_context.app_name,
            user_id=self._invocation_context.user_id,
            query=query,
        )
```

**设计要点**：

- **scope 自动填充**：用户调 `ctx.search_memory(query)` 只传 query，`app_name` / `user_id` 由 Context 从 `session` 自动提取并传给 MemoryService。这避免了用户在每个 tool 里重复拼接 scope，也避免"传错用户"的 bug。
- **Null check 统一**：四个方法都会先检查 `memory_service is None` 并抛语义化的 `ValueError`。不会让调用方遇到 `AttributeError: 'NoneType' object has no attribute 'search_memory'`。
- **`search_memory` 的 scope 来源**：调用 `self._invocation_context.app_name` / `self._invocation_context.user_id`（这两个是 InvocationContext 的 `@property`，实际上从 `session.app_name` / `session.user_id` 取值）。

**`ToolContext` 继承 `Context`**（见 `tools/tool_context.py`），因此所有 tool 通过 `tool_context.search_memory(...)` 等方法就能访问 Memory——这就是 `LoadMemoryTool` 和 `PreloadMemoryTool` 的实现基础。

### 7.5 命名歧义：多个 "Context"

ADK 里有几个同名类，容易混淆：

| 类 | 位置 | 功能 |
|---|---|---|
| `InvocationContext` | `agents/invocation_context.py` | 运行时数据容器（持有全部服务句柄） |
| `ReadonlyContext` | `agents/readonly_context.py` | 只读的 context，用于 InstructionProvider 之类的纯读场景 |
| `Context` / `CallbackContext` | `agents/context.py`（`Context` 是新名字，`CallbackContext` 是旧名字） | Agent / Model callback 的上下文，可写 state、访问 memory / artifact |
| `ToolContext` | `tools/tool_context.py` | `Context` 的子类，专门给 tool 用，多了 `function_call_id` 和 `tool_confirmation` |

所有与 Memory 相关的 API 都在 `Context` 层定义；`ToolContext` 继承它，不额外实现 memory 方法。

### 7.6 CLI / Web 层的 Memory 管理路由

`adk web` 提供一个 **UI 按钮**手动触发 "Update memory"——即调用 `MemoryService.add_session_to_memory`：

```python
# src/google/adk/cli/adk_web_server.py:1859-1878
if not self.memory_service:
    raise HTTPException(status_code=400, detail="Memory service is not configured.")
if update_memory_request is None or update_memory_request.session_id is None:
    raise HTTPException(status_code=400, detail="Update memory request is invalid.")

session = await self.session_service.get_session(
    app_name=app_name, user_id=user_id,
    session_id=update_memory_request.session_id,
)
if not session:
    raise HTTPException(status_code=404, detail="Session not found")
await self.memory_service.add_session_to_memory(session)
```

**意义**：ADK **不自动**在 session 结束时摄取 Memory——需要用户**显式**触发（通过 UI 按钮、API 调用、或者 `after_agent_callback` 里自己调）。这让 Memory 写入是**可控的**，不会无脑消耗 Vertex 配额。

---

## 8. 两个内置工具：PreloadMemoryTool 与 LoadMemoryTool

ADK 提供两个"**开箱即用**"的 Memory 工具。它们代表了 Memory 读取的两种模式：**隐式预加载**和**显式工具调用**。理解它们是用好 Memory 的关键。

### 8.1 PreloadMemoryTool：自动预加载

[`tools/preload_memory_tool.py:32`](../src/google/adk/tools/preload_memory_tool.py) 是一个**特殊的 tool**——它继承 `BaseTool` 但**从不被 LLM 调用**。它利用的是 `BaseTool.process_llm_request` 钩子，在每次 LLM 请求前自动执行。

```python
# src/google/adk/tools/preload_memory_tool.py:32-89
class PreloadMemoryTool(BaseTool):
    """A tool that preloads the memory for the current user.

    This tool will be automatically executed for each llm_request, and it won't be
    called by the model.

    NOTE: Currently this tool only uses text part from the memory.
    """

    def __init__(self):
        # Name/description 不会被用到，因为这个 tool 只修改 llm_request
        super().__init__(name='preload_memory', description='preload_memory')

    @override
    async def process_llm_request(self, *, tool_context, llm_request):
        user_content = tool_context.user_content
        if (not user_content or not user_content.parts
                or not user_content.parts[0].text):
            return                                           # 用户输入非文本 → 跳过

        user_query: str = user_content.parts[0].text
        try:
            response = await tool_context.search_memory(user_query)
        except Exception:
            logging.warning('Failed to preload memory for query: %s', user_query)
            return                                           # 失败只 warn 不传播

        if not response.memories:
            return

        memory_text_lines = []
        for memory in response.memories:
            if time_str := (f'Time: {memory.timestamp}' if memory.timestamp else ''):
                memory_text_lines.append(time_str)
            if memory_text := _memory_entry_utils.extract_text(memory):
                memory_text_lines.append(
                    f'{memory.author}: {memory_text}' if memory.author else memory_text
                )
        if not memory_text_lines:
            return

        full_memory_text = '\n'.join(memory_text_lines)
        si = f"""The following content is from your previous conversations with the user.
They may be useful for answering the user's current query.
<PAST_CONVERSATIONS>
{full_memory_text}
</PAST_CONVERSATIONS>
"""
        llm_request.append_instructions([si])
```

**工作原理拆解**：

1. **触发时机**：`process_llm_request` 被 `BaseLlmFlow._preprocess_async` 调用（见 [adk_design.md §6.2](./adk_design.md#62-processor-责任链single-flow-与-auto-flow)）。**每次 LLM 请求前都会跑一遍**。

2. **查询策略**：用 `tool_context.user_content.parts[0].text`——即**用户当前这一轮的输入文本**作为 query。简单粗暴，但对大多数场景够用。

3. **格式化为 system instruction**：
   ```
   The following content is from your previous conversations with the user.
   They may be useful for answering the user's current query.
   <PAST_CONVERSATIONS>
   Time: 2024-10-15T14:30:00
   user: 我住在北京，工作是程序员
   Time: 2024-10-20T09:15:00
   model: 已记录您的职业信息
   </PAST_CONVERSATIONS>
   ```

4. **错误吞掉**：`except Exception: logging.warning(...)` ——MemoryService 故障**不影响主流程**。这是合理的默认：Memory 是锦上添花，不应因为它故障让整个 agent 死掉。

5. **`append_instructions` 而不是拼到 user message**：用 ADK 的 `LlmRequest.append_instructions` 把 memory 作为附加 system instruction 注入——LLM 会视为"系统已知的上下文信息"，不会把它当作用户输入处理。

**使用方式**：

```python
from google.adk import Agent
from google.adk.tools import preload_memory

agent = Agent(
    name='assistant',
    model='gemini-2.5-flash',
    instruction='You are a helpful assistant.',
    tools=[preload_memory],        # 就这一行
)
```

只要在 `tools` 里加上 `preload_memory`，每次 LLM 调用前就会自动查 Memory 并注入相关历史。**LLM 看不到 `preload_memory` 的工具声明**——因为 `_get_declaration` 使用基类默认实现（返回 None），这个工具不进 `llm_request.config.tools`。

### 8.2 LoadMemoryTool：LLM 显式调用

[`tools/load_memory_tool.py:53`](../src/google/adk/tools/load_memory_tool.py) 走另一条路——它是一个**标准的 FunctionTool**，LLM 通过 function call 主动调用。

```python
# src/google/adk/tools/load_memory_tool.py:38-107
async def load_memory(query: str, tool_context: ToolContext) -> LoadMemoryResponse:
    """Loads the memory for the current user.

    Args:
      query: The query to load the memory for.

    Returns:
      A list of memory results.
    """
    search_memory_response = await tool_context.search_memory(query)
    return LoadMemoryResponse(memories=search_memory_response.memories)


class LoadMemoryTool(FunctionTool):
    def __init__(self):
        super().__init__(load_memory)

    @override
    def _get_declaration(self) -> types.FunctionDeclaration | None:
        # 标准的 function declaration，LLM 能调
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={'query': types.Schema(type=types.Type.STRING)},
                required=['query'],
            ),
        )

    @override
    async def process_llm_request(self, *, tool_context, llm_request):
        await super().process_llm_request(tool_context=tool_context, llm_request=llm_request)
        # 告诉 LLM 它有 memory 可以用
        llm_request.append_instructions(["""
You have memory. You can use it to answer questions. If any questions need
you to look up the memory, you should call load_memory function with a query.
"""])

load_memory_tool = LoadMemoryTool()
```

**工作原理**：

1. **工具对 LLM 可见**：`_get_declaration` 返回标准的 function declaration，LLM 在 tools 列表里能看到它。
2. **Instruction 注入**：`process_llm_request` 里调用 `super().process_llm_request` 把工具加到 `llm_request.config.tools`，然后 `append_instructions` 追加一段"你有 memory 可以用"的提示——帮助 LLM 知道"可以主动查"。
3. **执行逻辑 = `search_memory(query)` 的直通**：`load_memory(query, tool_context)` 本质就是调用 `tool_context.search_memory(query)`，包装返回值。

**使用方式**：

```python
from google.adk.tools import load_memory

agent = Agent(
    name='assistant',
    tools=[load_memory],
)
```

LLM 会自己判断何时调用 `load_memory`，比如用户问"上次我们聊到的 XX 怎么样了"，LLM 会调 `load_memory(query='XX')` 拉取相关历史。

### 8.3 两个工具对比

| 维度 | `PreloadMemoryTool` | `LoadMemoryTool` |
|---|---|---|
| LLM 可见 | ❌ | ✅（正常 function call） |
| 触发 | 每次 LLM 请求**自动** | LLM **主动决定**是否调用 |
| Query | 固定用 `user_content.parts[0].text` | LLM 自己决定 |
| 开销 | 每轮一次 MemoryService 调用 | 按需调用（但可能多次） |
| Prompt 污染 | 总是注入（即使无关） | 只在 LLM 认为需要时 |
| 智力要求 | 对 LLM 零要求 | 要求 LLM 能判断"需要查" |
| 适合 | 轻量用户画像、高命中率场景 | 复杂推理、开放领域问答 |

**两个工具可以共存**——一起挂在 tools 列表里：

```python
agent = Agent(
    tools=[preload_memory, load_memory, ...其他工具],
)
```

这时 PreloadMemoryTool 会先跑一遍自动查询（兜底相关历史），LoadMemoryTool 仍然作为 LLM 可调用的工具（按需深挖）。两者服务不同粒度的检索需求。

### 8.4 `_memory_entry_utils.extract_text`：格式化细节

PreloadMemoryTool 调用的辅助函数（[`tools/_memory_entry_utils.py:24-30`](../src/google/adk/tools/_memory_entry_utils.py)）：

```python
def extract_text(memory: MemoryEntry, splitter: str = ' ') -> str:
    """Extracts the text from the memory entry."""
    if not memory.content.parts:
        return ''
    return splitter.join(
        [part.text for part in memory.content.parts if part.text]
    )
```

一个很简单但关键的格式化函数——只保留 `Content.parts` 里的 text part，非文本 part（图片、函数调用等）被过滤。再次印证"**当前 Memory 工具只处理文本**"的约定。

---

## 9. 完整流程：写入 + 读取

把前面各章的知识点串起来，看一次真实交互中 Memory 是如何流转的。

### 9.1 典型 agent 配置

```python
from google.adk import Agent, Runner
from google.adk.apps import App
from google.adk.tools import preload_memory, load_memory
from google.adk.memory import VertexAiMemoryBankService
from google.adk.sessions import DatabaseSessionService

agent = Agent(
    name='personal_assistant',
    model='gemini-2.5-flash',
    instruction='You are a personal assistant. Use memory to recall user context.',
    tools=[preload_memory, load_memory],
)

app = App(name='pa', root_agent=agent)

runner = Runner(
    app=app,
    session_service=DatabaseSessionService('postgresql://...'),
    memory_service=VertexAiMemoryBankService(
        project='my-project', location='us-central1', agent_engine_id='456',
    ),
)
```

三个关键配置：**session 持久化** + **Memory 服务** + **两个 memory 工具**。

### 9.2 写入流程：session 结束后摄取

#### 场景 A：UI 手动触发

用户在 `adk web` 里聊完一轮，点"Update Memory"按钮：

```
[1] 前端发 PUT /apps/pa/users/u1/sessions/s1/memory
[2] adk_web_server 路由处理（adk_web_server.py:1859）
    ├── 检查 self.memory_service 是否配置
    ├── 调 session_service.get_session 取出当前 session
    └── await self.memory_service.add_session_to_memory(session)
[3] VertexAiMemoryBankService.add_session_to_memory(session)
    ├── 从 session.app_name / session.user_id 取 scope
    ├── 遍历 session.events 过滤掉无内容事件
    ├── 调 _add_events_to_memory_from_events
    │   ├── custom_metadata 为空 → 走 ingest 路径
    │   └── asyncio.create_task(api_client.agent_engines.memories.ingest_events(...))
    │       （fire-and-forget，~800ms 后触发 server-side 摘要）
    └── 立即返回（不等 Vertex 完成）
[4] UI 拿到 200 响应，显示 "Memory updated"
[5] Vertex Memory Bank 服务端异步做摘要、去重、生成 memory items
```

#### 场景 B：`after_agent_callback` 自动触发

用户希望每次 agent 结束就自动摄取：

```python
async def auto_save_memory(callback_context: Context):
    try:
        await callback_context.add_session_to_memory()
    except Exception as e:
        logger.warning(f'Failed to save memory: {e}')
    return None  # 不短路

agent = Agent(
    name='assistant',
    ...,
    after_agent_callback=auto_save_memory,
)
```

每次 agent 跑完一次 invocation，自动把整个 session 摄取到 Memory。注意：

- **这会重复摄取**——同一 session 多次被 add_session_to_memory。
- 对 VertexAiMemoryBankService：ingest 路径是**增量的**（Vertex 按 event_id 去重），所以重复调用等价于"把新事件追加"。
- 对 VertexAiRagMemoryService：每次都会上传一个新文件——可能造成重复。建议只在 session **真正结束**时调一次。
- 对 InMemoryMemoryService：完全覆盖写，只保留最后一次的 events。

#### 场景 C：增量摄取

如果想在每一轮 after_model_callback 就增量写入：

```python
async def on_model_done(callback_context: Context, llm_response: LlmResponse):
    # 只把刚产出的 model event 摄取到 memory
    # （假设我们能拿到刚产出的 event）
    await callback_context.add_events_to_memory(
        events=[just_produced_event],
        custom_metadata={'stream_id': f'session-{callback_context._invocation_context.session.id}'},
    )
    return None

agent = Agent(
    ...,
    after_model_callback=on_model_done,
)
```

这种方式适合**流式 / 长对话**——不用等 session 结束就开始沉淀。`custom_metadata['stream_id']` 让 Vertex 识别这是同一个事件流的增量。

### 9.3 读取流程：一次 invocation 的 memory 检索

假设 Memory 里已经存有 "用户住在北京、是程序员" 这条记忆。用户新问题："附近有什么好吃的吗？"

```
[1] Runner.run_async(user_id='u1', session_id='s1', new_message='附近有什么好吃的吗？')

[2] Runner 创建 InvocationContext，memory_service=VertexAiMemoryBankService

[3] LlmAgent.run_async → LlmAgent._llm_flow.run_async → BaseLlmFlow._run_one_step_async

[4] _run_one_step_async 开始 preprocess：
    for processor in request_processors: await processor.run_async(ctx, llm_request)

    其中 contents.request_processor 产生 llm_request.contents = [历史 events..., 新用户消息]
    其中 instructions.request_processor 装配基础 system instruction
    ...

[5] _preprocess_async 后段：await _process_agent_tools(ctx, llm_request)
    内部遍历 agent.canonical_tools(ctx)，对每个 tool 调 tool.process_llm_request(tool_context, llm_request)

    遍历到 PreloadMemoryTool：
    ├── tool_context.user_content = 新的用户消息 "附近有什么好吃的吗？"
    ├── user_query = "附近有什么好吃的吗？"
    ├── response = await tool_context.search_memory(user_query)
    │   └── self._invocation_context.memory_service.search_memory(
    │           app_name='pa', user_id='u1', query='附近有什么好吃的吗？',
    │       )
    │       → Vertex 返回 [MemoryEntry(content='用户住在北京、是程序员', timestamp='2024-...')]
    ├── 格式化成 memory_text:
    │     "Time: 2024-... \n user: 用户住在北京、是程序员"
    └── llm_request.append_instructions([
          '<PAST_CONVERSATIONS>\nTime: ...\nuser: 用户住在北京、是程序员\n</PAST_CONVERSATIONS>'
        ])

    遍历到 LoadMemoryTool：
    ├── 调 super().process_llm_request → 把 load_memory 声明加到 llm_request.config.tools
    └── append_instructions(["You have memory. If any questions need you to look up the memory, call load_memory..."])

[6] LLM 调用：
    llm_request 包含：
    ├── contents = 历史 + 用户新消息
    ├── config.system_instruction 已被追加：
    │   ├── 基础指令
    │   ├── <PAST_CONVERSATIONS>...</PAST_CONVERSATIONS>
    │   └── "You have memory..."
    └── config.tools = [load_memory]

[7] LLM 看到预注入的"用户住在北京"，理解"附近 = 北京附近"：
    LLM 直接回复："北京哪里好吃？推荐簋街小龙虾..."（无需额外调 load_memory）

[8] 如果用户问的是需要更具体历史的问题（如"上次我们聊过的项目怎么样了"），LLM 可能决定主动调 load_memory(query='项目'）：
    ├── function_call 返回 {name: 'load_memory', args: {query: '项目'}}
    ├── functions.py 的 handle_function_calls_async 并发执行
    ├── load_memory 执行体：await tool_context.search_memory('项目')
    ├── 返回 LoadMemoryResponse(memories=[...])
    └── LLM 基于结果生成回复
```

### 9.4 完整的"写-存-读"闭环时序

把上面两段合并成一个跨 session 的大流程：

```
Session A (2024-10-15)
  └─▶ 用户："我住在北京，是程序员"
       Agent："好的，记录了"
       [session A 结束]
       [用户点 UI "Update memory" / after_agent_callback 触发]
       MemoryService.add_session_to_memory(session_A)
         → Vertex Memory Bank 摄取 events → 后台生成 memory items

─────────────────────────────────────────

Session B (2024-10-20，新 session，同一用户)
  └─▶ 用户："附近有什么好吃的？"
       ├─ BaseLlmFlow 预处理
       │  └─ PreloadMemoryTool.process_llm_request
       │     └─ search_memory("附近有什么好吃的？")
       │        → Vertex 向量检索 → 返回 "用户住在北京、是程序员"
       │     └─ 注入到 system instruction
       ├─ LLM 调用（此时模型能看到用户住在北京）
       └─ Agent："北京的话，推荐簋街..."
```

**关键观察**：Memory 把**跨 session 的上下文**自动拼回了 prompt——用户在 Session B 只需要问"附近"，agent 能通过 Memory 知道"附近 = 北京"。

---

## 10. 关键设计点解读

这一章把贯穿 Memory 子系统的**非显而易见的设计决策**列出来，每个都配合源码引用解释。这些是"读代码会疑惑、但知道了就恍然大悟"的地方。

### 10.1 Memory 写入不在 invocation 主循环里

**观察**：ADK 没有在 `Runner.run_async` 或 `BaseLlmFlow.run_async` 的 finally 块里自动 `add_session_to_memory`。

**原因**：
1. **成本可控性**：Memory 写入可能调用付费 API（Vertex），每次 invocation 都写会产生高频费用。
2. **语义不对**：一次 invocation 可能是用户 10 次对话中的一次；合适的摄取时机是"这次对话暂时结束"或"用户明确表示要保存"，而不是"agent 说了一句话"。
3. **写入延迟**：ingest API 有 ~800ms 延迟，插到主循环里会拖慢响应。

**实现体现**：Memory 写入入口是**三个独立触发**——UI 按钮、`after_agent_callback` 用户自行调、`after_run_callback` 插件里调。ADK 只提供工具，不强制策略。

### 10.2 Fire-and-forget ingest

**源码**（[`vertex_ai_memory_bank_service.py:443-448`](../src/google/adk/memory/vertex_ai_memory_bank_service.py)）：

```python
task = asyncio.create_task(
    api_client.agent_engines.memories.ingest_events(**request_kwargs)
)
_background_tasks.add(task)
task.add_done_callback(_background_tasks.discard)
task.add_done_callback(_log_ingest_task_error)
logger.info('Ingest events request triggered.')
```

**关键细节**：

- **`asyncio.create_task` 不 await**——请求发出去就继续执行下一步。
- **模块级 `_background_tasks: set[asyncio.Task]` 强引用**——避免 Python GC 掉 task。根据官方文档（https://docs.python.org/3/library/asyncio-task.html#creating-tasks），`create_task` 只持弱引用，GC 可能意外回收。
- **两个 done_callback**：
  - `_background_tasks.discard` — task 完成时从集合移除，防止内存泄漏。
  - `_log_ingest_task_error` — task 失败时打 error log，不向外抛异常。

**权衡**：Memory 写入变成"best-effort"——失败了只有 log，调用方无法得知。但这是 **Memory 写入不应阻塞主业务**的合理默认。

### 10.3 `custom_metadata` 作为 API 路径选择器

**观察**：`VertexAiMemoryBankService.add_events_to_memory` 的 `custom_metadata` 不光是传给 Vertex 的配置——它**还决定 API 调用路径**（ingest vs generate）。

**源码**（[`vertex_ai_memory_bank_service.py:309-353`](../src/google/adk/memory/vertex_ai_memory_bank_service.py)）：

```python
if _should_use_generate_memories(custom_metadata):
    # custom_metadata 含 generate-only key → generate API
    direct_events = [GenerateMemoriesRequestDirectContentsSourceEvent(content=...) for ...]
    operation = await api_client.agent_engines.memories.generate(...)
    return

# 否则 ingest API
await self._add_events_to_memory_via_ingest(...)
```

**设计妙处**：

- 接口签名不变（`custom_metadata: Mapping[str, object]`）。
- 调用方表达"我需要 TTL" / "我需要等完成"等语义需求，Service 自动选合适的 API。
- 避免给 `BaseMemoryService` 塞一大堆只有部分实现支持的参数。

**学习价值**：当有多个功能相近但差异微妙的底层 API 时，用"配置驱动路径选择"比"多接口"更灵活。

### 10.4 事件过滤规则的两级差异

两个层级的事件过滤：

**InMemory 的宽松过滤**：
```python
# src/google/adk/memory/in_memory_memory_service.py:70-72
[event for event in session.events if event.content and event.content.parts]
```
只要 `content.parts` 非空就保留——即使 part 里是 thought、function call 也算。

**Vertex 的严格过滤**（[`vertex_ai_memory_bank_service.py:577-594`](../src/google/adk/memory/vertex_ai_memory_bank_service.py)）：
```python
def _should_filter_out_event(content):
    if not content or not content.parts:
        return True
    for part in content.parts:
        if (part.text or part.inline_data or part.file_data
            or part.function_call or part.function_response
            or part.executable_code or part.code_execution_result
            or part.tool_call or part.tool_response):
            return False
    return True
```
必须含至少一个"有意义"的 part。纯 thought-only / 纯 grounding_metadata 的 event 会被过滤。

**差异原因**：Vertex 会**计费和运行摘要**。空内容送过去既浪费成本又会污染摘要结果。InMemory 没这些顾虑，宽松即可。

### 10.5 InMemory 的 `_UNKNOWN_SESSION_ID` 占位符

**源码**（[`in_memory_memory_service.py:33, 86`](../src/google/adk/memory/in_memory_memory_service.py)）：

```python
_UNKNOWN_SESSION_ID = '__unknown_session_id__'

async def add_events_to_memory(self, *, app_name, user_id, events, session_id=None, ...):
    user_key = _user_key(app_name, user_id)
    scoped_session_id = session_id or _UNKNOWN_SESSION_ID
    ...
```

**为什么要这个占位符？** InMemory 的存储结构是 `dict[user_key, dict[session_id, list[Event]]]`——它需要一个非 None 的字符串作为内层 dict 的 key。如果 `session_id=None`，直接存会变成 `d[None] = [...]`（虽然 Python 支持 None 作 key，但和字符串 session_id 混用容易造成混乱）。

统一用 `__unknown_session_id__` 字符串做占位，让所有无 session 的增量事件归到同一个桶里。**这也意味着**：重复调用 `add_events_to_memory(session_id=None)` 会把事件累积到同一个桶里（去重仍靠 event.id）。

### 10.6 PreloadMemoryTool 不进 `tools` 声明

**源码**（[`preload_memory_tool.py:32-45`](../src/google/adk/tools/preload_memory_tool.py)）：

```python
class PreloadMemoryTool(BaseTool):
    def __init__(self):
        super().__init__(name='preload_memory', description='preload_memory')
    # 没有 override _get_declaration()
```

**关键点**：`BaseTool._get_declaration()` 默认返回 `None`（见 [`base_tool.py:81-94`](../src/google/adk/tools/base_tool.py)）。`BaseLlmFlow` 组装 `llm_request.config.tools` 时会过滤掉 `_get_declaration()` 返回 None 的工具。

**结果**：
- `preload_memory` 对 LLM **完全透明**，不占 tool 声明的 prompt 字符。
- 但它仍然参与 `_preprocess_async` 的循环——每次 LLM 请求前 `process_llm_request` 被调用。

**这是 ADK 工具系统里一个精巧的用法**——利用"工具"抽象实现"LLM 无感的 request 预处理"。

### 10.7 `ForwardingArtifactService` 与 Memory 的类比

**背景**：`AgentTool`（[`tools/agent_tool.py:235`](../src/google/adk/tools/agent_tool.py)）在执行 sub-agent 时会传 `memory_service=InMemoryMemoryService()`——**为什么不透传父 invocation 的 memory_service？**

```python
# src/google/adk/tools/agent_tool.py:230-240（精简）
sub_runner = Runner(
    app_name=self.agent.name,
    agent=self.agent,
    ...
    memory_service=InMemoryMemoryService(),       # 不透传父 memory service
    artifact_service=ForwardingArtifactService(...),  # Artifact 则透传
)
```

**设计意图**：AgentTool 运行 sub-agent 时，sub-agent 应该"独立完成子任务"，不应污染父 agent 的长期 Memory。Memory 通常存"用户级事实"；子 agent 的短期推理不该被沉淀。

**对比**：Artifact 通过 `ForwardingArtifactService` 透传——因为 artifact（文件）可能是子任务的实际输出，需要传回父 agent 后续使用。

这体现了 ADK 对 **Memory 语义**的判断：**Memory ≈ 用户级长期知识**，不是"任何 agent 的中间产物"。

### 10.8 `search_memory` 没有分页 / 过滤器

**签名**：

```python
async def search_memory(self, *, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
```

**只三个参数，返回扁平列表**。没有：
- `limit` / `offset`
- `start_time` / `end_time`
- `tags` / `custom_filter`
- 相关度 `min_score`

**原因判断**：

- **目标场景简单**：Memory 查询的目标是"挑几条最相关的塞进 prompt"，不是"浏览"。
- **实现差异大**：不同后端支持的过滤能力差异巨大（关键词 vs 向量 vs RAG）。统一抽象会降到最小公约数。
- **扩展走 custom_metadata**：如果后端支持高级过滤，可以通过自定义 `BaseMemoryService` 子类的**额外方法**暴露，不强塞进基类接口。

**副作用**：如果 Memory 里条目非常多（1000+），搜索可能返回大量结果——调用方（PreloadMemoryTool / LoadMemoryTool）要自己做截断。目前 ADK 没有截断，依赖后端的 top-K 逻辑。

---

## 11. 扩展指南

如果内置四个实现不满足需求，可以自定义 `BaseMemoryService` 子类。这一章给出模板和落地建议。

### 11.1 最小自定义实现模板

```python
from collections.abc import Mapping, Sequence
from typing import Optional
from typing_extensions import override

from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.events.event import Event
from google.adk.sessions.session import Session


class MyCustomMemoryService(BaseMemoryService):
    def __init__(self, my_backend_client):
        self.client = my_backend_client

    @override
    async def add_session_to_memory(self, session: Session) -> None:
        events_to_ingest = [
            e for e in session.events
            if e.content and e.content.parts
        ]
        # 调自己的后端写入 API
        await self.client.bulk_insert(
            app_name=session.app_name,
            user_id=session.user_id,
            events=events_to_ingest,
        )

    @override
    async def search_memory(
        self, *, app_name: str, user_id: str, query: str,
    ) -> SearchMemoryResponse:
        raw_hits = await self.client.search(
            app_name=app_name, user_id=user_id, query=query, top_k=10,
        )
        memories = [
            MemoryEntry(
                content=types.Content(parts=[types.Part(text=hit.text)]),
                author=hit.author,
                timestamp=hit.timestamp_iso,
            )
            for hit in raw_hits
        ]
        return SearchMemoryResponse(memories=memories)
```

**只实现这两个方法就能用**。`add_events_to_memory` / `add_memory` 可按需补充（默认抛 NotImplementedError）。

### 11.2 接入自建向量数据库（Pinecone / Weaviate / Qdrant）

**思路**：
1. 初始化时持有 DB 客户端和**embedding 模型**（因为你要自己做 embedding）。
2. `add_session_to_memory` 里：遍历 events → 提取文本 → embedding → 写入向量索引（带 metadata: app_name / user_id / timestamp）。
3. `search_memory` 里：embedding query → 向量检索 → filter by app_name + user_id → 返回 top-K。

**关键注意点**：

- **embedding 耗费 token**——ingest 时写量大，注意 batch。
- **scope 必须写入 metadata 并在 filter 里过滤**——Memory 里绝对不能让 A 用户搜到 B 用户的数据。
- **考虑 idempotency**：Pinecone 等允许指定 id，可以用 `event.id` 作为向量 id，重复写入同 id 的事件会自然去重。

### 11.3 注入到 CLI / FastAPI

`adk api_server` / `adk web` 接受 `--memory_service_uri` 参数，URI 前缀决定选哪个实现。自定义实现可以通过 Service Registry 注册：

```python
# 在 app 启动时
from google.adk.cli.service_registry import get_service_registry
from urllib.parse import urlparse

def create_my_memory_service(uri: str, agents_dir: str):
    parsed = urlparse(uri)
    # myvector://host:port/index?api_key=xxx
    return MyCustomMemoryService(
        backend_url=f'{parsed.hostname}:{parsed.port}',
        index_name=parsed.path.lstrip('/'),
        api_key=parsed.query,
    )

get_service_registry().register_memory_service(
    scheme='myvector',
    factory=create_my_memory_service,
)
```

之后启动：

```bash
adk api_server my_agents/ --memory_service_uri=myvector://localhost:6333/user_memory?api_key=secret
```

### 11.4 与 Session 的协同：**自动写入策略**

ADK 不内置"session 结束自动摄取"——如果需要这个行为，可以加一个 plugin：

```python
from google.adk.plugins import BasePlugin

class AutoMemoryPlugin(BasePlugin):
    def __init__(self):
        super().__init__(name='auto_memory')

    async def after_run_callback(self, *, invocation_context):
        # invocation 结束后自动把 session 写入 memory
        if invocation_context.memory_service:
            try:
                await invocation_context.memory_service.add_session_to_memory(
                    invocation_context.session
                )
            except Exception as e:
                logging.warning(f'Auto memory save failed: {e}')

app = App(
    name='my_app',
    root_agent=agent,
    plugins=[AutoMemoryPlugin()],
)
```

Plugin 方式比 after_agent_callback 更优雅——它是**应用级**的，不需要每个 agent 都挂。

### 11.5 性能优化建议

**写入**：
- **批量**：尽量不要在 every-event 触发摄取。用 buffer 累积 N 条或 N 秒后批量写入。
- **异步**：继续 fire-and-forget 模式；调用方不等结果。
- **字段精简**：能过滤的空内容 / 系统 event 早过滤，减少后端处理量。

**读取**：
- **缓存 query**：如果同一 invocation 内多次调 `search_memory`（比如多个 tool 都需要），可以加一个 lru_cache。
- **query 预处理**：`PreloadMemoryTool` 用的是原始用户输入作 query，有时可以改成 `query = instruction + user_content` 来增加召回。
- **裁剪结果**：返回的 memories 条数太多会挤占 prompt；可以在自定义 Service 里做 top-K + reranking。

**成本**：
- Vertex Memory Bank 按调用计费——UI 按钮触发比 `after_agent_callback` 触发频率低，成本低。
- `stream_id` + `generation_trigger_config` 可以让 Vertex 按 idle_duration 延迟摘要——避免每次 ingest 都立即计算。

---

## 12. 设计取舍总结

读完整个设计文档，回头提炼 Memory 子系统的**核心决策清单**，每一条都有代价也有收益。

### 12.1 Memory ≠ Session，严格分工

**决策**：两套独立存储，职责完全不重叠。

- Session：**线性事件流** + **状态键值**，按 session_id 定位。
- Memory：**可搜索知识**，按 user_id + query 定位。

**代价**：
- 双份存储成本。
- 写入需要显式调用（框架不自动联动）。

**收益**：
- 故障隔离——Memory 服务故障不影响对话。
- 可替换性——任何一方的后端可独立升级。
- 语义清晰——用户能准确理解"什么东西存在哪"。

### 12.2 写入三通路，读取两模式

**决策**：写入 API 分三种粒度（session / events / memories），读取 API 分两种访问模式（预加载 / 显式调用）。

**代价**：
- 接口不对称（写 3 + 读 1）。
- 用户要根据场景选合适的方法。

**收益**：
- 每个场景都有**最自然的表达方式**。
- 后端可以按需实现——不支持的方法抛 NotImplementedError 就行。

### 12.3 Scope 统一且隐私强制

**决策**：所有 API 以 `(app_name, user_id)` 为强制 scope。

**代价**：
- 不支持"全用户搜索"之类的管理员操作——需要绕过 BaseMemoryService。

**收益**：
- 隐私边界**在 API 层就强制**，不易写错。
- 后端可以把 scope 下推给服务端过滤（Vertex `scope={}`）。

### 12.4 框架不自动摄取

**决策**：Memory 写入**必须用户显式触发**（UI / callback / 自定义 plugin）。

**代价**：
- 新用户容易"忘记写入"——跑完 agent 发现 Memory 是空的。
- 最佳时机需要用户判断。

**收益**：
- 成本可控——不会有意外的 Vertex 调用费用。
- 策略灵活——每个应用按自己业务逻辑决定何时摄取。

### 12.5 读取默认"隐式优先"

**决策**：`PreloadMemoryTool`（自动）比 `LoadMemoryTool`（LLM 主动）使用更普遍。

**代价**：
- 每次 LLM 调用都会跑一次 search_memory——低命中率时浪费。
- Prompt 被无关历史污染的风险。

**收益**：
- 对 LLM 智力要求低——连小模型都能用。
- 用户体验可预测——不依赖 LLM 判断。

### 12.6 Fire-and-forget 写入

**决策**：ingest 类 API 不 await 结果。

**代价**：
- 写入失败只有日志，调用方不知道。
- 调试困难——需要看日志 / Vertex Console 确认。

**收益**：
- 主流程不受 Memory 后端性能影响。
- 大大降低 invocation 完成延迟。

### 12.7 只处理文本（当前）

**决策**：`extract_text` 只取 `part.text`，忽略 inline_data / file_data / function_call。

**代价**：
- 多模态 Memory 场景受限——图片作为 memory 的一部分存不进 prompt。
- 用户可能困惑"为什么这张图片没被记住"。

**收益**：
- 实现简单、token 可预测。
- 避免非文本 part 被错误序列化进 Vertex。

**可扩展方向**：未来可能通过多模态 embedding 支持。

### 12.8 `custom_metadata` 作为后门

**决策**：用 `custom_metadata: Mapping[str, object]` 承载所有实现专属配置。

**代价**：
- 没有类型安全——key 写错不会报错。
- 跨实现的 metadata 不兼容。

**收益**：
- 基类签名稳定——新增 Vertex 配置不用改 BaseMemoryService。
- 表达力强——甚至能用它**选择 API 路径**（如 generate vs ingest）。

### 12.9 扩展点清晰

**决策**：`BaseMemoryService` 是**唯一**扩展点。

**收益**：
- 接入 Pinecone / Weaviate / Redis 等向量库只需实现一个类。
- 框架核心代码不动——Runner / Agent / Flow 不感知具体 Memory 实现。

### 12.10 与 ADK 整体哲学的一致性

Memory 子系统是 ADK 设计哲学的缩影——它同时体现了：

- **事件是真相源**（Memory 从 events 派生）
- **服务抽象 + 后端可替换**（4 个内置 + 自定义）
- **异步生成器 / fire-and-forget**（非阻塞）
- **Scope 统一**（app + user）
- **简单核心 + 灵活扩展**（4 方法的抽象类 + custom_metadata 逃生舱）
- **成本意识**（不自动写入）
- **Agent 友好**（两个工具覆盖 99% 场景）

---

## 结语

ADK 的 Memory 子系统**代码不长**（memory/ 目录仅 ~1200 行，加上工具和集成也不到 2000 行），但承载了一个清晰、完整的跨 session 记忆抽象。它的设计把**"记忆 = 可搜索的衍生视图"**这个核心想法贯彻到底：

- 对应用开发者：两个工具挂上，一个 UI 按钮按下，Memory 就跑起来了。
- 对框架开发者：`BaseMemoryService` 是最小需求，想接什么后端都有空间。
- 对运维者：fire-and-forget + scope 过滤让它在生产里既稳又隐私合规。

推荐的阅读顺序：
1. [`base_memory_service.py`](../src/google/adk/memory/base_memory_service.py) 看抽象契约。
2. [`in_memory_memory_service.py`](../src/google/adk/memory/in_memory_memory_service.py) 看最简实现。
3. [`preload_memory_tool.py`](../src/google/adk/tools/preload_memory_tool.py) + [`load_memory_tool.py`](../src/google/adk/tools/load_memory_tool.py) 看 agent 怎么用。
4. [`vertex_ai_memory_bank_service.py`](../src/google/adk/memory/vertex_ai_memory_bank_service.py) 看生产级复杂度。
5. [`context.py:314-413`](../src/google/adk/agents/context.py) 看接入点。

读完这几个文件，对 ADK Memory 的一切就心中有数了。
