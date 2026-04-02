好，这是 Orchestrator 设计中最有挑战性的部分——**当用户的请求模糊、跨领域、或完全不在预定义规则范围内时，系统如何"自救"并动态生成合理的执行计划**。

先用图把完整决策链路展示出来，再逐层拆解代码实现。先看意图解析的完整决策链路——这是理解整个系统工作方式的关键：核心设计思想是**三级降级策略**：精确规则匹配 → RAG 语义相似 → LLM 动态生成，三条路径最终汇入同一个 DAG 执行引擎。现在逐层拆解每一级的实现细节。

---

## 第一级：信号提取与置信度评分

在路由之前，必须先从原始请求中提取结构化信号，并计算"模糊度"——这个分数决定走哪条路径：

```python
# intent_extractor.py
from dataclasses import dataclass, field
from anthropic import Anthropic
import re, json
from datetime import date

client = Anthropic()

@dataclass
class QuerySignal:
    """从用户请求中提取的结构化信号"""
    raw_query:        str
    keywords:         list[str]      # 提取的业务关键词
    entities:         dict           # 识别的实体 {vendor_id, material_id...}
    time_range:       dict | None    # 时间范围
    analysis_depth:   str            # SUMMARY / DRILLDOWN / ROOT_CAUSE
    domain_modules:   list[str]      # 涉及的 ERP 模块 [MM, FI, SD...]
    ambiguity_score:  float          # 0.0=清晰 1.0=完全模糊
    clarification_needed: bool       # 是否需要追问

SIGNAL_EXTRACTION_PROMPT = """
分析采购业务请求，提取结构化信号。输出纯 JSON，无其他内容。

{
  "keywords": ["三路匹配","发票","异常"],     // 业务关键词
  "entities": {
    "vendor_id": null,                        // 如提到具体供应商
    "material_group": null,
    "company_code": "1000",
    "plant": null
  },
  "time_range": {"start": "2025-01-01", "end": "2025-03-31"},
  "analysis_depth": "DRILLDOWN",             // SUMMARY/DRILLDOWN/ROOT_CAUSE
  "domain_modules": ["MM", "FI"],            // MM=采购 FI=财务 SD=销售
  "ambiguity_score": 0.15,                   // 0=完全清晰 1=完全模糊
  "ambiguity_reason": "",                    // 如果模糊，说明原因
  "clarification_needed": false,             // 是否必须追问才能继续
  "clarification_question": null             // 如需追问，给出最关键的一个问题
}

评分规则：
- 有明确 KPI 名称 → ambiguity_score -= 0.3
- 有具体时间范围 → ambiguity_score -= 0.2
- 有明确分析维度 → ambiguity_score -= 0.2
- 跨模块且未说明重点 → ambiguity_score += 0.3
- 完全没有业务术语 → ambiguity_score += 0.4

今天日期：{today}
用户请求：{query}
"""

async def extract_query_signal(query: str) -> QuerySignal:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": SIGNAL_EXTRACTION_PROMPT.format(
                today=str(date.today()),
                query=query
            )
        }]
    )
    data = json.loads(response.content[0].text)

    # 如果模糊度很高，必须先澄清再规划
    if data.get("clarification_needed") and data.get("ambiguity_score", 0) > 0.7:
        raise ClarificationRequired(
            question=data["clarification_question"],
            signal=data
        )

    return QuerySignal(
        raw_query=query,
        keywords=data.get("keywords", []),
        entities=data.get("entities", {}),
        time_range=data.get("time_range"),
        analysis_depth=data.get("analysis_depth", "SUMMARY"),
        domain_modules=data.get("domain_modules", []),
        ambiguity_score=data.get("ambiguity_score", 0.5),
        clarification_needed=data.get("clarification_needed", False)
    )

class ClarificationRequired(Exception):
    """当请求太模糊必须追问时抛出"""
    def __init__(self, question: str, signal: dict):
        self.question = question
        self.signal = signal
        super().__init__(question)
```

