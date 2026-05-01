# ERP Agent 能力增强分析（vs 通用业务 Agent 基线）

> 本文从"通用业务 Agent"视角对照当前 eragent 实现，盘点能力差距与增强机会，**不是**技术债清单（未必是缺陷），而是**演进路线图**。每条目包含：差距描述、对比基线、对 ERP 场景的价值、改造大致范围、与现有架构的契合度。
>
> 与 [docs/agent_issue.md](agent_issue.md) 的边界：本文聚焦"做更多 / 做得更好"的方向（feature / capability gaps），`agent_issue.md` 聚焦"明文规约违规 / 缺陷"（debt）。两者在交集处交叉引用。
>
> **生成日期**：2026-05-01。

## 0. 背景与对标基线

"通用业务 Agent"在本文指：以 LLM 为编排核心、面向企业级业务场景（财务、采购、销售、HR、运营）的可投产 Agent 应用，参考的能力基线包括：

- **框架层**：Anthropic Agent SDK（含 prompt cache / files / memory tool）、LangGraph supervisor 模式、AutoGen / CrewAI 多 Agent 协作
- **生产形态**：多租户 SaaS 形态的 ERP Copilot（Workday Illuminate、SAP Joule、Oracle 智能客服 Agent 等公开能力）
- **工程实践**：DAG 编排 + ReAct 双模执行、prompt cache、semantic cache、golden eval set、可观测性 + 成本看板

对标的目的不是"做成 SaaS 产品"，而是从这些参照系中识别**单租户私有部署**条件下仍然有价值的能力。

## 1. 现状盘点（已具备能力）

下列能力 eragent 当前已经实现或部分实现，在做差距分析时不再重复列出：

- **路由分层**：L0 bypass（早退）+ Unified LLM（意图+参数+消解一次出）+ Plan and Solve（L3 兜底一次 LLM 出 DAG 计划）+ ReAct（最终兜底）
- **执行编排**：DAGExecutor 并行 + ReportAgent 汇总；27 个 LangChain Tool（PG 15 + Graph 12）
- **数据层**：PostgreSQL 镜像 EBS + Graphiti（Neo4j）时序图谱 + Chroma 向量；双后端 `query_backend` 抽象（graphiti / postgresql / hybrid）
- **多数据源抽象**：`P2PRepositoryProtocol` + oracle_ebs / new_erp 双实现，工具仅依赖协议
- **记忆**：长期记忆（PG，按 `user_id` 隔离）+ 短期记忆（LangGraph PostgresSaver）+ 反馈识别（[core/memory/feedback.py](../core/memory/feedback.py)，识别 correction / preference / domain_fact）
- **可观测性**：LangChain 中间件 trace + 写 PG（`trace_spans`）+ `/traces` API + SSE 流式事件
- **异步执行**：`POST /analyze/async` + EventBus（memory / Redis 双后端）+ TaskRegistry 生命周期管理
- **本体推理**：Owlready2 + SWRL（合规规则在本体，KPI 在 Python）
- **ETL**：Graphiti ETL 全量 + 10 分钟增量同步，5 域 20 表声明式映射
- **基础多租户字段**：`core/database/models.py` 多张 EBS 镜像表已有 `org_id` 列，但**仅作为业务字段**，未参与权限隔离（详见 §3.1）

后续章节聚焦**未具备 / 实现不充分**的能力。

## 2. 业务能力扩展机会

### 2.1 写操作 / 执行能力（最大功能缺口）

