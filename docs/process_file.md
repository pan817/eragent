现状 vs execute.md 的完整 Gap 分析
工具层 Gap
execute.md 工具	当前状态
query_purchase_orders	✅ 存在
query_goods_receipts	✅ 存在（query_receipts，需对齐命名）
query_vendor_invoices	✅ 存在（query_invoices）
query_payments	✅ 存在（但 execute.md 没列）
run_three_way_match	✅ 存在
run_price_variance_analysis	✅ 存在（calculate_ppv 命名不同）
run_payment_compliance_check	✅ 存在
calculate_supplier_kpis	✅ 存在（get_vendor_scorecard 命名不同）
query_vendor_master	❌ 缺失
query_material_master	❌ 缺失
calculate_spend_analysis	❌ 缺失
calculate_po_cycle_time	❌ 缺失
run_vendor_risk_scoring	❌ 缺失
check_approval_limits	❌ 缺失
check_blacklist	❌ 缺失
generate_summary_report	❌ 缺失（目前由 LLM 直接生成报告）
架构层 Gap
能力	现状	execute.md 目标
意图识别	单级关键词匹配	三级路由（关键词→RAG→LLM生成）
信号提取	仅提取 supplier_id/po/days	QuerySignal（含模糊度评分、ERP模块、追问机制）
执行层	P2PAgent 内部串行	外部 DAG Executor 并行调度
多 Agent	仅 P2PAgent	5 种 Agent（目前只有 1 个）
自学习	无	成功案例写 Chroma，下次直接命中
追问机制	无	ClarificationRequired 多轮对话
API 协议	单轮请求→响应	需支持追问状态
实施方案拆解（四个 Phase）
Phase 1：三级意图路由（不改执行层）
目标：把 IntentParser 升级为三级 IntentRouter，执行层 P2PAgent 完全不动，输出仍是 AnalysisType。

新增文件：


core/orchestrator/
├── signal.py          # QuerySignal dataclass（轻量版，不含 LLM 信号提取）
└── router.py          # IntentRouter（Level 1/2/3）
具体工作：

Level 1 增强：在现有关键词匹配上加命中率评分（命中数/规则词总数），替换当前"有/无"二值判断，超过阈值才认为命中
Level 2 种子库：在 Chroma 建 intent_seeds collection，为 4 个分析类型各预置 15 条自然语言描述，Embedding 相似度 > 0.80 命中
Level 3 LLM 分类：仅做分类（输出 AnalysisType + 参数），不生成 DAG，合并现有 _extract_params 逻辑，同时提取 supplier_id/po_number/days
QuerySignal：轻量版 dataclass，Level 1/2 用正则填充，Level 3 由 LLM 填充；暂不含模糊度评分和追问机制（Phase 4 再做）
Orchestrator 调整：从调 IntentParser.parse() 改为调 IntentRouter.route()，其余不变
风险：低，执行层零改动，测试完全可回归

Phase 2：DAG 执行层（替换执行层）
目标：引入静态 DAG 模板 + asyncio Executor，把执行从"P2PAgent 内部串行"改为"外部 DAG 并行调度"。

新增文件：


core/orchestrator/dag/
├── __init__.py
├── executor.py        # asyncio DAGExecutor（~100 行）
├── validator.py       # DAGValidator（结构校验）
├── templates.py       # 4 个静态 DAG 模板
└── generator.py       # DynamicDAGGenerator（Level 3 DAG 生成）
具体工作：

DAG 模板：为现有 4 个 AnalysisType 各定义一个静态 DAG 模板（three_way_match_dag 等），节点对应现有 8 个工具
Tool Registry：把 8 个 @tool 函数包装成 {tool_name: callable} 字典，注入 Executor
asyncio Executor：拓扑排序 + 并行执行，per-node 超时和错误处理，集成现有 TimingMiddleware span
DAGValidator：工具白名单（仅现有 8 个工具）、环检测、依赖合法性
LLM 动态生成：Level 3 路由命中时，调 LLM 生成 DAG JSON → 经 Validator → 执行；三次失败降级到 COMPREHENSIVE 静态模板
Orchestrator 重构：analyze() 从调 agent.run() 改为调 DAGExecutor.execute()
关键决策：多 Agent 暂不实现。vendor_agent/finance_agent/rule_agent/report_agent 暂时全部映射到 P2PAgent，DAG 中的 agent 字段作为元数据保留但忽略路由，等 Phase 4 再真正拆分。

风险：中，需要完整的集成测试覆盖，特别是 DAG 执行的错误处理路径

Phase 3：自学习闭环
目标：成功执行的 DAG 自动沉淀到 Chroma，Level 2 逐步用运行时案例替代预置种子。

新增文件：


core/orchestrator/dag/case_store.py   # DAGCaseStore
具体工作：

DAGCaseStore：成功执行后（无 failed tasks）异步写入 Chroma intent_cases collection
去重：用 QuerySignal.raw_query 的 hash 去重，相同 query 的新案例覆盖旧案例
Level 2 升级：从检索 intent_seeds 改为同时检索 intent_cases（运行时案例权重更高），RRF 融合排序
冷启动保障：intent_seeds 永远存在作为兜底，不会因为案例库为空而 Level 2 失效
风险：低，是纯增量功能，不改已有执行路径

Phase 4：多 Agent + 追问机制（最大工作量，单独排期）
目标：实现真正的多 Agent 路由和 ClarificationRequired 多轮对话。

工作内容：

追问机制：

QuerySignal 加模糊度评分逻辑
API 新增 status: need_clarification + clarification_question 字段
前端/调用方需支持多轮
多 Agent 拆分：

vendor_agent：供应商绩效、黑名单校验
rule_agent：合规规则引擎（validate_compliance、check_approval_limits）
report_agent：报告聚合生成（generate_summary_report）
P2PAgent 聚焦三路匹配 + 价格差异核心逻辑
缺失工具补全（最好在 Phase 2 之前完成以避免阻塞）：

query_vendor_master、query_material_master（需 DB schema 支持）
calculate_spend_analysis、calculate_po_cycle_time（纯计算，DB 数据足够）
run_vendor_risk_scoring、check_approval_limits、check_blacklist（需业务规则定义）
风险：高，涉及 API 协议变更和多 Agent 协作设计，建议单独立项

依赖关系和建议排期

Phase 1（意图路由）
  ↓ 并行可选
Phase 2（DAG 执行层）← 依赖 Phase 1 完成
  ↓
Phase 3（自学习）← 依赖 Phase 2 完成
  ↓
Phase 4（多 Agent）← 依赖 Phase 1/2/3 稳定
Phase 1 和"缺失工具补全"可以并行，两者互不依赖，Phase 2 需要两者都完成才能开始。

一个关键决策需要你确认
execute.md 的 extract_query_signal 对每一条请求都先调一次 LLM。我的建议是：

方案 A（execute.md 原版）：所有请求都先 LLM 提取信号，统一入口，逻辑干净，但每次多 1 次 LLM 调用

方案 B（渐进式）：Level 1/2 命中时不调 LLM，只有进入 Level 3 才做完整信号提取，延迟更低但逻辑稍复杂

你倾向哪种？确认后我们按 Phase 1 开始实施。