---

## 第二级：三级路由的完整实现

```python
# intent_router.py

from dataclasses import dataclass
from typing import Callable
import json, hashlib

@dataclass
class RouteResult:
    route_type:  str       # STATIC / ADAPTED / DYNAMIC
    confidence:  float
    dag:         list[dict]
    reasoning:   str       # 为什么选这条路，便于调试

# ─── 预定义规则库 ──────────────────────────────────────────────────
# 每条规则：关键词集合 + 命中阈值 + 对应的 DAG 模板名
RULE_LIBRARY = [
    {
        "rule_id":   "THREE_WAY_MATCH",
        "keywords":  {"三路匹配", "invoice", "发票", "收货", "goods receipt",
                      "po", "采购订单", "匹配", "mismatch", "差异"},
        "threshold": 0.5,          # 命中关键词占比 ≥ 50% 触发
        "modules":   {"MM", "FI"},
        "dag_template": "three_way_match_dag"
    },
    {
        "rule_id":   "PPV_ANALYSIS",
        "keywords":  {"ppv", "价格差异", "price variance", "标准价", "采购价",
                      "成本节省", "价格偏差"},
        "threshold": 0.4,
        "modules":   {"MM"},
        "dag_template": "ppv_analysis_dag"
    },
    {
        "rule_id":   "VENDOR_PERFORMANCE",
        "keywords":  {"供应商绩效", "准时交货", "otif", "质量", "评分",
                      "scorecard", "供应商评估", "交期"},
        "threshold": 0.4,
        "modules":   {"MM"},
        "dag_template": "vendor_performance_dag"
    },
    {
        "rule_id":   "PAYMENT_DUE",
        "keywords":  {"付款", "到期", "due", "payment", "应付", "账期",
                      "early payment", "折扣"},
        "threshold": 0.45,
        "modules":   {"FI"},
        "dag_template": "payment_due_dag"
    },
    {
        "rule_id":   "SPEND_ANALYSIS",
        "keywords":  {"采购金额", "花费", "spend", "支出", "消耗",
                      "品类分析", "category"},
        "threshold": 0.4,
        "modules":   {"MM"},
        "dag_template": "spend_analysis_dag"
    },
]

class IntentRouter:

    def __init__(self, rag_retriever, dag_generator):
        self.rag    = rag_retriever
        self.llm_gen= dag_generator

    async def route(self, signal: QuerySignal) -> RouteResult:
        """
        三级路由：
        1. 精确规则匹配（关键词命中率）
        2. RAG 历史案例语义匹配
        3. LLM 动态 DAG 生成
        """

        # ── 第一级：关键词精确匹配 ─────────────────────────────────
        rule_result = self._try_rule_match(signal)
        if rule_result:
            return rule_result

        # ── 第二级：RAG 语义相似度匹配 ────────────────────────────
        rag_result = await self._try_rag_match(signal)
        if rag_result:
            return rag_result

        # ── 第三级：LLM 动态生成 ──────────────────────────────────
        return await self._generate_dynamic_dag(signal)

    def _try_rule_match(self, signal: QuerySignal) -> RouteResult | None:
        """关键词命中率匹配"""
        query_words = set(
            signal.raw_query.lower().replace("，", " ").replace("。", " ").split()
        ) | set(w.lower() for w in signal.keywords)

        best_rule = None
        best_score = 0.0

        for rule in RULE_LIBRARY:
            rule_keywords = rule["keywords"]
            # 关键词命中率 = 命中数 / 规则关键词总数
            hits = sum(
                1 for kw in rule_keywords
                if any(kw in word or word in kw for word in query_words)
            )
            hit_rate = hits / len(rule_keywords)

            # 模块匹配加权
            module_bonus = 0.1 if rule["modules"] & set(signal.domain_modules) else 0

            score = hit_rate + module_bonus
            if score > best_score and hit_rate >= rule["threshold"]:
                best_score = score
                best_rule = rule

        if best_rule:
            dag = load_dag_template(best_rule["dag_template"], signal)
            return RouteResult(
                route_type="STATIC",
                confidence=round(best_score, 3),
                dag=dag,
                reasoning=(
                    f"命中规则 {best_rule['rule_id']}，"
                    f"关键词命中率 {best_score:.1%}"
                )
            )
        return None

    async def _try_rag_match(self, signal: QuerySignal) -> RouteResult | None:
        """从历史成功案例中检索相似 DAG"""
        # 用请求+关键词组合查询历史案例库
        query_text = f"{signal.raw_query} {' '.join(signal.keywords)}"
        results    = self.rag.retrieve(
            query=query_text,
            doc_types=["successful_dag_case"],
            top_k=3
        )

        if not results:
            return None

        best = results[0]
        similarity = best.get("_score", 0)

        if similarity < 0.78:   # 相似度阈值，低于此不信任
            return None

        # 从历史案例改编 DAG（调整时间范围、过滤条件等）
        adapted_dag = self._adapt_dag_from_case(
            base_dag=best["dag_definition"],
            signal=signal
        )

        return RouteResult(
            route_type="ADAPTED",
            confidence=round(similarity, 3),
            dag=adapted_dag,
            reasoning=(
                f"匹配历史案例 '{best['case_name']}'，"
                f"语义相似度 {similarity:.1%}，已适配当前参数"
            )
        )

    def _adapt_dag_from_case(self, base_dag: list, signal: QuerySignal) -> list:
        """将历史 DAG 适配到当前请求的参数"""
        adapted = []
        for task in base_dag:
            new_task = dict(task)
            # 替换时间范围
            if signal.time_range and "inputs" in new_task:
                if "date_from" in new_task["inputs"]:
                    new_task["inputs"]["date_from"] = signal.time_range["start"]
                if "date_to" in new_task["inputs"]:
                    new_task["inputs"]["date_to"] = signal.time_range["end"]
            # 替换实体过滤条件
            for entity_key, entity_val in (signal.entities or {}).items():
                if entity_val and "inputs" in new_task:
                    new_task["inputs"][entity_key] = entity_val
            adapted.append(new_task)
        return adapted

    async def _generate_dynamic_dag(self, signal: QuerySignal) -> RouteResult:
        """最后兜底：让 LLM 根据本体+工具描述动态生成 DAG"""
        dag, reasoning = await self.llm_gen.generate(signal)
        return RouteResult(
            route_type="DYNAMIC",
            confidence=0.0,   # 动态生成无置信度，需校验
            dag=dag,
            reasoning=reasoning
        )
```