**现状**：CLAUDE.md 明确"当前版本纯分析只读，写操作接口预留"。27 个工具中无任何 `create_*` / `update_*` / `approve_*` / `notify_*`。
**通用基线**：成熟业务 Agent 通常具备：①创建采购订单 / 调整付款条款 / 标记发票异常 / 拉黑供应商 ②向 ERP 系统回写决策 ③触发外部通知（邮件 / 钉钉 / Slack）。
**对 ERP 价值**：用户从"看分析"升级到"做决策" — 例如分析完三路匹配差异后直接发起冻结付款 / 创建调差单。这是把 Agent 从"BI 工具"推到"运营助手"的关键能力。
**改造范围**：
- 新增 `modules/p2p/tools/write/` 子包，区分读 / 写工具的注入与权限策略
- 写工具必须配套审批流（见 §3.4）和审计日志（见 §3.3）
- Orchestrator 需要"动作前确认"模式（区别于纯分析的自动执行）
- 与现有 `P2PRepositoryProtocol` 协同 — 协议需扩展写方法，oracle_ebs / new_erp 各自实现
**契合度**：架构已为此预留（read-only 是显式注释）；ModuleProvider Protocol 可加入 write capability 标志位。属于**已规划未实施**。

### 2.2 业务模块横向扩展（结构已就绪）

**现状**：`modules/p2p/` 是唯一业务模块；`ModuleProvider` Protocol 设计支持多模块，但只有一个实现。
**通用基线**：完整 ERP Copilot 至少覆盖 P2P / O2C（Order-to-Cash，销售订单 → 应收）/ R2R（Record-to-Report，总账 → 报表）/ H2R（Hire-to-Retire，HR）/ Inventory / Manufacturing 几大流程。
**对 ERP 价值**：
- O2C：客户欠款分析、销售订单履约率、信用额度预警
- R2R：月结异常发现、科目对账、合并报表差异定位
- Inventory：呆滞料分析、安全库存合理性、ABC 分类
**改造范围**：
- 每个新模块新建 `modules/<name>/`，实现 Provider + 工具集 + DAG 模板 + 规则
- ETL 侧需要扩展抽取域（当前仅 5 域聚焦 P2P）
- 跨模块查询需要"联邦"路由 — 例如"对比这家供应商既是客户又是供应商时的净敞口"
**契合度**：架构层面就绪度高（Protocol 已定义），ETL / 本体 / 工具集需要逐模块新建。属于**纵深扩展**，每个模块约 2-4 周工作量。

### 2.3 文档 / 政策 RAG（结构化数据之外的盲区）

**现状**：所有数据源都是结构化的（PG / Neo4j），本体提供合规规则 SWRL，但**没有非结构化文档检索**。
**通用基线**：业务 Agent 普遍配备文档 RAG — 检索公司采购政策、供应商合同条款、历史分析报告、SOP、邮件 / 工单。
**对 ERP 场景价值**：
- 用户问"这笔合同条款里付款日是月末还是发票日 +30 天" — 当前 Agent 只能查结构化字段，无法读合同 PDF
- "为什么这家供应商被列入受限清单" — 答案在采购政策文档里
- "上次类似情况是怎么处理的" — 答案在历史分析报告 / 工单
**改造范围**：
- 已有 Chroma 向量库基础，新增文档摄入管道（PDF / Word / 邮件 → chunk → embedding）
- 新增 `search_policy_docs` / `search_contract_clauses` / `search_historical_reports` 工具
- 在 Plan and Solve / ReAct 路径让 Agent 学会何时调文档检索（vs 结构化查询）
- ChromaDB 已就位，缺的是**摄入管道 + 工具封装 + Prompt 引导**
**契合度**：技术栈已具备（Chroma 在线），属于**新建管道**而非架构改造。

### 2.4 多模态输出（图表 / 表格 / 文件）

**现状**：`ReportAgent` 输出纯文本 / Markdown；SSE 流式输出文本片段。
**通用基线**：业务 Agent 输出常包含：①柱状图 / 趋势线（matplotlib / plotly JSON spec）②结构化表格（HTML / Excel 附件）③可下载文件（PDF 报告 / CSV 导出）。
**对 ERP 价值**：财务 / 采购报告天生是表格 + 图表混合体；纯文字描述 30 个供应商的金额排名不如直接给一张表。
**改造范围**：
- ReportAgent 输出契约扩展：除了 `narrative_text` 外新增 `attachments`（图表规格 / 表格数据 / 文件路径）
- 前端 SSE 协议扩展事件类型（chart / table / file）
- 后端新增图表渲染（如果不交给前端纯渲染）：可选 matplotlib server-side render → PNG，或返回 Vega-Lite spec 让前端渲染
- Excel 导出工具：`pandas.to_excel` 直接生成
**契合度**：协议 / 工具新增；不冲突现有架构。

