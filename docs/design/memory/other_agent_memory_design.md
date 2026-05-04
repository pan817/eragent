# cc-haha 记忆模块设计分析

> 基于 cc-haha（Claude Code 泄露源码修复版）的完整记忆架构解读。涵盖**短期（会话内）+ 长期（跨会话）**两大记忆体系，以及它们背后的设计哲学。

## 目录

- [A. 架构概览](#a-架构概览)
- [B. 短期记忆 — 会话内上下文管理](#b-短期记忆--会话内上下文管理)
  - [B1. Auto-Compact — 会话自动压缩](#b1-auto-compact--会话自动压缩)
  - [B2. Micro-Compact — 工具结果级压缩](#b2-micro-compact--工具结果级压缩)
  - [B3. SessionMemory — 会话摘要文件](#b3-sessionmemory--会话摘要文件)
  - [B4. Forked Agent — 后台任务的统一底座](#b4-forked-agent--后台任务的统一底座)
- [C. 长期记忆 — 跨会话持久化](#c-长期记忆--跨会话持久化)
  - [C1. 存储布局](#c1-存储布局)
  - [C2. 路径解析链](#c2-路径解析链)
  - [C3. 提示注入](#c3-提示注入)
  - [C4. 类型系统](#c4-类型系统)
  - [C5. 检索流程](#c5-检索流程)
- [D. 写入管线](#d-写入管线)
  - [D1. extractMemories — 每轮末提取](#d1-extractmemories--每轮末提取)
  - [D2. autoDream — 做梦式整合](#d2-autodream--做梦式整合)
- [E. 辅助管线](#e-辅助管线)
  - [E1. 子代理记忆（三级作用域）](#e1-子代理记忆三级作用域)
  - [E2. 团队记忆同步](#e2-团队记忆同步)
- [F. 架构抽象](#f-架构抽象)
  - [F1. 三元结构：数据 / 管线 / 触发](#f1-三元结构数据--管线--触发)
  - [F2. 关键设计权衡 — 10 个 "为什么不"](#f2-关键设计权衡--10-个-为什么不)
  - [F3. 性能与安全护栏总表](#f3-性能与安全护栏总表)
- [G. 参考速查](#g-参考速查)
  - [G1. 完整数据流时序图 T0–T9](#g1-完整数据流时序图-t0t9)
  - [G2. 源码速查表](#g2-源码速查表)
- [结语](#结语)

---

## A. 架构概览

cc-haha 的记忆系统由**四个层次**堆叠而成，每层寿命、共享范围、成本都不同：

```
┌────────────────────────────────────────────────────────────────┐
│ 第 1 层：会话内（短期）                                            │
│                                                                │
│  消息历史 (messages[])                                          │
│    ├─ Auto-Compact     —— 全会话压缩为 <summary>                │
│    ├─ Micro-Compact    —— 逐个清理旧 tool_result                │
│    └─ SessionMemory    —— 每 N 轮更新 10-section 摘要文件        │
│                                                                │
│  寿命：一次 REPL/CLI 进程                                         │
│  存储：内存 + ~/.claude/session-memory/<id>.md（压缩用）          │
└────────────────────────────────────────────────────────────────┘
                              ↑↓
┌────────────────────────────────────────────────────────────────┐
│ 第 2 层：自动记忆（长期 / 个人）                                     │
│                                                                │
│  ~/.claude/projects/<sanitized-git-root>/memory/                │
│    ├─ MEMORY.md          ← 索引（启动时载入 system prompt）       │
│    ├─ <type>_<topic>.md  ← 主题（按需 Sonnet 选 ≤5 attach 注入）  │
│    └─ .consolidate-lock  ← AutoDream PID 锁 + mtime 时间戳        │
│                                                                │
│  写：extractMemories（每轮末）+ autoDream（每 24h/5 会话）         │
│  读：loadMemoryPrompt（启动）+ findRelevantMemories（每轮）         │
│  寿命：用户级,跨所有会话                                           │
└────────────────────────────────────────────────────────────────┘
                              ↑↓
┌────────────────────────────────────────────────────────────────┐
│ 第 3 层：子代理记忆（Agent Memory,跨代理类型）                       │
│                                                                │
│  三级作用域：                                                     │
│    user     → ~/.claude/agent-memory/<agentType>/               │
│    project  → <cwd>/.claude/agent-memory/<agentType>/（提交 VCS）│
│    local    → <cwd>/.claude/agent-memory-local/<agentType>/      │
│                                                                │
│  读：loadAgentMemoryPrompt（Agent 工具派生时）                     │
│  写：子代理自身通过 Edit/Write（权限白名单限在 agent-memory 内）    │
└────────────────────────────────────────────────────────────────┘
                              ↑↓
┌────────────────────────────────────────────────────────────────┐
│ 第 4 层：团队记忆（Team Memory,跨成员）                            │
│                                                                │
│  ~/.claude/projects/<hash>/memory/team/                         │
│    ├─ MEMORY.md          ← 团队索引                              │
│    └─ *.md               ← 团队主题                              │
│                                                                │
│  HTTP sync: GET/PUT /api/claude_code/team_memory?repo=<owner/repo>│
│  语义：server-wins / delta upload / 删除不传播 / 上传前扫密         │
└────────────────────────────────────────────────────────────────┘
```

**四层关系**：

- **数据方向**：第 1 层从第 2/3 层读取相关记忆注入上下文，并通过 extractMemories 反向写回第 2 层
- **寿命递增**：第 1 层（进程）→ 第 2/3 层（用户）→ 第 4 层（组织）
- **共享递增**：个人私有 → 同用户多会话 → 同项目多代理 → 同组织多成员
- **更新成本递增**：内存操作 → 磁盘写 → 磁盘写 + 冲突检查 → HTTP 往返 + 密钥扫描
- **SOT 哲学**：除第 1 层外，**文件系统是唯一事实源**——用户可直接 `cat`/`vim` 编辑，进程崩溃零恢复成本

下面 B/C 节逐层拆解实现细节，D/E 节讲写入与同步管线，F 节抽象出设计哲学，G 节是速查参考。

---

## B. 短期记忆 — 会话内上下文管理

### B1. Auto-Compact — 会话自动压缩

**定位**：当会话 token 数逼近模型上下文窗口时，把整段历史消息压缩成一份 `<summary>`，换回一个可继续的瘦身对话。这是短期记忆的**最后兜底**——Micro-Compact 和 SessionMemory 都先行，Auto-Compact 只在它们无法避免 OOM 时启动。

**阈值判定**（[autoCompact.ts:33-91](../src/services/compact/autoCompact.ts#L33-L91)）：

```typescript
const MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000  // 基于 p99.99 = 17,387 tok
export const AUTOCOMPACT_BUFFER_TOKENS = 13_000

export function getEffectiveContextWindowSize(model: string): number {
  const reserved = Math.min(getMaxOutputTokensForModel(model), MAX_OUTPUT_TOKENS_FOR_SUMMARY)
  let window = getContextWindowForModel(model, getSdkBetas())
  // CLAUDE_CODE_AUTO_COMPACT_WINDOW env 允许手工收窄（CI/测试）
  return window - reserved
}

export function getAutoCompactThreshold(model: string): number {
  return getEffectiveContextWindowSize(model) - AUTOCOMPACT_BUFFER_TOKENS
}
```

即：触发阈值 = `上下文窗口 - 20K (留给摘要输出) - 13K (buffer)`。对 200K 窗口的 Sonnet，阈值约 167K。

**断路器**（`autoCompact.ts:62-70`）：

```typescript
const MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
// BQ 2026-03-10: 1,279 sessions had 50+ consecutive failures (up to 3,272)
// in a single session, wasting ~250K API calls/day globally.
```

连续 3 次 autocompact 失败后熔断——曾有真实事故：部分会话因 `prompt_too_long` 反复重试，单日浪费 25 万 API 调用。断路器打开后用户需手动 `/compact` 或退出会话。

**压缩提示词设计**（[compact/prompt.ts:19-44](../src/services/compact/prompt.ts#L19-L44)）：

```typescript
const NO_TOOLS_PREAMBLE = `CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.
`
```

这段 preamble 是**为缓存共享付出的代价**：fork 继承父会话完整 tools 数组（cache key 一部分，不能动），Sonnet 4.6+ 自适应思考模型偶尔会无视"别调工具"的弱指令直接调工具。`maxTurns: 1` + 被拒 → 零输出 → 回退流式生成。把告警放最前面 + 明确拒绝后果，把 4.6 的浪费率从 2.79% 压到 0.01%。

**`<analysis>` + `<summary>` 两段结构**：Analysis 是临时草稿（记录时序、用户意图、关键决策、错误处理），`formatCompactSummary()` 在注入前剥掉。模型在 Analysis 中"慢思考"比直接写 summary 质量高，但 Analysis 的 tokens 不进下一轮上下文。

**PTL 降级重试**（[compact.ts:243-291](../src/services/compact/compact.ts#L243-L291)）：

```typescript
export function truncateHeadForPTLRetry(
  messages: Message[],
  ptlResponse: AssistantMessage,
): Message[] | null {
  // 去除历史遗留 PTL_RETRY_MARKER 防止 group-0 空转
  const input = messages[0]?.isMeta && messages[0].message.content === PTL_RETRY_MARKER
    ? messages.slice(1) : messages

  const groups = groupMessagesByApiRound(input)
  if (groups.length < 2) return null

  const tokenGap = getPromptTooLongTokenGap(ptlResponse)
  let dropCount: number
  if (tokenGap !== undefined) {
    let acc = 0; dropCount = 0
    for (const g of groups) {
      acc += roughTokenCountEstimationForMessages(g)
      dropCount++
      if (acc >= tokenGap) break
    }
  } else {
    // Vertex/Bedrock 不返回结构化 gap → 丢 20%
    dropCount = Math.max(1, Math.floor(groups.length * 0.2))
  }
  dropCount = Math.min(dropCount, groups.length - 1)  // 至少留 1 组可压缩

  const sliced = groups.slice(dropCount).flat()
  if (sliced[0]?.type === 'assistant') {
    // 丢完 group 0 后首条不能是 assistant（API 要 role=user 开头）
    return [createUserMessage({ content: PTL_RETRY_MARKER, isMeta: true }), ...sliced]
  }
  return sliced
}
```

当压缩请求自身触发 `prompt_too_long` 时（即要压缩的内容已经超窗口）：按 API round 分组、丢最早的组、最多重试 3 次（`MAX_PTL_RETRIES`）。这是**有损回退**——会丢失最早的上下文，但避免用户彻底卡死。

**Post-compact 预算**（`compact.ts:122-130`）：

```typescript
export const POST_COMPACT_TOKEN_BUDGET = 50_000
export const POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
export const POST_COMPACT_MAX_FILES_TO_RESTORE = 5
export const POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
export const POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000
```

压缩后的"重注入阶段"：最多恢复 5 个最相关的文件（总 50K），最多 5 个 skills（总 25K），每个单 cap 5K。早期实现不限 skill 大小导致每次 compact 多烧 5-10K token，现在按 skill 截断——skill 文件头通常是关键指令，截尾更合理。

**与 Micro-Compact / SessionMemory 的协作**：

```
每轮末 → SessionMemory.extract   （每 ~3 轮，写 10-section 摘要）
       → Micro-Compact            （把旧 tool_result 替换为占位符）
       ↓
上限逼近
       ↓
Auto-Compact 触发
       ├─ trySessionMemoryCompaction  ← 优先：把 SessionMemory 当 primer
       └─ compactConversation          ← 退回：全量压缩
       ↓
notifyCompaction（打破 Anthropic prompt cache）
       ↓
postCompactCleanup（恢复 file read + skills）
```

**遥测事件**：`tengu_compact_turn` / `tengu_autocompact_*`，每次压缩记录 before/after token、summary 长度、重试次数。

---

### B2. Micro-Compact — 工具结果级压缩

**核心思想**：不压缩整个会话，而是**逐条替换过旧的工具结果内容**。保留 `tool_use`（调用记录）和 assistant 文本（推理链条），只把 `tool_result` 的 body 换成占位符。模型仍能看到"历史上调用过这个工具"，但不占 token。

**占位符**（[microCompact.ts:36](../src/services/compact/microCompact.ts#L36)）：

```typescript
export const TIME_BASED_MC_CLEARED_MESSAGE = '[Old tool result content cleared]'
```

**可压缩工具白名单**（`microCompact.ts:41-50`）：

```typescript
const COMPACTABLE_TOOLS = new Set<string>([
  FILE_READ_TOOL_NAME,
  ...SHELL_TOOL_NAMES,     // Bash/Sh 等
  GREP_TOOL_NAME,
  GLOB_TOOL_NAME,
  WEB_SEARCH_TOOL_NAME,
  WEB_FETCH_TOOL_NAME,
  FILE_EDIT_TOOL_NAME,
  FILE_WRITE_TOOL_NAME,
])
```

**为什么是这 8 类**：它们的输出体积大、重复读同一文件代价高、但对**未来决策**价值低（用户后续只需要"你之前读过 X"这个元信息）。未列入的工具（如 TodoWrite、AgentTool、MCP 工具）输出小且包含关键状态，不压缩。

**触发时机**：由 `TimeBasedMCConfig`（[timeBasedMCConfig.ts](../src/services/compact/timeBasedMCConfig.ts)）按**消息年龄阈值**决定——早于阈值且属白名单的 `tool_result` 被清理。阈值来自 GrowthBook 配置，可远程调整。

**图片/文档特殊处理**：对超过 `IMAGE_MAX_TOKEN_SIZE = 2000` 的图片，直接替换为 `[image]` 标记（`compact.ts:145-200` 的 `stripImagesFromMessages` 逻辑被 Micro-Compact 复用）。

**CachedMicrocompact 变体**（`feature('CACHED_MICROCOMPACT')`, [cachedMicrocompact.ts](../src/services/compact/cachedMicrocompact.ts)）：

- 通过 **cache edits** 机制把清理操作注入到 Anthropic prompt cache 中
- 核心 API：`consumePendingCacheEdits` / `getPinnedCacheEdits`
- 目标：让 cache 感知到"这段 tool_result 已被替换"，避免每次 API 调用重复付 cache creation 费用
- 仅在支持 cache editing 的模型上启用（`isModelSupportedForCacheEditing`）

**与 Auto-Compact 的协作**：

```
每轮末（如果启用 MC）
  ↓
roughTokenCountEstimation 判断是否接近阈值
  ↓
Micro-Compact 先行：清白名单工具的旧 result
  ↓  （降几 K~几十 K tokens，通常足够）
Auto-Compact 仅在 MC 不够用时触发
```

**设计权衡**：
- **优点**：近乎无损语义（保留调用历史），成本便宜（不调模型生成摘要）
- **缺点**：只能清"大体积 + 低未来价值"的工具结果，对推理密集会话效果有限

**遥测**：`tengu_time_based_mc_*`、`notifyCacheDeletion`（cache edits 触发时通知）。

---

### B3. SessionMemory — 会话摘要文件

**定位**：SessionMemory 和长期记忆的 `extractMemories` 完全不同——它是**当前会话的笔记本**，用于 Auto-Compact 时作为 primer 保留上下文连续性。寿命 = 一次会话。

**存储位置**（`sessionMemory.ts:183-233` 的 `setupSessionMemoryFile`）：

```typescript
const sessionMemoryDir = getSessionMemoryDir()  // ~/.claude/session-memory/
await fs.mkdir(sessionMemoryDir, { mode: 0o700 })
const memoryPath = getSessionMemoryPath()       // <sessionId>.md

// wx = O_CREAT|O_EXCL,只在第一次创建时写模板
await writeFile(memoryPath, '', { mode: 0o600, flag: 'wx' })
await writeFile(memoryPath, template, { mode: 0o600 })
```

目录 0700 + 文件 0600 —— 仅当前用户可读写。

**固定 10-section 模板**（[SessionMemory/prompts.ts:11-41](../src/services/SessionMemory/prompts.ts#L11-L41)）：

```markdown
# Session Title
_A short and distinctive 5-10 word descriptive title for the session._

# Current State
_What is actively being worked on right now? Pending tasks. Immediate next steps._

# Task specification
# Files and Functions
# Workflow
# Errors & Corrections
# Codebase and System Documentation
# Learnings
# Key results
# Worklog
```

**模板的两层结构**：每个 section 有 `#` 标题 + `_italic_` 描述行。**italic 是给模型的指令**（what belongs here），**不能被改动或删除**。模型只能更新描述行**之后**的实际内容。这是通过提示词强约束实现的（[prompts.ts:57-78](../src/services/SessionMemory/prompts.ts#L57-L78)）：

> NEVER modify, delete, or add section headers...
> NEVER modify or delete the italic _section description_ lines

**双阈值触发**（[sessionMemory.ts:134-181](../src/services/SessionMemory/sessionMemory.ts#L134-L181)）：

```typescript
export function shouldExtractMemory(messages: Message[]): boolean {
  const currentTokenCount = tokenCountWithEstimation(messages)

  // 1. 首次初始化阈值
  if (!isSessionMemoryInitialized()) {
    if (!hasMetInitializationThreshold(currentTokenCount)) return false
    markSessionMemoryInitialized()
  }

  // 2. Token 增量阈值（自上次提取以来）
  const hasMetTokenThreshold = hasMetUpdateThreshold(currentTokenCount)

  // 3. Tool call 计数阈值
  const toolCallsSinceLastUpdate = countToolCallsSince(messages, lastMemoryMessageUuid)
  const hasMetToolCallThreshold = toolCallsSinceLastUpdate >= getToolCallsBetweenUpdates()

  // 4. 最后一轮无 tool_use（安全提取点）
  const hasToolCallsInLastTurn = hasToolCallsInLastAssistantTurn(messages)

  // 触发条件：Token 阈值始终必需,再叠加其一
  return (hasMetTokenThreshold && hasMetToolCallThreshold) ||
         (hasMetTokenThreshold && !hasToolCallsInLastTurn)
}
```

**设计要点**：
- **Token 阈值是硬门槛**——即使 tool call 多，也得等 token 涨够才提取，防止过度抽取
- **"最后一轮无 tool_use"** 是自然断点：正在做事的过程中不打断
- 两个阈值可通过 GB 动态调整（`tengu_sm_config`）

**权限极限收紧 `createMemoryFileCanUseTool`**（`sessionMemory.ts:460-482`）：

```typescript
export function createMemoryFileCanUseTool(memoryPath: string): CanUseToolFn {
  return async (tool: Tool, input: unknown) => {
    if (tool.name === FILE_EDIT_TOOL_NAME &&
        typeof input === 'object' && input !== null &&
        'file_path' in input) {
      const filePath = input.file_path
      if (typeof filePath === 'string' && filePath === memoryPath) {
        return { behavior: 'allow', updatedInput: input }
      }
    }
    return {
      behavior: 'deny',
      message: `only ${FILE_EDIT_TOOL_NAME} on ${memoryPath} is allowed`,
      decisionReason: { type: 'other', reason: `only Edit on ${memoryPath}` },
    }
  }
}
```

相比 `createAutoMemCanUseTool`（允许整个目录），SessionMemory 的权限是**精确到文件 + 精确到工具**——只允许 `Edit` 操作这一个 `.md` 文件，Read/Grep/Bash/Write 全部 deny。提示词也配合要求"只用 Edit、可并行多次、之后停止"。

**与 Auto-Compact 的协作**：

```typescript
// sessionMemory.ts:487-495
function updateLastSummarizedMessageIdIfSafe(messages: Message[]): void {
  if (!hasToolCallsInLastAssistantTurn(messages)) {
    const lastMessage = messages[messages.length - 1]
    if (lastMessage?.uuid) {
      setLastSummarizedMessageId(lastMessage.uuid)
    }
  }
}
```

提取成功后记录 `lastSummarizedMessageId`。Auto-Compact 触发时，`trySessionMemoryCompaction`（[compact/sessionMemoryCompact.ts](../src/services/compact/sessionMemoryCompact.ts)）会用这个摘要文件作为 primer——相比重新生成完整 `<summary>`，可以省一大半摘要生成成本。

**Gate 逻辑**（`sessionMemory.ts:357-375`）：

```typescript
export function initSessionMemory(): void {
  if (getIsRemoteMode()) return
  const autoCompactEnabled = isAutoCompactEnabled()  // SessionMemory 为 compact 服务
  if (!autoCompactEnabled) return
  registerPostSamplingHook(extractSessionMemory)  // 挂到 postSamplingHooks
}
```

两层门：
- `tengu_session_memory` feature flag（远程）
- `isAutoCompactEnabled()`（SessionMemory 依附于 Auto-Compact，后者关则前者无意义）

**手动触发**：`/summary` 命令调 `manuallyExtractSessionMemory`（`sessionMemory.ts:387-453`），跳过阈值检查，直接 runForkedAgent。

**遥测**：`tengu_session_memory_init` / `_extraction` / `_manual_extraction` / `_gate_disabled` / `_file_read`。

---

### B4. Forked Agent — 后台任务的统一底座

**为什么单独一节**：`extractMemories` / `autoDream` / `SessionMemory` / `AgentSummary` / `compact` 全部走同一个 `runForkedAgent()` 机制。理解不了它，就看不懂 D/E 节。

**核心目标**：让后台任务**共享主会话的 Anthropic prompt cache**，fork 成本接近零。

**`CacheSafeParams` 五元组**（[utils/forkedAgent.ts:57-68](../src/utils/forkedAgent.ts#L57-L68)）：

```typescript
export type CacheSafeParams = {
  /** System prompt - must match parent for cache hits */
  systemPrompt: SystemPrompt
  /** User context - prepended to messages, affects cache */
  userContext: { [k: string]: string }
  /** System context - appended to system prompt, affects cache */
  systemContext: { [k: string]: string }
  /** Tool use context containing tools, model, and other options */
  toolUseContext: ToolUseContext
  /** Parent context messages for prompt cache sharing */
  forkContextMessages: Message[]
}
```

**Anthropic cache key 的组成部分**（注释中明确列出）：system prompt + tools + model + messages prefix + thinking config。只要五元组任一项与父会话不同，fork 的第一次 API 请求就会 cache miss，付全额 cache creation 费。

**thinking config 的陷阱**：不在 CacheSafeParams 字段里，而是通过 `toolUseContext.options.thinkingConfig` 间接继承。但如果 fork 设置了 `maxOutputTokens`，在老模型上会 clamp `budget_tokens`（claude.ts 内部），意外破坏 cache。

**全局缓存槽**（`forkedAgent.ts:71-80`）：

```typescript
// Slot written by handleStopHooks after each turn so post-turn forks
// (promptSuggestion, postTurnSummary, /btw) can share the main loop's
// prompt cache without each caller threading params through.
let lastCacheSafeParams: CacheSafeParams | null = null

export function saveCacheSafeParams(params: CacheSafeParams | null): void {
  lastCacheSafeParams = params
}
export function getLastCacheSafeParams(): CacheSafeParams | null {
  return lastCacheSafeParams
}
```

每次 `handleStopHooks` 结束前保存——所有 turn-end fork（prompt suggestion、`/btw`、post-turn summary）可以直接拿。

**隔离维度**：

| 维度 | 实现 | 目的 |
|------|------|------|
| **工具权限** | `canUseTool` 回调 | 收紧写入能力，不影响主会话 |
| **消息历史** | `skipTranscript: true` | fork 产生的消息不写主转录，避免竞态 |
| **中断** | 独立 `AbortController` | 用户可单独 kill 后台任务 |
| **读文件缓存** | `cloneFileStateCache` | 避免污染父会话的读缓存 |

**关键约束：不能改 tools 数组**：

```typescript
// extractMemories.ts:176-182
// REPL mode: when enabled, primitive tools are hidden from the tool list
// so the forked agent calls REPL instead. REPL's VM context re-invokes this
// canUseTool for each inner primitive, so the Read/Bash/Edit/Write checks
// below still gate the actual file and shell operations. Giving the fork a
// different tool list would break prompt cache sharing (tools are part of
// the cache key — see CacheSafeParams in forkedAgent.ts).
if (tool.name === REPL_TOOL_NAME) {
  return { behavior: 'allow', updatedInput: input }
}
```

所以权限收紧不是"删工具"，而是**保留完整 tools 数组 + 在 canUseTool 层拦截**。这是整个设计的核心约束——代价是提示词必须明确告诉模型"这些工具你看得到但调用会被拒"。

**调用例子**（[extractMemories.ts:415-427](../src/services/extractMemories/extractMemories.ts#L415-L427)）：

```typescript
const result = await runForkedAgent({
  promptMessages: [createUserMessage({ content: userPrompt })],
  cacheSafeParams,
  canUseTool,                     // 不同任务传不同函数
  querySource: 'extract_memories',
  forkLabel: 'extract_memories',
  skipTranscript: true,
  maxTurns: 5,                    // 硬顶防兔子洞
})
```

**不同后台任务的 maxTurns 策略**：

| 任务 | maxTurns | 理由 |
|------|----------|------|
| `extractMemories` | 5 | 良好行为 2-4 turn 完成（read + write + 可选 verify） |
| `autoDream` | ∞（无硬顶） | 多阶段整合需要灵活空间 |
| `compactConversation` | 1 | 一次性输出 `<summary>`，多 turn 浪费 |
| `SessionMemory extract` | 1 | 只需 Edit，不需要探索 |
| `AgentSummary` | 1 | 3-5 字的进度描述 |

**跨 fork 的用量合并**（`forkedAgent.ts:accumulateUsage`）：
每次 fork 完成都调 `accumulateUsage` 累加 input/output/cache_read/cache_creation。最终遥测事件 `tengu_fork_agent_query` 记录：
- `cache_read_input_tokens` —— 命中量（越高越好）
- `cache_creation_input_tokens` —— 首次创建量
- 两者比值 = cache hit rate

典型 extractMemories cache hit rate 在 95%+——证明 CacheSafeParams 设计成功。

**drain 机制**：所有 fork 加入 `inFlightExtractions: Set<Promise>`，进程退出前 `drainPendingExtraction(60_000)` 等待（`Promise.race` vs `setTimeout(60s).unref()`）。`.unref()` 确保定时器自身不阻塞退出。

**Cowork 场景的扩展**：`CLAUDE_CODE_REMOTE` 模式下，fork 的文件写入会被路由到挂载盘；`CLAUDE_COWORK_MEMORY_EXTRA_GUIDELINES` 注入到提示尾，这些都通过不改变 CacheSafeParams 实现——Cowork 团队证明了这个抽象的可扩展性。

---

## C. 长期记忆 — 跨会话持久化

### C1. 存储布局

**完整目录树**：

```
~/.claude/                                         ← memoryBase（或 CLAUDE_CODE_REMOTE_MEMORY_DIR）
│
├─ projects/<sanitized-git-root>/memory/           ← 自动记忆（个人 + 可选 team/）
│  ├─ MEMORY.md                                    ← 索引：启动时载入 system prompt
│  ├─ user_role.md                                 ← User 类型主题文件
│  ├─ feedback_testing.md                          ← Feedback 类型
│  ├─ project_freeze.md                            ← Project 类型
│  ├─ reference_linear.md                          ← Reference 类型
│  ├─ .consolidate-lock                            ← AutoDream PID 锁 + mtime=lastConsolidatedAt
│  ├─ logs/YYYY/MM/YYYY-MM-DD.md                   ← KAIROS 日志追加模式才有
│  └─ team/                                        ← TEAMMEM feature 启用时
│     ├─ MEMORY.md                                 ← 团队索引
│     └─ *.md                                      ← 团队主题（HTTP 同步）
│
├─ agent-memory/<agent-type>/                      ← 子代理 user 作用域
│  ├─ MEMORY.md
│  └─ *.md
│
├─ session-memory/<session-id>.md                  ← SessionMemory（短期,进程退出后可清）
│
└─ settings.json                                   ← autoMemoryEnabled / autoDreamEnabled 等

<cwd>/
├─ .claude/agent-memory/<agent-type>/              ← 子代理 project 作用域（提交 VCS）
└─ .claude/agent-memory-local/<agent-type>/        ← 子代理 local 作用域（不提交）
```

**为什么 `<sanitized-git-root>` 而不是 cwd**（[paths.ts:200-205](../src/memdir/paths.ts#L200-L205)）：

```typescript
function getAutoMemBase(): string {
  return findCanonicalGitRoot(getProjectRoot()) ?? getProjectRoot()
}
```

同一 repo 的所有 worktree 共享一份记忆（issue anthropics/claude-code#24382）——否则每切换一个 worktree 就是一个"新项目"，记忆完全隔离，用户体验灾难。`sanitizePath` 把 `/` 换成 `-` 等避免文件名非法。

**为什么 `session-memory` 独立于 `projects/<hash>/memory/`**：

- SessionMemory 是**单会话**的、用于 compact 的 primer，寿命 ≪ auto-memory
- 每会话一个文件（`<sessionId>.md`），不按项目分组——方便单独清理
- 权限 0700/0600，更严格（单用户单会话）

**为什么 `team/` 嵌套在 auto memory 目录下**：

- 团队记忆的 route 规则（C4 展开）要求主模型在写入时**就**决定放个人还是团队——两个目录必须同时可见
- 嵌套而非并列目录，递归 mkdir 的副作用让创建团队目录自动创建父目录
- 一次 scan 就能看到两种记忆（`memoryScan` 的 `recursive: true`）

**主题文件格式**（YAML frontmatter + Markdown body）：

```markdown
---
name: 测试策略偏好
description: 集成测试必须使用真实数据库，不要 mock    ← Sonnet 选择器只看这一行
type: feedback                                     ← 四选一：user/feedback/project/reference
---

集成测试必须使用真实数据库，不要 mock。

**Why:** 上季度 mock 测试通过但生产环境迁移失败，mock/prod 差异掩盖了问题。

**How to apply:** 编写或审查测试时，确保数据库操作使用真实连接。
```

**为什么 frontmatter 而非 JSON**：
- 人类可读可编辑（文件系统 SOT 哲学的体现）
- `description` 字段专为检索优化——Sonnet 选择器只看 manifest 中 description 就能决策
- 缺失字段优雅降级（`parseMemoryType` 未知值返回 `undefined`，legacy 文件继续可用）

**MEMORY.md 索引格式**（纯索引，一行一个条目）：

```markdown
- [用户角色](user_role.md) — 数据科学家，关注可观测性/日志
- [测试策略](feedback_testing.md) — 集成测试用真实数据库，不 mock
- [合并冻结](project_freeze.md) — 2026-03-05 起冻结非关键合并
- [Bug 追踪](reference_linear.md) — 流水线 bug 在 Linear INGEST 项目
```

**为什么索引与内容分离**：
- 索引常驻 system prompt（模型永远能看到"有什么可用"）
- 主题文件只在 Sonnet 判定相关时 attach 注入（节省 token）
- 索引 ≤200 行 + ≤25KB 双截断，不会吃光提示词
- 主题文件无限增长（受扫描 cap 200 控制）

**关键常量速查**（[memdir.ts:34-38](../src/memdir/memdir.ts#L34-L38) + [memoryScan.ts:21-22](../src/memdir/memoryScan.ts#L21-L22)）：

```typescript
ENTRYPOINT_NAME = 'MEMORY.md'        // 索引文件名
MAX_ENTRYPOINT_LINES = 200           // 行数 cap
MAX_ENTRYPOINT_BYTES = 25_000        // 字节 cap（长行绕过）
MAX_MEMORY_FILES = 200               // scan 总数 cap
FRONTMATTER_MAX_LINES = 30           // 每文件只读前 30 行
AUTO_MEM_DIRNAME = 'memory'          // 目录名
```

**命名约定**：
- 类型前缀：`user_*` / `feedback_*` / `project_*` / `reference_*`——**非强制**，只是提示词引导
- 实际分类权威在 frontmatter `type:` 字段
- 文件名用下划线而非空格（文件系统友好 + grep 友好）

**锁文件的特殊路径设计**（[consolidationLock.ts:21-23](../src/services/autoDream/consolidationLock.ts#L21-L23)）：

```typescript
function lockPath(): string {
  return join(getAutoMemPath(), LOCK_FILE)  // '.consolidate-lock'
}
```

**放在记忆目录内而非 `~/.claude/` 根**的理由：
- 与记忆共用同一 root（git-root 哈希）—— key 一致
- 当 `CLAUDE_CODE_REMOTE_MEMORY_DIR` 或 `autoMemoryDirectory` override 时自动跟随
- 父目录写权限已由 memory 系统保证（filesystem.ts 白名单）——无需单独加路径

---

### C2. 路径解析链

**两层判定**：先判"是否启用"，再判"物理路径"。

**启用判定 `isAutoMemoryEnabled`**（[paths.ts:30-55](../src/memdir/paths.ts#L30-L55)）：

```typescript
export function isAutoMemoryEnabled(): boolean {
  const envVal = process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY
  if (isEnvTruthy(envVal)) return false                          // 1. 环境变量显式关
  if (isEnvDefinedFalsy(envVal)) return true                     // 1b. 显式 0 → 开（绕过设置）

  if (isEnvTruthy(process.env.CLAUDE_CODE_SIMPLE)) return false  // 2. --bare 模式

  if (isEnvTruthy(process.env.CLAUDE_CODE_REMOTE) &&             // 3. 远程无持久化
      !process.env.CLAUDE_CODE_REMOTE_MEMORY_DIR) return false

  const settings = getInitialSettings()
  if (settings.autoMemoryEnabled !== undefined) {                // 4. settings.json
    return settings.autoMemoryEnabled
  }
  return true                                                     // 5. 默认开
}
```

**设计要点**：`isEnvDefinedFalsy` 为 "0"/"false" 返回 true——允许用 `CLAUDE_CODE_DISABLE_AUTO_MEMORY=0` 强制覆盖 settings.json 的 `false`。CI 临时开启很方便。

**路径解析 `getAutoMemPath`**（[paths.ts:223-235](../src/memdir/paths.ts#L223-L235)）：

```typescript
export const getAutoMemPath = memoize(
  (): string => {
    const override = getAutoMemPathOverride() ?? getAutoMemPathSetting()
    if (override) return override
    const projectsDir = join(getMemoryBaseDir(), 'projects')
    return (
      join(projectsDir, sanitizePath(getAutoMemBase()), AUTO_MEM_DIRNAME) + sep
    ).normalize('NFC')
  },
  () => getProjectRoot(),  // 缓存 key = 项目根目录
)
```

**优先级链**（从高到低）：

```
1. CLAUDE_COWORK_MEMORY_PATH_OVERRIDE env         ← Cowork VM 专用
2. settings.json → autoMemoryDirectory            ← 仅可信来源（见下）
3. <memoryBase>/projects/<sanitized-git-root>/memory/
   其中 memoryBase = CLAUDE_CODE_REMOTE_MEMORY_DIR ?? ~/.claude
```

**`validateMemoryPath` 的 5 类拒绝**（[paths.ts:109-150](../src/memdir/paths.ts#L109-L150)）：

| 拒绝类型 | 例子 | 原因 |
|---------|------|------|
| 非绝对路径 | `../foo` | 相对 CWD，攻击者可劫持 |
| 根/近根 | `/`、`/a` | 长度 < 3，可匹配整个文件系统 |
| Windows 驱动器根 | `C:\` → `C:` | 归一化后仍危险 |
| UNC 路径 | `\\server\share` | 网络路径，信任边界不透明 |
| 含 `\0` | 任意带空字节 | 可在 syscall 中截断，绕过校验 |
| `~/.`、`~/..`、空 `~/` | — | 展开为 `$HOME` 或祖先 |

**为什么 projectSettings 被显式排除**（[paths.ts:168-186](../src/memdir/paths.ts#L168-L186)）：

```typescript
function getAutoMemPathSetting(): string | undefined {
  const dir =
    getSettingsForSource('policySettings')?.autoMemoryDirectory ??  // 管理员策略
    getSettingsForSource('flagSettings')?.autoMemoryDirectory ??    // --settings 命令行
    getSettingsForSource('localSettings')?.autoMemoryDirectory ??   // 用户本地
    getSettingsForSource('userSettings')?.autoMemoryDirectory       // 用户全局
  return validateMemoryPath(dir, true)
  // 注意：不读 projectSettings (.claude/settings.json 提交到 repo)
}
```

**攻击场景**：恶意 repo 在 `.claude/settings.json` 设 `autoMemoryDirectory: "~/.ssh"` → `filesystem.ts` 的 memory 目录写豁免会让记忆写入误作 ssh 密钥修改。

**防御**：projectSettings 来自不可信源（`git clone` 即带入），**完全排除**。`hasSkipDangerousModePermissionPrompt` 等其他敏感设置也是同样模式。

**memoize 的必要性**（`paths.ts:216-222` 注释）：

```typescript
// Render-path callers (collapseReadSearchGroups → isAutoManagedMemoryFile)
// fire per tool-use message per Messages re-render; each miss costs
// getSettingsForSource × 4 → parseSettingsFile (realpathSync + readFileSync).
// Keyed on projectRoot so tests that change its mock mid-block recompute;
// env vars / settings.json / CLAUDE_CONFIG_DIR are session-stable in
// production and covered by per-test cache.clear.
```

- **触发频率**：React 渲染中每 tool_use 都调用
- **单次成本**：4 × getSettingsForSource（含 realpath + readFile）
- **缓存 key**：仅 `getProjectRoot()`——env 和 settings 会话期内稳定，改不改重新挂载进程

**NFC 归一化**：`.normalize('NFC')` 处理 macOS 的 Unicode 分解（HFS+ 用 NFD），避免 `isAutoMemPath` 字符串比对因编码差异而 false negative。

**`isAutoMemPath` 白名单判定**（[paths.ts:274-278](../src/memdir/paths.ts#L274-L278)）：

```typescript
export function isAutoMemPath(absolutePath: string): boolean {
  const normalizedPath = normalize(absolutePath)
  return normalizedPath.startsWith(getAutoMemPath())
}
```

**`normalize` 先行**是关键——防御 `.../memory/../../../.ssh/id_rsa` 穿越攻击。`extractMemories` 的 `createAutoMemCanUseTool` 依赖此函数判断 Edit/Write 目标是否落在记忆目录内。

**`tengu_team_memdir_disabled` 降级遥测**（[memdir.ts:503-505](../src/memdir/memdir.ts#L503-L505)）：

```typescript
// Gate on the GB flag directly, not isTeamMemoryEnabled() — that function
// checks isAutoMemoryEnabled() first, which is definitionally false in this
// branch. We want "was this user in the team-memory cohort at all."
if (getFeatureValue_CACHED_MAY_BE_STALE('tengu_herring_clock', false)) {
  logEvent('tengu_team_memdir_disabled', {})
}
```

Auto memory 被关闭时，如果该用户本来在团队记忆 cohort 中，仍然记一次 `tengu_team_memdir_disabled`——保留 A/B 分析的分母，不然"启用率"指标会失真。

---

### C3. 提示注入

**入口**：`loadMemoryPrompt()`（[memdir.ts:419-507](../src/memdir/memdir.ts#L419-L507)）是记忆系统与 system prompt 的唯一接口。

**分派逻辑**：

```
loadMemoryPrompt()
  │
  ├─ !autoEnabled
  │     → logEvent('tengu_memdir_disabled', {...})
  │     → return null
  │
  ├─ feature('KAIROS') + autoEnabled + kairosActive
  │     → buildAssistantDailyLogPrompt()     [KAIROS 日志追加模式]
  │
  ├─ feature('TEAMMEM') + isTeamMemoryEnabled()
  │     → ensureMemoryDirExists(teamDir)     [递归 mkdir 创建 auto + team]
  │     → buildCombinedMemoryPrompt()         [个人 + 团队双目录]
  │
  └─ autoEnabled（普通单目录）
        → ensureMemoryDirExists(autoDir)
        → buildMemoryLines('auto memory', autoDir, ...).join('\n')
```

**`buildMemoryLines` 产出结构**（[memdir.ts:199-266](../src/memdir/memdir.ts#L199-L266)）：

```
# auto memory

You have a persistent, file-based memory system at `{memoryDir}`.
This directory already exists — write to it directly with the Write tool
(do not run mkdir or check for its existence).

You should build up this memory system over time so that future
conversations can have a complete picture of who the user is...

## Types of memory              ← 四类定义（Individual 或 Combined 版本）
## What NOT to save             ← 负向约束（代码/架构/git/临时）
## How to save memories         ← 两步保存（skipIndex 时退化为一步）
## When to access memories
## Before recommending from memory  ← 引用前验证
## Memory and other forms of persistence  ← 与 Plan/Task 的区别
## Searching past context       ← tengu_coral_fern 开关

## MEMORY.md                    ← 索引内容（或 "currently empty"）
```

**`DIR_EXISTS_GUIDANCE` 的起源**（[memdir.ts:116-119](../src/memdir/memdir.ts#L116-L119)）：

```typescript
export const DIR_EXISTS_GUIDANCE =
  'This directory already exists — write to it directly with the Write tool (do not run mkdir or check for its existence).'
```

> Shipped because Claude was burning turns on `ls`/`mkdir -p` before writing.

**这是真实的提示工程教训**：没有这句话，模型会在每次写记忆前先 `ls` / `mkdir -p` 确认目录存在，浪费一个完整 turn。现在由 harness 层（`ensureMemoryDirExists`）保证目录已创建，提示词明确告知模型"不用检查"。

**`ensureMemoryDirExists` 的幂等实现**（[memdir.ts:129-147](../src/memdir/memdir.ts#L129-L147)）：

```typescript
export async function ensureMemoryDirExists(memoryDir: string): Promise<void> {
  const fs = getFsImplementation()
  try {
    await fs.mkdir(memoryDir)  // recursive=true by default, swallows EEXIST
  } catch (e) {
    // 真正失败只在 EACCES/EPERM/EROFS 等——log 不抛
    const code = e instanceof Error && 'code' in e && typeof e.code === 'string'
      ? e.code : undefined
    logForDebugging(`ensureMemoryDirExists failed: ${code ?? String(e)}`, { level: 'debug' })
  }
}
```

失败不阻塞提示词构建——`FileWriteTool` 自己还会再 mkdir 父链，真正的权限错误会在那一层抛给用户。

**MEMORY.md 双截断**（[memdir.ts:57-103](../src/memdir/memdir.ts#L57-L103) 的 `truncateEntrypointContent`）：

```typescript
export function truncateEntrypointContent(raw: string): EntrypointTruncation {
  const trimmed = raw.trim()
  const contentLines = trimmed.split('\n')
  const lineCount = contentLines.length
  const byteCount = trimmed.length

  const wasLineTruncated = lineCount > MAX_ENTRYPOINT_LINES    // 200
  const wasByteTruncated = byteCount > MAX_ENTRYPOINT_BYTES    // 25_000

  if (!wasLineTruncated && !wasByteTruncated) return { content: trimmed, ... }

  // 1. 先按行截
  let truncated = wasLineTruncated
    ? contentLines.slice(0, MAX_ENTRYPOINT_LINES).join('\n')
    : trimmed

  // 2. 再按字节截（处理超长行）
  if (truncated.length > MAX_ENTRYPOINT_BYTES) {
    const cutAt = truncated.lastIndexOf('\n', MAX_ENTRYPOINT_BYTES)
    truncated = truncated.slice(0, cutAt > 0 ? cutAt : MAX_ENTRYPOINT_BYTES)
  }

  // 3. 附加 WARNING 行告知哪条 cap 触发
  const reason = wasByteTruncated && !wasLineTruncated
    ? `${formatFileSize(byteCount)} (limit: ${formatFileSize(MAX_ENTRYPOINT_BYTES)}) — index entries are too long`
    : wasLineTruncated && !wasByteTruncated
      ? `${lineCount} lines (limit: ${MAX_ENTRYPOINT_LINES})`
      : `${lineCount} lines and ${formatFileSize(byteCount)}`

  return {
    content: truncated + `\n\n> WARNING: ${ENTRYPOINT_NAME} is ${reason}. Only part of it was loaded. Keep index entries to one line under ~200 chars; move detail into topic files.`,
    ...
  }
}
```

**为什么双截断**：行数 cap 挡住普通索引，但用户把长内容写一行里会绕过——p100 观测 197KB 而只有 200 行以内。字节 cap 在最后换行符处切，保留语义完整。

**WARNING 行是给模型看的**：模型收到后会尝试整理索引（移内容到主题文件），这是"行内 self-healing"机制。

**`systemPromptSection('memory', ...)` 缓存**（[constants/prompts.ts:495](../src/constants/prompts.ts#L495)）：

```typescript
const dynamicSections = [
  systemPromptSection('session_guidance', () => getSessionSpecificGuidanceSection(...)),
  systemPromptSection('memory', () => loadMemoryPrompt()),   // ← 此处
  systemPromptSection('ant_model_override', () => getAntModelOverrideSection()),
  // ...
]
```

`systemPromptSection` 启动时调一次，整会话复用。意味着**会话中即使修改了记忆文件，system prompt 里的 MEMORY.md 内容不会更新**——要等下次启动。这是一个有意的权衡：保住 Anthropic prompt cache（system prompt 是 cache 前缀的一部分）。

**KAIROS 日志模式的缓存绕过设计**（[memdir.ts:328-335](../src/memdir/memdir.ts#L328-L335)）：

```typescript
function buildAssistantDailyLogPrompt(skipIndex = false): string {
  const memoryDir = getAutoMemPath()
  // Describe the path as a pattern rather than inlining today's literal path:
  // this prompt is cached by systemPromptSection('memory', ...) and NOT
  // invalidated on date change. The model derives the current date from the
  // date_change attachment (appended at the tail on midnight rollover) rather
  // than the user-context message — the latter is intentionally left stale to
  // preserve the prompt cache prefix across midnight.
  const logPathPattern = join(memoryDir, 'logs', 'YYYY', 'MM', 'YYYY-MM-DD.md')
  ...
}
```

**问题**：KAIROS 模式下每天要写不同的日期文件。如果 system prompt 里写死 `2026-04-19.md`，过午夜后 prompt 变了 → cache 失效。

**解法**：system prompt 里写 **pattern**（`YYYY/MM/YYYY-MM-DD.md`），让模型从 `date_change` attachment（跨午夜时附加到消息尾）读当天日期。System prompt 永远不变 → cache 永不失效。

**Cowork 注入**（[memdir.ts:441-446](../src/memdir/memdir.ts#L441-L446)）：

```typescript
const coworkExtraGuidelines = process.env.CLAUDE_COWORK_MEMORY_EXTRA_GUIDELINES
const extraGuidelines = coworkExtraGuidelines && coworkExtraGuidelines.trim().length > 0
  ? [coworkExtraGuidelines]
  : undefined
```

Cowork 平台通过 env 注入额外策略文字（比如"本 VM 属 X 组织，记忆需遵守 Y 规范"），拼到提示词尾部。

**目录计数遥测**（`logMemoryDirCounts` 在 memdir.ts:153-185）：fire-and-forget 异步 `readdir`，事件 `tengu_memdir_loaded` 含 `total_file_count` + `total_subdir_count`——分析用户记忆规模分布。

---

### C4. 类型系统

**四类封闭分类**（[memoryTypes.ts:14-19](../src/memdir/memoryTypes.ts#L14-L19)）：

```typescript
export const MEMORY_TYPES = ['user', 'feedback', 'project', 'reference'] as const
export type MemoryType = (typeof MEMORY_TYPES)[number]
```

**为什么必须是封闭集合**：
- 模型自由分类会漂移（今天叫 `preference`、明天叫 `habit`），检索时无法稳定过滤
- 封闭四类强制语义一致，给 Sonnet 检索器一个可靠的 `[type]` 维度
- 扩展类型需要改源码——天然防止"类型爆炸"

**每类语义表**：

| 类型 | 记什么 | 何时触发 | 如何使用 | body 结构 |
|------|--------|----------|----------|-----------|
| **user** | 用户角色、目标、技能、偏好 | 学到用户任何背景信息 | 调整协作方式（senior vs student） | 无特殊结构 |
| **feedback** | 纠正与肯定（"不要 X" / "正是这样"） | 纠正 OR 确认非显性选择 | 避免重复提醒 | Rule + **Why:** + **How to apply:** |
| **project** | 谁在做什么、为什么、截止日期 | 听到人/时间/动机 | 理解请求上下文 | Fact + **Why:** + **How to apply:** |
| **reference** | 外部系统指针（Linear/Grafana/Slack） | 用户提到外部资源 | 跳转查新鲜数据 | 简单指针 |

**"What NOT to save" 负向约束**（`memoryTypes.ts` 中的 `WHAT_NOT_TO_SAVE_SECTION`）：

```
- Code patterns, conventions, architecture, file paths, or project structure
  — these can be derived by reading the current project state.
- Git history, recent changes, or who-changed-what — `git log` / `git blame`
  are authoritative.
- Debugging solutions or fix recipes — the fix is in the code; the commit
  message has the context.
- Anything already documented in CLAUDE.md files.
- Ephemeral task details: in-progress work, temporary state, current
  conversation context.

These exclusions apply even when the user explicitly asks you to save.
```

**设计哲学**：记忆槽位珍贵（被 200 MEMORY_FILES + 25KB MEMORY.md 双 cap 约束），必须留给**无法从代码推断**的信息。代码问题用 grep，历史用 git——这些是"可再生信息"。

**Combined vs Individual 两套提示词**：

| 维度 | Individual（单目录） | Combined（团队记忆） |
|------|---------------------|---------------------|
| 函数 | `TYPES_SECTION_INDIVIDUAL` | `TYPES_SECTION_COMBINED` |
| 有 `<scope>` 标签 | ❌ | ✅ |
| 示例带 team/private 限定 | ❌ | ✅ |
| 适用场景 | 只启用 auto memory | 同时启用 auto + TEAMMEM |

**Combined 模式的 `<scope>` 路由规则**（`memoryTypes.ts:37-130`）：

| 类型 | 默认作用域 | 说明 |
|------|-----------|------|
| **user** | always private | 用户个人画像永不共享 |
| **feedback** | 默认 private | 项目级规范才 team（测试政策、构建不变量），个人风格留 private |
| **project** | 偏 team | 项目动态一般所有贡献者都要知道 |
| **reference** | 通常 team | 外部系统指针组织共享价值高 |

**为什么维护两份而不是生成**（[memoryTypes.ts:8-11](../src/memdir/memoryTypes.ts#L8-L11) 注释）：

```typescript
// The two TYPES_SECTION_* exports below are intentionally duplicated rather
// than generated from a shared spec — keeping them flat makes per-mode edits
// trivial without reasoning through a helper's conditional rendering.
```

抗过度工程化的典型例子：两份提示词词差 ~30%，抽象出来的 helper 要处理 `<scope>` 条件渲染、示例定制、指令分支，代码量比两份独立文本还多且更难 review。**"扁平重复"优于"聪明抽象"**——提示词工程中尤其如此。

**`parseMemoryType` 降级**（[memoryTypes.ts:28-31](../src/memdir/memoryTypes.ts#L28-L31)）：

```typescript
export function parseMemoryType(raw: unknown): MemoryType | undefined {
  if (typeof raw !== 'string') return undefined
  return MEMORY_TYPES.find(t => t === raw)
}
```

- 无 `type:` 字段的 legacy 文件 → 返回 `undefined`
- 未知 type（如 `type: habit`）→ 返回 `undefined`
- 检索时展示为无 `[type]` 前缀的 manifest 条目，但仍然参与选择——graceful degrade 而非 fail hard

**关键 `when_to_save` 设计细节**：

1. **Date 规范化**（project 类型）：
   > Always convert relative dates in user messages to absolute dates when saving (e.g., "Thursday" → "2026-03-05"), so the memory remains interpretable after time passes.

   三个月后回看记忆，"Thursday" 完全失去意义。强制绝对日期让记忆不会随时间失效。

2. **"Record from failure AND success"**（feedback 类型）：
   > Corrections are easy to notice; confirmations are quieter — watch for them.
   > In both cases, save what is applicable to future conversations.

   只记纠正会导致模型越来越防御性，漂移到过度保守的行为。非显性的成功选择（用户不 pushback 的非常规判断）也要记——否则模型不敢再做那个选择。

3. **"Include *why*"**（feedback + project 类型）：
   > Knowing *why* lets you judge edge cases instead of blindly following the rule.

   body 结构强制 **Why:** 行——让模型能在新场景下判断规则是否适用，而不是机械复述。

**与类型的提示词耦合**：
- `TYPES_SECTION_*` 在 `loadMemoryPrompt` 中拼入 system prompt
- `findRelevantMemories` 的 Sonnet 选择器 manifest 也带 `[type]` 前缀——类型系统**同时作用于写入提示词和读取 ranking**

---

### C5. 检索流程

**调用链概览**：

```
用户发送 query
    ↓
attachments.ts: getRelevantMemoryAttachments (attachments.ts:2192)
    ├─ 提取 @-mention 的 agent → 切换到该 agent 的 memoryDir
    │   否则用 getAutoMemPath()（可扩展为多目录）
    │
    ↓
findRelevantMemories(query, memoryDir, signal, recentTools, alreadySurfaced)
    ├─ scanMemoryFiles(memoryDir, signal)
    │   → 扫目录、并行读前 30 行 frontmatter、按 mtime 降序、slice(0, 200)
    │
    ├─ 过滤 alreadySurfaced（近期已 attach 过的路径）
    │
    ├─ formatMemoryManifest(memories)
    │   → `- [type] filename (iso-ts): description` 格式
    │
    ├─ selectRelevantMemories() → Sonnet sideQuery
    │   → JSON schema 输出 { selected_memories: string[] }
    │   → max 5
    │
    └─ return { path, mtimeMs }[]  ≤5
    ↓
readMemoriesForSurfacing(selected, signal)
    → readFileInRange 读内容，行+字节双截断
    → freshness warning 附加
    ↓
attachment { type: 'relevant_memories', memories: [...] }
    → 注入到下一轮 prompt
```

**`scanMemoryFiles` 的单次遍历优化**（[memoryScan.ts:35-77](../src/memdir/memoryScan.ts#L35-L77)）：

```typescript
export async function scanMemoryFiles(
  memoryDir: string,
  signal: AbortSignal,
): Promise<MemoryHeader[]> {
  try {
    const entries = await readdir(memoryDir, { recursive: true })
    const mdFiles = entries.filter(
      f => f.endsWith('.md') && basename(f) !== 'MEMORY.md',
    )

    const headerResults = await Promise.allSettled(
      mdFiles.map(async (relativePath): Promise<MemoryHeader> => {
        const filePath = join(memoryDir, relativePath)
        const { content, mtimeMs } = await readFileInRange(
          filePath, 0, FRONTMATTER_MAX_LINES,  // 前 30 行
          undefined, signal,
        )
        const { frontmatter } = parseFrontmatter(content, filePath)
        return {
          filename: relativePath,
          filePath, mtimeMs,
          description: frontmatter.description || null,
          type: parseMemoryType(frontmatter.type),
        }
      }),
    )

    return headerResults
      .filter((r): r is PromiseFulfilledResult<MemoryHeader> =>
        r.status === 'fulfilled')
      .map(r => r.value)
      .sort((a, b) => b.mtimeMs - a.mtimeMs)
      .slice(0, MAX_MEMORY_FILES)  // 200
  } catch {
    return []
  }
}
```

**关键优化**：`readFileInRange` 内部 stat，返回 `{ content, mtimeMs }`——一次 syscall 同时拿时间戳和内容。对比"先 stat 再 read"减半 syscall。在 N≤200 的常见场景直接 halve；N>200 时多读几个小文件但仍避开幸存 200 个的双 stat。

**Sonnet 选择器系统提示**（[findRelevantMemories.ts:18-24](../src/memdir/findRelevantMemories.ts#L18-L24)）：

```typescript
const SELECT_MEMORIES_SYSTEM_PROMPT = `You are selecting memories that will be useful to Claude Code as it processes a user's query. You will be given the user's query and a list of available memory files with their filenames and descriptions.

Return a list of filenames for the memories that will clearly be useful to Claude Code as it processes the user's query (up to 5). Only include memories that you are certain will be helpful based on their name and description.
- If you are unsure if a memory will be useful in processing the user's query, then do not include it in your list. Be selective and discerning.
- If there are no memories in the list that would clearly be useful, feel free to return an empty list.
- If a list of recently-used tools is provided, do not select memories that are usage reference or API documentation for those tools (Claude Code is already exercising them). DO still select memories containing warnings, gotchas, or known issues about those tools — active use is exactly when those matter.
`
```

**"工具正在用就不选其文档，但选其警告"的精妙**：当 `mcp__X__spawn` 正在被调用，它的 reference 文档就是噪音——对话里已有工作中的示例；但它的"已知问题 / gotcha"恰恰是此时最相关的。**这是向量检索做不到的语义区分**。

**JSON Schema 输出约束**（`findRelevantMemories.ts:109-119`）：

```typescript
const result = await sideQuery({
  model: getDefaultSonnetModel(),
  system: SELECT_MEMORIES_SYSTEM_PROMPT,
  skipSystemPromptPrefix: true,
  messages: [{
    role: 'user',
    content: `Query: ${query}\n\nAvailable memories:\n${manifest}${toolsSection}`,
  }],
  max_tokens: 256,
  output_format: {
    type: 'json_schema',
    schema: {
      type: 'object',
      properties: {
        selected_memories: { type: 'array', items: { type: 'string' } },
      },
      required: ['selected_memories'],
      additionalProperties: false,
    },
  },
  signal,
  querySource: 'memdir_relevance',
})
```

- **强制 JSON 输出**——server-side schema 验证，parse 失败直接拒绝
- **max_tokens: 256** —— 只要文件名列表，不需要解释
- **二次校验**：`parsed.selected_memories.filter(f => validFilenames.has(f))`——防模型幻觉文件名

**`alreadySurfaced` 去重**（[attachments.ts:2247-2262](../src/utils/attachments.ts#L2247-L2262)）：

```typescript
export function collectSurfacedMemories(messages: ReadonlyArray<Message>): {
  paths: Set<string>
  totalBytes: number
} {
  const paths = new Set<string>()
  let totalBytes = 0
  for (const m of messages) {
    if (m.type === 'attachment' && m.attachment.type === 'relevant_memories') {
      for (const mem of m.attachment.memories) {
        paths.add(mem.path)
        totalBytes += mem.content.length
      }
    }
  }
  return { paths, totalBytes }
}
```

**设计要点**：
- 扫描消息历史里所有 `relevant_memories` attachment
- 在 `findRelevantMemories` **调用前**传入（filter 在 scanMemoryFiles 之后、Sonnet 之前）—— Sonnet 的 5 槽预算不浪费在重选
- Compact 会自动重置：旧 attachment 从 compacted transcript 消失 → 重新成为候选

**多目录检索扩展**（attachments.ts:2202-2209）：

```typescript
const memoryDirs = extractAgentMentions(input).flatMap(mention => {
  const agentType = mention.replace('agent-', '')
  const agentDef = agents.find(def => def.agentType === agentType)
  return agentDef?.memory
    ? [getAgentMemoryDir(agentType, agentDef.memory)]
    : []
})
const dirs = memoryDirs.length > 0 ? memoryDirs : [getAutoMemPath()]

const allResults = await Promise.all(
  dirs.map(dir => findRelevantMemories(input, dir, signal, recentTools, alreadySurfaced)
    .catch(() => [])),  // 单目录失败不影响其他
)
// 多目录结果 flat + readFileState 过滤 + slice(0, 5)
const selected = allResults
  .flat()
  .filter(m => !readFileState.has(m.path) && !alreadySurfaced.has(m.path))
  .slice(0, 5)
```

`@agent-planner` mention 时只检索该代理的 memoryDir（隔离），否则检索主记忆。多目录用 `Promise.all` 并行，单目录失败返回空数组不影响整体。

**新鲜度警告**（`memoryAge.ts` 的 `memoryFreshnessText`）：

```typescript
function memoryFreshnessText(mtimeMs: number): string {
  const d = memoryAgeDays(mtimeMs)
  if (d <= 1) return ''  // 今天/昨天,无警告
  return `This memory is ${d} days old. Memories are point-in-time observations...
          Verify against current code before asserting as fact.`
}
```

注入 attachment 时附加到主题文件内容前——让主模型在引用前先用 grep/read 验证。

**内容截断**（`readMemoriesForSurfacing` 使用 `readFileInRange` + `truncateOnByteLimit`）：

- 行 cap + 字节 cap 双重
- 截断时保留 frontmatter + 文件开头（最有价值的信息通常在 body 前段）
- 附加 "content truncated" 注记——让模型知道还有更多
- 优先 **部分呈现 + 警告** 而非完全丢弃——Sonnet 已判定此条最相关，部分总比没有强

**遥测 `tengu_auto_mem_recall_shape`**（`feature('MEMORY_SHAPE_TELEMETRY')`）：

```typescript
// Fires even on empty selection: selection-rate needs the denominator,
// and -1 ages distinguish "ran, picked nothing" from "never ran".
logMemoryRecallShape(memories, selected)
```

空选择也上报——用于计算 selection rate 的分母（多少次调用选了 0 个）。`-1` 年龄作为 sentinel 区分"跑了但没选"和"根本没跑"。

---

## D. 写入管线

### D1. extractMemories — 每轮末提取

#### D1a. 触发点与调用关系

**触发位置**（[query/stopHooks.ts:141-156](../src/query/stopHooks.ts#L141-L156)）：

```typescript
if (!isBareMode()) {
  // promptSuggestion 先行（与 memory 独立）
  if (!isEnvDefinedFalsy(process.env.CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION)) {
    void executePromptSuggestion(stopHookContext)
  }
  // extractMemories
  if (
    feature('EXTRACT_MEMORIES') &&
    !toolUseContext.agentId &&
    isExtractModeActive()
  ) {
    // Fire-and-forget in both interactive and non-interactive. For -p/SDK,
    // print.ts drains the in-flight promise after flushing the response
    // but before gracefulShutdownSync (see drainPendingExtraction).
    void extractMemoriesModule!.executeExtractMemories(
      stopHookContext,
      toolUseContext.appendSystemMessage,
    )
  }
  // autoDream（见 D2）
  if (!toolUseContext.agentId) {
    void executeAutoDream(stopHookContext, toolUseContext.appendSystemMessage)
  }
}
```

**触发条件（AND 关系）**：
1. **非 bare 模式** —— `--bare` / `CLAUDE_CODE_SIMPLE=1` 时关闭所有后台
2. **`feature('EXTRACT_MEMORIES')`** —— 编译期宏，外部构建可 DCE 掉整块代码
3. **非子代理**（`!toolUseContext.agentId`）—— 子代理有自己的 agent memory，不触发主记忆提取
4. **`isExtractModeActive()`** —— 含 `tengu_passport_quail` GB flag + 交互/非交互判断（[paths.ts:69-77](../src/memdir/paths.ts#L69-L77)）：

```typescript
export function isExtractModeActive(): boolean {
  if (!getFeatureValue_CACHED_MAY_BE_STALE('tengu_passport_quail', false)) return false
  return (
    !getIsNonInteractiveSession() ||
    getFeatureValue_CACHED_MAY_BE_STALE('tengu_slate_thimble', false)
  )
}
```

**fire-and-forget 设计**：`void executeExtractMemories(...)` 不 await——stopHooks 必须快速返回让主循环继续。真正的等待发生在进程退出前：

```
  主循环 stopHook → void executeExtractMemories()
       ↓
  主循环继续下一 turn 或退出
       ↓
  -p/SDK 模式：print.ts 在 response flush 后,graceful shutdown 前调
              drainPendingExtraction(60_000) 等 fork 完成
       ↓
  否则进程退出,fork 自然中断（.unref() 定时器不阻塞）
```

**初始化位置**（`utils/backgroundHousekeeping.ts` 中的 `initExtractMemories`）：

```typescript
// 启动时统一初始化所有后台模块
initExtractMemories()   // 建立 extractor/drainer 闭包
initAutoDream()         // 建立 runner 闭包
initSessionMemory()     // 注册 postSamplingHook
```

三者都用 `init*` 模式返回闭包——见下节 D1b 详解。

---

#### D1b. 闭包状态设计

**模块导出槽**（[extractMemories.ts:280-288](../src/services/extractMemories/extractMemories.ts#L280-L288)）：

```typescript
/** The active extractor function, set by initExtractMemories(). */
let extractor:
  | ((
      context: REPLHookContext,
      appendSystemMessage?: AppendSystemMessageFn,
    ) => Promise<void>)
  | null = null

/** The active drain function, set by initExtractMemories(). No-op until init. */
let drainer: (timeoutMs?: number) => Promise<void> = async () => {}
```

模块级只有两个函数槽，**所有状态都在闭包里**。未 init 时 extractor = null，公开 API 直接 return；drainer 是 noop async。

**`initExtractMemories` 的六个闭包变量**（`extractMemories.ts:297-325`）：

```typescript
export function initExtractMemories(): void {
  // 1. 所有未结束的 fork Promise
  //    coalesced 快速 resolve 的也加进来;真正干活的 call 加的 promise
  //    覆盖整个 trailing chain（runExtraction 的 recursive finally）
  const inFlightExtractions = new Set<Promise<void>>()

  // 2. 游标 UUID —— 每次 run 只看此 UUID 之后的消息
  let lastMemoryMessageUuid: string | undefined

  // 3. 一次性日志标志（ant-only）
  let hasLoggedGateFailure = false

  // 4. 互斥开关 —— runExtraction 正在跑时为 true
  let inProgress = false

  // 5. 自上次提取以来的合格 turn 计数
  let turnsSinceLastExtraction = 0

  // 6. 进行中时暂存最新上下文
  //    trailing run 完成后消费并清空
  let pendingContext:
    | { context: REPLHookContext; appendSystemMessage?: AppendSystemMessageFn }
    | undefined
  // ...
}
```

**为什么闭包而非 module-level**：

```typescript
// 作者注释（extractMemories.ts:11-14）：
// State is closure-scoped inside initExtractMemories() rather than module-level,
// following the same pattern as confidenceRating.ts. Tests call
// initExtractMemories() in beforeEach to get a fresh closure.
```

- **测试隔离**：单测用 `beforeEach(() => initExtractMemories())` 拿到干净闭包，不用 `vi.resetModules()` 或 export setter 来手动清 module-level state
- **封装**：外部只能通过 `executeExtractMemories` 和 `drainPendingExtraction` 两个函数访问，状态完全不可见
- **并发安全**：单 REPL 进程只有一个闭包（init 一次），六个变量的读写在单线程事件循环里天然串行

**与 `confidenceRating.ts` / `sessionMemory.ts` 的对比**：

| 模块 | 状态容器 | 典型状态 |
|------|---------|----------|
| `extractMemories` | init 返回的闭包 | 游标、互斥、pending、频率 |
| `confidenceRating` | init 返回的闭包 | 同模式 |
| `sessionMemory` | module-level + `memoize` | 更多是配置，状态较简单 |
| `teamMemorySync` | `SyncState` 对象 | ETag map、watcher 引用 |

**设计决策边界**：过程式状态（游标、布尔）用闭包；结构化状态（map、引用）用对象——SyncState 那种场景闭包会导致字段命名失控。

**`extractor` 与 `drainer` 的绑定**（`extractMemories.ts:569-586`）：

```typescript
  extractor = async (context, appendSystemMessage) => {
    const p = executeExtractMemoriesImpl(context, appendSystemMessage)
    inFlightExtractions.add(p)
    try {
      await p
    } finally {
      inFlightExtractions.delete(p)
    }
  }

  drainer = async (timeoutMs = 60_000) => {
    if (inFlightExtractions.size === 0) return
    await Promise.race([
      Promise.all(inFlightExtractions).catch(() => {}),
      new Promise<void>(r => setTimeout(r, timeoutMs).unref()),
    ])
  }
}
```

`extractor` 把 impl 包一层记账 —— 每个 call 的 promise 自动加入/移出 `inFlightExtractions`，drainer 等所有未结束的一起完成。

---

#### D1c. 守卫链 + 互斥 + 频率控制

**`executeExtractMemoriesImpl` 的五重守卫**（[extractMemories.ts:527-567](../src/services/extractMemories/extractMemories.ts#L527-L567)，按成本升序）：

| # | 守卫 | 检查点 | 跳过方式 |
|---|------|--------|----------|
| 1 | 子代理？ | `context.toolUseContext.agentId` | 直接 return |
| 2 | GB gate | `tengu_passport_quail` | return + 一次性 log（ant-only）|
| 3 | autoMem 启用？ | `isAutoMemoryEnabled()` | 直接 return |
| 4 | 远程模式？ | `getIsRemoteMode()` | 直接 return |
| 5 | 互斥（in-progress） | 闭包 `inProgress` | 暂存到 `pendingContext` + 记 `tengu_extract_memories_coalesced` |

**守卫 5（pending）源码**：

```typescript
if (inProgress) {
  logForDebugging('[extractMemories] extraction in progress — stashing for trailing run')
  logEvent('tengu_extract_memories_coalesced', {})
  pendingContext = { context, appendSystemMessage }
  return
}

await runExtraction({ context, appendSystemMessage })
```

**关键语义**：`pendingContext` 会**覆盖**之前暂存的值——只保留最新的，因为最新的包含最多的消息（旧的已经是子集）。

**`runExtraction` 内部的两个二次检查**：

**(a) 游标互斥 `hasMemoryWritesSince`**（[extractMemories.ts:121-148](../src/services/extractMemories/extractMemories.ts#L121-L148)）：

```typescript
function hasMemoryWritesSince(
  messages: Message[],
  sinceUuid: string | undefined,
): boolean {
  let foundStart = sinceUuid === undefined
  for (const message of messages) {
    if (!foundStart) {
      if (message.uuid === sinceUuid) foundStart = true
      continue
    }
    if (message.type !== 'assistant') continue
    const content = (message as AssistantMessage).message.content
    if (!Array.isArray(content)) continue
    for (const block of content) {
      const filePath = getWrittenFilePath(block)
      if (filePath !== undefined && isAutoMemPath(filePath)) {
        return true  // 主 agent 已写过记忆
      }
    }
  }
  return false
}
```

**哲学**：主 agent 的系统提示词**已包含完整保存指令**（`buildMemoryLines`）——如果用户说"记住 X"，主 agent 会当场 Edit/Write 记忆文件。此时后台 fork 是**重复工作**。

**行为**（`extractMemories.ts:348-360`）：

```typescript
if (hasMemoryWritesSince(messages, lastMemoryMessageUuid)) {
  logForDebugging('[extractMemories] skipping — conversation already wrote to memory files')
  const lastMessage = messages.at(-1)
  if (lastMessage?.uuid) {
    lastMemoryMessageUuid = lastMessage.uuid  // 推进游标
  }
  logEvent('tengu_extract_memories_skipped_direct_write', { message_count: newMessageCount })
  return
}
```

跳过 fork **且推进游标**——下次运行不会重新考虑这段区间。

**(b) 频率限速 `tengu_bramble_lintel`**（`extractMemories.ts:374-386`）：

```typescript
if (!isTrailingRun) {
  turnsSinceLastExtraction++
  if (
    turnsSinceLastExtraction <
    (getFeatureValue_CACHED_MAY_BE_STALE('tengu_bramble_lintel', null) ?? 1)
  ) {
    return
  }
}
turnsSinceLastExtraction = 0
```

- 默认每轮都跑（`?? 1`）
- GB 远程可调到更低频（比如每 3 轮才跑一次，降成本）
- **`isTrailingRun: true` 时跳过限速**——trailing run 处理已提交的工作，不应该被限速

**`countModelVisibleMessagesSince` 的 fallback**（`extractMemories.ts:82-110`）：

```typescript
function countModelVisibleMessagesSince(messages: Message[], sinceUuid: string | undefined): number {
  if (sinceUuid === null || sinceUuid === undefined) {
    return count(messages, isModelVisibleMessage)
  }
  let foundStart = false
  let n = 0
  for (const message of messages) {
    if (!foundStart) {
      if (message.uuid === sinceUuid) foundStart = true
      continue
    }
    if (isModelVisibleMessage(message)) n++
  }
  // If sinceUuid was not found (e.g., removed by context compaction),
  // fall back to counting all model-visible messages rather than returning 0
  // which would permanently disable extraction for the rest of the session.
  if (!foundStart) {
    return count(messages, isModelVisibleMessage)
  }
  return n
}
```

**关键 edge case**：游标 UUID 在 compact 后可能从消息数组消失——简单实现会 count 出 0 → 再也不 trigger。这里 fallback 到全量计数，保证 compact 之后 extraction 能继续。

---

#### D1d. 权限函数 `createAutoMemCanUseTool`

**完整源码**（[extractMemories.ts:171-222](../src/services/extractMemories/extractMemories.ts#L171-L222)）：

```typescript
export function createAutoMemCanUseTool(memoryDir: string): CanUseToolFn {
  return async (tool: Tool, input: Record<string, unknown>) => {
    // 1. REPL 允许（REPL 模式下原子工具被隐藏,fork 通过 REPL 调 VM 再 re-invoke canUseTool）
    if (tool.name === REPL_TOOL_NAME) {
      return { behavior: 'allow', updatedInput: input }
    }

    // 2. 读类工具无限制
    if (
      tool.name === FILE_READ_TOOL_NAME ||
      tool.name === GREP_TOOL_NAME ||
      tool.name === GLOB_TOOL_NAME
    ) {
      return { behavior: 'allow', updatedInput: input }
    }

    // 3. Bash 仅允许只读命令
    if (tool.name === BASH_TOOL_NAME) {
      const parsed = tool.inputSchema.safeParse(input)
      if (parsed.success && tool.isReadOnly(parsed.data)) {
        return { behavior: 'allow', updatedInput: input }
      }
      return denyAutoMemTool(
        tool,
        'Only read-only shell commands are permitted in this context (ls, find, grep, cat, stat, wc, head, tail, and similar)',
      )
    }

    // 4. Edit/Write 仅限记忆目录内
    if (
      (tool.name === FILE_EDIT_TOOL_NAME || tool.name === FILE_WRITE_TOOL_NAME) &&
      'file_path' in input
    ) {
      const filePath = input.file_path
      if (typeof filePath === 'string' && isAutoMemPath(filePath)) {
        return { behavior: 'allow', updatedInput: input }
      }
    }

    // 5. 其他全部拒绝
    return denyAutoMemTool(
      tool,
      `only ${FILE_READ_TOOL_NAME}, ${GREP_TOOL_NAME}, ${GLOB_TOOL_NAME}, read-only ${BASH_TOOL_NAME}, and ${FILE_EDIT_TOOL_NAME}/${FILE_WRITE_TOOL_NAME} within ${memoryDir} are allowed`,
    )
  }
}
```

**五类规则速览**：

| 工具 | 策略 | 原因 |
|------|------|------|
| **REPL** | allow | REPL 模式的工具隐藏策略；内部会 re-invoke canUseTool 对原子操作再判 |
| **Read/Grep/Glob** | allow 无限制 | 读类本身无副作用 |
| **Bash** | allow 仅 `isReadOnly` 通过 | ls/find/grep/cat/stat/wc/head/tail 等 |
| **Edit/Write** | allow 仅 `isAutoMemPath()` 为 true | 限死记忆目录 |
| **其他（MCP/Agent/非只读 Bash/外部 Write）** | deny + 记遥测 | 防止后台 fork 越权 |

**共享性**：autoDream 也用同一个函数（[autoDream.ts:227](../src/services/autoDream/autoDream.ts#L227)）—— 两者权限需求完全一致（只读 + 写自己目录），唯一共享点让安全策略演进无需同步改两处。

**`denyAutoMemTool` 的遥测**（`extractMemories.ts:154-164`）：

```typescript
function denyAutoMemTool(tool: Tool, reason: string) {
  logForDebugging(`[autoMem] denied ${tool.name}: ${reason}`)
  logEvent('tengu_auto_mem_tool_denied', {
    tool_name: sanitizeToolNameForAnalytics(tool.name),
  })
  return {
    behavior: 'deny' as const,
    message: reason,  // 告知模型"为什么不能用"
    decisionReason: { type: 'other' as const, reason },
  }
}
```

**model-facing message**：拒绝时返回的 `message` 会进入 tool_result 反馈给模型——让它知道"这个权限场景下该工具不可用"而不是困惑地反复尝试。

**`sanitizeToolNameForAnalytics`**：MCP 工具名含用户自定义字符串（可能泄露信息），sanitize 到 `mcp__<anonymized>__<op>` 形式保护隐私。

**为什么不从 tools 数组里删？**（核心设计约束，B4 已铺垫）

Anthropic prompt cache 的 key 包含 tools 数组的完整序列化。如果给 fork 换一个缩减版的 tools 数组：
- fork 第一次请求 → cache miss → 付全额 cache creation
- 每次 extractMemories 都是 cache miss → 成本飙升 10x+
- 5 分钟 TTL 内的其他 fork 也受牵连

**现行方案的代价**：模型会看到工具列表里有 `Agent`、`MCP`、`WebFetch` 等但调用被拒 —— 所以提示词必须教会模型"这些工具不可用"。系统提示词 + tool_result 里的 `message` 共同承担这个沟通任务。

**相关对照 `createMemoryFileCanUseTool`**（SessionMemory 专用）：

```typescript
// 更严：精确到文件+精确到工具
if (tool.name === FILE_EDIT_TOOL_NAME &&
    typeof filePath === 'string' &&
    filePath === memoryPath) {
  return { behavior: 'allow', ... }
}
return { behavior: 'deny', ... }
```

SessionMemory 只需 Edit 一个文件 → 权限可以极限收紧。设计模式一致：**不改 tools，只改 canUseTool**。

---

#### D1e. Trailing run + drain 机制

**问题背景**：extractMemories 一次运行需要 2-4 个 LLM turn + 数百 ms 到几秒。如果用户快速连续发两条消息，第二条的 stopHook 触发时第一次还在跑 → **需要优雅处理并发**。

**方案：覆盖式暂存 + trailing run**。

**`runExtraction` 的 finally 段**（[extractMemories.ts:503-522](../src/services/extractMemories/extractMemories.ts#L503-L522)）：

```typescript
} finally {
  inProgress = false

  // If a call arrived while we were running, run a trailing extraction
  // with the latest stashed context. The trailing run will compute its
  // newMessageCount relative to the cursor we just advanced — so it only
  // picks up messages added between the two calls, not the full history.
  const trailing = pendingContext
  pendingContext = undefined
  if (trailing) {
    logForDebugging('[extractMemories] running trailing extraction for stashed context')
    await runExtraction({
      context: trailing.context,
      appendSystemMessage: trailing.appendSystemMessage,
      isTrailingRun: true,
    })
  }
}
```

**关键语义**：

1. **递归 await**：trailing run 的 promise 串在当前 promise 链上 → `inFlightExtractions` 里的单个 promise 覆盖**整条链**，drainer 不用单独追踪 trailing
2. **只留最新的 stash**：第 N 次 call 到来时覆盖第 N-1 次的 pendingContext——旧上下文是新上下文的子集，浪费 fork 成本没意义
3. **游标已推进**：trailing run 的 `newMessageCount` 相对"刚刚推进的 lastMemoryMessageUuid"计算，只处理两次 call 之间的新消息
4. **跳过频率限速**（`isTrailingRun: true` 绕过 `tengu_bramble_lintel`）—— trailing 是"补偿"，处理已承诺的工作

**典型时序**：

```
t=0    Call A → 开始 runExtraction A (in-progress)
t=1    Call B → in-progress=true → stash,记 coalesced
t=2    Call C → in-progress=true → 覆盖 stash 为 C
t=5    A 完成,finally 段：
         trailing = C（不是 B!）
         pendingContext = undefined
         await runExtraction(C, isTrailingRun=true)
t=6    C 完成 → inProgress=false → 无新 stash → 退出
```

B 被合并到 C 里处理 —— 完全符合"用户只关心最新状态"的直觉。

**`drainPendingExtraction` 的精妙**（[extractMemories.ts:579-586](../src/services/extractMemories/extractMemories.ts#L579-L586)）：

```typescript
drainer = async (timeoutMs = 60_000) => {
  if (inFlightExtractions.size === 0) return
  await Promise.race([
    Promise.all(inFlightExtractions).catch(() => {}),
    // eslint-disable-next-line no-restricted-syntax -- sleep() has no .unref(); timer must not block exit
    new Promise<void>(r => setTimeout(r, timeoutMs).unref()),
  ])
}
```

**三个设计点**：

1. **`Promise.all([...]).catch(() => {})`**：任何一个 fork 失败不阻止 drain 继续等其他的——丢弃错误是合理的，错误已在 fork 内部 log
2. **`Promise.race` 实现软超时**：正常完成的快走，超时了也走——不卡住进程退出
3. **`.unref()` 是关键**：普通 `setTimeout` 会让事件循环保持活跃直到触发。`.unref()` 告诉 Node "如果我是唯一活跃源，不用等我"。这样如果所有 fork 都完成，定时器不会阻止 60s 提前退出

**注释里的 `// eslint-disable-next-line`**：自定义 ESLint 规则禁止裸 `setTimeout`（推荐用 `sleep()` 辅助函数）。这里必须用裸版本，因为 `sleep()` 没暴露 `.unref()`。

**集成点**（`print.ts` —— 处理 `-p/SDK` 模式）：

```
print mode 流程：
  1. 主 query 循环跑完,产出最终 response
  2. 把 response flush 到 stdout（用户看到结果）
  3. await drainPendingExtraction(60_000)   ← 等 extractMemories 写完
  4. gracefulShutdownSync()                 ← 进程退出
```

**为什么 print 模式特别需要 drain**：交互 REPL 模式下，用户关闭前 extractMemories 通常早已完成（秒级），没等也无所谓（下次启动读记忆文件即可看到）。但 `-p` 模式是**一次性 CLI 调用**：
- 如果不 drain，`claude -p "记住 X"` 返回后进程立即退出，fork 被 kill → 记忆文件没写入
- 下次调用不会读到本次的 "记住 X"——用户感知"命令不生效"
- 所以 print 模式必须 drain（有 60s 软超时兜底）

**graceful shutdown 配合**：[entrypoints/cli.tsx](../src/entrypoints/cli.tsx) 的 `gracefulShutdownSync` 有 5s failsafe——如果 drain 60s 都没完成，后续 5s 会强杀。两层超时避免僵尸进程。

**Coalesced 事件的用处**：`tengu_extract_memories_coalesced` 统计 —— 如果这个率很高说明用户消息节奏远快于 fork 完成速度，可能要：
- 调低 `tengu_bramble_lintel`（更不频繁跑，降 fork 次数）
- 调短 maxTurns（加快单次 fork）
- 调整提示词让模型更简练

---

### D2. autoDream — 做梦式整合

#### D2a. 定位与触发

**核心隐喻**（[autoDream.ts:1-3](../src/services/autoDream/autoDream.ts#L1-L3) 注释）：

```
Background memory consolidation. Fires the /dream prompt as a forked
subagent when time-gate passes AND enough sessions have accumulated.
```

**白天 vs 晚上**：

| 管线 | 人类类比 | 频率 | 目标 |
|------|---------|------|------|
| `extractMemories` | 白天随手记笔记 | 每轮末 | 写入**新**记忆 |
| `autoDream` | 晚上整理笔记本 | ≥24h + ≥5 会话 | **整合 / 去重 / 修剪**已有记忆 |

两者共存是设计选择：合并到一个管线会损失任一目标的优化（B4 已讨论 maxTurns 为何不同；F2-Q7 会详述）。

**触发点**（[stopHooks.ts:154-156](../src/query/stopHooks.ts#L154-L156)）：

```typescript
if (!toolUseContext.agentId) {
  void executeAutoDream(stopHookContext, toolUseContext.appendSystemMessage)
}
```

**与 extractMemories 的独立关系**：在 `stopHooks.ts` 中**串行 fire-and-forget**，顺序：

```
stopHook 触发
  ↓
void executePromptSuggestion()       ← 完全无关
  ↓
void executeExtractMemories()         ← 要求 feature('EXTRACT_MEMORIES')
  ↓
void executeAutoDream()               ← 只要求 !agentId
```

三个 void 互不等待，各自 fire-and-forget。因此在同一个 turn 末可能**三个 fork 并发**（通过 `canUseTool` 和锁机制避免写冲突）。

**入口守卫 `isGateOpen`**（[autoDream.ts:95-100](../src/services/autoDream/autoDream.ts#L95-L100)）：

```typescript
function isGateOpen(): boolean {
  if (getKairosActive()) return false        // KAIROS 用独立 disk-skill dream
  if (getIsRemoteMode()) return false        // 远程模式不跑
  if (!isAutoMemoryEnabled()) return false   // 依赖 autoMem 启用
  return isAutoDreamEnabled()                 // 自身 flag
}
```

**`isAutoDreamEnabled`**（[autoDream/config.ts:13-21](../src/services/autoDream/config.ts#L13-L21)）：

```typescript
export function isAutoDreamEnabled(): boolean {
  const setting = getInitialSettings().autoDreamEnabled
  if (setting !== undefined) return setting                // 用户设置优先
  const gb = getFeatureValue_CACHED_MAY_BE_STALE<{ enabled?: unknown } | null>(
    'tengu_onyx_plover', null,
  )
  return gb?.enabled === true                              // 否则看 GB
}
```

**两层降级**：设置显式 > GrowthBook 默认。设置的"未定义"才回落到远程 flag——给用户最终否决权。

**`init` 模式**（`autoDream.ts:111-116`）：

```typescript
let runner: ((context, appendSystemMessage?) => Promise<void>) | null = null

export function initAutoDream(): void {
  let lastSessionScanAt = 0
  runner = async function runAutoDream(context, appendSystemMessage) {
    // ... 闭包内读 cfg、门控链、fork 等
  }
}
```

闭包只有一个变量 `lastSessionScanAt`（用于扫描节流，D2b 详解）——autoDream 本身几乎无状态（锁文件即持久状态），与 extractMemories 的 6 变量闭包对比鲜明。

---

#### D2b. 五重门控

**按成本升序**（autoDream.ts:125-190）：

| # | 门控 | 成本 | 条件 |
|---|------|------|------|
| 1 | **功能** | 内存读 | `isGateOpen()`：非 KAIROS + 非 remote + autoMem 启用 + autoDream 启用 |
| 2 | **时间** | 1 stat | `(now - lockMtime) / 3600000 >= minHours`（默认 24h）|
| 3 | **扫描节流** | 时间戳比较 | `now - lastSessionScanAt >= 10min` |
| 4 | **会话** | 目录扫描 | 排除当前 session 后，自 lastAt 后被碰过的 sessions ≥ minSessions（默认 5）|
| 5 | **锁** | stat + read | `tryAcquireConsolidationLock` 成功 |

**默认参数**（`autoDream.ts:63-66`）：

```typescript
const DEFAULTS: AutoDreamConfig = {
  minHours: 24,
  minSessions: 5,
}
```

**GB 动态配置**（`tengu_onyx_plover`, autoDream.ts:73-93）：

```typescript
function getConfig(): AutoDreamConfig {
  const raw = getFeatureValue_CACHED_MAY_BE_STALE<Partial<AutoDreamConfig> | null>(
    'tengu_onyx_plover', null,
  )
  return {
    minHours:
      typeof raw?.minHours === 'number' &&
      Number.isFinite(raw.minHours) &&
      raw.minHours > 0
        ? raw.minHours
        : DEFAULTS.minHours,
    minSessions:
      typeof raw?.minSessions === 'number' &&
      Number.isFinite(raw.minSessions) &&
      raw.minSessions > 0
        ? raw.minSessions
        : DEFAULTS.minSessions,
  }
}
```

**防御性逐字段校验**：GB 缓存可能返回 stale 错类型（比如 `minHours: "24"` 字符串、`NaN`、`-1`），每个字段独立校验 + 回落默认。不用 Zod 是因为 GB cache 调用路径要极快。

**时间门**（`autoDream.ts:132-141`）：

```typescript
let lastAt: number
try {
  lastAt = await readLastConsolidatedAt()   // stat 锁文件 mtime
} catch (e) {
  logForDebugging(`[autoDream] readLastConsolidatedAt failed: ${(e as Error).message}`)
  return
}
const hoursSince = (Date.now() - lastAt) / 3_600_000
if (!force && hoursSince < cfg.minHours) return
```

**锁文件的双重身份**：`.consolidate-lock` 的 mtime **既是锁时间戳又是 lastConsolidatedAt** —— 省一个独立的 "last dream" 文件。详见 D2c。

**扫描节流的必要性**（`autoDream.ts:54-56`）：

```typescript
// Scan throttle: when time-gate passes but session-gate doesn't, the lock
// mtime doesn't advance, so the time-gate keeps passing every turn.
const SESSION_SCAN_INTERVAL_MS = 10 * 60 * 1000
```

**问题场景**：
1. 用户 24h 没跑 dream → 时间门通过
2. 但这 24h 只有 3 个会话（`< minSessions=5`）→ 会话门未通过
3. 会话门未通过**不更新锁 mtime**（没真正 dream）
4. 下个 turn：时间门仍然 >24h → 再次通过 → 再次扫描会话目录
5. **每个 turn 都扫描**——浪费 I/O

**解法**：扫描节流 —— 时间门通过后的扫描之间至少 10min 间隔。

```typescript
const sinceScanMs = Date.now() - lastSessionScanAt
if (!force && sinceScanMs < SESSION_SCAN_INTERVAL_MS) {
  logForDebugging(`[autoDream] scan throttle — time-gate passed but last scan was ${Math.round(sinceScanMs / 1000)}s ago`)
  return
}
lastSessionScanAt = Date.now()
```

`lastSessionScanAt` 是闭包变量（进程级，崩溃重启后会重新扫一次——可接受）。

**会话门**（`autoDream.ts:154-171`）：

```typescript
let sessionIds: string[]
try {
  sessionIds = await listSessionsTouchedSince(lastAt)
} catch (e) { /* log and return */ }

// Exclude the current session (its mtime is always recent).
const currentSession = getSessionId()
sessionIds = sessionIds.filter(id => id !== currentSession)

if (!force && sessionIds.length < cfg.minSessions) {
  logForDebugging(`[autoDream] skip — ${sessionIds.length} sessions since last consolidation, need ${cfg.minSessions}`)
  return
}
```

**关键细节**：
- **排除当前 session**——当前会话的 transcript 文件 mtime 必然 > lastAt（因为正在被写），不排除会导致"1 个新会话也能触发"
- `listSessionsTouchedSince` 用 **mtime 而非 birthtime**——ext4 的 birthtime 是 0，用 mtime 表示"此后被碰过"
- 扫描每 cwd 的 transcript 目录（非跨 worktree）——作为 skip-gate，undercount 是安全的

**Forced 模式**（`autoDream.ts:105-107, 178-190`）：

```typescript
function isForced(): boolean { return false }   // ant-only test override
```

内部测试用——绕过 enabled/time/session gates 但**保留锁**（避免刷屏），保留扫描步骤（prompt 的 session-hint 需要）。`priorMtime` 在 force 路径下复用 `lastAt`，让 abort rollback 无 op。

---

#### D2c. 锁文件机制

**核心数据结构**（[consolidationLock.ts:16-23](../src/services/autoDream/consolidationLock.ts#L16-L23)）：

```typescript
const LOCK_FILE = '.consolidate-lock'
const HOLDER_STALE_MS = 60 * 60 * 1000  // 1h

function lockPath(): string {
  return join(getAutoMemPath(), LOCK_FILE)
}
```

**文件语义**：

| 属性 | 值 | 双重用途 |
|------|-----|----------|
| **mtime** | 最近 acquire 时间 | (1) 锁时间戳 (2) `lastConsolidatedAt` |
| **body** | 持有者 PID（`String(process.pid)`）| (1) 判活 (2) 竞争仲裁 |
| **过期** | mtime > 1h → stale | 防 PID 复用误判 |

**Acquire 流程**（[consolidationLock.ts:46-84](../src/services/autoDream/consolidationLock.ts#L46-L84)）：

```typescript
export async function tryAcquireConsolidationLock(): Promise<number | null> {
  const path = lockPath()

  let mtimeMs: number | undefined
  let holderPid: number | undefined
  try {
    const [s, raw] = await Promise.all([stat(path), readFile(path, 'utf8')])
    mtimeMs = s.mtimeMs
    const parsed = parseInt(raw.trim(), 10)
    holderPid = Number.isFinite(parsed) ? parsed : undefined
  } catch {
    // ENOENT — no prior lock.
  }

  // 若锁新鲜（<1h）且 PID 活 → 让路
  if (mtimeMs !== undefined && Date.now() - mtimeMs < HOLDER_STALE_MS) {
    if (holderPid !== undefined && isProcessRunning(holderPid)) {
      logForDebugging(`[autoDream] lock held by live PID ${holderPid} (mtime ${Math.round((Date.now() - mtimeMs) / 1000)}s ago)`)
      return null
    }
    // Dead PID or unparseable body — reclaim.
  }

  // 抢占：覆盖 PID
  await mkdir(getAutoMemPath(), { recursive: true })
  await writeFile(path, String(process.pid))

  // 两个 reclaimer 同时写 → 最后一个赢 PID；输家 re-read 时退出
  let verify: string
  try {
    verify = await readFile(path, 'utf8')
  } catch {
    return null
  }
  if (parseInt(verify.trim(), 10) !== process.pid) return null

  return mtimeMs ?? 0   // 返回 pre-acquire mtime（给 rollback 用）
}
```

**并发安全论证**：

| 场景 | 行为 |
|------|------|
| 无锁文件 | ENOENT → mtime undefined → 直接 write PID |
| 锁新鲜 + PID 活 | 返回 null，让路 |
| 锁新鲜 + PID 死 | 回收（write PID）|
| 锁过期（>1h）| 无条件回收（防 PID 复用）|
| 两 reclaimer 同时 write | 最后 write 的 PID 赢；输家 re-read 时不等于 self PID → return null |
| body 不可解析 | 视为死 PID，回收 |

**无 fcntl / flock 依赖**：纯文件系统原语（mkdir/stat/read/write）—— 跨平台兼容，无 POSIX 假设。

**Rollback 流程**（[consolidationLock.ts:91-108](../src/services/autoDream/consolidationLock.ts#L91-L108)）：

```typescript
export async function rollbackConsolidationLock(priorMtime: number): Promise<void> {
  const path = lockPath()
  try {
    if (priorMtime === 0) {
      await unlink(path)        // 无锁 → 恢复无锁
      return
    }
    await writeFile(path, '')   // 清空 PID（否则 self-process 还在跑,被误判持有）
    const t = priorMtime / 1000  // utimes 要秒
    await utimes(path, t, t)
  } catch (e) {
    logForDebugging(`[autoDream] rollback failed: ${(e as Error).message} — next trigger delayed to minHours`)
  }
}
```

**为什么必须清 PID**：
- fork 失败时 self-process 仍活着
- 如果不清 body 只调 mtime，下个 turn `isProcessRunning(holderPid)=true` 且 mtime 新鲜 → `tryAcquire` 让路
- 结果：一次 rollback 让 autoDream 锁死到 mtime 过期（1h）

所以 rollback 是"清 body + 调 mtime" 原子对，让下次 acquire 顺利接手。

**Rollback 失败的兜底**：`catch` 只 log 不抛——就算 rollback 失败也只是"下次触发延迟到 minHours 之后"，不会数据损坏。扫描节流 + 时间门双重兜底。

**`readLastConsolidatedAt`** vs **`tryAcquireConsolidationLock`**：

```typescript
export async function readLastConsolidatedAt(): Promise<number> {
  try {
    const s = await stat(lockPath())
    return s.mtimeMs
  } catch {
    return 0  // 无文件视为从未 consolidated
  }
}
```

**分离读和写**：时间门只读 mtime（1 stat，最便宜），session 门和 acquire 需要更多操作——按成本升序排列。

**`recordConsolidation`**（手动 `/dream` 专用, `consolidationLock.ts:130-140`）：

```typescript
export async function recordConsolidation(): Promise<void> {
  try {
    await mkdir(getAutoMemPath(), { recursive: true })
    await writeFile(lockPath(), String(process.pid))
  } catch (e) {
    logForDebugging(`[autoDream] recordConsolidation write failed: ${(e as Error).message}`)
  }
}
```

**乐观 stamp**：手动 `/dream` 在**构建 prompt 时**就打时间戳，不等 skill 完成——skill 无完成钩子，best-effort。下次 autoDream 会看到新时间戳自然跳过。

**崩溃恢复**：
- 进程崩溃 → 锁 PID 变死 PID，mtime 卡在崩溃瞬间
- 下个进程 acquire 时判 `isProcessRunning(pid)=false` → 无条件 reclaim
- **无需 crash recovery 代码**——文件系统自然做到

**锁放记忆目录内的好处**（C1 已提）：
- 随 override 环境变量自动跟随
- 写权限由 filesystem.ts 的 memory 目录白名单保证
- 不污染 `~/.claude/` 根

---

#### D2d. 四阶段整合提示词

**入口**（[autoDream.ts:211-233](../src/services/autoDream/autoDream.ts#L211-L233)）：

```typescript
const memoryRoot = getAutoMemPath()
const transcriptDir = getProjectDir(getOriginalCwd())
const extra = `

**Tool constraints for this run:** Bash is restricted to read-only commands (\`ls\`, \`find\`, \`grep\`, \`cat\`, \`stat\`, \`wc\`, \`head\`, \`tail\`, and similar). Anything that writes, redirects to a file, or modifies state will be denied. Plan your exploration with this in mind — no need to probe.

Sessions since last consolidation (${sessionIds.length}):
${sessionIds.map(id => `- ${id}`).join('\n')}`

const prompt = buildConsolidationPrompt(memoryRoot, transcriptDir, extra)

const result = await runForkedAgent({
  promptMessages: [createUserMessage({ content: prompt })],
  cacheSafeParams: createCacheSafeParams(context),
  canUseTool: createAutoMemCanUseTool(memoryRoot),
  querySource: 'auto_dream',
  forkLabel: 'auto_dream',
  skipTranscript: true,
  overrides: { abortController },
  onMessage: makeDreamProgressWatcher(taskId, setAppState),
})
```

**`extra` 放 `buildConsolidationPrompt` 的 "Additional context" 段尾**（不放共享 body 中），因为手动 `/dream` 不需要"tool constraints"那段话——它跑在主循环里有完整权限。

**四阶段结构**（[consolidationPrompt.ts:10-64](../src/services/autoDream/consolidationPrompt.ts#L10-L64)）：

```typescript
export function buildConsolidationPrompt(
  memoryRoot: string,
  transcriptDir: string,
  extra: string,
): string {
  return `# Dream: Memory Consolidation

You are performing a dream — a reflective pass over your memory files.
Synthesize what you've learned recently into durable, well-organized memories
so that future sessions can orient quickly.

Memory directory: \`${memoryRoot}\`
${DIR_EXISTS_GUIDANCE}

Session transcripts: \`${transcriptDir}\` (large JSONL files — grep narrowly, don't read whole files)

---

## Phase 1 — Orient

- \`ls\` the memory directory to see what already exists
- Read \`${ENTRYPOINT_NAME}\` to understand the current index
- Skim existing topic files so you improve them rather than creating duplicates
- If \`logs/\` or \`sessions/\` subdirectories exist (assistant-mode layout), review recent entries there

## Phase 2 — Gather recent signal

Look for new information worth persisting. Sources in rough priority order:

1. **Daily logs** (\`logs/YYYY/MM/YYYY-MM-DD.md\`) if present — append-only stream
2. **Existing memories that drifted** — facts that contradict something you see in the codebase now
3. **Transcript search** — if you need specific context, grep the JSONL transcripts for narrow terms

Don't exhaustively read transcripts. Look only for things you already suspect matter.

## Phase 3 — Consolidate

For each thing worth remembering, write or update a memory file at the top level of the memory directory. Use the memory file format and type conventions from your system prompt's auto-memory section.

Focus on:
- Merging new signal into existing topic files rather than creating near-duplicates
- Converting relative dates ("yesterday", "last week") to absolute dates
- Deleting contradicted facts — if today's investigation disproves an old memory, fix it at the source

## Phase 4 — Prune and index

Update \`${ENTRYPOINT_NAME}\` so it stays under ${MAX_ENTRYPOINT_LINES} lines AND under ~25KB.

- Remove pointers to memories that are now stale, wrong, or superseded
- Demote verbose entries: if an index line is over ~200 chars, shorten it and move detail to topic file
- Add pointers to newly important memories
- Resolve contradictions — if two files disagree, fix the wrong one

---

Return a brief summary of what you consolidated, updated, or pruned.${extra ? \`\n\n## Additional context\n\n${extra}\` : ''}`
}
```

**每阶段设计意图**：

| Phase | 目标 | 关键动作 | 防什么 |
|-------|------|---------|--------|
| **1 Orient** | 建立现状认知 | ls/read index/skim topics | 盲写 → 创建重复文件 |
| **2 Gather** | 收集信号（不是穷举）| daily logs > drifted memories > transcripts grep | 花光 turn 读整个 transcript |
| **3 Consolidate** | 合并、规范化、删错 | 更新已有文件优先于新建；日期绝对化 | 记忆碎片化 + 日期腐烂 |
| **4 Prune & index** | 修剪索引 | 删过时 / 压缩长行 / 解决矛盾 | 索引超限被硬截断 |

**与 extractMemories 提示词的对比**：

| 维度 | extract prompt | dream prompt |
|------|---------------|--------------|
| 视角 | 对话新增信息 → 写 | 历史全量 → 重写 |
| 源头 | 当前会话消息 | 记忆文件 + daily logs + 会话 transcript |
| 允许动作 | 新建 / 小改 | 合并 / 删除 / 重写 / 修剪 |
| 动词 | Save / Extract | Consolidate / Prune |
| 完成标志 | 写了 > 0 个主题文件 | 摘要 summary |

**narrow grep 的反例规定**：

```
Don't exhaustively read transcripts. Look only for things you already suspect matter.
```

Transcript JSONL 可能是 MB 级——穷举读会花光整个 dream 预算且无产出。规定：先从 daily logs / 漂移记忆得到关键词，再 grep transcript 找上下文。

**`logs/YYYY/MM/` 结构的跨模式复用**：这是 KAIROS 模式的日志布局（[memdir.ts:335](../src/memdir/memdir.ts#L335)）。autoDream 通过检测 `logs/` 子目录自适应——KAIROS 模式下 dream 就读日志整理，非 KAIROS 模式下 dream 跳过这段。

**narrow grep 示例**：

```bash
grep -rn "<narrow term>" <transcriptDir>/ --include="*.jsonl" | tail -50
```

`| tail -50` 预防 match 太多——只看最新的 50 条。

---

#### D2e. 进度监控 + UI + 失败处理

**进度 watcher**（[autoDream.ts:281-313](../src/services/autoDream/autoDream.ts#L281-L313)）：

```typescript
function makeDreamProgressWatcher(
  taskId: string,
  setAppState: import('../../Task.js').SetAppState,
): (msg: Message) => void {
  return msg => {
    if (msg.type !== 'assistant') return
    let text = ''
    let toolUseCount = 0
    const touchedPaths: string[] = []
    for (const block of msg.message.content) {
      if (block.type === 'text') {
        text += block.text
      } else if (block.type === 'tool_use') {
        toolUseCount++
        if (block.name === FILE_EDIT_TOOL_NAME || block.name === FILE_WRITE_TOOL_NAME) {
          const input = block.input as { file_path?: unknown }
          if (typeof input.file_path === 'string') {
            touchedPaths.push(input.file_path)
          }
        }
      }
    }
    addDreamTurn(taskId, { text: text.trim(), toolUseCount }, touchedPaths, setAppState)
  }
}
```

**抽取三种信号**：

| 信号 | 用途 |
|------|------|
| `text` | assistant 的推理/摘要 —— 给 UI 展示"它在想什么" |
| `toolUseCount` | 调用计数 —— 展示"做了多少事" |
| `touchedPaths` | Edit/Write 的目标 —— 用于阶段翻转 + 完成通知 |

**注入点**：`runForkedAgent` 的 `onMessage` 回调，每个 assistant turn 结束时触发。

**DreamTask UI 集成**（[tasks/DreamTask/DreamTask.ts](../src/tasks/DreamTask/DreamTask.ts)）：

```
registerDreamTask({ sessionsReviewing, priorMtime, abortController })
  ↓ 返回 taskId
addDreamTurn(taskId, { text, toolUseCount }, touchedPaths, setAppState)
  ↓ 更新 Task 状态
completeDreamTask(taskId, setAppState)     ← 成功
failDreamTask(taskId, setAppState)         ← fork 抛异常
```

**底部状态栏标签**（[tasks/pillLabel.ts:61-62](../src/tasks/pillLabel.ts#L61-L62)）：

```typescript
case 'dream':
  return 'dreaming'
```

用户看到底部有 `dreaming` pill 时就知道后台在做梦。

**详情对话框**（`components/tasks/DreamDetailDialog.tsx`）：
- `Shift+Down` 打开
- 展示：正在回顾的会话数、当前阶段、最近 assistant text、tool_use 数、已 touch 文件路径
- 按 `x` 键终止 → DreamTask.kill → abortController.abort() + rollbackConsolidationLock

**阶段翻转**：UI 根据 `touchedPaths.length` 决定展示：

```
touchedPaths = []      → starting    （还在 Phase 1-2：读取/搜索）
touchedPaths.length > 0 → updating   （进入 Phase 3-4：开始写入）
```

**完成通知**（autoDream.ts:236-248）：

```typescript
completeDreamTask(taskId, setAppState)
const dreamState = context.toolUseContext.getAppState().tasks?.[taskId]
if (
  appendSystemMessage &&
  isDreamTask(dreamState) &&
  dreamState.filesTouched.length > 0
) {
  appendSystemMessage({
    ...createMemorySavedMessage(dreamState.filesTouched),
    verb: 'Improved',   // extractMemories 用 'Saved'
  })
}
```

**"Improved" vs "Saved" 的细节**：动词反映行为本质 —— extract 是写新内容（Saved），dream 是优化已有（Improved）。这是微小的 UX polish 但重要：用户看到消息知道"不是又写了新东西，是整理优化"。

**失败处理**（autoDream.ts:258-271）：

```typescript
} catch (e) {
  // 如果用户从 bg-tasks 对话框 kill: DreamTask.kill 已经 abort + rollback + 设 killed
  // 不覆盖状态，不重复 rollback
  if (abortController.signal.aborted) {
    logForDebugging('[autoDream] aborted by user')
    return
  }
  logForDebugging(`[autoDream] fork failed: ${(e as Error).message}`)
  logEvent('tengu_auto_dream_failed', {})
  failDreamTask(taskId, setAppState)
  // Rewind mtime so time-gate passes again. Scan throttle is the backoff.
  await rollbackConsolidationLock(priorMtime)
}
```

**两种失败路径**：

| 情况 | 处理 |
|------|------|
| 用户 abort | DreamTask.kill 已做所有清理，catch 只 log 不重复 |
| fork 异常（API 超时等） | log + failTask + rollback lock |

**Rollback 后的节奏**：时间门会立刻放行（mtime 已回滚），但扫描节流 `SESSION_SCAN_INTERVAL_MS = 10min` 让下次尝试延迟——避免"失败立刻重试"的死循环。

**遥测事件**（已在 F3 速查表）：

| 事件 | 时机 | 附带数据 |
|------|------|---------|
| `tengu_auto_dream_fired` | 梦境启动 | `hours_since`, `sessions_since` |
| `tengu_auto_dream_completed` | 完成 | `cache_read`, `cache_created`, `output`, `sessions_reviewed` |
| `tengu_auto_dream_failed` | 失败 | — |

`cache_read` / `cache_created` 比例是关键健康指标：autoDream 的 fork 应该与主会话**共享大量缓存**（system prompt、tools、userContext），`cache_read` 占比应 >90%。如果远低于这个值说明 CacheSafeParams 被意外破坏。

---

## E. 辅助管线

### E1. 子代理记忆（三级作用域）

**定位**：通过 `Agent` 工具派生的子代理（explorer、planner 等）有**独立于主记忆**的三级作用域记忆目录。与主记忆的 auto-memory 隔离，避免子代理的局部学习污染全局记忆。

**三级作用域定义**（[agentMemory.ts:13 + 52-65](../src/tools/AgentTool/agentMemory.ts#L13)）：

```typescript
export type AgentMemoryScope = 'user' | 'project' | 'local'

export function getAgentMemoryDir(
  agentType: string,
  scope: AgentMemoryScope,
): string {
  const dirName = sanitizeAgentTypeForPath(agentType)
  switch (scope) {
    case 'project':
      return join(getCwd(), '.claude', 'agent-memory', dirName) + sep
    case 'local':
      return getLocalAgentMemoryDir(dirName)
    case 'user':
      return join(getMemoryBaseDir(), 'agent-memory', dirName) + sep
  }
}
```

**三级作用域表**：

| 作用域 | 路径 | VCS | 跨项目 | 跨机器 | 用途 |
|--------|------|-----|--------|--------|------|
| **user** | `<memoryBase>/agent-memory/<type>/` | — | ✅ | — | 全局用户级学习 |
| **project** | `<cwd>/.claude/agent-memory/<type>/` | 提交 | ❌ | ✅（通过 VCS） | 团队共享的项目知识 |
| **local** | `<cwd>/.claude/agent-memory-local/<type>/` | 不提交 | ❌ | ❌ | 本机私有项目数据 |

**Remote 模式特殊处理 `getLocalAgentMemoryDir`**（`agentMemory.ts:29-44`）：

```typescript
function getLocalAgentMemoryDir(dirName: string): string {
  if (process.env.CLAUDE_CODE_REMOTE_MEMORY_DIR) {
    return (
      join(
        process.env.CLAUDE_CODE_REMOTE_MEMORY_DIR,
        'projects',
        sanitizePath(findCanonicalGitRoot(getProjectRoot()) ?? getProjectRoot()),
        'agent-memory-local',
        dirName,
      ) + sep
    )
  }
  return join(getCwd(), '.claude', 'agent-memory-local', dirName) + sep
}
```

**Remote 模式下 `local` 也走挂载盘**：因为 Remote VM 的 cwd 可能每次会话不同，单靠 cwd 的 local 会丢失；用 git-root 哈希做命名空间，保证 Remote VM 跨会话也能找到之前的 local 记忆。

**agentType sanitize**（`agentMemory.ts:20-22`）：

```typescript
function sanitizeAgentTypeForPath(agentType: string): string {
  return agentType.replace(/:/g, '-')
}
```

**为什么冒号**：插件命名空间代理类型形如 `my-plugin:my-agent`，冒号在 Windows 文件名非法。替换为破折号后 `my-plugin-my-agent`。

**`isAgentMemoryPath` 白名单判定**（`agentMemory.ts:68-104`）：

```typescript
export function isAgentMemoryPath(absolutePath: string): boolean {
  const normalizedPath = normalize(absolutePath)   // 防 .. 穿越
  const memoryBase = getMemoryBaseDir()

  // user scope
  if (normalizedPath.startsWith(join(memoryBase, 'agent-memory') + sep)) return true
  // project scope (永远 cwd-based)
  if (normalizedPath.startsWith(join(getCwd(), '.claude', 'agent-memory') + sep)) return true
  // local scope（根据 Remote 环境变量分支）
  if (process.env.CLAUDE_CODE_REMOTE_MEMORY_DIR) {
    if (normalizedPath.includes(sep + 'agent-memory-local' + sep) &&
        normalizedPath.startsWith(join(process.env.CLAUDE_CODE_REMOTE_MEMORY_DIR, 'projects') + sep)) {
      return true
    }
  } else if (normalizedPath.startsWith(join(getCwd(), '.claude', 'agent-memory-local') + sep)) {
    return true
  }
  return false
}
```

用于 `filesystem.ts` 的写豁免——代理可以写到这三类路径中的任一个，`..` 穿越被 normalize 拦下。

**主记忆 vs 代理记忆的差异表**：

| 维度 | 主记忆 (auto) | 代理记忆 |
|------|---------------|----------|
| 目录 | `~/.claude/projects/<hash>/memory/` | 按作用域三选一 |
| 按代理类型隔离 | 否（统一） | ✅（每种类型独立目录） |
| MEMORY.md 索引 | 两步保存（主题文件 + 索引） | `skipIndex=true` —— 一步保存 |
| 内容源 | `loadMemoryPrompt()` 动态读 | `buildMemoryPrompt()` **构建时读入快照** |
| 提示词缓存 | `systemPromptSection('memory', ...)` 整会话复用 | 每次代理派生时读 |
| 检索 | `findRelevantMemories` + Sonnet 选择 | 无检索，索引全量载入 |

**`buildMemoryPrompt` vs `buildMemoryLines`**（[memdir.ts:272-316 vs 199-266](../src/memdir/memdir.ts#L272-L316)）：

```typescript
// buildMemoryLines: 只产出"指令",MEMORY.md 内容走 attachment
// 主记忆用,内容频繁变,attach 可刷新
export function buildMemoryLines(
  displayName: string,
  memoryDir: string,
  extraGuidelines?: string[],
  skipIndex = false,
): string[] { /* 构造指令行 */ }

// buildMemoryPrompt: 指令 + 同步读 MEMORY.md 拼进去
// 代理记忆用,派生时固定快照
export function buildMemoryPrompt(params: {
  displayName: string
  memoryDir: string
  extraGuidelines?: string[]
}): string {
  const { displayName, memoryDir, extraGuidelines } = params
  const fs = getFsImplementation()
  const entrypoint = memoryDir + ENTRYPOINT_NAME
  let entrypointContent = ''
  try {
    entrypointContent = fs.readFileSync(entrypoint, { encoding: 'utf-8' })
  } catch { /* No memory file yet */ }

  const lines = buildMemoryLines(displayName, memoryDir, extraGuidelines)
  if (entrypointContent.trim()) {
    const t = truncateEntrypointContent(entrypointContent)
    lines.push(`## ${ENTRYPOINT_NAME}`, '', t.content)
  } else {
    lines.push(`## ${ENTRYPOINT_NAME}`, '', `Your ${ENTRYPOINT_NAME} is currently empty.`)
  }
  return lines.join('\n')
}
```

**设计差异的 why**：
- **主记忆**：内容频繁更新（extract 每轮写），用 attachment 动态刷新；system prompt 里只放指令段，保住 cache
- **代理记忆**：代理生命期短（一次 Agent tool 调用），内容量小（数十条），派生时固定快照比维护 attachment 机制简单
- **`skipIndex=true`**：代理记忆不搞索引——作用域小、文件少，两步保存是过度工程

**`loadAgentMemoryPrompt` 三段 scope note**（[agentMemory.ts:138-177](../src/tools/AgentTool/agentMemory.ts#L138-L177)）：

```typescript
export function loadAgentMemoryPrompt(agentType: string, scope: AgentMemoryScope): string {
  let scopeNote: string
  switch (scope) {
    case 'user':
      scopeNote = '- Since this memory is user-scope, keep learnings general since they apply across all projects'
      break
    case 'project':
      scopeNote = '- Since this memory is project-scope and shared with your team via version control, tailor your memories to this project'
      break
    case 'local':
      scopeNote = '- Since this memory is local-scope (not checked into version control), tailor your memories to this project and machine'
      break
  }

  const memoryDir = getAgentMemoryDir(agentType, scope)
  void ensureMemoryDirExists(memoryDir)   // fire-and-forget

  const coworkExtraGuidelines = process.env.CLAUDE_COWORK_MEMORY_EXTRA_GUIDELINES
  return buildMemoryPrompt({
    displayName: 'Persistent Agent Memory',
    memoryDir,
    extraGuidelines:
      coworkExtraGuidelines && coworkExtraGuidelines.trim().length > 0
        ? [scopeNote, coworkExtraGuidelines]
        : [scopeNote],
  })
}
```

**fire-and-forget mkdir 的注释**（`agentMemory.ts:160-165`）：

```typescript
// Fire-and-forget: this runs at agent-spawn time inside a sync
// getSystemPrompt() callback (called from React render in AgentDetail.tsx,
// so it cannot be async). The spawned agent won't try to Write until after
// a full API round-trip, by which time mkdir will have completed. Even if
// it hasn't, FileWriteTool does its own mkdir of the parent directory.
```

约束链：React render 必须同步 → getSystemPrompt 必须同步 → mkdir 只能 void。代理真正写之前有 API round-trip（数百 ms），mkdir（几 ms）早就完了——即使没完 FileWriteTool 自己会再 mkdir。多层保护。

**@-mention 检索协作**（[attachments.ts:2202-2209](../src/utils/attachments.ts#L2202-L2209)）：

```typescript
const memoryDirs = extractAgentMentions(input).flatMap(mention => {
  const agentType = mention.replace('agent-', '')
  const agentDef = agents.find(def => def.agentType === agentType)
  return agentDef?.memory
    ? [getAgentMemoryDir(agentType, agentDef.memory)]
    : []
})
const dirs = memoryDirs.length > 0 ? memoryDirs : [getAutoMemPath()]
```

用户 query 里 `@agent-planner` → `findRelevantMemories` 切换到该代理的 memoryDir（而非主记忆），**隔离检索**。多个 @-mention 可同时搜多个代理的记忆。

**代理记忆快照 `agentMemorySnapshot.ts`**：用于 agent 启动前后 diff —— 方便 UI 展示"本次代理修改了哪些记忆"。`registerAgentMemorySnapshot` / `getAgentMemoryDelta` 捕获 before/after 状态，Agent UI 的"修改"标签从这里读。

---

### E2. 团队记忆同步

**启用条件**：`feature('TEAMMEM')` + `tengu_herring_clock` GB flag + autoMem 启用。

**目录结构**（C1 已列）：

```
~/.claude/projects/<hash>/memory/
├─ MEMORY.md              ← 个人索引
├─ *.md                   ← 个人主题
└─ team/
   ├─ MEMORY.md           ← 团队索引
   └─ *.md                ← 团队主题（HTTP 同步）
```

**HTTP API**（[teamMemorySync/index.ts:8-13](../src/services/teamMemorySync/index.ts#L8-L13) 注释）：

```
GET  /api/claude_code/team_memory?repo={owner/repo}              → TeamMemoryData
GET  /api/claude_code/team_memory?repo={owner/repo}&view=hashes → 仅 checksums
PUT  /api/claude_code/team_memory?repo={owner/repo}              → upsert entries
404 = 无数据
```

**repo 作为 key**：从 git remote 提取 `getGithubRepo()` —— 同一仓库的所有 clone 共享，不同 fork 的 PR 工作不会污染原仓库的团队记忆。

**同步语义**：

| 方向 | 策略 | 语义 |
|------|------|------|
| **Pull** | server-wins per-key | 服务器内容覆盖本地文件 |
| **Push** | delta upload | 只传 hash 不同的 key（checksums 比对） |
| **删除** | **不传播** | 本地删除不会删服务器；下次 pull 会恢复 |

**关键常量**（`index.ts:71-91`）：

```typescript
const TEAM_MEMORY_SYNC_TIMEOUT_MS = 30_000
const MAX_FILE_SIZE_BYTES = 250_000      // 单条记忆上限（服务器默认）
const MAX_PUT_BODY_BYTES = 200_000       // 批量 PUT body 上限
const MAX_RETRIES = 3
const MAX_CONFLICT_RETRIES = 2
```

**为什么 `MAX_PUT_BODY_BYTES = 200KB < MAX_FILE_SIZE_BYTES = 250KB`**（注释详述）：

```
// Gateway body-size cap. The API gateway rejects PUT bodies over ~256-512KB
// with an unstructured (HTML) 413 before the request reaches the app server —
// distinguishable from the app's structured entry-count 413 only by latency
// (~750ms gateway vs ~2.3s app on comparable payloads). #21969 removed the
// client entry-count cap; cold pushes from heavy users then sent 300KB-1.4MB
// bodies and hit this. 200KB leaves headroom under the observed threshold
// and keeps a single-entry-at-MAX_FILE_SIZE_BYTES solo batch (~250KB) just
// under the real gateway limit. Batches larger than this are split into
// sequential PUTs — server upsert-merge semantics make that safe.
```

真实事故驱动：早期无 body cap → 频繁遇到非结构化 413（HTML 错误页）→ 无法区分 gateway 413 和 app 413 → 200KB 是"能放一条 MAX_FILE_SIZE 记录 + 网关余量"的经验值。

**SyncState 对象设计**（`index.ts:92-100`）：

```typescript
export type SyncState = {
  // ETag 跟踪、watcher suppression flag 等
}
// 创建一次,每个 sync 函数都显式传入;tests 每个 test case 创建新 SyncState
```

**为什么用对象而非闭包**（与 `extractMemories` 对比）：

- Team sync 状态是**结构化的**（多个 ETag map、watcher 引用、suppression 标志）——对象字段命名更清晰
- 单元测试希望不同场景独立初始化（pull / push / watcher 各测一份 state）
- 多个方法共享状态，外传对象比共享闭包 setter/getter 更 idiomatic

**关键子模块**：

| 文件 | 职责 |
|------|------|
| `index.ts` | 主 sync 逻辑：pull / push / delta / retry |
| `watcher.ts` | chokidar 监控 `team/` 变更 → push；反向 watcher suppression 防回环 |
| `secretScanner.ts` | 上传前扫密（API key / 证书头等） |
| `teamMemSecretGuard.ts` | scanner 包装 + SkippedSecretFile 生成 |
| `types.ts` | Zod schema：TeamMemoryDataSchema / TeamMemoryTooManyEntriesSchema |

**认证**：OAuth（`getClaudeAIOAuthTokens` + `checkAndRefreshOAuthTokenIfNeeded`），需要 `CLAUDE_AI_INFERENCE_SCOPE` + `CLAUDE_AI_PROFILE_SCOPE`。非交互模式下如果 token 过期会抛 —— watcher 静默抑制（不能阻塞文件保存操作）。

**Delta upload 实现思路**：

```
1. GET ?view=hashes      ← 拿服务器所有 key 的 checksums
2. 本地 readdir team/ 计算每个文件的 sha256
3. diff = 本地 hash !== 服务器 hash 或服务器没有的 key
4. 打包 diff entries → PUT
5. 如果 body > MAX_PUT_BODY_BYTES → 拆分串行 PUT（upsert 幂等）
```

**413 错误分两类处理**：

| 错误类型 | 识别 | 处理 |
|---------|------|------|
| 结构化（entry-count 超限）| Zod 解析 `TeamMemoryTooManyEntriesSchema` 成功，含 `max_entries` | 截断最旧 N 条重试 |
| 非结构化（gateway body 超限）| Zod 解析失败，HTML 响应 | 拆分当前批次，减半重试 |

**不用客户端硬编码 max_entries**（注释）：服务器 per-org GB-tunable，客户端硬编码会 drift。**错误驱动自适应** 比 "猜测服务器配置" 更健壮。

**`secretScanner.ts` 的拦截**：

```
上传前扫每个 entry 的内容：
  - /api[_-]?key\s*[:=]\s*["']?[\w-]{16,}/i
  - /-----BEGIN (RSA |EC )?PRIVATE KEY-----/
  - /aws_secret_access_key\s*=\s*\S+/i
  - 若干其他 pattern
Match 则跳过该 entry + 记入 SkippedSecretFile { filename, matchedRule }
返回 caller,UI 可提示"这几条因含密钥跳过"
```

**为什么不是静默丢弃**：用户需知道为何某条记忆没同步——提示里建议"把密钥换成 reference 记忆指向 password manager"，教育式反馈。

**Watcher 防回环**：

```
chokidar 监控 team/ 目录:
  本地文件变更 → push 到服务器
  但 pull 时也在本地写 team/ → 如果不抑制,会再次触发 push → 死循环
解法: pull 期间设置 suppressUntil = now + ε,watcher 在该时间窗内忽略事件
```

**为什么个人 + 团队同 prompt（`buildCombinedMemoryPrompt`）**：

`memoryTypes.ts` 的 `TYPES_SECTION_COMBINED` 已带 `<scope>` 标签 —— 让**主 agent 在写时**决定放个人还是团队，而不是写完再搬。route 规则（C4 已详述）：user=always private、reference=usually team 等。

**提取路径分发**（`extractMemories.ts:403-413`）：

```typescript
const userPrompt = feature('TEAMMEM') && teamMemoryEnabled
  ? buildExtractCombinedPrompt(newMessageCount, existingMemories, skipIndex)
  : buildExtractAutoOnlyPrompt(newMessageCount, existingMemories, skipIndex)
```

TEAMMEM 启用时调 combined 提示词，把两个目录的 manifest 都喂给提取代理，让它决定新记忆放哪边。

**`tengu_team_memdir_disabled` 降级遥测**（已在 C2 展开）：
Auto memory 关闭时，如果用户曾在 team-memory cohort 中，仍记一次 disabled 事件——保留 A/B 分析的分母。

---

## F. 架构抽象

### F1. 三元结构：数据 / 管线 / 触发

从前面 B/C/D/E 节的具体实现中，可以抽象出一个**三元结构**——cc-haha 记忆系统的本质架构。

**三元结构总览图**：

```
┌──────────────── DATA（数据） ────────────────┐
│  文件系统为唯一事实源（SOT）                      │
│                                              │
│  ├─ 索引文件  MEMORY.md（常驻 context）           │
│  ├─ 主题文件  *.md（frontmatter + body）         │
│  ├─ 锁文件    .consolidate-lock（mtime=时间戳）   │
│  ├─ 摘要文件  session-memory/<id>.md（10-section）│
│  ├─ 日志文件  logs/YYYY/MM/*.md（KAIROS 追加）    │
│  └─ 代理目录  agent-memory/<type>/（三级作用域）   │
└──────────────────────────────────────────────┘
                       ↑↓
┌──────────────── PIPELINES（管线） ────────────┐
│  读取路径：                                      │
│    loadMemoryPrompt           → system prompt   │
│    findRelevantMemories       → attachment      │
│    loadAgentMemoryPrompt       → agent spawn    │
│                                              │
│  写入路径：                                      │
│    extractMemories（每轮末 fork）                │
│    autoDream（每 24h fork）                      │
│    sessionMemory（每 N 轮 fork）                 │
│    主 agent Edit/Write（权限豁免）                 │
│                                              │
│  整合路径：                                      │
│    compactConversation（会话压缩）                │
│    autoDream 4 阶段（去重 + 修剪）                 │
│                                              │
│  同步路径：                                      │
│    teamMemorySync pull/push（HTTP delta）        │
└──────────────────────────────────────────────┘
                       ↑↓
┌─────────────── TRIGGERS（触发） ──────────────┐
│  同步触发（无延迟）：                             │
│    会话启动          → loadMemoryPrompt          │
│    用户提问（每轮）   → getRelevantMemoryAttachments│
│    Agent 工具派生    → loadAgentMemoryPrompt      │
│                                              │
│  异步触发（fire-and-forget）：                   │
│    stopHooks（每轮末）→ extractMemories / autoDream│
│    postSamplingHook → sessionMemory             │
│                                              │
│  手动触发：                                      │
│    /memory   /remember   /dream   /summary   /compact│
│                                              │
│  事件触发：                                      │
│    chokidar watch team/ → teamMemorySync push    │
└──────────────────────────────────────────────┘
```

**为什么"文件系统为 SOT"**：

1. **用户是最终仲裁者** —— 可直接 `cat` / `vim` 编辑，人的意图高于 LLM 的判断
2. **零运维依赖** —— 不引 sqlite/pg/redis，没有服务进程、schema 迁移、备份恢复
3. **Git 天然支持** —— team/project scope 直接走 VCS 审计/回滚/code review
4. **多进程互斥靠 mtime + PID 锁** —— consolidationLock.ts 证明了可行性
5. **崩溃恢复零成本** —— 进程崩了文件还在，下次启动继续

**代价**：
- 全目录扫描受 N 限制（所以 `MAX_MEMORY_FILES = 200`）
- 不能做复杂查询（用 Sonnet 选择器补偿，见 F2-Q2）
- 并发写需要锁文件

**管线围绕 forkedAgent 统一**（B4 铺垫）：

```
所有后台管线的共同底座:
  extractMemories / autoDream / SessionMemory / AgentSummary / compact
      ↓
  统一调用 runForkedAgent({ cacheSafeParams, canUseTool, skipTranscript, maxTurns })
      ↓
  共享主会话 Anthropic prompt cache（key = CacheSafeParams 五元组）
  canUseTool 在权限层隔离（不动 tools 数组,否则破缓存）
  skipTranscript 避免与主线程竞态
  drainPendingExtraction 统一等候 fork 完成
```

**"一个基础抽象支撑多种业务"** —— 任何新的后台任务只需提供 canUseTool + prompt，就能享受缓存共享 + 权限隔离 + 优雅终止。

**触发层的三分结构**：

```
同步注入（模型永远知道）:
  MEMORY.md 索引 → system prompt 常驻

异步 attachment（模型收到最相关的几条）:
  query → findRelevantMemories → Sonnet 选 ≤5 → attach 注入

异步写入（模型不用操心存储）:
  回复完成 → fork 分析 → 自动写 *.md + 更新索引
```

三层分工：**索引同步化、相关性外包给 Sonnet、写入异步化**。

**状态寿命金字塔**：

```
                      ┌──────────────┐
                      │  Prompt       │ 一次 API 调用
                      │  attachment   │
                      └──────────────┘
                  ┌────────────────────┐
                  │  Session memory     │ 一次会话
                  │  (.md 10-section)   │
                  └────────────────────┘
              ┌───────────────────────────┐
              │  Auto-memory (~/.claude/)  │ 跨会话、用户级
              │  Agent memory              │
              └───────────────────────────┘
          ┌──────────────────────────────────┐
          │  Team memory (HTTP sync)           │ 跨成员、组织级
          └──────────────────────────────────┘
```

| 层 | 寿命 | 共享 | 更新成本 | 触发节奏 |
|---|------|------|---------|---------|
| Prompt attachment | 一次 API 调用 | 主 agent 本人 | 近零 | 每轮 |
| Session memory | 一次会话 | 主 agent 本人 | fork + Edit | 每 N 轮 |
| Auto / Agent memory | 永久 | 用户所有会话 | fork + Edit + lock | 每 24h+ / 每次 spawn |
| Team memory | 永久 + 组织 | 所有成员 | HTTP + secret scan | 文件系统事件 |

每上一层：寿命更长、共享更广、但更新成本更高。触发节奏自然匹配——高成本更新频率低。

**读 / 写的非对称**：

```
读取（主 agent 直接做）:
  主 agent 读 attachment + MEMORY.md + 主题文件 → 一次性、同步、零门槛

写入（永远经过代理）:
  主 agent 写        ← 用户说 "记住 X" 时
  extractMemories fork ← 自动捕获（未被主 agent 写时）
  autoDream fork      ← 定期整合
```

**关键约束**：**模型不能在主流程里"顺手"污染记忆**。所有写都是一次独立的深思熟虑——要么主 agent 明确决定写（用户显式请求或自发决策），要么 fork 专门跑一遍分析。这是为"避免记忆腐烂"付出的设计代价。

**降级策略金字塔**（已在 C2 / F3 涉及）：

```
环境变量（CLAUDE_CODE_DISABLE_AUTO_MEMORY 等）  ← 最高优先级,CI/临时
  ↓ 未设置
settings.json autoMemoryEnabled                 ← 用户持久化
  ↓ 未定义
GrowthBook feature flag（tengu_*）              ← 组织级灰度
  ↓ 未命中
feature() 宏（编译期 DCE）                       ← 外部构建不含代码
  ↓ 被 tree-shake
代码不运行
```

每层都是"关"的退路——即使最底层的代码没被 DCE 掉，上面任一层都能禁用功能。**N+1 防御**：用户、管理员、版本、构建任一一方能完全关闭。

---

### F2. 关键设计权衡 — 10 个"为什么不"

每一个选择都伴随一个被否决的替代方案。

---

**Q1：为什么用文件系统而不是数据库？**

**被否决方案**：sqlite / pg / redis 存储结构化记忆。

**理由**：
- 用户可 `cat`/`vim` 直接编辑 → 最终仲裁权在人
- Git diff 天然支持 team/project 作用域——团队审计/code review 零成本
- 零运维依赖——不用装数据库、不用迁移 schema、不用备份
- 多进程互斥用 mtime + PID 锁即可（consolidationLock.ts 证明可行）
- 进程崩溃零恢复成本——文件还在，重启继续

**代价**：全目录扫描受 N 限制（`MAX_MEMORY_FILES = 200`），N=10K 会退化。但记忆本应是"精选的关键信息"而非全量日志——N 小是**功能特性**而非 bug。

---

**Q2：为什么用 Sonnet 做相关性选择，而不是 embedding 向量检索？**

**被否决方案**：对每条记忆计算 embedding，query 时 KNN。

**理由**：
- 记忆文件数量级 ≤200 —— 向量索引的启动/维护成本 > 收益
- Sonnet 懂**语义约束**："正在用 X 工具 → 不选其文档 → 但选其 warnings" —— 向量无法表达
- Sonnet 可直接读 `tool_result` 上下文做二次筛选
- 选择器成本通过 prompt cache 摊薄（`tengu_extract_memories_extraction` 事件里的 cache_read 占比说明 90%+ 命中）
- 无需维护 embedding pipeline、无需处理 model drift、无需索引重建

**代价**：每次检索多一次 API round-trip（所以 `alreadySurfaced` 去重、max 5 槽预算要珍惜）。Sonnet 256 token 响应毫秒级——对用户无感。

---

**Q3：为什么写操作永远经过代理（主 agent 或 forked），而不是直接文件 API / JSON patch？**

**被否决方案**：从对话中提取结构化字段（用户角色、偏好等）→ 直接 Object → JSON.stringify → 写文件。

**理由**：
- "how to save"规则复杂（类型分类、Why 字段、去重、索引维护、相对日期→绝对日期、合并近似主题）—— 只有 LLM 能做"把新信号并入已有主题文件"这种语义决策
- 直接写容易产生近似重复（用户 5 次提"测试策略"会写 5 个独立文件）
- extract/dream/主 agent 的互斥机制（`hasMemoryWritesSince`）就是为此存在 —— 确保"同一时刻只有一个写者"
- 记忆质量 > 写入速度——一次多 API round-trip 换取可读性/可维护性

**代价**：每次写需一次完整 fork（成本通过 cache 共享降低），不能做原子操作（靠锁文件 + 互斥补偿）。

---

**Q4：为什么不给 forked agent 独立的 tools 列表？**

**被否决方案**：给 fork 一个只包含 Read/Grep/Glob/Edit/Write 的精简 tools 数组。

**理由**：
- Anthropic 提示词缓存的 key **包含 tools 数组**—— 改了就 cache miss
- Fork 第一次请求 → 全额 cache creation 费 → 成本飙升 10x+
- 5 分钟 TTL 内的其他 fork 也受牵连（共享的 cache block 被污染）
- 所以权限收紧**必须**在 `canUseTool` 回调层做，不能动 tools 数组

**副作用**：fork 看到完整工具列表但调用某些会被拒。需要提示词 + tool_result 的 message 共同沟通。`NO_TOOLS_PREAMBLE`（compact 的 prompt）是同类问题的解 —— 模型仍偶尔强行调工具，强制前置提醒 + 明确拒绝后果把浪费率从 2.79% 压到 0.01%。

**代价**：提示词工程负担——必须教会模型"这些工具你看得到但不能用"。

---

**Q5：为什么 MEMORY.md 始终载入 system prompt，而不是也走 attachment？**

**被否决方案**：MEMORY.md 也像主题文件一样按需 attach。

**理由**：
- 索引必须**无条件可见**——模型知道"这里有什么"才能决定要不要读主题文件
- 放在 system prompt 享受整会话 prompt cache（user 不分段的那段）
- 如果走 attachment，每轮重新载入 → 每轮 cache miss 索引那部分
- 索引是"全局地图"，不是"相关内容"——不适合用相关性检索

**`tengu_moth_copse` 实验**（memdir.ts:422）：允许走 attachment 的 A/B —— 当前在观测阶段。理论上 attachment 更灵活（索引变更会立即反映），但代价是放弃索引的缓存。

**代价**：占系统提示词 token —— 所以 200 行 + 25KB 双截断；意味着会话内记忆变更不反映到 system prompt 需重启会话（用户可通过 attachment 里的主题文件看到最新）。

---

**Q6：为什么 MEMORY.md 不存 JSON 而是 Markdown？**

**被否决方案**：MEMORY.md 用 JSON 结构化存储（`[{path, title, hook, type}, ...]`）。

**理由**：
- 用户可读可编辑（Q1 同源理由）——JSON 对手工编辑不友好
- 主模型不需要 parse —— 作为上下文自然读（LLM 擅长 Markdown）
- YAML frontmatter 只给 scanner 和 Sonnet 选择器读（`parseFrontmatter`）—— 机器可读 + 人可读
- `[title](file.md) — hook` 语法自带链接 attribute（未来可接 IDE 跳转）

**代价**：机器解析弱一些——`parseFrontmatter` 前 30 行 cap 缓解；主模型直接理解原文，不需要 parse 步骤。

**对比**：`package.json` 必须是 JSON（npm 工具链消费），但 `README.md` 是 Markdown（人消费）。MEMORY.md **更像后者**——主要消费方是 LLM（= 类人阅读者）。

---

**Q7：为什么 extract/dream 分两个后台管线，而不是一个？**

**被否决方案**：统一管线，内部根据时间/消息数条件切换行为。

**理由**：

| 维度 | extract | dream | 融合管线的问题 |
|------|---------|-------|---------------|
| **写时机** | 每轮末新鲜信号 | 跨会话历史 | 要在同一函数切换模式，条件分支爆炸 |
| **目标** | recall（不漏） | precision（去重修剪）| 两种目标用同一 maxTurns 不合理 |
| **成本** | 必须快（每轮跑） | 可以贵（24h 一次）| 合并后"便宜"路径被"昂贵"路径污染 |
| **锁** | pendingContext 合并 | PID + mtime | 合并后锁语义混乱 |
| **UI** | "Saved N memories" | "Improved N memories" dreaming pill | 合并后动词失去区分力 |

**人类类比**：白天随手记 vs 晚上整理笔记本——合并成"连续整理"会丢掉任一目标的优化。

**代价**：两份代码需要维护，共享部分（canUseTool、权限、锁的语义）通过显式共用函数保持一致。

---

**Q8：为什么团队同步不传播删除？**

**被否决方案**：本地删除文件 → 同步时在服务器也删。

**理由**：
- 避免工具/手滑导致的误删传染到整个组织
- 本地误删 + 下次 pull 会恢复——这是**安全默认值而不是 bug**
- 真正的删除走服务端管理界面（不通过客户端）——有审计记录

**现实案例**：用户在 team/ 目录跑 `rm *.md.bak` 不小心多删了几个真实记忆 → 如果传播到服务器，整个团队的记忆丢失。不传播让误删的影响局限在本地，下次 pull 自动恢复。

**代价**：清理旧记忆必须通过服务端——管理员门槛。对"快速迭代团队记忆"的需求不友好，但这正是设计初衷：**团队记忆慢变更、审慎变更**。

---

**Q9：为什么用 mtime 而不是独立的 `lastConsolidatedAt` 文件？**

**被否决方案**：`.consolidate-lock`（body = PID）+ `.last-consolidated`（body = ISO 时间戳）分开两个文件。

**理由**：
- 省一个文件——同一个 `.consolidate-lock` 既是 PID 锁又是时间戳
- `stat` 是最便宜的 syscall——时间门只需一个 stat 即可通过（D2b 的最便宜门）
- `utimes` 让 rollback 成本等于一次 write——不需要维护两个文件的一致性
- 两文件会引入"时间戳写了但 PID 没写" / "PID 写了但时间戳没写" 的部分失败状态

**代价**：文件语义紧耦合（PID + 时间戳在同一文件）——所以函数命名必须清晰区分（`readLastConsolidatedAt` 只读 mtime，`tryAcquireConsolidationLock` 读 body + mtime + 竞争）。

**同类设计**：Unix `mtime` 本身就是这种复用——既是"最后修改时间"又是"缓存有效性标志"（`make` 看 mtime 决定是否重建）。cc-haha 把这个模式扩展到应用层。

---

**Q10：为什么 extractMemories 用闭包而 teamMemorySync 用 SyncState 对象？**

**被否决方案**：两者风格统一（全用闭包 或 全用对象）。

**理由**：

| 因素 | extractMemories 闭包 | teamMemorySync SyncState 对象 |
|------|--------------------|------------------------------|
| 状态性质 | 过程式（游标、布尔、Set） | 结构化（ETag map、watcher 引用、suppression）|
| 调用方式 | 单入口 executeExtractMemories | 多方法（pull/push/watcher/secret scan）|
| 并发访问 | 单入口保证串行 | 多方法需共享引用 |
| 测试 | `beforeEach(() => init())` 拿新闭包 | `new SyncState()` 更 idiomatic |

**选择原则**：
- 状态少且扁平 + 调用入口单一 → 闭包够用
- 状态多且结构化 + 多方法共享 → 对象更清晰

两者**都避免 module-level mutable state** —— 核心共同目标是测试隔离。闭包 vs 对象只是同一目标在不同场景下的形态。

**代价**：代码风格不完全统一——不同场景用最合适的工具而非强求一致。

---

**10 条权衡一览表**：

| # | 被否决方案 | 采用理由 | 主要代价 |
|---|-----------|---------|---------|
| 1 | DB 存储 | SOT + 零运维 + Git | N ≤ 200 |
| 2 | Embedding 检索 | 语义规则 + cache 摊薄 | API round-trip |
| 3 | 直接 JSON 写 | 合并语义 + 避免重复 | 需要 fork |
| 4 | Fork 独立 tools | 保 cache 命中 | 提示词工程负担 |
| 5 | MEMORY.md attach | 索引常驻 + 缓存 | 会话内无刷新 |
| 6 | JSON MEMORY.md | 人机双可读 | parse 稍弱 |
| 7 | 合并 extract/dream | 保留各自优化 | 两份代码 |
| 8 | 传播删除 | 防误删传染 | 清理走后台 |
| 9 | 分离时间戳文件 | 省一文件 + 原子性 | 函数命名要求高 |
| 10 | 统一状态风格 | 适配不同场景 | 风格不一 |

每条权衡的**共同哲学**：**为可靠性和可维护性付短期不便，换长期不出事**。记忆系统是跨会话、长期运行的信息管理——一次设计失误会在几个月后才暴露，保守优先于激进。

---

### F3. 性能与安全护栏总表

这一节把散落在 B/C/D/E 的护栏集中成表，便于速查和 audit。

**性能护栏**：

| 护栏 | 位置 | 作用 |
|------|------|------|
| **提示词缓存共享** | `forkedAgent.ts:57-68`（CacheSafeParams） | fork 近零成本 |
| **`systemPromptSection('memory', ...)`** | `prompts.ts:495` | 启动一次，全会话复用 |
| **Frontmatter 30 行截取** | `memoryScan.ts:22`（FRONTMATTER_MAX_LINES） | 扫描 N 文件只读前 30 行 |
| **单次遍历 read-then-sort** | `memoryScan.ts:48`（readFileInRange 内部 stat） | 免双重 syscall |
| **扫描上限 200 文件** | `memoryScan.ts:21`（MAX_MEMORY_FILES） | 限制全目录遍历成本 |
| **MEMORY.md 双截断** | `memdir.ts:57-103` | 200 行 + 25KB 双 cap |
| **Sonnet 选择器 5 槽** | `findRelevantMemories.ts:21` | 限制主模型 attach 注入量 |
| **alreadySurfaced 去重** | `attachments.ts:2247-2262` | 5 槽预算不浪费 |
| **`tengu_bramble_lintel` 限速** | `extractMemories.ts:374-386` | 每 N 轮才 extract |
| **pre-inject manifest** | `extractMemories.ts:395-400` | 省掉代理一个 `ls` turn |
| **扫描节流 10 分钟** | `autoDream.ts:54-56` | 防时间门过但会话门未过时刷屏 |
| **maxTurns 硬顶** | extract=5 / compact=1 / summary=1 | 防兔子洞 |
| **Micro-Compact 工具白名单** | `microCompact.ts:41-50` | 便宜压缩优先 |
| **Auto-Compact 断路器** | `autoCompact.ts:70`（MAX_CONSECUTIVE=3） | 阻止无效重试浪费 |
| **post-compact 预算** | `compact.ts:122-130` | file=50K/skill=25K |
| **memoize getAutoMemPath** | `paths.ts:223-235` | 省 4× getSettingsForSource |
| **skipTranscript** | 所有 fork | 不写主转录避竞态 |
| **`.unref()` drain 定时器** | `extractMemories.ts:584` | 不阻塞进程退出 |
| **delta upload (team)** | `teamMemorySync/index.ts` | 只传 hash 变的 |
| **批量 PUT 拆分** | `MAX_PUT_BODY_BYTES = 200_000` | 网关 413 规避 |

**安全护栏**：

| 护栏 | 位置 | 防什么 |
|------|------|--------|
| **`validateMemoryPath` 5 类拒绝** | `paths.ts:109-150` | 路径穿越、根目录、UNC、空字节 |
| **projectSettings 排除** | `paths.ts:179-186` | 恶意 repo 写 `~/.ssh` |
| **`isAutoMemPath` normalize 检查** | `paths.ts:274-278` | `../` 穿越 |
| **`createAutoMemCanUseTool` 权限白名单** | `extractMemories.ts:171-222` | fork 写出目录 |
| **`createMemoryFileCanUseTool` 单文件锁定** | `sessionMemory.ts:460-482` | 只允许改一个 session file |
| **Bash isReadOnly 过滤** | `extractMemories.ts:195-204` | ls/grep/cat 白名单 |
| **PID 锁 + mtime** | `consolidationLock.ts` | 多进程并发 dream |
| **PID stale 1h** | `HOLDER_STALE_MS` | PID 复用误判 |
| **lock rollback** | `consolidationLock.ts:91-108` | fork 失败时时间门可再通过 |
| **secret scanner (team)** | `teamMemorySync/secretScanner.ts` | 上传前扫 API key |
| **删除不传播 (team)** | `teamMemorySync/index.ts:14-19` | 误删工具 → 团队知识丢 |
| **OAuth token 刷新** | `checkAndRefreshOAuthTokenIfNeeded` | 过期 token |
| **agentType sanitize** | `agentMemory.ts:20-22` | Windows 非法文件名 |
| **frontmatter parse 降级** | `parseMemoryType` 返回 undefined | legacy 文件兼容 |
| **UUID 游标 fallback** | `countModelVisibleMessagesSince:106-108` | 游标被 compact 移除时不永久禁用 extract |
| **`hasMemoryWritesSince` 互斥** | `extractMemories.ts:121-148` | 主/后台同时写重复 |
| **双截断 + WARNING** | `truncateEntrypointContent` | 模型能看到"被截断"提示 |
| **freshness warning** | `memoryFreshnessText` | 模型对旧记忆需验证 |
| **工具权限 deny 不改 tools 数组** | `canUseTool` 回调 | 保留 prompt cache |

**降级策略金字塔**（F1 已提，这里展开）：

```
┌─ env var (最高) ─────────────────────────┐
│  CLAUDE_CODE_DISABLE_AUTO_MEMORY=1         │  临时/CI 关闭
│  CLAUDE_CODE_SIMPLE=1 (--bare)              │  一次性 script
│  CLAUDE_CODE_REMOTE without _MEMORY_DIR     │  无持久化远程
└───────────────────────────────────────────┘
                 ↓ 未设置
┌─ settings.json (用户持久化) ───────────────┐
│  { "autoMemoryEnabled": false }            │  用户明确关闭
│  { "autoDreamEnabled": false }              │  或只关梦
│  { "autoMemoryDirectory": "~/..." }          │  自定义路径（安全源限定）
└───────────────────────────────────────────┘
                 ↓ 未定义
┌─ GrowthBook remote flag (组织级) ─────────┐
│  tengu_passport_quail    ← extract 开关    │
│  tengu_onyx_plover       ← dream 配置       │
│  tengu_herring_clock     ← team 开关        │
│  tengu_session_memory    ← sm 开关          │
│  tengu_moth_copse        ← skipIndex 实验   │
│  tengu_coral_fern        ← 搜索提示实验      │
└───────────────────────────────────────────┘
                 ↓ 未命中
┌─ feature() 编译期宏 (构建级) ─────────────┐
│  feature('EXTRACT_MEMORIES')               │  外部构建可 DCE
│  feature('TEAMMEM')                        │  团队功能剥离
│  feature('KAIROS')                         │  助手模式剥离
│  feature('CACHED_MICROCOMPACT')            │  缓存 MC 剥离
│  feature('MEMORY_SHAPE_TELEMETRY')         │  遥测剥离
└───────────────────────────────────────────┘
                 ↓ tree-shaken
代码从未运行
```

**N+1 防御**：用户、管理员、版本、构建任一一方能完全关闭。

**关键遥测事件速查**：

| 事件 | 时机 | 关键字段 |
|------|------|---------|
| `tengu_memdir_loaded` | 记忆目录扫描完成 | `memory_type` (auto/team/agent), `total_file_count`, `total_subdir_count` |
| `tengu_memdir_disabled` | 因 env/settings 禁用 | `disabled_by_env_var`, `disabled_by_setting` |
| `tengu_team_memdir_disabled` | team 曾启用后关闭 | — |
| `tengu_extract_memories_extraction` | extract 完成 | `input_tokens`, `cache_read_input_tokens`, `files_written`, `memories_saved`, `duration_ms` |
| `tengu_extract_memories_coalesced` | 合并进行中的 extract | — |
| `tengu_extract_memories_skipped_direct_write` | 主 agent 已写过 | `message_count` |
| `tengu_extract_memories_error` | extract 出错 | `duration_ms` |
| `tengu_extract_memories_gate_disabled` | ant-only, gate 关 | — |
| `tengu_auto_dream_fired` | dream 启动 | `hours_since`, `sessions_since` |
| `tengu_auto_dream_completed` | dream 完成 | `cache_read`, `cache_created`, `output`, `sessions_reviewed` |
| `tengu_auto_dream_failed` | dream 失败 | — |
| `tengu_auto_mem_tool_denied` | canUseTool 拒绝 | `tool_name` |
| `tengu_session_memory_init` | SM 注册 hook | `auto_compact_enabled` |
| `tengu_session_memory_extraction` | SM 提取 | token counts + config |
| `tengu_session_memory_manual_extraction` | `/summary` 触发 | — |
| `tengu_session_memory_gate_disabled` | ant-only | — |
| `tengu_session_memory_file_read` | 读取 SM 文件 | `content_length` |
| `tengu_fork_agent_query` | 任何 fork 完成 | `forkLabel`, cache stats, token usage |
| `tengu_compact_turn` | auto-compact 执行 | before/after tokens, retries |

**遥测驱动的演进**：
- `coalesced` 率高 → 调低 `bramble_lintel` 或缩短 fork
- `skipped_direct_write` 率高 → 主 agent 提示词有效，说明 fork 可以更保守
- cache hit rate <90% → CacheSafeParams 被破坏，检查是否有人改了 fork 参数
- `tool_denied` 聚类 → 某工具被模型频繁误用，需调整提示词
- `dream_failed` 高 → consolidationPrompt 可能给错指令或模型能力不够

---

## G. 参考速查

### G1. 完整数据流时序图 T0–T9

把前面所有节串成一个跨管线故事。每个 T 阶段配"关键设计"一句。

---

**T0 — 会话启动**

```
进程启动
  ├─ getSystemPrompt()
  │   └─ systemPromptSection('memory', () => loadMemoryPrompt())
  │       ├─ isAutoMemoryEnabled?  (env → settings → default)
  │       ├─ feature('KAIROS')?    → buildAssistantDailyLogPrompt
  │       ├─ feature('TEAMMEM')?   → buildCombinedMemoryPrompt
  │       └─ 正常路径              → buildMemoryLines + 读 MEMORY.md
  │           ├─ ensureMemoryDirExists (mkdir -p)
  │           └─ truncateEntrypointContent (200 行 / 25KB 双截断)
  │
  ├─ initExtractMemories()   ← 闭包: inFlightExtractions / lastMemoryMessageUuid / ...
  ├─ initAutoDream()         ← 闭包: lastSessionScanAt
  └─ initSessionMemory()     ← registerPostSamplingHook
```

**关键设计**：system prompt 只构建一次，`systemPromptSection` 整会话缓存——保住 Anthropic prompt cache 前缀。

---

**T1 — 用户提问**

```
用户输入 query
  │
  └─ attachments.ts: computeAttachments
      └─ getRelevantMemoryAttachments(input, agents, readFileState, recentTools, ...)
          ├─ extractAgentMentions(input) → 有 @agent- 则切 agentMemoryDir
          ├─ collectSurfacedMemories(messages) → alreadySurfaced Set
          ├─ findRelevantMemories(query, dir, signal, recentTools, alreadySurfaced)
          │   ├─ scanMemoryFiles
          │   │   ├─ readdir(recursive: true) → filter *.md
          │   │   ├─ Promise.allSettled: readFileInRange(0, 30) 并行读前 30 行
          │   │   ├─ parseFrontmatter → description + type
          │   │   └─ sort by mtime desc, slice(0, 200)
          │   ├─ filter alreadySurfaced
          │   ├─ formatMemoryManifest → `- [type] filename (iso-ts): description`
          │   └─ selectRelevantMemories (Sonnet sideQuery)
          │       ├─ JSON schema: { selected_memories: string[] }
          │       ├─ max_tokens: 256
          │       └─ 二次校验: filter(f => validFilenames.has(f))
          ├─ readMemoriesForSurfacing → 行+字节双截断 + memoryFreshnessText
          └─ return attachment { type: 'relevant_memories', memories: [...] }
  │
  └─ 组装 prompt (system + userContext + attachment + messages) 发 Claude API
```

**关键设计**：Sonnet 256-token 选择器 + alreadySurfaced 去重 + 行+字节双截断——3 层防御让 attachment 注入始终有界。

---

**T2 — Claude 推理循环**

```
  Claude API 响应
    │
    ├─ tool_use block 0..N
    │   ├─ canUseTool 检查（主 agent 权限宽松）
    │   ├─ 工具执行 → tool_result
    │   └─ Claude 继续推理（可能再调工具）
    │
    └─ 最终 text block 无 tool_use → stop
```

**关键设计**：主 agent 推理过程**可能直接写记忆**（用户显式要求或模型判断有必要）——这是 `hasMemoryWritesSince` 存在的原因。

---

**T3 — stopHooks 分发（每轮末）**

```
stopHook 触发
  ├─ saveCacheSafeParams(params)          ← 供所有 fork 复用主会话缓存
  │
  ├─ executePromptSuggestion()             ← 独立后台任务
  │
  ├─ executeExtractMemories()              ← 长期记忆写入
  │   ├─ 守卫: agentId/gate/autoMem/remote/inProgress
  │   ├─ hasMemoryWritesSince? → 跳过 + 推游标 + 记 skipped_direct_write
  │   ├─ 频率: turnsSinceLastExtraction < tengu_bramble_lintel?
  │   ├─ scanMemoryFiles + formatMemoryManifest (pre-inject)
  │   ├─ runForkedAgent:
  │   │   ├─ cacheSafeParams (共享主缓存)
  │   │   ├─ canUseTool: createAutoMemCanUseTool (只读+记忆目录写)
  │   │   ├─ skipTranscript: true
  │   │   ├─ maxTurns: 5
  │   │   └─ fork 完成: writtenPaths + cache stats
  │   ├─ 推进 lastMemoryMessageUuid
  │   ├─ pendingContext → trailing run (递归 await)
  │   └─ appendSystemMessage "Memory updated in ..."
  │
  └─ executeAutoDream()                    ← 跨会话整合（见 T6）
```

**关键设计**：三个 void 并发启动、独立 fork、统一 cache 基座——"基础设施统一 + 业务隔离"典型应用。

---

**T4 — SessionMemory postSamplingHook（独立轨道）**

```
每次 API 响应后（postSamplingHook）
  │
  └─ extractSessionMemory (sequential wrapper)
      ├─ 只在 repl_main_thread 运行（跳过 subagents/teammates）
      ├─ tengu_session_memory gate
      ├─ initSessionMemoryConfigIfNeeded (memoized)
      ├─ shouldExtractMemory(messages)
      │   ├─ 初始化阈值: tokenCount >= minimumMessageTokensToInit
      │   ├─ token 增量阈值: 自上次提取以来 >= minimumTokensBetweenUpdate
      │   ├─ tool_call 阈值: 自上次以来 >= toolCallsBetweenUpdates
      │   └─ 触发: (tokenThreshold && toolThreshold) || (tokenThreshold && !hasToolCallsInLastTurn)
      │
      └─ setupSessionMemoryFile
          ├─ mkdir(~/.claude/session-memory/, 0o700)
          ├─ writeFile(memoryPath, '', { flag: 'wx', mode: 0o600 })
          └─ 首次: 写 10-section 模板
      │
      └─ runForkedAgent
          ├─ cacheSafeParams: createCacheSafeParams(context)
          ├─ canUseTool: createMemoryFileCanUseTool(memoryPath)
          │   └─ 只允许 Edit 这一个文件
          ├─ maxTurns: 1
          └─ prompt: buildSessionMemoryUpdatePrompt
      │
      └─ setLastSummarizedMessageId(uuid)  ← 供 Auto-Compact 用
```

**关键设计**：SessionMemory 的 canUseTool 是整个系统里最严的——精确到"只允许 Edit 这个 path"，连 Read/Grep 都不给。因为任务极简单：看已有消息 → 更新一个文件。

---

**T5 — Auto-Compact 判定（token 逼近窗口）**

```
query 构建 API 请求前
  │
  └─ 计算 tokenCountWithEstimation(messages)
  │
  └─ 若 >= getAutoCompactThreshold(model)（= window - 20K output - 13K buffer）:
      │
      ├─ 断路器: consecutiveFailures >= 3 → 放弃，提示用户 /compact
      │
      ├─ trySessionMemoryCompaction
      │   ├─ 读取 ~/.claude/session-memory/<sessionId>.md
      │   ├─ 用作 primer 构建精简 context
      │   └─ 成功 → 跳过下面的完整压缩
      │
      ├─ 否则 compactConversation
      │   ├─ stripImages (图片 → [image] 标记)
      │   ├─ stripReinjectedAttachments (skill_discovery 等)
      │   ├─ runForkedAgent (maxTurns=1)
      │   │   └─ prompt: NO_TOOLS_PREAMBLE + DETAILED_ANALYSIS + <summary>
      │   ├─ 若 prompt_too_long:
      │   │   └─ truncateHeadForPTLRetry (最多 3 次，按 API round 分组丢最早)
      │   └─ formatCompactSummary → 剥离 <analysis>，保留 <summary>
      │
      ├─ notifyCompaction (打破 Anthropic prompt cache)
      └─ postCompactCleanup
          ├─ 恢复最多 5 个 fileRead (~5K tok/each, 总 50K)
          ├─ 恢复最多 ~5 个 skills (~5K tok/each, 总 25K)
          └─ 构建 PostCompactBoundaryMessage
```

**关键设计**：Auto-Compact 是**最后兜底**——前面有 Micro-Compact（清工具结果）、SessionMemory（保留摘要）、用户手动 `/compact` 三层优先。真到 Auto 触发说明前面都没兜住。

---

**T6 — 手动 `/dream`**

```
用户输入 /dream
  │
  └─ dream skill 触发
      ├─ recordConsolidation()       ← 乐观 stamp: mkdir + writeFile(PID)
      │                                 （skill 无完成钩子,best-effort）
      │
      └─ 主 agent 跑 consolidationPrompt 的 4 阶段
          ├─ Phase 1 Orient: ls + read MEMORY.md + skim
          ├─ Phase 2 Gather: daily logs / drifted / transcript grep
          ├─ Phase 3 Consolidate: 合并 + 日期绝对化 + 删矛盾
          └─ Phase 4 Prune: 索引修剪 + 压缩 + 解决矛盾
          ├─ 注意: 主 agent 有完整权限（不像 autoDream 的 fork 受限）
          └─ 完成: 记忆文件变更 → 下次会话启动后 MEMORY.md 刷新
```

**关键设计**：手动 `/dream` **用主 agent 而非 fork**——用户显式触发时希望看到详细输出和进度，fork 的 skipTranscript 会隐藏这些。代价：用户当前 turn 被占用做整合。

---

**T7 — 子代理派生（Agent 工具调用）**

```
主 agent 调 Agent 工具: { agentType: 'planner', description: '...' }
  │
  └─ AgentTool.call
      ├─ loadAgentsDir → 找到 planner 的 AgentDefinition
      ├─ agentDef.memory ? → 有内存定义（user/project/local 之一）
      │
      └─ loadAgentMemoryPrompt(agentType, scope)
          ├─ getAgentMemoryDir(agentType, scope) → 三级作用域之一
          ├─ void ensureMemoryDirExists(dir)   ← fire-and-forget（sync 上下文）
          ├─ 确定 scopeNote (user/project/local 各有指引)
          └─ buildMemoryPrompt
              ├─ readFileSync(entrypoint)  ← 派生时读取快照
              └─ truncateEntrypointContent + 组装 lines
      │
      └─ 派生子代理（独立 memoryDir，与主记忆隔离）
          ├─ 子代理执行任务
          ├─ 子代理通过 Edit/Write 更新自己的 MEMORY.md + 主题文件
          │   └─ filesystem.ts 的 isAgentMemoryPath 写豁免放行
          └─ 返回结果给主 agent
      │
      └─ agentMemorySnapshot (before/after diff)
          └─ UI 展示: "本次代理修改了 N 个记忆文件"
```

**关键设计**：子代理记忆**作用域严格隔离**——explorer 的记忆不会污染 planner 的记忆（按 agentType 分目录）；且根据 scope 自动选择 VCS 提交 / 本机 / 全局——代理自己不操心。

---

**T8 — 团队同步（chokidar watcher + 周期 pull/push）**

```
初始化:
  ├─ feature('TEAMMEM') + tengu_herring_clock + autoMem 启用?
  ├─ SyncState 对象创建
  └─ chokidar.watch(teamDir, { ignored: ['.git', ...] })

本地文件变更（用户手动编辑或 extractMemories 写了 team/*.md）:
  │
  └─ chokidar 触发 change event
      ├─ 检查 suppressUntil（pull 期间的回环抑制）
      ├─ debounce（避免短时间多次事件）
      ├─ scanForSecrets → 发现 API key/证书 → 跳过 + 记 SkippedSecretFile
      └─ push
          ├─ GET ?view=hashes → 服务器 checksums
          ├─ diff = 本地 sha256 != 服务器 sha256 或新 key
          ├─ 打包 entries → PUT
          ├─ body > MAX_PUT_BODY_BYTES? → 拆分串行 PUT
          ├─ 413 结构化（entry 超限）→ 截断最旧重试
          └─ 413 非结构化（gateway）→ 减半重试
      │
      └─ 成功 → 更新 SyncState 的 ETag

周期 pull（或启动时）:
  │
  └─ GET /api/claude_code/team_memory?repo=...
      ├─ 304 ETag 未变 → 跳过
      ├─ 200 → 解析 TeamMemoryDataSchema
      ├─ 对每个 entry:
      │   ├─ 本地 hash == 服务器 hash? → 跳过
      │   └─ 否则 overwrite 本地文件
      └─ 注意: 服务器没有的本地 key **不删** (删除不传播)

OAuth token 管理:
  └─ checkAndRefreshOAuthTokenIfNeeded before each call
      └─ 401? → refresh；仍失败 → 静默抑制（非阻塞）
```

**关键设计**：push 基于 delta（checksum diff）+ 413 自适应（不硬编码 max_entries）+ 不传播删除——三层防御避免"一次误操作毁整个团队记忆"。

---

**T9 — 进程退出**

```
SIGINT / SIGTERM / 正常退出 / -p 模式 response flush 后
  │
  ├─ print.ts:
  │   ├─ await drainPendingExtraction(60_000)
  │   │   ├─ inFlightExtractions.size === 0? → 立即返回
  │   │   ├─ Promise.race:
  │   │   │   ├─ Promise.all(inFlight).catch(() => {})
  │   │   │   └─ setTimeout(60_000).unref()   ← .unref() 让此 timer 不阻塞 exit
  │   │   └─ 先到者赢
  │   │
  │   └─ drain 完成后继续
  │
  ├─ gracefulShutdownSync (5s failsafe)
  │   ├─ 关闭 watchers、MCP clients
  │   ├─ flush 遥测
  │   ├─ 未完成 fork 被强杀
  │   └─ 进程退出
  │
  └─ autoDream 若还在跑:
      ├─ abortController.abort()
      ├─ consolidationLock rollback (或保持锁 PID=我，1h 后过期被回收)
      └─ DreamTask 状态设 killed
```

**关键设计**：`drain(60s)` + `gracefulShutdown(5s failsafe)` = 两层超时——保证 `-p "记住 X"` 一次性调用能写完记忆，且永远能退出。`.unref()` 是关键细节：没有它，60s 定时器会让所有 fork 完成后进程还要等 60s 才退出。

---

### G2. 源码速查表

#### A. 文件职责表

| 文件 | 职责 |
|------|------|
| `memdir/paths.ts` | 路径解析、启用判定、validate、memoize |
| `memdir/memdir.ts` | 系统提示构建、双截断、loadMemoryPrompt 分派 |
| `memdir/memoryTypes.ts` | 四类定义、Combined/Individual 两套提示词 |
| `memdir/memoryScan.ts` | scanMemoryFiles、formatMemoryManifest |
| `memdir/findRelevantMemories.ts` | Sonnet 选择器、sideQuery 调用 |
| `memdir/memoryAge.ts` | memoryAgeDays / memoryFreshnessText |
| `memdir/teamMemPaths.ts` / `teamMemPrompts.ts` | 团队目录 + 组合提示词 |
| `memdir/memoryShapeTelemetry.ts` | 检索形状遥测 |
| `services/extractMemories/extractMemories.ts` | 每轮末提取主逻辑、drain、权限函数 |
| `services/extractMemories/prompts.ts` | buildExtractAutoOnlyPrompt / buildExtractCombinedPrompt |
| `services/autoDream/autoDream.ts` | 门控链、fork、progress watcher |
| `services/autoDream/consolidationPrompt.ts` | 四阶段梦境提示词 |
| `services/autoDream/consolidationLock.ts` | PID 锁、mtime 时间戳、rollback |
| `services/autoDream/config.ts` | isAutoDreamEnabled |
| `services/SessionMemory/sessionMemory.ts` | 会话摘要、双阈值、单文件权限 |
| `services/SessionMemory/prompts.ts` | 10-section 模板、更新指令 |
| `services/SessionMemory/sessionMemoryUtils.ts` | Config、阈值 helpers |
| `services/compact/autoCompact.ts` | Auto-Compact 阈值、断路器 |
| `services/compact/compact.ts` | 压缩主逻辑、PTL 降级 |
| `services/compact/microCompact.ts` | 工具结果级压缩 |
| `services/compact/cachedMicrocompact.ts` | cache edits 机制 |
| `services/compact/prompt.ts` | NO_TOOLS_PREAMBLE、`<analysis>/<summary>` |
| `services/compact/postCompactCleanup.ts` | 恢复 file read / skill |
| `services/compact/sessionMemoryCompact.ts` | 用 SessionMemory 作 primer |
| `services/compact/timeBasedMCConfig.ts` | Micro-Compact 时间阈值 |
| `services/teamMemorySync/index.ts` | HTTP sync 主逻辑、SyncState |
| `services/teamMemorySync/watcher.ts` | chokidar 监控、debounce |
| `services/teamMemorySync/secretScanner.ts` | 密钥扫描 |
| `services/teamMemorySync/teamMemSecretGuard.ts` | scanner 包装 |
| `services/teamMemorySync/types.ts` | Zod schema |
| `tools/AgentTool/agentMemory.ts` | 三级作用域路径 + loadAgentMemoryPrompt |
| `tools/AgentTool/agentMemorySnapshot.ts` | 代理前后 diff |
| `utils/forkedAgent.ts` | runForkedAgent / CacheSafeParams / drain |
| `utils/attachments.ts` | getRelevantMemoryAttachments / readMemoriesForSurfacing / collectSurfacedMemories |
| `query/stopHooks.ts` | 每轮末分发 extract/dream/suggestion |
| `constants/prompts.ts` | systemPromptSection('memory', ...) 注入点 |
| `utils/memoryFileDetection.ts` | 路径归属判定 |
| `utils/permissions/filesystem.ts` | memory 目录写豁免 |
| `utils/backgroundHousekeeping.ts` | initExtractMemories/initAutoDream/initSessionMemory 统一启动 |

#### B. 关键函数表

| 函数 | 文件 | 用途 |
|------|------|------|
| `loadMemoryPrompt` | memdir.ts:419 | 系统提示入口 |
| `buildMemoryLines` / `buildMemoryPrompt` | memdir.ts:199 / :272 | 主/代理两种构建 |
| `truncateEntrypointContent` | memdir.ts:57 | MEMORY.md 双截断 |
| `ensureMemoryDirExists` | memdir.ts:129 | 幂等 mkdir |
| `getAutoMemPath` | paths.ts:223 | 记忆目录路径（memoize） |
| `isAutoMemoryEnabled` / `isExtractModeActive` | paths.ts:30 / :69 | 两层启用判定 |
| `validateMemoryPath` | paths.ts:109 | 路径安全 |
| `isAutoMemPath` | paths.ts:274 | 写豁免判定 |
| `scanMemoryFiles` / `formatMemoryManifest` | memoryScan.ts:35 / :84 | 扫描与格式化 |
| `parseMemoryType` | memoryTypes.ts:28 | type 字段降级 |
| `findRelevantMemories` / `selectRelevantMemories` | findRelevantMemories.ts:39 / :77 | 检索 |
| `memoryFreshnessText` | memoryAge.ts | 新鲜度警告 |
| `getRelevantMemoryAttachments` / `collectSurfacedMemories` | attachments.ts:2192 / :2247 | attachment 生成/去重 |
| `readMemoriesForSurfacing` | attachments.ts:2275 | 内容读取 + 双截断 |
| `executeExtractMemories` / `drainPendingExtraction` | extractMemories.ts:598 / :611 | 入口 / drain |
| `initExtractMemories` | extractMemories.ts:296 | 闭包初始化 |
| `hasMemoryWritesSince` | extractMemories.ts:121 | 主/后台互斥 |
| `createAutoMemCanUseTool` | extractMemories.ts:171 | 共用权限函数（extract + dream） |
| `executeAutoDream` / `initAutoDream` | autoDream.ts:319 / :122 | AutoDream 入口 |
| `buildConsolidationPrompt` | consolidationPrompt.ts:10 | 四阶段提示 |
| `tryAcquireConsolidationLock` / `rollbackConsolidationLock` | consolidationLock.ts:46 / :91 | 锁管理 |
| `readLastConsolidatedAt` / `recordConsolidation` | consolidationLock.ts:29 / :130 | 时间戳读写 |
| `listSessionsTouchedSince` | consolidationLock.ts:118 | 会话门扫描 |
| `initSessionMemory` / `shouldExtractMemory` | sessionMemory.ts:357 / :134 | Session 管线 |
| `manuallyExtractSessionMemory` | sessionMemory.ts:387 | `/summary` 入口 |
| `createMemoryFileCanUseTool` | sessionMemory.ts:460 | 单文件权限 |
| `runForkedAgent` / `saveCacheSafeParams` | forkedAgent.ts | fork 核心 |
| `getAutoCompactThreshold` / `compactConversation` | autoCompact.ts / compact.ts | 压缩入口 |
| `truncateHeadForPTLRetry` | compact.ts:243 | PTL 降级 |
| `stripImagesFromMessages` | compact.ts:145 | 压缩前图片清理 |
| `getAgentMemoryDir` / `loadAgentMemoryPrompt` | agentMemory.ts:52 / :138 | 代理记忆 |
| `isAgentMemoryPath` | agentMemory.ts:68 | 写豁免判定（agent） |

#### C. 关键常量表

```typescript
// ===== memdir =====
ENTRYPOINT_NAME = 'MEMORY.md'
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000
MAX_MEMORY_FILES = 200
FRONTMATTER_MAX_LINES = 30
AUTO_MEM_DIRNAME = 'memory'
AUTO_MEM_ENTRYPOINT_NAME = 'MEMORY.md'

// ===== extractMemories =====
maxTurns = 5                       // fork 硬顶
drain default timeout = 60_000 ms

// ===== autoDream =====
DEFAULTS = { minHours: 24, minSessions: 5 }
SESSION_SCAN_INTERVAL_MS = 10 * 60 * 1000   // 扫描节流
HOLDER_STALE_MS = 60 * 60 * 1000            // PID 过期
LOCK_FILE = '.consolidate-lock'

// ===== compact =====
AUTOCOMPACT_BUFFER_TOKENS = 13_000
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS = 20_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
MAX_COMPACT_STREAMING_RETRIES = 2
MAX_PTL_RETRIES = 3
POST_COMPACT_TOKEN_BUDGET = 50_000
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000

// ===== microCompact =====
TIME_BASED_MC_CLEARED_MESSAGE = '[Old tool result content cleared]'
IMAGE_MAX_TOKEN_SIZE = 2000

// ===== teamMemorySync =====
TEAM_MEMORY_SYNC_TIMEOUT_MS = 30_000
MAX_FILE_SIZE_BYTES = 250_000
MAX_PUT_BODY_BYTES = 200_000
MAX_RETRIES = 3
MAX_CONFLICT_RETRIES = 2

// ===== SessionMemory =====
MAX_SECTION_LENGTH = 2000
MAX_TOTAL_SESSION_MEMORY_TOKENS = 12000

// ===== AgentSummary =====
SUMMARY_INTERVAL_MS = 30_000
```

#### D. 环境变量表

| 变量 | 作用 |
|------|------|
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `1`/`true` 关闭自动记忆；`0`/`false` 强制开（覆盖 settings）|
| `CLAUDE_CODE_SIMPLE` / `--bare` | 精简模式，关所有后台 |
| `CLAUDE_CODE_REMOTE` | 远程模式标记 |
| `CLAUDE_CODE_REMOTE_MEMORY_DIR` | 远程模式的记忆挂载目录 |
| `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE` | Cowork 完整路径覆盖（不展开 `~`） |
| `CLAUDE_COWORK_MEMORY_EXTRA_GUIDELINES` | Cowork 注入额外提示 |
| `CLAUDE_CONFIG_DIR` | 自定义 `~/.claude/` 根目录 |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | 自定义压缩窗口（token 数） |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | 测试用阈值百分比 |
| `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION` | `0`/`false` 关提示词建议 |
| `CLAUDE_JOB_DIR` | 模板作业模式目录 |

#### E. GrowthBook Feature Flag 表

| Flag | 类型 | 控制 |
|------|------|------|
| `tengu_passport_quail` | bool | extractMemories 主开关 |
| `tengu_slate_thimble` | bool | 非交互也跑 extract |
| `tengu_bramble_lintel` | number | extract 频率（每 N 轮） |
| `tengu_onyx_plover` | `{enabled, minHours, minSessions}` | AutoDream 开关 + 调度 |
| `tengu_moth_copse` | bool | skipIndex 模式实验 |
| `tengu_coral_fern` | bool | "Searching past context" 提示 |
| `tengu_herring_clock` | bool | 团队记忆 |
| `tengu_session_memory` | bool | SessionMemory 开关 |
| `tengu_sm_config` | `{minimumMessageTokensToInit, ...}` | SessionMemory 动态 config |
| `MEMORY_SHAPE_TELEMETRY` | feature() | 检索形状遥测 |
| `EXTRACT_MEMORIES` | feature() | 提取管线编译期开关 |
| `TEAMMEM` | feature() | 团队记忆编译期开关 |
| `KAIROS` | feature() | 助手日志追加模式 |
| `CACHED_MICROCOMPACT` | feature() | 缓存版 Micro-Compact |

#### F. 遥测事件表

| 事件 | 关键字段 |
|------|---------|
| `tengu_memdir_loaded` | `memory_type`, `total_file_count`, `total_subdir_count`, `content_length`, `line_count`, `was_truncated`, `was_byte_truncated` |
| `tengu_memdir_disabled` | `disabled_by_env_var`, `disabled_by_setting` |
| `tengu_team_memdir_disabled` | — |
| `tengu_extract_memories_extraction` | 完整 token usage + `files_written` + `memories_saved` + `team_memories_saved` + `turn_count` + `duration_ms` |
| `tengu_extract_memories_coalesced` | — |
| `tengu_extract_memories_skipped_direct_write` | `message_count` |
| `tengu_extract_memories_error` | `duration_ms` |
| `tengu_extract_memories_gate_disabled` | — |
| `tengu_auto_dream_fired` | `hours_since`, `sessions_since` |
| `tengu_auto_dream_completed` | `cache_read`, `cache_created`, `output`, `sessions_reviewed` |
| `tengu_auto_dream_failed` | — |
| `tengu_auto_mem_tool_denied` | `tool_name`（sanitized） |
| `tengu_session_memory_init` | `auto_compact_enabled` |
| `tengu_session_memory_extraction` | token usage + config |
| `tengu_session_memory_manual_extraction` | — |
| `tengu_session_memory_gate_disabled` | — |
| `tengu_session_memory_file_read` | `content_length` |
| `tengu_fork_agent_query` | `forkLabel`, cache stats, token usage |
| `tengu_compact_turn` | before/after tokens, retries |
| `tengu_autocompact_*` | auto-compact 子事件族 |

#### G. 斜杠命令表

| 命令 | 位置 | 作用 |
|------|------|------|
| `/memory` | `commands/memory/memory.tsx` | 打开编辑器改记忆文件（文件选择器 UI） |
| `/remember` | `commands/memory/` | 审查自动记忆，提议提升到 CLAUDE.md |
| `/dream` | skill / dream skill | 手动触发 AutoDream（主 agent 跑 4 阶段） |
| `/summary` | `commands/memory/summary/` | 手动触发 SessionMemory 提取 |
| `/compact` | `commands/compact/` | 手动触发 Auto-Compact |
| `/context` | `commands/context/` | 查看当前 context 构成（含记忆占比） |

---

## 结语

cc-haha 的记忆模块呈现了一个**看似朴素但深思熟虑**的设计：

- **朴素**：Markdown 文件 + YAML frontmatter + `~/.claude/` 目录——没有数据库、没有向量索引、没有复杂协议
- **深思**：每一个决策背后都有具体的 "为什么不"（见 F2 的 10 条权衡）—— 文件系统的 SOT 哲学、forkedAgent 的缓存共享、Sonnet 选择器的语义理解、双管线 extract/dream 的职责分离

核心抽象可以归纳为一句话：

> **以文件系统为事实源，以 forked agent 为执行引擎，以分类法为写入约束，以 Sonnet 为检索智能，以锁和游标为并发护栏。**

这套架构的可扩展性在 agent memory 的三级作用域、team memory 的 HTTP 同步、KAIROS 的日志追加模式中都得到了验证——**核心抽象未变，新功能只是参数化**。

对照 [claude.md](../claude.md) 的项目总览可见，cc-haha 是对 Claude Code 泄露源码的**本地可运行版**，所有原始源码版权归 Anthropic。本文档面向希望深入理解记忆系统设计哲学的开发者，代码引用以 `file:line` 形式给出，可直接跳转验证。

> **免责声明**：本仓库基于 2026-03-31 从 Anthropic npm registry 泄露的 Claude Code 源码修复而成，仅供学习研究。所有原始源码版权归 [Anthropic](https://www.anthropic.com) 所有。










