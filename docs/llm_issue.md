# ReportAgent LLM 性能分析

## 结论（先说结果）

ReportAgent 慢的根因按影响从大到小排序：

1. **实际跑的是主模型 qwen3-max，不是"fast 小模型"** —— `config.yaml` 的 `llm_fast` 整块注释掉，`Settings` 镜像策略会把 `llm` 的字段全量继承过来，所以 ReportAgent/L3 名义上用 fast 通道，实际跑的和主 Agent 同一个大模型（[config.yaml:29-40](../config/config.yaml#L29-L40)）。这一条不修，其它优化收益都被吃掉。
2. **同步等待整段报告、无流式** —— [report_agent.py:160](../modules/p2p/report_agent.py#L160) 直接 `await llm.ainvoke(prompt)`，用户必须等 `max_tokens=4096` 的完整生成。Qwen 这个量级 TPS 典型 30–60 tok/s，尾延迟 60–120s 很正常。整条请求的 TTFB 和总时间都压在这里。
3. **输入 prompt 偏大且每次全量重发** —— outputs 单项截断 3000 字符、典型 4–6 个节点 → 输入 1.2w–1.8w 字符 + 固定指令约 500 字（[report_agent.py:35-63](../modules/p2p/report_agent.py#L35-L63)）。Qwen 兼容接口对输入是按 token 计时/计费的，大输入直接推高 TTFT。而指令模板和严重等级阈值是**完全静态**的，每次都重算 prompt、重传。
4. **无任何缓存** —— 同一会话里相同 outputs 再次生成会整轮重跑。
5. **输出 max_tokens=4096 偏大** —— 镜像自主模型，报告实际很少需要 4k token；上限越大，模型越容易拖尾生成。
6. **重试串行叠加** —— `stop_after_attempt(3) + wait_exponential(1,4)` + 单次 `timeout=240s`，最坏 3×240s+退避 ≈ 12 分钟（[report_agent.py:147-149](../modules/p2p/report_agent.py#L147-L149)）。瞬时错误分类正确，但单次超时设得过松。
7. **disable_thinking 只对 qwen3\* 生效，qwen3-max 在 model_factory 里走的是"qwen 系 + qwen3 前缀"护栏分支，会注入 `enable_thinking=False`，这条目前是有效的**（不是问题，是已正确做了；但如果有人改 provider 到 qwen-max/qwen-plus 就失效，需要留意）。

## 分析过程

**Prompt 结构（report_agent.py:98-134）**：scenario + 多节点 JSON（每项 ≤3000 chars） + 硬编码 700 字指令 + 可选 output_mode_prompt。固定段落占比高，是 prompt caching 的理想候选。

**调用链**：DAG 并行执行业务工具 → 全部完成后 ReportAgent 串行兜底生成（[executor.py:88-98](../core/orchestrator/dag/executor.py#L88-L98)）。ReportAgent 是 DAG 的关键路径终点，它慢 = 端到端慢。

**超时预算**：app 级 `response_timeout_seconds=900`，LLM 单次 240s × 3 次重试 + 退避 ≈ 720s+。预算和实际一致，但对用户体感过长。

## 建议的优化手段（按性价比排序，业界惯例）

| # | 手段 | 预期收益 | 复杂度 |
|---|------|---------|-------|
| 1 | `llm_fast` 显式配 `qwen-flash`/`qwen-turbo`/`qwen3-30b-a3b` 等真小模型，断开对主模型的镜像 | **首屏 3–10×** | 只改 yaml |
| 2 | 打开**流式输出** `astream`，SSE/WebSocket 推给前端，TTFB 从"整段时长"降到"首 token 时间" | **感知大幅改善**，TTFT 通常 <2s | 中（API/前端都要配合） |
| 3 | 调小 `max_tokens`（如 1500–2000） + 在 prompt 里明确"报告 ≤ 800 字" | 20–40% | 低 |
| 4 | **Prompt caching**：把 `_REPORT_PROMPT` 里的角色/规则/约束段落作为稳定前缀，用 Qwen/OpenAI 的 prompt cache 机制（Qwen-dashscope 支持 `enable_cache` / OpenAI 自动前缀缓存） | 输入 token 费用 + TTFT 降 30–60% | 低 |
| 5 | **输出压缩**：工具 JSON 输出先做结构化摘要（保留异常行/TOP-N 指标，抛弃原始明细），再喂给 LLM；或引入 map-reduce（每节点先 summarize，再 reduce 写总报告） | 输入 50–80% | 中 |
| 6 | **结果缓存**：对 `(scenario, hash(outputs), prompt_version)` 做短 TTL 缓存（内存或 Redis） | 命中即 ~0ms | 低 |
| 7 | **降超时、降重试**：`timeout` 从 240s 降到 60–90s，重试 3→2；配合 Hedged request（并发打两路，取先返回） | 尾延迟显著降 | 低-中 |
| 8 | **连接层预热**：`_ensure_llm()` 做懒初始化，首次生成多花一次握手时间；启动时预热一次 | 首次请求 200–800ms | 低 |
| 9 | **结构化输出**：JSON schema / tool calling 让模型直接产出分节字段，前端用模板渲染 Markdown，模型只输出数据部分，大幅压缩生成 token | 20–40% | 中 |
| 10 | **温度/采样**：`temperature=0.1` 已合理，可考虑 `top_p=0.9`、关闭 n>1；不要用 beam search | 边际 | 低 |
| 11 | **观测定位**：在 `model` span 里加 `usage.prompt_tokens / completion_tokens / first_token_ms`，确认瓶颈在输入还是输出侧，再决策优先级 | 诊断必备 | 低 |

## 最小可执行的第一步建议

先做 **#1 + #3 + #11**：把 `llm_fast` 配成真的 fast 模型、`max_tokens` 降到 2000、打点输入/输出 tokens 和 TTFT。跑一次端到端就能量化剩余瓶颈，再决定是否上流式 / 缓存 / map-reduce。