### 2.5 主动监控与定时报告

**现状**：所有交互都是用户发起（pull 模式）。ETL Scheduler 是数据同步的 cron，但**没有"分析任务"的 cron**。
**通用基线**：成熟业务 Agent 提供：①定时报告（每周一推送上周 KPI）②阈值告警（三路匹配异常率 > 5% 自动通知采购经理）③订阅式分析（"以后每次新合同签订后帮我分析一次价格差异"）。
**对 ERP 价值**：从"被动应答"到"主动陪跑" — 业务用户最想要的能力之一。
**改造范围**：
- 复用 ETL Scheduler 模式新建 `core/agent_scheduler/`，配置文件描述定时任务（cron + 查询模板 + 通知渠道）
- 通知 sink 需要外部集成（邮件 / 钉钉 / Slack / Webhook）
- 阈值告警需要**有状态**对比（上次值 vs 本次值），与现有无状态分析不同
- Trace / 任务历史需要持久化"机器发起的分析"以便审计
**契合度**：基础设施（DAG / 工具 / Report）就绪，缺**调度层 + 通知层**。

## 3. 多租户与权限

### 3.1 多公司 / 法人主体隔离

**现状**：长期记忆按 `user_id` 隔离；`core/database/models.py` 多张 EBS 镜像表已带 `org_id` 列（line 76 / 272 / 327 / 380 等），但 `org_id` **仅作为业务字段被查询**，未在以下层面参与隔离：
- API 层无 `org_id` 维度的请求过滤
- Repository 层无 `org_id` scoping
- 长期记忆 / trace 不带 `org_id`
- 工具调用不强制传 `org_id` 参数
**通用基线**：企业级 Agent 默认按 `tenant_id` / `legal_entity_id` 分库或分行隔离，跨租户查询需显式提权。
**对 ERP 价值**：集团企业部署时，财务子公司数据不能被采购子公司看到；不同法人主体的合同条款、付款规则不同，混查会出错。
**改造范围**：
- 引入 `tenant_id` / `org_id` 作为请求级强制上下文（API 中间件注入）
- Repository 所有方法增加 `org_id` 过滤参数 — 这是**侵入式改造**，27 个工具 + Repository 全量
- 长期记忆 / trace 加 `org_id` 维度
- 配置层支持"用户可访问哪些 org"
**契合度**：底层数据有字段，**业务层完全未实施**。建议在多模块扩展（§2.2）启动前先做，否则后期改造成本翻倍。

### 3.2 工具与数据级 RBAC

**现状**：工具注入是**全集模式** — 通过 `_inject.py` 把 27 个工具全部注入给 Agent；任何持有 API token 的用户调到的工具集相同；数据行级权限完全不存在。
**通用基线**：业务 Agent 通常有：①工具白名单（操作员只能查询，分析师可调聚合工具，管理员可调写工具）②数据范围限制（采购员只看自己负责的品类）③敏感字段脱敏（合同金额对非授权角色显示为脱敏值）。
**对 ERP 价值**：避免越权数据泄露 — 例如普通用户不应看到全部供应商的报价细节、不应看到 HR 工资数据。
**改造范围**：
- 新增 `core/auth/` 模块：用户身份解析 + 角色 → 工具白名单 / 数据范围映射
- `_inject.py` 改为按角色动态过滤工具集
- Repository 层注入用户上下文，自动追加 WHERE 条件（如 `category IN (...用户授权品类)`）
- 敏感字段脱敏在 `_output.py` 层做（已有输出格式化的钩子）
**契合度**：现有 `_inject.py` / `_output.py` 是天然切入点，但需要先有**身份与角色模型**。

