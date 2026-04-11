# 会话历史后端接口设计

> 本文档描述将前端 `useChatSessions` hook（当前基于 localStorage）迁移至后端持久化所需的 REST 接口。
> 所有路径前缀默认为 `/api/v1`，与现有 `analyzeQuery` / `initData` / `getTrace` 接口保持一致。

## 目录

- [1. 设计目标与范围](#1-设计目标与范围)
- [2. 数据模型](#2-数据模型)
- [3. 通用约定](#3-通用约定)
- [4. 接口列表](#4-接口列表)
  - [4.1 列出会话](#41-列出会话-get-apiv1sessions)
  - [4.2 创建会话](#42-创建会话-post-apiv1sessions)
  - [4.3 获取会话详情](#43-获取会话详情-get-apiv1sessionssession_id)
  - [4.4 更新会话标题](#44-更新会话标题-patch-apiv1sessionssession_id)
  - [4.5 删除会话](#45-删除会话-delete-apiv1sessionssession_id)
  - [4.6 清空用户全部会话](#46-清空用户全部会话-delete-apiv1sessions)
  - [4.7 追加消息](#47-追加消息-post-apiv1sessionssession_idmessages)
  - [4.8 更新消息](#48-更新消息-patch-apiv1sessionssession_idmessagesmessage_id)
  - [4.9 搜索会话](#49-搜索会话-get-apiv1sessionssearch)
- [5. 与 /analyze 接口的联动](#5-与-analyze-接口的联动)
- [6. 数据库 schema 建议](#6-数据库-schema-建议)
- [7. 前端迁移步骤](#7-前端迁移步骤)
- [8. 验证清单](#8-验证清单)

---

## 1. 设计目标与范围

### 目标

- **零前端功能回退**：`useChatSessions` hook 当前对外暴露的所有能力都要由后端支撑，前端改造后用户体感无差别。
- **多端同步**：同一 `user_id` 在任何设备登录后能看到完整历史。
- **与 `/analyze` 无侵入集成**：现有 `POST /api/v1/analyze` 的请求体已经携带 `session_id`，后端只需在接收到查询时自动落库 user / assistant 两条消息。
- **向后兼容**：未登录用户（guest）仍可走前端 localStorage，不强制调接口；接口层仅为登录态用户启用。

### 非目标

- 实时协作 / 多人共享会话（当前产品场景不需要）
- WebSocket 推送 / 流式消息增量（`/analyze` 是一次性返回，维持现状即可）
- 富媒体消息（图片、附件）—— 消息仅文本 + Markdown
- 会话归档 / 标签 / 置顶等高级特性

### 当前前端行为映射（`useChatSessions` → HTTP）

| 前端方法 | 新接口 | 备注 |
|---|---|---|
| 初始化（`loadFromStorage`） | `GET /sessions` | 按 updatedAt 倒序 |
| `newChat()` | `POST /sessions` | 返回新建会话 |
| `switchTo(id)` | `GET /sessions/{id}` | 按需加载消息 |
| `deleteSession(id)` | `DELETE /sessions/{id}` | |
| `clearAll()` | `DELETE /sessions` | 当前用户所有会话 |
| `setMessages(updater)` 追加 | `POST /sessions/{id}/messages` | 批量追加 |
| `setMessages(updater)` 改写（regenerate 时修改 assistant 占位） | `PATCH /sessions/{id}/messages/{message_id}` | 仅更新 content / status / metadata |
| `deriveTitle(messages)` | 后端自动推导 + `PATCH /sessions/{id}` 允许手改 | |
| `filteredSessions`（search） | `GET /sessions/search?q=...` | 服务端模糊匹配 |

---

## 2. 数据模型

### 2.1 ChatSession

```ts
interface ChatSession {
  id: string;              // UUID v4，后端生成
  user_id: string;         // 所有者
  title: string;           // 展示标题，默认 "新对话"；首次写入消息后自动取首条用户消息前 24 字
  title_auto: boolean;     // true=自动推导；false=用户手工编辑过，后端不再覆盖
  message_count: number;   // 冗余字段，避免列表页联表 count
  last_message_preview: string | null;  // 最后一条消息正文前 60 字，用于列表副标题（可选展示）
  created_at: string;      // ISO8601
  updated_at: string;      // ISO8601，任意 message 变动都要刷新
}
```

**与前端类型的差异：**
- 前端 `ChatSession.messages` 是内联数组，后端拆到 `ChatMessage` 表，列表接口不返回消息体，详情接口才返回。
- 前端 `createdAt` / `updatedAt` 是 `number`（epoch ms），后端统一用 ISO8601 字符串，前端 revive 成 Date。
- 新增 `title_auto`：避免用户手动改过标题后被自动推导覆盖（前端当前版本没这个概念，建议补上）。

### 2.2 ChatMessage

```ts
interface ChatMessage {
  id: string;                                 // UUID，后端生成
  session_id: string;
  role: 'user' | 'assistant';
  content: string;                            // Markdown（assistant） 或纯文本（user）
  status: 'sending' | 'success' | 'error';    // 对应前端 status
  duration_ms: number | null;                 // 仅 assistant 成功回复有值
  trace_id: string | null;                    // /analyze 返回的 trace，用于跳转 TraceModal
  created_at: string;                         // ISO8601
  /** 结构化元信息，保留扩展空间；前端当前会写入：role/useMemory/useExtData 等 SendOptions */
  metadata: Record<string, unknown> | null;
}
```

**关键字段说明：**
- `status='sending'` 仅在前端乐观渲染期间出现；经 `/analyze` 完成后后端固化为 `success` 或 `error`。若是刷新页面后从后端拉的消息，理论上不会再看到 `sending`。
- `metadata` 用于日后扩展，例如把 `SendOptions.role` 存进去，做"分析师角度"回溯；首版留字段但可以不写。

### 2.3 前后端字段映射

前端类型（见 [src/types/api.ts](../src/types/api.ts)）用的是 camelCase，后端响应统一 snake_case。前端服务层做一次映射即可：

| 前端 | 后端 | 备注 |
|---|---|---|
| `ChatMessage.id` | `id` | |
| `ChatMessage.role` | `role` | |
| `ChatMessage.content` | `content` | |
| `ChatMessage.timestamp` (Date) | `created_at` (string) | 前端 `new Date(created_at)` |
| `ChatMessage.status` | `status` | |
| `ChatMessage.durationMs` | `duration_ms` | |
| `ChatMessage.traceId` | `trace_id` | |
| `ChatSession.createdAt` (number) | `created_at` (string) | 前端 `Date.parse(...)` |
| `ChatSession.updatedAt` (number) | `updated_at` (string) | 同上 |

---

## 3. 通用约定

### 3.1 鉴权

- 所有 `/sessions*` 接口均**必须登录**，通过 `X-User-Id` header 或既有会话 cookie 传递 `user_id`。
  - 沿用现在 `/analyze` 的做法：`user_id` 作为 header 或 query 参数；具体鉴权方式由后端团队决定，前端适配。
- 未登录用户所有请求返回 `401 Unauthorized`，前端在 401 时回退到本地 localStorage 路径（保留现有 hook 作为 fallback）。
- 所有接口**必须校验资源归属**：`session_id` 属于当前 `user_id`，否则返回 `404 Not Found`（而不是 403，避免探测）。

### 3.2 错误响应

统一格式：

```json
{
  "error": {
    "code": "SESSION_NOT_FOUND",
    "message": "会话不存在或已被删除",
    "details": null
  }
}
```

| HTTP | code | 场景 |
|---|---|---|
| 400 | `INVALID_PARAMS` | 参数校验失败（role 非法、content 为空等） |
| 401 | `UNAUTHORIZED` | 未登录 |
| 404 | `SESSION_NOT_FOUND` / `MESSAGE_NOT_FOUND` | 资源不存在或不属于当前用户 |
| 409 | `SESSION_FULL` | 单会话消息条数达上限（建议 500） |
| 413 | `CONTENT_TOO_LARGE` | 单条消息 content > 32KB |
| 429 | `RATE_LIMITED` | 触发限流 |
| 500 | `INTERNAL_ERROR` | 服务端异常 |

### 3.3 分页

- 列表接口默认用 **cursor 分页**（按 `updated_at DESC, id DESC`），避免 offset 在高频写入下翻页错位。
- 查询参数：`limit`（默认 20，最大 50）、`cursor`（不透明字符串，服务端自己编码）。
- 响应包含 `next_cursor`（下页 cursor，null 表示到底）。

### 3.4 时间与时区

- 所有时间字段使用 **UTC ISO8601**（例：`2026-04-11T08:30:00.123Z`）。
- 前端按本地时区显示；后端不做时区转换。

### 3.5 幂等性

- `POST /sessions` 支持可选请求头 `Idempotency-Key`，同一 key 在 24h 内重复提交返回同一结果，防止网络重试创建多条。
- `POST /sessions/{id}/messages` 同理，避免断网重试产生重复消息（前端在 `/analyze` 失败重试场景会用到）。

---

## 4. 接口列表

### 4.1 列出会话 `GET /api/v1/sessions`

对应前端 `loadFromStorage` 初始化路径。返回**不含消息体**的会话概览列表，按 `updated_at DESC` 排序。

**Query 参数：**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `limit` | integer | 否 | 20 | 每页条数，1–50 |
| `cursor` | string | 否 | - | 翻页 cursor；为空表示取第一页 |

**请求示例：**

```http
GET /api/v1/sessions?limit=20 HTTP/1.1
X-User-Id: alice
```

**响应 200：**

```json
{
  "sessions": [
    {
      "id": "b2e4c1d8-...",
      "user_id": "alice",
      "title": "分析最近三路匹配异常",
      "title_auto": true,
      "message_count": 4,
      "last_message_preview": "共发现 12 条匹配异常，其中 3 条为高风险...",
      "created_at": "2026-04-10T02:15:33.000Z",
      "updated_at": "2026-04-11T08:30:00.123Z"
    }
  ],
  "next_cursor": "eyJ1IjoiMjAyNi0wNC0xMFQwMjoxNSJ9",
  "total": 17
}
```

**字段说明：**
- `total`：用户全部会话数，用于侧栏显示"X 条历史"徽标（可选，性能敏感可去掉）。
- `next_cursor` 为 `null` 时表示已到末页。

**前端用法：** 首屏只拉第一页即可（20 条），用户滚动到底部再请求 next page。当前 `useChatSessions` 无分页概念，首版可以不实现分页、直接返回全部（MAX_SESSIONS=50），但接口层要预留 cursor。

---

### 4.2 创建会话 `POST /api/v1/sessions`

对应前端 `newChat()`。

**请求体：**

```json
{
  "title": "新对话"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | string | 否 | 初始标题，省略时后端用默认值 "新对话" |

**请求头（可选）：** `Idempotency-Key: <uuid>` 防止重试重复创建。

**响应 201：**

```json
{
  "session": {
    "id": "f3a9e2b0-...",
    "user_id": "alice",
    "title": "新对话",
    "title_auto": true,
    "message_count": 0,
    "last_message_preview": null,
    "created_at": "2026-04-11T08:35:12.000Z",
    "updated_at": "2026-04-11T08:35:12.000Z"
  }
}
```

**行为约定：**
- 后端对同一用户的**空会话数量做限制**（建议最多 3 个，超过时返回最老的空会话而不是新建）。这与前端 `newChat()` "当前是空会话则不创建"的策略一致，避免用户点"新建对话"按钮时产生无限空壳。
- 创建后不自动切换"当前会话"——前端自己维护"当前打开哪个"的状态，无需服务端记。

---

### 4.3 获取会话详情 `GET /api/v1/sessions/{session_id}`

对应前端 `switchTo(id)` —— 点击历史项时加载该会话的全部消息。

**Path 参数：**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session_id` | string | 是 | 会话 UUID |

**Query 参数：**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `message_limit` | integer | 否 | 200 | 返回消息数上限；超出时只返回最近 N 条 + 提示 `has_more_messages=true` |

**响应 200：**

```json
{
  "session": {
    "id": "b2e4c1d8-...",
    "user_id": "alice",
    "title": "分析最近三路匹配异常",
    "title_auto": true,
    "message_count": 4,
    "last_message_preview": "共发现 12 条匹配异常...",
    "created_at": "2026-04-10T02:15:33.000Z",
    "updated_at": "2026-04-11T08:30:00.123Z"
  },
  "messages": [
    {
      "id": "m-001",
      "session_id": "b2e4c1d8-...",
      "role": "user",
      "content": "分析最近的三路匹配异常",
      "status": "success",
      "duration_ms": null,
      "trace_id": null,
      "created_at": "2026-04-10T02:15:33.000Z",
      "metadata": null
    },
    {
      "id": "m-002",
      "session_id": "b2e4c1d8-...",
      "role": "assistant",
      "content": "## 分析结果\n\n共发现 12 条...",
      "status": "success",
      "duration_ms": 8432,
      "trace_id": "trace-abc-123",
      "created_at": "2026-04-10T02:15:41.000Z",
      "metadata": null
    }
  ],
  "has_more_messages": false
}
```

**错误：**
- `404 SESSION_NOT_FOUND`：会话不存在或不属于当前用户

**前端用法：** `switchTo` 触发后拉取，结果写入 `useChatSessions` 的当前会话 state。首版建议一次性返回全部消息（前端 hook 当前也是一次性加载），`has_more_messages` 字段保留扩展。

---

### 4.4 更新会话标题 `PATCH /api/v1/sessions/{session_id}`

对应用户手动改标题的需求。前端当前 hook 没有此能力（标题完全自动推导），建议同步引入——用户双击标题编辑。

**请求体：**

```json
{
  "title": "三路匹配异常专项"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | string | 是 | 新标题，1–80 字；保存后 `title_auto` 置为 `false` |

**响应 200：** 返回更新后的 session 对象（同 4.1）。

**错误：**
- `400 INVALID_PARAMS`：title 为空或超长
- `404 SESSION_NOT_FOUND`

**行为约定：**
- 手动改过标题（`title_auto=false`）的会话，**后端在追加消息时不再覆盖标题**。
- 如果用户想恢复自动标题，可以传空字符串 / `null`，后端重置 `title_auto=true` 并用当前首条消息重新推导。

---

### 4.5 删除会话 `DELETE /api/v1/sessions/{session_id}`

对应前端 `deleteSession(id)`。

**Path 参数：**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session_id` | string | 是 | 会话 UUID |

**请求示例：**

```http
DELETE /api/v1/sessions/b2e4c1d8-... HTTP/1.1
X-User-Id: alice
```

**响应 204 No Content**（无响应体）。

**错误：**
- `404 SESSION_NOT_FOUND`

**行为约定：**
- **级联删除**该会话下所有 `ChatMessage`。
- 为了数据审计安全，建议后端做**软删除**（`deleted_at IS NOT NULL`），列表接口过滤掉，30 天后物理清理。前端无感知。
- 若删除的是前端"当前会话"，前端要自行选择新的 current（拿列表第一条），或者调 4.2 建一个空会话。这是前端职责，接口不管。

---

### 4.6 清空用户全部会话 `DELETE /api/v1/sessions`

对应前端 `clearAll()`。

**请求示例：**

```http
DELETE /api/v1/sessions HTTP/1.1
X-User-Id: alice
```

**可选请求体（强烈推荐）：**

```json
{
  "confirm": "DELETE_ALL"
}
```

**响应 200：**

```json
{
  "deleted_count": 17
}
```

**行为约定：**
- 这是**不可逆操作**，必须要求客户端传 `confirm=DELETE_ALL` 字符串作为双重确认，否则返回 `400 INVALID_PARAMS`。前端现有的确认弹窗（见 [Sidebar.tsx](../src/components/Sidebar.tsx) 中的 `confirmingClear`）天然契合。
- 同样采用软删除。
- 建议同时支持查询参数 `?older_than=<ISO8601>`，只清理某时间之前的会话，为未来"只清 7 天前历史"功能预留；首版不实现，仅留字段。

---

### 4.7 追加消息 `POST /api/v1/sessions/{session_id}/messages`

**这是本次方案里最关键的接口**，直接对应前端 `setMessages(prev => [...prev, userMsg, assistantPlaceholder])` 的等价物。

#### 两种使用方式

**方式 A（推荐）：让 `/analyze` 接口代写**

前端不单独调这个接口，而是把 `session_id` 传给 `/analyze`，由后端在处理分析请求的事务里同时写入两条消息（user 问题 + assistant 回答）。这是**最省事、最不易出 bug** 的做法，详见 [第 5 节](#5-与-analyze-接口的联动)。

方式 A 下，接口 4.7 只作为**补偿通道**存在：前端在某些边缘场景（如 `/analyze` 失败重试、手动粘贴消息、未来的本地命令）需要补录消息时使用。

**方式 B：前端显式调用**

前端完全控制写入时机，每条消息都显式 POST 一次。适合"流式消息" / "消息可编辑" 等未来场景。首版不建议，但接口定义要支持。

#### 请求体

```json
{
  "messages": [
    {
      "role": "user",
      "content": "分析最近的三路匹配异常",
      "client_id": "temp-uuid-1",
      "metadata": {
        "analyst_role": "procurement",
        "use_memory": true,
        "use_ext_data": false
      }
    },
    {
      "role": "assistant",
      "content": "## 分析结果\n\n共发现 12 条...",
      "client_id": "temp-uuid-2",
      "status": "success",
      "duration_ms": 8432,
      "trace_id": "trace-abc-123"
    }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `messages` | array | 是 | 一次可批量追加多条；按数组顺序写入 |
| `messages[].role` | string | 是 | `user` / `assistant` |
| `messages[].content` | string | 是 | 非空，≤32KB |
| `messages[].client_id` | string | 否 | 前端生成的临时 ID，用于响应时做乐观更新匹配 |
| `messages[].status` | string | 否 | 默认 `success`；前端可写 `error` 用于失败占位 |
| `messages[].duration_ms` | number | 否 | 仅 assistant |
| `messages[].trace_id` | string | 否 | 仅 assistant |
| `messages[].metadata` | object | 否 | 任意结构；首版只存不用，未来用于绩效分析 |

**请求头：** `Idempotency-Key: <uuid>` 推荐带上，防止断网重试造成消息重复。

#### 响应 201

```json
{
  "messages": [
    {
      "id": "m-100",
      "client_id": "temp-uuid-1",
      "session_id": "b2e4c1d8-...",
      "role": "user",
      "content": "分析最近的三路匹配异常",
      "status": "success",
      "duration_ms": null,
      "trace_id": null,
      "created_at": "2026-04-11T08:40:00.000Z",
      "metadata": { "analyst_role": "procurement", "use_memory": true, "use_ext_data": false }
    },
    {
      "id": "m-101",
      "client_id": "temp-uuid-2",
      "session_id": "b2e4c1d8-...",
      "role": "assistant",
      "content": "## 分析结果\n\n共发现 12 条...",
      "status": "success",
      "duration_ms": 8432,
      "trace_id": "trace-abc-123",
      "created_at": "2026-04-11T08:40:08.432Z",
      "metadata": null
    }
  ],
  "session": {
    "id": "b2e4c1d8-...",
    "title": "分析最近的三路匹配异常",
    "title_auto": true,
    "message_count": 2,
    "last_message_preview": "共发现 12 条匹配异常...",
    "updated_at": "2026-04-11T08:40:08.432Z"
  }
}
```

**关键响应字段：**
- 每条 message 带回 `client_id`，前端可以根据它把乐观渲染的临时消息替换成服务端权威版本。
- 同时返回更新后的 `session` 概览（新 `title` / `message_count` / `updated_at`），省得前端再调一次列表。

#### 副作用（后端必须实现）

1. **标题自动推导**：若 `session.title_auto=true` 且该会话此前 message_count=0，则用新追加的首条 user 消息 content 前 24 字设置 title。
2. **更新 `updated_at`**：推到当前时间，让列表排序生效。
3. **更新 `message_count`** 和 `last_message_preview`。
4. **事务保证**：批量追加必须在同一事务里完成，任何一条失败整体回滚。

#### 错误

- `404 SESSION_NOT_FOUND`
- `400 INVALID_PARAMS`：role 非法、content 为空
- `409 SESSION_FULL`：会话消息数达上限（建议 500，防止单会话无限增长）
- `413 CONTENT_TOO_LARGE`：单条 content > 32KB

---

### 4.8 更新消息 `PATCH /api/v1/sessions/{session_id}/messages/{message_id}`

对应前端 `handleRegenerate` 场景 —— 用户对某条 assistant 回答点"重新生成"，需要把旧的 assistant 消息替换成新内容。

另一个场景：若走方式 A（`/analyze` 代写），前端需要一个补偿通道来修正某条消息（比如纠正状态、补 trace_id）。

**Path 参数：**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session_id` | string | 是 | |
| `message_id` | string | 是 | 要更新的消息 ID |

**请求体**（所有字段可选，只更新传了的字段）：

```json
{
  "content": "## 分析结果（更新）\n\n...",
  "status": "success",
  "duration_ms": 9120,
  "trace_id": "trace-def-456",
  "metadata": { "regenerated_from": "m-101" }
}
```

**响应 200：** 返回更新后的 ChatMessage 对象。

**错误：**
- `404 MESSAGE_NOT_FOUND`
- `400 INVALID_PARAMS`：试图修改 `role` / `created_at` 等不可变字段

**行为约定：**
- **`role` 和 `created_at` 不可修改**，请求里传了也忽略。
- 更新 assistant 消息会刷新 `session.updated_at` 和 `last_message_preview`（若被更新的是最后一条）。
- 前端 `handleRegenerate` 当前做法是"切掉旧 assistant 消息 + 重发"——如果后端只支持"更新"而没有"删除单条消息"，那前端可以选择：
  1. 保留旧消息（`status='regenerated'`）+ 创建新的 assistant 消息 → 符合 ChatGPT 的版本化思路
  2. 直接 PATCH 旧消息的 content → 简单但丢失历史

本文档**推荐方案 1**，保留审计追溯性。首版可以先做方案 2，后续升级。

---

### 4.9 搜索会话 `GET /api/v1/sessions/search`

对应前端 `filteredSessions`（`state.sessions.filter(...)` 按 title 和消息内容模糊匹配）。

**Query 参数：**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `q` | string | 是 | - | 关键词；空白 trim 后至少 1 字符 |
| `limit` | integer | 否 | 20 | 最多返回 N 个会话 |
| `scope` | string | 否 | `all` | `title` / `content` / `all`，匹配范围 |

**请求示例：**

```http
GET /api/v1/sessions/search?q=SUP-001&limit=20 HTTP/1.1
X-User-Id: alice
```

**响应 200：**

```json
{
  "sessions": [
    {
      "id": "b2e4c1d8-...",
      "title": "分析 SUP-001 价格差异",
      "title_auto": true,
      "message_count": 6,
      "last_message_preview": "SUP-001 华为科技的实际采购价高于合同价 8.2%...",
      "created_at": "2026-04-09T10:00:00.000Z",
      "updated_at": "2026-04-11T08:30:00.123Z",
      "matched_snippets": [
        {
          "message_id": "m-050",
          "role": "user",
          "snippet": "分析供应商 <mark>SUP-001</mark> 华为科技的价格差异情况"
        }
      ]
    }
  ]
}
```

**字段说明：**
- `matched_snippets`（可选）：命中的消息片段，用于前端高亮展示，最多返回 3 条，每条前后各取 40 字符上下文，用 `<mark>` 包裹关键词。
- 首版可以不返回 snippets，前端保留现在的 "title 命中" 简单过滤即可。

**匹配策略建议：**
- 默认 `scope=all`：同时搜 title + messages.content
- 全文检索推荐用 PostgreSQL 的 `tsvector` + GIN 索引；查询量不大时也可以直接 `ILIKE '%q%'`，数据量大才优化
- 中文分词：PostgreSQL 用 `zhparser` 插件，或前期直接 LIKE
- **性能边界**：搜索结果按 `updated_at DESC` 排序，不做相关性打分（keep it simple）

**错误：**
- `400 INVALID_PARAMS`：q 为空

---

## 5. 与 /analyze 接口的联动

### 5.1 现状

[src/services/api.ts](../src/services/api.ts) 中的 `analyzeQuery` 当前只负责把 `{query, user_id, session_id}` 发给 `/api/v1/analyze`，拿到 `report_markdown` 后由前端自己塞进 `messages` 数组。后端完全**不知道**会话历史这回事。

### 5.2 推荐改造：`/analyze` 自动落库

在后端引入会话表后，`/api/v1/analyze` 的职责扩展为：

1. **前置**：校验 `session_id` 属于当前 `user_id`；不存在时报 `404`（或 auto-create，取决于业务选择）
2. **落库 user 消息**：在 analysis pipeline 启动前写入 user 消息，状态 `success`
3. **运行分析**：现有逻辑不变
4. **落库 assistant 消息**：
   - 成功 → `{content: report_markdown, status: success, duration_ms, trace_id}`
   - 失败 → `{content: error message, status: error, duration_ms}`
5. **更新 session**：`updated_at` / `message_count` / `title`（首次）/ `last_message_preview`
6. **返回**：保持现有 `AnalyzeResponse` 结构不变，额外加入新字段：

```ts
interface AnalyzeResponse {
  // ... 现有字段不变 ...
  session?: {
    id: string;
    title: string;
    message_count: number;
    updated_at: string;
  };
  user_message_id?: string;      // 新增
  assistant_message_id?: string; // 新增
}
```

这样前端不用额外调 4.7，只需要拿着 `assistant_message_id` 就能定位消息做后续 regenerate / 更新。

### 5.3 新老接口共存策略

| 前端动作 | 调用路径 |
|---|---|
| 发送一条新查询 | `POST /analyze`（后端代写两条消息） |
| 重新生成 | `POST /analyze`（带 `regenerate_of=<assistant_message_id>`，后端内部调 4.8 而不是新建） |
| 修改历史消息（未来场景） | `PATCH /sessions/{id}/messages/{mid}` |
| 直接写系统消息 / 补录消息 | `POST /sessions/{id}/messages`（补偿通道） |

### 5.4 `/analyze` 建议新增字段

在请求体追加可选字段：

```json
{
  "query": "...",
  "user_id": "alice",
  "session_id": "b2e4c1d8-...",
  "auto_persist": true,
  "regenerate_of": null,
  "client_user_message_id": "temp-uuid-1",
  "client_assistant_message_id": "temp-uuid-2",
  "metadata": {
    "analyst_role": "procurement",
    "use_memory": true,
    "use_ext_data": false
  }
}
```

| 新字段 | 类型 | 说明 |
|---|---|---|
| `auto_persist` | boolean | 默认 true；未登录用户或前端 fallback 模式可传 false，后端不落库 |
| `regenerate_of` | string \| null | 若为已有 assistant message_id，本次请求视为对它的重试，后端更新该消息而不是新建 |
| `client_user_message_id` | string | 前端乐观渲染时用的临时 ID，便于用响应里的 `user_message_id` 替换 |
| `client_assistant_message_id` | string | 同上 |
| `metadata` | object | 请求级元信息，会被写到两条消息的 `metadata` 字段 |

---

## 6. 数据库 schema 建议

PostgreSQL 参考建表语句（SQLite 同理，去掉 `jsonb` 换 `text` 即可）：

```sql
CREATE TABLE chat_sessions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         VARCHAR(64) NOT NULL,
  title           VARCHAR(120) NOT NULL DEFAULT '新对话',
  title_auto      BOOLEAN NOT NULL DEFAULT TRUE,
  message_count   INTEGER NOT NULL DEFAULT 0,
  last_message_preview VARCHAR(120),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at      TIMESTAMPTZ                -- 软删除
);

CREATE INDEX idx_sessions_user_updated
  ON chat_sessions (user_id, updated_at DESC)
  WHERE deleted_at IS NULL;

CREATE TABLE chat_messages (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  role            VARCHAR(16) NOT NULL CHECK (role IN ('user', 'assistant')),
  content         TEXT NOT NULL,
  status          VARCHAR(16) NOT NULL DEFAULT 'success'
                    CHECK (status IN ('sending', 'success', 'error')),
  duration_ms     INTEGER,
  trace_id        VARCHAR(64),
  metadata        JSONB,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_messages_session_created
  ON chat_messages (session_id, created_at);

-- 全文搜索（可选，数据量上来再加）
CREATE INDEX idx_messages_content_fts
  ON chat_messages USING gin (to_tsvector('simple', content));

CREATE INDEX idx_sessions_title_fts
  ON chat_sessions USING gin (to_tsvector('simple', title))
  WHERE deleted_at IS NULL;

-- Idempotency key 去重表（可选）
CREATE TABLE idempotency_keys (
  key          VARCHAR(64) PRIMARY KEY,
  user_id      VARCHAR(64) NOT NULL,
  endpoint     VARCHAR(64) NOT NULL,
  response     JSONB NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_idempotency_expiry
  ON idempotency_keys (created_at);
-- 定时清理超过 24h 的记录
```

**索引说明：**
- `idx_sessions_user_updated`：按用户 + 时间倒序查列表，最常用
- `idx_messages_session_created`：拉会话详情时按时间顺序
- FTS 索引仅在数据量 > 数万后再加；初期用 `ILIKE` 足够

**容量预估：**
- 单条消息平均 2KB（Markdown 报告较长，保守估计）
- 单会话平均 10 条消息 = 20KB
- 1000 用户 × 50 会话 = 50K 条会话 × 20KB = 1GB —— 非常轻量，单表无压力

---

## 7. 前端迁移步骤

现有 `useChatSessions` hook（[src/hooks/useChatSessions.ts](../src/hooks/useChatSessions.ts)）的对外签名已经足够稳定，后端接入只需要替换**内部实现**，不动调用方。推荐按以下顺序改造：

### 阶段 1：新增 service 层（不动 hook）

1. 新建 [src/services/chatSessions.ts](../src/services/chatSessions.ts)，封装所有 `/sessions*` HTTP 调用，返回 Promise。
2. 类型定义扩展到 [src/types/api.ts](../src/types/api.ts)，新增 `ApiChatSession` / `ApiChatMessage`（snake_case）以及转换函数 `fromApiSession` / `fromApiMessage`（snake→camel + 时间字符串→Date）。
3. 写单元测试覆盖映射函数，确保 `created_at: "2026-..."` → `Date` 无误。

### 阶段 2：改造 useChatSessions 内部实现

4. Hook 初始化改为 `GET /sessions` 调用 + loading 状态。首版全量拉（不分页）。
5. `newChat` → `POST /sessions`，拿到返回的 session 塞进 state
6. `switchTo(id)` → 若当前内存里没有该会话的消息，调 `GET /sessions/{id}` 拉一次；有缓存则直接切
7. `deleteSession` / `clearAll` → `DELETE` 对应接口，乐观更新 state
8. `setMessages` → 拆分成两个语义：
   - 追加消息（`/analyze` 返回后） → 不再走 `setMessages`，而是走 [阶段 3](#阶段-3)
   - 修改已有消息（regenerate 占位替换） → `PATCH /sessions/{id}/messages/{mid}`
9. `filteredSessions` → debounce 300ms 后调 `GET /sessions/search`，带 loading 状态

### 阶段 3：`analyzeQuery` 改造

10. [src/services/api.ts](../src/services/api.ts) 的 `analyzeQuery` 请求体补 `auto_persist: true` + `client_*_message_id`
11. 响应类型 `AnalyzeResponse` 加上 `session` / `user_message_id` / `assistant_message_id`
12. `ChatWindow.handleSend` 里的乐观渲染不变（立刻显示 user + assistant placeholder），`/analyze` 返回后用 `user_message_id` / `assistant_message_id` 替换 `client_id`

### 阶段 4：localStorage fallback

13. 保留 `useChatSessions` 里现有的 localStorage 路径作为 **guest 模式**：用户未登录或接口 401 时自动走本地路径
14. 登录成功后做一次"本地会话上传"：把本地未同步的会话 POST 到后端，成功后清本地；冲突检测按需实现（首版可以跳过）

### 阶段 5：验证 + 清理

15. 跑通验证清单（见下节）
16. localStorage key `erp-agent-chat-sessions-v1` **保留**，不要删 —— 充当 guest 模式的存储

---

## 8. 验证清单

后端 / 前端联调时按以下场景逐项过：

### 基础 CRUD
- [ ] 新用户首次登录 → `GET /sessions` 返回空列表（或仅一个默认空会话）
- [ ] 点"新建对话" → `POST /sessions` 成功，侧栏出现新条目
- [ ] 连续点两次"新建对话"且都没发消息 → 只产生一个空会话（后端去重）
- [ ] 发一条消息 → 会话标题自动变成消息前 24 字，`updated_at` 刷新到最前
- [ ] 切换到历史会话 → `GET /sessions/{id}` 返回全部消息，渲染正常
- [ ] 删除单条会话 → 成功且列表移除；若删除的是当前会话，前端切到下一条
- [ ] 清空全部 → 确认弹窗 + `DELETE /sessions` 带 confirm，成功后只剩一个空会话

### /analyze 集成
- [ ] 发一条消息 → `/analyze` 返回时用户消息和 AI 回答都已入库
- [ ] 刷新页面 → 历史完整，消息顺序正确，时间戳保留
- [ ] 查看 trace → assistant 消息的 `trace_id` 正确透传，TraceModal 能打开
- [ ] 重新生成 → 旧 assistant 消息状态变为 "regenerated" 或被更新，新消息紧跟其后
- [ ] `/analyze` 失败 → 前端看到 error 状态，后端落库 status=error

### 鉴权
- [ ] 未登录调任何 `/sessions*` → 401
- [ ] A 用户尝试访问 B 用户的 session_id → 404（不是 403）
- [ ] A 用户删 B 用户的 session → 404

### 搜索
- [ ] 搜索关键字（中文、英文、ID 如 `SUP-001`）都能命中
- [ ] 搜索空字符串 → 400
- [ ] 搜索未命中 → 返回空列表，前端显示"未找到匹配对话"

### 幂等 / 边界
- [ ] 带同一 `Idempotency-Key` 重复 `POST /sessions` → 返回相同 session，只建一次
- [ ] 单条消息 > 32KB → 413
- [ ] 单会话消息数 > 500 → 409
- [ ] 高频新建会话 → 限流 429

### 多端同步
- [ ] 同一用户两个浏览器登录 → A 发消息，B 刷新后看到

### 回退路径
- [ ] 后端 500 期间 → 前端自动降级为 localStorage 模式（提示用户"网络异常，本地模式"）
- [ ] 降级期间创建的会话，恢复后能否同步 → 若不支持，至少不能丢

---

## 附：接口一览

| # | Method | Path | 说明 | 对应前端 |
|---|---|---|---|---|
| 4.1 | `GET` | `/api/v1/sessions` | 列出会话 | 初始化 |
| 4.2 | `POST` | `/api/v1/sessions` | 创建会话 | `newChat()` |
| 4.3 | `GET` | `/api/v1/sessions/{id}` | 会话详情 | `switchTo()` |
| 4.4 | `PATCH` | `/api/v1/sessions/{id}` | 更新标题 | 未来手动编辑 |
| 4.5 | `DELETE` | `/api/v1/sessions/{id}` | 删除单会话 | `deleteSession()` |
| 4.6 | `DELETE` | `/api/v1/sessions` | 清空 | `clearAll()` |
| 4.7 | `POST` | `/api/v1/sessions/{id}/messages` | 追加消息 | 补偿通道 |
| 4.8 | `PATCH` | `/api/v1/sessions/{id}/messages/{mid}` | 更新消息 | regenerate |
| 4.9 | `GET` | `/api/v1/sessions/search` | 搜索会话 | `filteredSessions` |
| 5.2 | `POST` | `/api/v1/analyze`（改造） | 分析 + 代写消息 | `handleSend` |