---

## 第三级：LLM 动态 DAG 生成器（最核心的部分）

这是未命中任何规则时的兜底，也是架构中最有技术深度的环节：

```python
# dynamic_dag_generator.py

DAG_GENERATION_PROMPT = """
你是采购 ERP 分析系统的任务规划引擎。
根据用户的分析意图，设计一个可执行的 DAG（有向无环图）任务计划。

## 可用工具清单（你只能使用这些工具）
{available_tools}

## 可用的 Sub-Agent 类型
- p2p_agent:     采购到付款全流程分析
- vendor_agent:  供应商绩效与管理
- finance_agent: 财务合规与报表
- rule_agent:    业务规则合规校验
- report_agent:  综合报告生成（必须是最后一个节点）

## 本体知识上下文（用于理解业务语义）
{ontology_context}

## 约束规则（严格遵守）
1. DAG 中不能有环（task A 依赖 task B，B 不能依赖 A）
2. 所有叶节点（无后继）必须汇入 report_agent 任务
3. 每个任务的 tool_name 必须来自"可用工具清单"
4. 数据采集任务必须放在分析任务之前
5. rule_agent 任务必须有对应的数据采集前置任务
6. 单个 DAG 任务数量不超过 12 个（避免过度拆解）
7. 高风险操作（写操作）任务必须标记 requires_approval: true

## 输出格式（纯 JSON，无其他文字）
{
  "dag_name": "描述性名称",
  "estimated_duration_sec": 15,
  "tasks": [
    {
      "task_id":    "t1",
      "name":       "简短任务名",
      "agent":      "p2p_agent",
      "tool_name":  "query_purchase_orders",
      "depends_on": [],
      "inputs": {
        "date_from": "{date_from}",
        "date_to":   "{date_to}",
        "filters":   {}
      },
      "output_key": "po_data",        // 该任务的输出键名，供后续任务引用
      "timeout_sec": 30,
      "requires_approval": false,
      "description": "采集指定时段采购订单数据"
    }
  ],
  "reasoning": "为什么这样设计的说明"
}

## 用户分析意图
场景：{scenario_description}
时间范围：{time_range}
重点关注：{focus_points}
分析深度：{analysis_depth}
模糊度：{ambiguity_score}（0=清晰 1=模糊）
"""

AVAILABLE_TOOLS = """
数据采集类：
- query_purchase_orders(date_from, date_to, vendor_id?, status?) → po_list
- query_goods_receipts(date_from, date_to, po_ids?) → gr_list
- query_vendor_invoices(date_from, date_to, status?, vendor_id?) → invoice_list
- query_vendor_master(vendor_ids?) → vendor_info
- query_material_master(material_ids?) → material_info
- get_vendor_scorecard(vendor_ids?, period_months) → scorecard

分析计算类：
- run_three_way_match(po_ids?, tolerance_pct) → match_result
- calculate_ppv(group_by, min_ppv_pct?, date_from, date_to) → ppv_result
- calculate_spend_analysis(group_by, date_from, date_to) → spend_data
- calculate_po_cycle_time(date_from, date_to) → cycle_times
- run_vendor_risk_scoring(vendor_ids) → risk_scores

合规校验类：
- validate_compliance(po_ids?, rule_types?) → violations
- check_approval_limits(po_ids?) → limit_violations
- check_blacklist(vendor_ids) → blacklist_hits

报告生成类：
- generate_summary_report(findings, kpis, scenario) → report  [仅 report_agent]
- generate_chart(chart_type, data, title) → chart_url         [仅 report_agent]
"""

class DynamicDAGGenerator:

    def __init__(self, rag_retriever):
        self.rag = rag_retriever

    async def generate(self, signal: QuerySignal) -> tuple[list[dict], str]:
        """
        动态生成 DAG，含重试和结构修复
        返回：(dag_tasks, reasoning)
        """
        # 检索相关本体知识作为上下文
        ontology_ctx = self.rag.retrieve(
            query=signal.raw_query,
            doc_types=["ontology_class", "kpi", "business_rule"],
            top_k=6
        )

        prompt = DAG_GENERATION_PROMPT.format(
            available_tools=AVAILABLE_TOOLS,
            ontology_context=format_ontology_docs(ontology_ctx),
            scenario_description=signal.raw_query,
            time_range=json.dumps(signal.time_range or {"start": "近30天"}),
            focus_points=", ".join(signal.keywords) or "全面分析",
            analysis_depth=signal.analysis_depth,
            ambiguity_score=signal.ambiguity_score,
            date_from=signal.time_range["start"] if signal.time_range else "30天前",
            date_to=signal.time_range["end"] if signal.time_range else "今天",
        )

        # 最多重试 3 次（DAG 校验失败时重新生成）
        last_error = None
        for attempt in range(3):
            if attempt > 0:
                # 把上次的错误反馈给模型，让它修正
                prompt += f"\n\n上次生成的 DAG 校验失败，错误：{last_error}\n请修正后重新输出。"

            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )

            raw = response.content[0].text.strip()
            # 清理可能的 markdown 代码块
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip().rstrip("```")

            try:
                dag_def = json.loads(raw)
                tasks   = dag_def["tasks"]

                # 结构校验
                validator = DAGValidator()
                is_valid, error = validator.validate(tasks)

                if is_valid:
                    return tasks, dag_def.get("reasoning", "动态生成")
                else:
                    last_error = error
                    print(f"[DynamicDAG] 第{attempt+1}次生成校验失败: {error}")

            except json.JSONDecodeError as e:
                last_error = f"JSON 解析失败: {e}"

        # 三次都失败，返回最小可行 DAG（安全兜底）
        print("[DynamicDAG] 三次生成均失败，使用最小可行 DAG")
        return self._minimal_fallback_dag(signal), "自动降级到最小分析方案"

    def _minimal_fallback_dag(self, signal: QuerySignal) -> list[dict]:
        """
        最后的安全兜底：一个极简的数据采集+报告 DAG
        确保系统永远有响应，不会完全失败
        """
        return [
            {
                "task_id":   "t1",
                "name":      "采集采购订单",
                "agent":     "p2p_agent",
                "tool_name": "query_purchase_orders",
                "depends_on": [],
                "inputs": {
                    "date_from": signal.time_range["start"] if signal.time_range else None,
                    "date_to":   signal.time_range["end"]   if signal.time_range else None,
                },
                "output_key":       "po_data",
                "timeout_sec":      30,
                "requires_approval":False,
                "description":      "基础数据采集"
            },
            {
                "task_id":    "t2",
                "name":       "生成基础报告",
                "agent":      "report_agent",
                "tool_name":  "generate_summary_report",
                "depends_on": ["t1"],
                "inputs": {
                    "scenario":  signal.raw_query,
                    "note":      "由于请求较复杂，已降级为基础分析，请尝试更具体的描述"
                },
                "output_key":       "report",
                "timeout_sec":      60,
                "requires_approval":False,
                "description":      "基础摘要报告"
            }
        ]