### 3.3 审计日志与合规

**现状**：`core/observability/` 记录技术 trace（LLM 调用、工具调用、耗时），但**没有业务级审计日志**：谁在什么时候问了什么问题、看到了什么数据、做了什么决策。技术 trace 可被覆盖 / 删除，不满足审计"不可篡改"要求。
**通用基线**：金融 / 采购级业务 Agent 必须有：①操作审计（追加写、不可改）②查询审计（含查询参数 + 返回结果摘要）③导出 / 下载审计（含目标文件 hash）④保留期合规（至少 7 年）。
**对 ERP 价值**：内审 / SOX 合规硬性要求；写操作（§2.1）一旦上线，审计是前置条件。
**改造范围**：
- 新增 `core/audit/`，独立 PG schema（避免与业务 trace 混淆）
- 设计 append-only 表 + 周期归档到对象存储
- API 中间件统一拦截请求 / 响应，提取审计字段
- 数据脱敏（审计记录可包含敏感字段时需要单独的访问控制）
**契合度**：完全新模块；与现有可观测性是不同关注点（运维 vs 合规），不能复用 `trace_spans`。

### 3.4 写操作审批流

**现状**：无（因当前系统纯只读，不存在审批需求）。
**通用基线**：写操作通常配审批 — ①Agent 提议动作 → ②人工 / 多人确认 → ③系统执行。Anthropic SDK 中的 "Tool Approval" 模式 / LangGraph human-in-the-loop checkpoint 都是这个范式。
**对 ERP 价值**：所有写操作（§2.1）的前置必要条件 — 让 Agent 提议"将这笔异常付款打回退回"，由有权限的人按一下确认。
**改造范围**：
- 新增 `core/approval/`：动作建议 → 待审任务 → 审批 / 驳回 → 执行 / 取消
- 与 §2.1 写工具配套设计 — 写工具应该返回"动作 ID"而非直接执行
- API 暴露 `/approvals` 路由列表查询 + 单条审批
- 通知集成（与 §2.5 共用通知 sink）
**契合度**：是 §2.1 + §3.3 的衍生需求，三者通常一起规划。

## 4. Agent 智能层

### 4.1 反思 / 自检 / 输出校验

**现状**：DAG 执行完毕 → ReportAgent 直接生成报告；ReAct 沿"思考 → 行动 → 观察"循环但**没有"我真的回答了用户的问题吗"的自检阶段**。如果 SQL 查到 0 行，Agent 倾向于直接说"无数据"，不会反问"是不是查询条件错了"。
**通用基线**：Reflexion / Self-Critique 模式 — 主流程结束后追加"评判者"角色：对照原始问题校验输出是否覆盖、是否有逻辑漏洞、是否需要重跑。
**对 ERP 场景价值**：分析报告的可信度提升；减少"看似回答其实没答到点"的体验问题。
**改造范围**：
- 新增 `core/orchestrator/critic.py`：在 `orchestrator.py` 主路径输出后增加 critic 阶段
- Critic 可以决策：①直接放行 ②要求 ReportAgent 重写 ③要求重跑某个 DAG 任务
- 需要预算控制 — critic 失败不能无限重试，最多 N 次
- 现有可观测性可以记录 critic 的判断，便于评估其价值
**契合度**：在 `orchestrator.py` 末尾增加阶段即可，不破坏现有流程；可由 feature flag 控制启用。

### 4.2 多 Agent 协作（监督者模式）

