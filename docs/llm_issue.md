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

## 其他业界优化措施（补充）

有，下面补几类没在上一版覆盖到的，业界在 LLM 推理链路上常见：

### 一、生成侧（降低单次 LLM 调用延迟）

- **推测解码 / Speculative Decoding**：用小模型起草、大模型一次性校验多 token。Qwen 兼容接口暂不暴露，但自托管 vLLM/TGI/TensorRT-LLM 默认支持，典型 1.5–3×。
- **Skeleton-of-Thought**：先让模型一次输出"报告骨架/分节标题"，再**并行**让多路调用分别填各节内容，最后拼接。长报告 2–4× 提速。
- **Chain-of-Density**：迭代压缩式生成，适合"长输入 → 短输出"的摘要型报告，token 产出更紧凑。
- **Constrained/Structured Decoding**（JSON schema / grammar-guided，如 Outlines、XGrammar）：不只是省 token，还能避免重试式"输出不合规再调一次"的浪费。
- **Stop sequences / early stopping**：在 prompt 里定义明确结束符（如 `---END---`），防止模型尾部啰嗦。

### 二、请求侧（编排层）

- **Progressive / 渐进式 prompting**：DAG 的工具节点一旦完成，就把已有 outputs 先喂给 ReportAgent 开始生成骨架，不必等全部并行节点收齐。端到端"重叠"而非"串行"。
- **Hedged request + cancel**：相同请求同时发到两个 endpoint/provider，先返回的胜出并 cancel 另一个。拿尾延迟换成本。
- **多模型 Router**：用 LLM-Router（如 RouteLLM、Martian 式）或手写分级——简单场景走 flash，复杂场景走 max；代码里已有 `llm` / `llm_fast` 二分，可再细化到按 `scenario` 或 outputs 体量路由。
- **批处理 API（Batch API）**：对离线/T+1 的日报、周报类报告，走 Anthropic/OpenAI/Dashscope 的 Batch endpoint，单价 50%、延迟允许小时级。

### 三、缓存侧（更精细）

- **语义缓存（Semantic Cache）**：不止 hash，把 `(scenario + outputs)` 嵌入向量，向量近邻命中即复用历史报告。GPTCache / Redis-VSS 是业界常用方案。尤其适合"同一供应商上周分析过"这种近似重复查询。
- **KV-cache 复用**：自托管场景可把系统 prompt 的 KV 状态固定驻留，跳过 prefill 阶段（vLLM prefix caching、SGLang radix cache）。托管 API 对应的就是 prompt caching。
- **负缓存**：把"输入数据不足 / 无异常"这类短报告也缓存，避免每次都点一次 LLM。

### 四、数据侧（让 LLM 干更少的活）

- **模板化 + LLM 只填"异常解读"**：标题、指标表、图表链接全部用 Jinja 模板渲染，LLM 只生成"关键发现"和"建议措施"两段，输出 token 可降 70%+。
- **抽取式优先、生成式兜底**：先用规则/Pandas 产出事实句（"PO-123 偏差 8%，超容差 3%"），LLM 只做措辞润色与归并。
- **领域小模型微调 / 蒸馏**：用历史报告做 SFT 得到一个 7B 级的专用报告模型，延迟和成本能再压一个数量级。前提是有≥几千条标注报告。
- **Few-shot 检索**：向量库存历史高质量报告，按 scenario 取最相似 1–2 条作为 few-shot，生成更稳、可以适当减小 `max_tokens` 和废话率。

### 五、运行时/网络侧

- **HTTP/2 或 gRPC 复用 + 连接池长驻**：httpx AsyncClient 已用，但要确保单例复用、不是每次请求新建。
- **就近 endpoint / 多区域**：Dashscope 的就近接入点（北京/张家口等）可以省几十到上百毫秒 RTT。
- **压缩**（Accept-Encoding: zstd/br）：长 prompt 上行也是带宽瓶颈。
- **租户级限流与优先级队列**：避免突发流量把所有请求一起拖慢；报告类请求走低优队列、交互类走高优。
- **可观测的 SLO**：P50/P95/P99 TTFT 与 total latency 分开看，不然"平均慢"定位不到是输入侧还是输出侧的问题。

### 六、产品层（最被低估）

- **流式 + 分段可展开的 UI**：即使总耗时不变，用户看到报告"边写边出"就不焦虑。比任何后端优化性价比都高。
- **"快稿 + 深稿"双档**：先 5 秒返回模板化快稿，后台异步跑 LLM 生成深度稿，Ready 后 push 更新。