```

---

## DAG 结构校验器：LLM 生成结果的守门人

LLM 生成的 DAG 不能直接执行，必须先通过严格的结构校验：

```python
# dag_validator.py

class DAGValidator:
    """
    对 LLM 生成的 DAG 进行结构合法性校验
    校验失败的结果会被反馈给 LLM 重新生成
    """

    VALID_TOOLS = {
        "query_purchase_orders", "query_goods_receipts",
        "query_vendor_invoices", "query_vendor_master",
        "run_three_way_match", "calculate_ppv",
        "validate_compliance", "get_vendor_scorecard",
        "calculate_spend_analysis", "calculate_po_cycle_time",
        "run_vendor_risk_scoring", "check_approval_limits",
        "check_blacklist", "generate_summary_report", "generate_chart"
    }

    VALID_AGENTS = {
        "p2p_agent", "vendor_agent", "finance_agent",
        "rule_agent", "report_agent"
    }

    # 工具 → 必须归属的 Agent（防止越权调用）
    TOOL_AGENT_MAP = {
        "generate_summary_report": "report_agent",
        "generate_chart":          "report_agent",
        "validate_compliance":     "rule_agent",
        "check_approval_limits":   "rule_agent",
        "check_blacklist":         "rule_agent",
    }

    def validate(self, tasks: list[dict]) -> tuple[bool, str | None]:
        """
        全量校验，返回 (是否通过, 错误原因)
        """

        # ── 1. 基础结构校验 ──────────────────────────────────────
        if not tasks:
            return False, "任务列表不能为空"
        if len(tasks) > 12:
            return False, f"任务数量 {len(tasks)} 超过上限 12"

        task_ids = [t.get("task_id") for t in tasks]
        if len(task_ids) != len(set(task_ids)):
            return False, "存在重复的 task_id"

        # ── 2. 工具和 Agent 合法性 ───────────────────────────────
        for t in tasks:
            if t.get("tool_name") not in self.VALID_TOOLS:
                return False, f"非法工具 '{t.get('tool_name')}'"
            if t.get("agent") not in self.VALID_AGENTS:
                return False, f"非法 Agent '{t.get('agent')}'"

            # 工具与 Agent 归属检查
            required_agent = self.TOOL_AGENT_MAP.get(t["tool_name"])
            if required_agent and t["agent"] != required_agent:
                return False, (
                    f"工具 '{t['tool_name']}' 必须由 '{required_agent}' 执行，"
                    f"但分配给了 '{t['agent']}'"
                )

        # ── 3. 依赖关系合法性 ────────────────────────────────────
        id_set = set(task_ids)
        for t in tasks:
            for dep in t.get("depends_on", []):
                if dep not in id_set:
                    return False, f"任务 '{t['task_id']}' 依赖不存在的任务 '{dep}'"

        # ── 4. 环检测（拓扑排序） ──────────────────────────────
        has_cycle, cycle_path = self._detect_cycle(tasks)
        if has_cycle:
            return False, f"检测到循环依赖: {' → '.join(cycle_path)}"

        # ── 5. 报告节点必须是终点 ────────────────────────────────
        report_tasks = [t for t in tasks if t["agent"] == "report_agent"]
        if not report_tasks:
            return False, "DAG 必须包含至少一个 report_agent 任务"

        # 报告任务不能被其他任务依赖（必须是叶节点）
        all_deps = {dep for t in tasks for dep in t.get("depends_on", [])}
        for rt in report_tasks:
            if rt["task_id"] in all_deps:
                return False, (
                    f"报告任务 '{rt['task_id']}' 不能被其他任务依赖"
                    f"（必须是 DAG 的终点）"
                )

        # ── 6. 分析任务必须有数据依赖 ───────────────────────────
        analysis_tools = {
            "run_three_way_match", "calculate_ppv",
            "calculate_spend_analysis", "run_vendor_risk_scoring"
        }
        data_tools = {
            "query_purchase_orders", "query_goods_receipts",
            "query_vendor_invoices", "query_vendor_master",
            "get_vendor_scorecard"
        }
        # 找出所有数据任务的 id
        data_task_ids = {
            t["task_id"] for t in tasks
            if t["tool_name"] in data_tools
        }
        for t in tasks:
            if t["tool_name"] in analysis_tools:
                if not set(t.get("depends_on", [])) & data_task_ids:
                    return False, (
                        f"分析任务 '{t['task_id']}' ({t['tool_name']}) "
                        f"没有依赖任何数据采集任务"
                    )

        return True, None

    def _detect_cycle(self, tasks: list[dict]) -> tuple[bool, list]:
        """DFS 环检测"""
        graph = {t["task_id"]: set(t.get("depends_on", [])) for t in tasks}
        visited, in_stack, path = set(), set(), []

        def dfs(node):
            visited.add(node)
            in_stack.add(node)
            path.append(node)
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in in_stack:
                    path.append(neighbor)
                    return True
            path.pop()
            in_stack.discard(node)
            return False

        for node in graph:
            if node not in visited:
                if dfs(node):
                    return True, path

        return False, []