**现状**：单 Agent 持有全部 27 个工具，意图模糊时容易选错工具。Plan and Solve 缓解了部分（一次出全计划），但"专业领域分工"未落地。
**通用基线**：LangGraph Supervisor / CrewAI Crew / Anthropic SDK 子 Agent — 主 Agent 把子任务派发给专精子 Agent（如 PriceAnalyst / ComplianceChecker / SupplierExpert），子 Agent 各自的工具集 / 提示词更聚焦，错误率显著降低。
**对 ERP 价值**：
- 工具集 27 个 → 单次推理 token 消耗大；按子 Agent 拆分（每个 5-8 工具）可降本
- 专家 Prompt 可针对领域定制（合规 vs KPI vs 异常 检测的话术不同）
- 同时启用并行（一个供应商分析需求 → 同时跑价格 / 合规 / 绩效三个子 Agent）
**改造范围**：
- 现有 DAG 已经是某种"显式编排"，与监督者模式有重叠 — 决策点是 Plan and Solve / DAG 是否已经够用
- 如要做：在 `core/orchestrator/` 引入 `subagents/`，每个子 Agent 用 `create_agent` + 专属工具集
- ReportAgent 升级为 Supervisor，决策"派给谁"
- Trace 需要记录跨 Agent 的父子关系
**契合度**：与现有 DAG 路径有架构竞争 — DAG 是"任务并行"，Supervisor 是"角色并行"，混用要小心。**建议先充分发挥 DAG**，仅在 ReAct 路径考虑引入子 Agent。

### 4.3 主动澄清（意图模糊时反问）

**现状**：CLAUDE.md 显式策略"意图模糊时不前置拦截，遵循'尽量回复'原则" — Agent 不会主动澄清，会硬猜。
**通用基线**：现代 Agent 在置信度低时主动反问 — "您说的'最近'是指最近 30 天还是本季度？"。
**对 ERP 价值**：减少错误分析（节省一次完整 DAG 跑的 token）；提升用户感知专业度。但**与"尽量回复"哲学冲突** — 这是产品决策，不是技术决策。
**改造范围**：
- 在 Unified Router 出口增加"模糊度检测"分支：低置信度且能列出可能选项时，直接返回澄清提问
- 需要会话状态承载"澄清中"上下文（用户回复后接着原计划执行）— LangGraph checkpointer 已支持
**契合度**：技术上简单，**产品策略选择**比技术实现更重要。建议先做 A/B 看效果。

### 4.4 来源引用与可解释性

**现状**：ReportAgent 生成自然语言结论，但**不强制引用具体数据源行** — 例如"该供应商有 5 笔异常付款"无法点击跳转到那 5 行。
**通用基线**：现代分析型 Agent（Perplexity / Notion AI / Glean）输出每条结论都带行内引用 `[1][2]`，鼠标悬停展示原始数据片段。
**对 ERP 价值**：用户对分析结果可校验、可下钻；审计 / 合规也需要可追溯。
**改造范围**：
- DAG 任务结果新增 `evidence` 字段（PO 号 / 行号 / Cypher 查询的具体节点 ID）
- ReportAgent 输出 Markdown 中保留 `[evidence:po-123]` 占位符
- API 响应额外携带 `evidence_map`，前端渲染时把占位符变可点击
- Repository 层需要在返回数据时附带"来源指针"（哪张表 / 哪个查询 / 时间戳）
**契合度**：跨层改造（Repository → Tool → DAG → Report → API），中等成本但价值明确。建议作为**用户可见质量提升**优先级靠前。

### 4.5 长程对话记忆（跨周 / 跨月）

**现状**：短期记忆按 `session_id` 隔离（LangGraph PostgresSaver），长期记忆按 `user_id` 累积事实 / 偏好；但没有"会话历史索引检索" — 用户问"上周我们讨论的那家供应商" 时 Agent 不会主动回查 chat 历史。
**通用基线**：Anthropic SDK 的 Memory Tool / ChatGPT 的 Memory / Claude Projects — 主动维护用户的实体清单 / 关注主题 / 历史结论。
**对 ERP 价值**：长期客户关系类场景刚需 — 财务月结 / 季度复盘 / 年度审计都需要"回顾"。
**改造范围**：
- 现有 `core/chat/` 已持久化消息历史
- 新增 `search_my_chat_history` 工具或 RAG 链路
- 长期记忆扩展"主题持续追踪"（例如"用户长期关注供应商 SUP-007"）
- 需要隐私 / 合规边界 — 哪些会话内容可以纳入长期记忆
**契合度**：基础设施齐备（chat repo + 长期记忆 + Chroma），缺**索引建立 + 检索工具 + Prompt 引导**。

## 5. 工程效能层

### 5.1 评估 harness（golden Q&A 集）

**现状**：项目有 1521 个 pytest 用例（覆盖率 90.41%），但**全部是工程测试**（API / Repository / 路由逻辑等）；**没有针对"LLM 输出质量"的回归测试** — 改 Prompt 或换模型后，业务回答质量是否退化无客观度量。
**通用基线**：成熟 Agent 项目都维护 golden eval set — 一组"问题 → 期望答案要点 / 期望工具调用序列"的对照数据，每次主要变更跑一遍打分（LLM-as-judge 或人工）。
**对 ERP 价值**：模型升级 / Prompt 调优 / 工具改造的**质量回归防线**。当前没有这道防线，改动信心完全靠 e2e 单测，覆盖面不足。
**改造范围**：
- 新增 `tests/golden/` 目录，按场景分文件（三路匹配 / 价格异常 / 付款合规 / 供应商绩效）
- 每条用例：query + 期望工具序列（容许多种合法路径）+ 期望关键字段 + 反例（不应出现的内容）
- 评估脚本：跑 query → 收集实际输出 → LLM-as-judge 打分 → 汇总分数
- 可与现有 e2e 测试共用 fixture / mock 数据
- 集成到 CI（建议作为 nightly job，因为 token 成本不低）
**契合度**：完全新增，但**最高 ROI 工程债**之一。建议优先于其它"看起来更复杂"的项目。

### 5.2 反馈闭环（用户纠错 → 学习）

**现状**：[core/memory/feedback.py](../core/memory/feedback.py) 已识别 correction / preference / domain_fact 三类反馈并存入长期记忆；但**反馈到 Prompt 的注入路径** / **反馈对 routing 的影响**未充分闭环 — 下次同类问题会用上反馈，还是仅作"事实记忆"被动展示？
**通用基线**：完整闭环包含：①捕获（已有）②存储（已有）③**注入**（部分有）④**评估反馈是否被采纳**（无）⑤**反馈聚合后影响默认 Prompt / 阈值**（无）。
**对 ERP 价值**：让 Agent 越用越准；用户重复纠错的同一个问题会让人挫败感很强。
**改造范围**：
- 审视 `core/memory/injection.py` 是否把 feedback 充分注入了 Agent 上下文
- 新增 feedback 采纳率指标：用户提了 N 次纠错，下次相同/相似问题改正率？
- 高频反馈（同样的错误说了 5 次）应该升级为 system prompt 修订建议（人工审核后落地）
- 与 §5.1 的 golden eval set 联动 — 用户反馈成为 eval set 的新增条目
**契合度**：基础设施齐备，缺**度量 + 升级机制**。

### 5.3 成本跟踪与预算控制

**现状**：可观测性记录 LLM 调用耗时和 token；CLAUDE.md "关键指标埋点"列出 token 消耗作为必埋项。但**未见请求级 / 用户级 / 租户级的成本汇总和预算硬限**。
**通用基线**：①每个分析请求显式标注预估成本和实际成本 ②用户 / 部门预算池，超额 fail-fast ③成本看板（按工具 / 按用户 / 按时段）。
**对 ERP 价值**：私有部署用 Qwen 成本相对可控，但**云端 LLM（OpenAI / Claude）单次复杂分析可能 0.5-2 USD，规模化后预算管理是刚需**。
**改造范围**：
- `core/observability/` 已记录 token，扩展成本表（model_id × token_count → unit_price → cost）
- 新增预算管理：`core/budget/` 或并入 settings — 配置每用户 / 每租户日 / 月预算
- 中间件检查预算：超限时降级（用 llm_fast）或拒绝
- Admin metrics 路由扩展成本看板视图
**契合度**：现有 metrics 是基础，新增**单价表 + 预算策略 + 限流逻辑**即可。