```

---

## 将成功的 DAG 案例写入 RAG，形成自学习闭环

每次 LLM 动态生成的 DAG 成功执行后，自动沉淀到 RAG 历史案例库，供下次相似请求直接复用：

```python
# dag_case_store.py

class DAGCaseStore:
    """
    成功执行的 DAG 案例存储器
    形成自学习闭环：执行成功 → 存入 RAG → 下次相似请求直接命中
    """

    def __init__(self, rag_builder):
        self.rag = rag_builder

    async def store_successful_case(
        self,
        signal:       QuerySignal,
        dag:          list[dict],
        route_type:   str,
        exec_result:  dict,
        user_feedback:float | None = None  # 用户满意度 0-1，可选
    ):
        """
        将成功执行的 DAG 案例写入 RAG
        只存储真正成功的案例（所有任务完成 + 无严重错误）
        """
        # 只有真正成功的案例才存
        if exec_result.get("status") != "completed":
            return
        if exec_result.get("failed_tasks", 0) > 0:
            return

        case_doc = {
            "doc_id":   f"dag_case_{hashlib.md5(signal.raw_query.encode()).hexdigest()[:8]}",
            "doc_type": "successful_dag_case",
            "module":   "PROCUREMENT",
            "text": f"""
成功案例：{signal.raw_query}
关键词：{', '.join(signal.keywords)}
分析场景：{signal.domain_modules}
时间范围：{signal.time_range}
分析深度：{signal.analysis_depth}
执行结果：{exec_result.get('summary', '分析完成')}
任务数：{len(dag)}
执行时长：{exec_result.get('duration_sec', 0)}秒
            """.strip(),
            "dag_definition": dag,
            "case_name":      signal.raw_query[:50],
            "success_score":  user_feedback or 0.8,
            "created_at":     str(date.today()),
            "route_type":     route_type,  # 记录是静态/改编/动态生成的
        }

        # 向量化并写入 RAG
        await self.rag.index_single_document(case_doc)
        print(f"[CaseStore] 案例已沉淀: {case_doc['case_name']}")