### 5.4 Prompt 缓存与语义缓存

**现状**：检查代码无 `cache_control` / `prompt_cache` / `ephemeral.*cache` 引用 — 既未使用 Anthropic prompt cache，也未做语义缓存（相似问题命中历史答案）。
**通用基线**：
- **Anthropic prompt cache**：5 分钟 TTL，对**长 system prompt + 工具定义**的复用极其划算（首次缓存写入有 cache_creation 费，后续 cache_read 仅 10% 价格）
- **语义缓存**：用户问"该供应商付款异常吗" 与 5 分钟前另一用户问相同问题 — 直接复用结果（带去识别 / 数据更新策略）
**对 ERP 价值**：
- ERP Agent 的 system prompt + 工具 schema 经常 5-10K token，每次重复送入浪费严重；prompt cache 单项可省 70%+ 输入成本
- 语义缓存对"运营报表类"高频重复问题节省显著
- 当前主模型是 Qwen，需确认 Dashscope 是否支持类似缓存机制（如不支持，至少切到 Claude 兼容路径时可用）
**改造范围**：
- Prompt cache：在 LLM 客户端封装层（`core/llm/model_factory.py`）添加 `cache_control` 标注，仅对 Claude 兼容 API 生效
- 语义缓存：新增 `core/cache/semantic.py`，用 Chroma 缓存 query embedding → 结果，TTL + invalidation 策略需设计
- 与可观测性集成：cache_hit / cache_miss 指标
**契合度**：prompt cache 改造小、回报立竿见影；语义缓存涉及更多设计（命中策略 / 数据时效）。

## 6. 优先级建议

### 6.1 短期高 ROI（1-2 周内可启动）

1. **§5.1 golden eval set** — 不做这个，后续所有改动都是"凭感觉调"，改 Prompt 会无意识地搞坏旧场景。**强烈建议优先**。
2. **§5.4 Prompt cache** — 单点改动 + 立竿见影的成本下降。
3. **§4.4 来源引用** — 用户感知最直接的"质量提升"，跨层但每层改动都不大。
4. **§2.3 文档 RAG（采购政策 / 合同摘要）** — 已有 Chroma 基础，加一条管道即拓展能力边界。

### 6.2 中期能力建设（1-2 月）

5. **§3.1 多租户隔离 + §3.2 RBAC** — 多模块扩展启动前**必须**完成，否则后期改造成本翻倍。
6. **§2.1 写操作 + §3.4 审批流 + §3.3 审计** — 三件套捆绑设计，把 Agent 从"分析助手"升级到"运营助手"。
7. **§5.2 反馈闭环升级** — 从"识别 + 存储"扩展到"注入有效性 + 采纳率度量"。
8. **§2.5 主动监控 / 定时报告** — ETL Scheduler 模式可复用，重点是通知 sink 集成。

### 6.3 长期架构演进（3+ 月）

9. **§2.2 业务模块横向扩展（O2C / R2R / Inventory）** — 必须在 §3.1/3.2 之后做。
10. **§4.1 反思 / 自检** — 价值明确但需要先有 §5.1 度量基础再上，否则无法判断 critic 是否真的有用。
11. **§4.2 多 Agent 协作** — 与 DAG 架构有竞争，需要充分评估再决定。
12. **§4.5 长程记忆** — 与 §5.2 反馈闭环可协同设计。

### 6.4 不建议立即做

- **§4.3 主动澄清**：与现有"尽量回复"产品哲学冲突，需要先做产品决策再谈技术实现。