```

---

## 完整流程串联：从请求到 DAG 执行

```python
# orchestrator_full.py

async def orchestrate(user_request: str, session_id: str) -> dict:
    """Orchestrator 完整入口，含三级路由和自学习"""

    rag      = ProcurementRAGBuilder()
    router   = IntentRouter(rag, DynamicDAGGenerator(rag))
    store    = DAGCaseStore(rag)
    executor = DAGExecutor()

    # Step 1: 信号提取（含模糊度评分）
    try:
        signal = await extract_query_signal(user_request)
    except ClarificationRequired as e:
        # 模糊度过高，先追问用户
        return {
            "status":   "need_clarification",
            "question": e.question,
            "hint":     "请提供更具体的信息，例如时间范围或关注的供应商"
        }

    # Step 2: 三级路由
    route = await router.route(signal)
    print(f"[Orchestrator] 路由类型: {route.route_type} "
          f"置信度: {route.confidence} | {route.reasoning}")

    # Step 3: 执行 DAG
    result = await executor.execute(route.dag, signal)

    # Step 4: 成功后沉淀案例（仅动态生成或改编的案例才沉淀，静态规则不需要）
    if result["status"] == "completed" and route.route_type in ("DYNAMIC", "ADAPTED"):
        await store.store_successful_case(
            signal=signal,
            dag=route.dag,
            route_type=route.route_type,
            exec_result=result
        )

    return {
        "status":     result["status"],
        "route_type": route.route_type,
        "reasoning":  route.reasoning,
        "report":     result.get("report"),
        "duration":   result.get("duration_sec")
    }
```

---

## 三条路径的典型触发场景对比

| 用户请求 | 路由路径 | 原因 |
|---|---|---|
| "分析Q1三路匹配异常" | 静态 DAG | 关键词"三路匹配"命中率 70%，高于阈值 |
| "看看最近供应商送货怎么样" | RAG 改编 | 无精确关键词，但"供应商+送货"在历史案例中有 0.85 相似度案例 |
| "分析哪些采购员的价格谈判能力弱" | LLM 动态生成 | 全新视角，规则库和历史案例均无匹配，触发 LLM 规划 |
| "给我看看采购数据" | 追问澄清 | 模糊度评分 0.82，完全无法确定分析方向 |

**核心设计哲学：** 三级降级不是三选一，而是从确定到不确定的渐进式探索。每一级失败都会把更多上下文（本体知识、历史案例、失败原因）带入下一级，使最终生成的 DAG 越来越有针对性。成功执行后的自动沉淀则让系统随使用时间变得越来越少依赖 LLM 动态生成，越来越多命中精确路由。