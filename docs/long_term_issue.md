# 长期记忆设计问题分析

## 12 个问题的结论

1. **总开关缺失**：`MemorySettings` 没有 `long_term_enabled`，`P2PAgent.analyze` 无条件读写 LTM。`chroma.enable_long_term_indexing` 只控向量旁路，不控 SQL 主路径。→ 加 `memory.long_term.enabled` 字段 + agent 两处 gate。

2. **LIKE 召回几乎不命中**：`search_memories` 用 `content LIKE '%query%'`，而 query 是整句自然语言（"最近一个月有没有三路匹配异常"），content 存的是 `Q: ... A: ...` 的英文/中文混合摘要，字面重叠概率极低。→ 改为 PostgreSQL 全文检索（`tsvector` + `GIN`）或彻底切到向量召回 + 关键词作为辅助过滤。

3. **无上限 / 无淘汰**：`save_memory` 每轮 analyze 必写一行，`memories` 表只增不减，没有 TTL、没有按 user_id 的 cap、没有去重。长跑后表会膨胀且召回质量下降（老的无关结论挤占）。→ 引入 `(user_id, memory_type)` 维度的滚动上限（如 200 条），超出按 created_at 删除；或加 TTL 字段定期清理。

4. **写入策略粗放**：每次 analyze 无条件 `save_memory(content=Q+A[:1500])`，不判断本轮是否真的有价值（是否检出异常、是否 LLM 失败、是否重复 query）。→ 至少加一个"有 anomalies 才写"或"与最近一条相似度高就跳过"的过滤，避免噪声入库。

问题 4 重新评估(生产版本)
生产视角下的额外要求
之前 MVP 版的方案有几处不达标:

静默 skip:跳过写入只打 _logger.debug → 生产应该是 INFO + 带原因码,且要可观测
没有 metric:被跳过多少次、按原因分布是多少 → 没有数据,没法运营
去重判定太弱:仅靠 "query 字符串相等" → 用户多打一个空格就绕过
去重逻辑放 agent 层:其他 caller(orchestrator、未来 module)会重复实现 → 应下沉到 LongTermMemory
过滤 2(无 anomalies 跳过)被我武断否决:生产场景应该配置化而不是写死,让运营可调
没有"近似重复"的内容指纹:生产应该用 content hash 而不是依赖 metadata.query
生产版方案
写入策略下沉到 LongTermMemory.save_memory
原则:调用方只管"我有这个内容,你看着办",过滤策略由 LTM 集中管理,所有 caller 受益。
save_memory 的返回值变化:

成功写入 → 返回 memory_id: str
被跳过 → 返回 None,并记录 skip 原因到日志 + metric
三层过滤(全部配置化)
层	触发条件	配置项	默认	关闭方式
L1 内容长度	len(content.strip()) < min_content_len	min_content_len	50	0
L2 内容指纹去重	同 user_id + 同 content_hash 在窗口内已存在	dedupe_window_seconds	900(15 分钟)	0
L3 低价值过滤	metadata.skip_if_empty=True 且 metadata 中 anomaly_count==0 且 summary 空	skip_empty_conclusions	false(默认不启用)	false
L2 的内容指纹:

content_hash = sha256(normalize(content))[:16]
normalize:re.sub(r'\s+', ' ', content).strip().lower() —— 消除空格/大小写差异
在 memories_table 加列 content_hash CHAR(16),加索引 (user_id, content_hash, created_at DESC)
写入时先查"该 user_id 下、相同 content_hash、created_at > now() - window"是否存在,存在则 skip
表结构变更(走 alembic 迁移)

op.add_column('memories', sa.Column('content_hash', sa.String(16), nullable=True))
op.create_index(
    'memories_user_hash_created',
    'memories',
    ['user_id', 'content_hash', sa.text('created_at DESC')],
)
回填:对老数据,一次性脚本计算并填充 content_hash(UPDATE ... SET content_hash = ...)。生产环境数据量小可一次跑完;数据量大用批次。

可观测性接入
_logger.info("ltm.save.skipped", reason=..., user_id=..., memory_type=...)(结构化日志)
计数器(放 core/observability 或简单的模块级 Counter):
ltm_save_total{result="written"}
ltm_save_total{result="skipped",reason="too_short"}
ltm_save_total{result="skipped",reason="duplicate"}
ltm_save_total{result="skipped",reason="empty_conclusion"}
不直接接入 prometheus(那是问题 10 的范围),先用 logger.info 结构化日志兜住,问题 10 一并升级
Agent 端改动
删除 if not content/手工过滤逻辑
直接调 save_memory(...),把 anomaly_count / summary 放进 metadata
不再判断返回值(None 也可接受),日志由 LTM 自己打
配置(MemorySettings)

long_term_min_content_len: int = 50
long_term_dedupe_window_seconds: int = 900
long_term_skip_empty_conclusions: bool = False
config.yaml 同步加 memory.long_term.{min_content_len, dedupe_window_seconds, skip_empty_conclusions},from_yaml 扁平化映射加 3 行。

测试矩阵(生产标准)
单元测试(tests/unit/test_long_term_memory.py):

用例	验证点
test_save_skipped_too_short	content < min_content_len 返回 None,DB 无新行
test_save_skipped_too_short_disabled	min_content_len=0 时,短内容也写入
test_save_dedupe_within_window	同 content 15 分钟内写两次,第二次 skip
test_save_dedupe_normalizes_whitespace	内容仅空白/大小写差异也被识别为重复
test_save_dedupe_after_window	超出窗口,相同内容可再次写入
test_save_dedupe_isolated_per_user	同内容不同 user 互不影响
test_save_dedupe_disabled	window=0 时不去重
test_save_skip_empty_conclusion	metadata.anomaly_count=0 且 summary 空 → skip
test_save_skip_empty_disabled_by_default	默认 False,不触发
test_content_hash_normalization	_normalize_content / _content_hash 单元
test_save_returns_none_on_skip	跳过时返回 None,正常写入返回 uuid
集成测试(tests/integration/test_long_term_memory_pg.py 新文件,可选,需 PG):

端到端验证 content_hash 索引被使用(EXPLAIN)
并发写入同 content 的去重正确性
Agent 测试(tests/unit/test_agent_extra.py):

移除/调整 test_analyze_long_term_memory_enabled —— save_memory 现在可能返回 None,不能 assert called(但 mock 默认仍 called,只是返回值变化)
数据迁移路径(生产)
新增 alembic 迁移文件 migrations/versions/xxxx_add_memories_content_hash.py
up: add column + index + 回填 SQL
down: drop index + drop column
回填 SQL(放在 up 里):

UPDATE memories
SET content_hash = SUBSTRING(MD5(LOWER(REGEXP_REPLACE(content, '\s+', ' ', 'g'))), 1, 16)
WHERE content_hash IS NULL;
注意:Python 端用 sha256,SQL 回填用 md5(避免在 PG 里装额外扩展),可以接受不一致——回填出来的老 hash 永远不会和未来的新 hash 碰撞,只是老数据之间的去重略弱。或者用 python 脚本回填以保持一致(更稳)。
init_db.py 不再幂等创建表,改为"检查是否有 alembic 版本表,无则提示运行迁移"
不做的事(明确边界)
❌ 不引入 LLM 自评质量打分(成本/延迟不值得)
❌ 不做向量相似度去重(content_hash 已够,语义去重交给问题 5 的结构化字段)
❌ 不做"近义查询识别"(GLM 能识别"分析三路匹配"和"看一下三路匹配怎么样"是同义,但去重要的是"结论",不是"问题",结论由 LLM 输出后用 hash 兜住)
决策点(生产版需要你拍板)
alembic 迁移:项目当前似乎没接入 alembic(create_all 模式),要不要这次顺带把 alembic 引入?

选 A:这次引入 alembic(投入更大,但符合生产标准,后续所有问题的 schema 变更都受益)
选 B:继续 create_all + 一次性 SQL 脚本(快,但违反 CLAUDE.md 刚写的"不依赖 create_all 兜底")
我推荐 A,既然刚写了"生产标准",就一次到位
skip_empty_conclusions 默认值:False(保守,保留所有结论)还是 True(激进,过滤无价值结论)?

我推荐 False——"分析后无异常"对运营也有价值,默认不该被丢
content_hash 列宽:CHAR(16)(sha256 前 16 字符,够用)还是 CHAR(64)(完整)?

我推荐 CHAR(16),16 字符已经 64 bit,碰撞概率可忽略,索引更小
请就这三点给反馈,确认后实施。

5. **写入内容结构化不足**：content 是裸字符串 `Q: ...\nA: ...`，metadata 只放了 `{"query": query}`，丢掉了 analysis_type / anomaly_count / time_range / 关键供应商或 PO 号。后续既无法按结构筛，也无法按维度去重。→ metadata 落 `analysis_type / anomaly_count / time_range / entities`，content 拆成结构化字段或 JSON 摘要。

6. **双轨召回未去重未排序**：`search_memories`（SQL）和 `search_memories_semantic`（向量）结果直接 `extend`，可能同一 memory_id 出现两次，且 SQL 按 created_at 排、向量按 distance 排，合并后顺序混乱，top-N 截断不公平。→ 按 memory_id 去重 + RRF（reciprocal rank fusion）或加权融合后再截断。

7. **自建 engine 不复用统一数据库层**：`LongTermMemory.__init__` 自己 `create_engine(dsn, pool_size=5, max_overflow=10)`，跟 `core/database/engine.py` 完全平行，违反 CLAUDE.md 写明的"统一数据库层"原则。连接池被分裂、配置被硬编码、测试时无法注入 SQLite engine。→ 改为接收 `Engine` 注入，由 `get_engine(settings.postgresql)` 统一提供。

8. **`metadata` 列名反模式**：SQLAlchemy Declarative 里 `metadata` 是保留属性名（MetaData 对象），Core Table 虽然能用，但任何后续往 ORM 模型迁移时都会冲突；而且 PG 里 `metadata` 也是常见保留语义，可读性差。→ 改名 `meta` / `extra` / `attributes`，迁移用 alembic。

9. **索引策略不匹配查询形态**：表上只有 `user_id`、`session_id` 两个单列 index，但实际查询是 `WHERE user_id = ? AND content LIKE '%...%' ORDER BY created_at DESC`。LIKE 用不上 btree，排序也用不上 user_id 单列索引。→ 加复合索引 `(user_id, created_at DESC)`，以及 content 的 `GIN (to_tsvector(...))` 全文索引（配合问题 2 改造）。

10. **异常全静默**：`save_memory` / `search_memories` 失败都被 `except Exception: pass` 或 `_logger.debug` 吞掉，没有 metric、没有告警、没有 trace span。Postgres 宕机时表面一切正常，实际 LTM 整体失效且无人知道。→ 至少接入 observability middleware，把每次 LTM 调用作为一个 span 落库，失败时打 WARNING 而非 DEBUG；新增 `ltm_save_failures_total` / `ltm_search_failures_total` 计数。

11. **reports 与 memories 职责混杂**：同一个 `LongTermMemory` 类既管"可召回的会话记忆片段"，又管"完整业务报告归档"，二者生命周期、读模式、留存策略完全不同（memory 要 TTL/淘汰，report 要长期归档且按 id 检索）。耦合导致问题 3/4 的方案没法只针对 memory 改。→ 拆成 `MemoryRepository` 与 `ReportRepository` 两个类，共享 engine 不共享语义。

12. **单例 + vector_store 永久降级**：`get_long_term_memory()` 是模块级懒单例，VectorStore 初始化失败一次就**永久**为 None，直到进程重启。Chroma 临时不可用（磁盘满、目录被锁）恢复后，语义召回也起不来。→ 把 vector_store 改成"每次使用前检查 + 失败后带退避的重试"，或注册一个后台 health-check 重新挂载。



## 长期存储方案
长期记忆常见的存储方式有哪几种，这几种感觉都不是最优方案
你说得对——前面的方案都在"修补 LIKE",没跳出当前的存储范式。退一步看,业界长期记忆(LLM Agent memory)主流的存储形态其实有几大类,各自的设计哲学完全不同:

长期记忆的常见存储范式
1. 纯关系型 + 关键词检索(当前方案的原型)
代表:自建 PG/MySQL + LIKE / FTS / trigram
哲学:把记忆当"日志",按时间和关键词捞
适用:结构化字段为主、需要精确匹配、数据量小
短板:语义检索弱,自然语言 query 命中率低
2. 纯向量库
代表:Chroma / Qdrant / Milvus / Weaviate / pgvector
哲学:记忆 = embedding,查询 = 相似度
适用:自然语言 query、跨表达方式召回
短板:精确实体(PO 号、ID)召回不稳;无法按结构化字段筛选(部分支持 metadata filter,但弱);冷启动嵌入成本
3. 混合检索(Hybrid Search)★ 业界事实标准
代表:
pgvector + PG FTS / pg_trgm —— 一个 PG 实例搞定
Qdrant / Weaviate / Elasticsearch 的 hybrid 模式 —— 内置 BM25 + 向量
OpenSearch / Vespa
哲学:稀疏检索(BM25/关键词)+ 稠密检索(向量)并行,用 RRF / 加权融合排序
适用:几乎所有 LLM 记忆场景的默认选择
优点:精确召回 + 语义召回都强,失败模式互补
本项目契合度:★★★★★ —— 你已经有 PG + Chroma,只需把 Chroma 换 pgvector 就能用一个库做 hybrid
4. 知识图谱型记忆
代表:Zep、Mem0、Graphiti、Neo4j + LLM 抽取
哲学:从对话里抽取"实体-关系-时间"三元组存图,查询时按图遍历 + 时间过滤
特点:
记忆有时效性(Zep 的 temporal knowledge graph)
能回答"某供应商最近的问题是什么"这种关系性查询
自动去重、自动更新过期事实
适用:多轮对话连续性、用户画像演化、需要"事实更新"语义
短板:抽取链路重(LLM 调用成本)、调试难
本项目契合度:★★★★ —— 项目已经有 Neo4j,但当前只用于 ERP 实体图,不是记忆图;引入会增加 LLM 调用
5. 分层记忆(Hierarchical / Episodic + Semantic)
代表:MemGPT / Letta、Generative Agents(斯坦福小镇论文)
哲学:模仿人类记忆——
Episodic(情景记忆):原始对话/事件,按时间存
Semantic(语义记忆):从情景中抽取的稳定结论
Working memory:当前对话窗口
后台进程定期把 episodic 压缩进 semantic,做"记忆整理"
特点:有"记忆衰减/巩固"机制,能长期运行不爆炸
适用:超长会话、Agent 持续运行数月的场景
短板:架构最复杂,需要后台 worker
6. KV / 文档型 + 标签索引
代表:Redis / MongoDB / DynamoDB
哲学:记忆按 user_id / session_id 组织,精确字段查找
适用:用户偏好、配置项类记忆;不适合内容召回
短板:没有相似度概念
7. 托管记忆服务
代表:Mem0、Zep Cloud、LangMem(LangChain)、Letta
哲学:把上面的 3/4/5 打包成 SDK,自动做抽取、去重、召回
优点:零造轮子,几行代码接入
短板:外部依赖、数据出域、定制空间小
这几种映射到本项目
范式	现状契合	改造成本	适合本项目?
1. 纯 SQL+LIKE	当前实现	—	已证明不行
2. 纯向量	部分(Chroma 旁路)	中	单一会失败
3. Hybrid(pgvector+FTS)	PG 已在用	中	★★★★★
4. 知识图谱(Zep/Mem0 风格)	Neo4j 已在但用途不同	大	★★★(后期)
5. 分层记忆(MemGPT)	无	很大	★★(过早)
6. KV/文档	无	中	用途不匹配
7. 托管服务(Mem0 等)	无	小	★★★(若可外部依赖)
我的真实建议
目标方案:Hybrid 检索,落到 pgvector

具体形态:

存储:把 memories 表加一列 embedding vector(384)(pgvector),Chroma 那条旁路废弃或保留作降级
写入:save_memory 时同步算 embedding 写入同一行(一个事务,不再有"双写不一致")
检索:一条 SQL 同时拿到 BM25/FTS 候选 + 向量候选,用 RRF 融合排序
好处:
一个数据库搞定关系字段 + 全文 + 向量,问题 6(双轨未去重)、问题 7(自建 engine)、问题 12(向量库降级)一起被解决
事务一致性,不会出现 SQL 写成功向量没写
pgvector 是 PG 官方扩展,运维成本低于 pg_trgm + Chroma 两套
这是 2024 年以后业界 LLM 记忆的事实默认架构(LangChain、LlamaIndex 文档里的首选示例都是 pgvector)
进一步,如果想要"记忆会演化/有时效" —— 那就在 pgvector 之上引入 Mem0 或自己实现轻量的"记忆抽取+去重",对应范式 4。但这是问题 4(写入策略粗放)和问题 5(结构化不足)的范畴,可以分阶段做。

决策点
请确认两件事,我就能给出最终方案:

运维能否装 pgvector 扩展?(标准 PG 扩展,Docker 镜像 pgvector/pgvector:pg16 直接可用)
是否接受废弃 Chroma 这条旁路?(简化为单一存储)
如果两个都 yes → 走 pgvector hybrid,这是一次性解决问题 2/6/7/12 四个问题的方案,而不是只补问题 2。
如果有一个 no → 退回到方案 B(pg_trgm)或 F(jieba+FTS),只解决问题 2 本身。



有没有不依赖pgvector的hybrid方案，我感觉pgvector无法实施
有。Hybrid 检索 ≠ 必须用 pgvector——hybrid 的本质是"两路召回 + 融合排序",两路可以分别落在不同存储里。下面是不依赖 pgvector 的几种实现方式。

不依赖 pgvector 的 Hybrid 方案
方案 H1:PG(关键词路) + Chroma(向量路) + 应用层 RRF 融合 ★推荐
这其实是当前架构稍加改造就能达到的形态,你已经具备所有零件。

架构:

稀疏路:memories 表 + PG FTS(tsvector+GIN)或 pg_trgm
稠密路:Chroma collection memory_long_term(已存在)
融合:Python 端拿到两路的 (id, rank) 列表,用 Reciprocal Rank Fusion 合并去重排序
RRF 公式(简单到 10 行 Python 就能写完):


def rrf_fuse(rank_lists, k=60):
    scores = {}
    for ranks in rank_lists:
        for rank, doc_id in enumerate(ranks):
            scores[doc_id] = scores.get(doc_id, 0) + 1 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])
流程:

search_memories(user_id, query, limit)
并发(或顺序)发起:PG FTS 查 top-20 候选 id、Chroma 查 top-20 候选 id
RRF 融合 → 截断到 limit
用融合后的 id 列表回 PG 拿完整行(SQL WHERE id = ANY(:ids))
save_memory
PG 写主行
Chroma 写 embedding(已有逻辑,移到主路径而非 best-effort)
优点:

零新依赖:PG 你有、Chroma 你有
改动小:search_memories 重写一次,加一个 _rrf 工具函数,加 PG FTS 索引
失败模式清晰:任何一路挂掉,降级为另一路单路召回(配置控制)
直接解决问题 2(命中率)+ 问题 6(双轨未融合)
缺点:

双写一致性需要管(PG 写成功 Chroma 写失败时怎么办)——见下方"一致性处理"
两个存储,运维上比单 pgvector 略重(但你本来就在维护这两个)
一致性处理(简单可接受):

写入:PG 是主,Chroma 是辅;Chroma 写失败 → 记 vector_dirty=true 标志位 + 后台 worker 重试。MVP 阶段甚至可以容忍 Chroma 偶尔丢条目,反正还有 FTS 路兜底
读取:任何一路超时/异常,退化为单路,不阻塞主流程
方案 H2:PG(FTS + pg_trgm)+ Chroma + 融合
同 H1,但稀疏路用 pg_trgm 代替 FTS
中文场景比 FTS simple 配置更好
需要装 pg_trgm 扩展(比 pgvector 容易很多,几乎所有 PG 默认支持)
方案 H3:纯 Chroma 双路(BM25 + Vector)
Chroma 0.4+ 内置 where_document 全文过滤,但没有原生 BM25 排序,只能做布尔 contains。所以 Chroma 单库做不了真正的 hybrid。不推荐,列出来只是排除。

方案 H4:换稠密路存储,稀疏路保持 PG
把 Chroma 换成 Qdrant 或 Weaviate(内置 hybrid 支持,自带 BM25)
但这等于引入新服务,违背"不想动基础设施"的初衷
不推荐
方案 H5:PG FTS + 客户端嵌入存到 PG bytea 列 + Python 端 numpy 算相似度
不用 pgvector,把 embedding 序列化成 bytea 存 PG,查询时把候选行的向量全拉到 Python 算余弦相似度
优点:零扩展,单库
缺点:不可扩展——召回时要把候选集全部拉回内存,数据量一上来就崩
只在数据量极小(<1 万条)时可用,不推荐
推荐:H1(PG FTS + Chroma + 应用层 RRF)
理由:

真的零新依赖——你列的所有零件都已经在跑
改动量比 pgvector 方案还小(不用动表结构 schema、不用迁移数据)
一次性解决问题 2 + 问题 6
顺带把 Chroma 旁路从"best-effort 静默降级"提升到"主路径之一",问题 12(单例永久降级)的隐患也大幅降低
未来如果想合并到 pgvector,这套 RRF 融合代码完全可以复用,只是把"两路 SQL 调用"换成"一路 SQL"
如果选 H1,具体改动清单(给你预览)
core/memory/tables.py
给 memories_table.content 增加 PG FTS 索引(只在 PG dialect 下创建,SQLite 测试跳过)
core/memory/long_term.py
新增 _rrf_fuse(...) 工具函数
重写 search_memories:并发跑 PG FTS + Chroma → RRF → 回表取行
删除独立的 search_memories_semantic(合进主方法,问题 6 顺带解决);或保留作为"只走向量"的调试入口
save_memory:Chroma 写入从 best-effort 提升为"失败要 WARN 日志 + 标脏",但仍不阻塞 PG 主写
tests/unit/test_long_term_memory.py
新增 hybrid 召回用例(mock Chroma + 真 SQLite FTS 退化路径)
新增 RRF 单元测试
配置
memory.long_term.hybrid_enabled: true(默认 true)
memory.long_term.fusion_k: 60(RRF 的 k 参数)
决策
请确认两件事:

PG 上能不能 CREATE INDEX ... USING GIN (to_tsvector('simple', content))?——这是标准 PG 功能,无需扩展,几乎肯定可以
是否接受 Chroma 仍然是依赖(只是从旁路提升为主路径之一)?
如果两个都 yes → 走 H1,我开始实施。
如果想更好的中文召回 → 在 H1 基础上加装 pg_trgm 变成 H2(pg_trgm 安装比 pgvector 容易得多,Debian/RDS 都默认支持)。




问题 3:长期记忆数量无上限 / 无淘汰
现状
save_memory 每次 analyze 必写一行
memories 表只增不减,无 TTL、无 cap、无去重
长跑后果:表膨胀(GIN 索引也膨胀)、召回质量下降(老的无关结论挤占 RRF top-N)、向量库 collection 也同步膨胀
设计目标
有上限:避免无限增长
保留有价值的记忆:不能简单按时间砍头(可能砍掉关键的早期结论)
逻辑简单可控:不要引入后台 worker / 调度器(MVP 阶段)
PG 与向量库一致:删 PG 也要删 Chroma 对应文档
三个候选方案
方案 A:按 user 滑动窗口 cap(推荐)
机制:每次 save_memory 写入后,检查该 user_id + memory_type 维度的总数,超过 max_per_user(如 200)就删除最老的若干条
优点:
简单,纯 SQL,一次额外查询 + 一次条件 DELETE
用户隔离天然保证(一个用户暴写不会挤掉其他用户)
写路径同步完成,无需后台任务
缺点:
严格按时间淘汰,无法保留"老但重要"的记忆
在 save_memory 路径上加一次 DELETE,写入轻微变慢
失败模式:DELETE 异常时不影响 INSERT 主路径(try/except 包裹)
方案 B:TTL(基于 created_at)
机制:加 expires_at 字段(可空),save_memory 写入时设置默认 TTL(如 90 天),search_memories 在查询时加 WHERE expires_at IS NULL OR expires_at > now(),另外提供 purge_expired() 方法供管理端调用
优点:
时间维度淘汰符合"记忆衰减"直觉
不阻塞写路径
缺点:
需要外部调 purge_expired(cron / 管理 API),否则表照样膨胀
表结构变更(新增字段),需要迁移
"时间到了就过期"对低频用户不友好(一个月用一次的人记忆全没了)
方案 C:按 user cap + 写入端去重
机制:在方案 A 基础上,save_memory 写入前先看最近 N 条里有没有"高度相似"的(SQL 上简化为 content 前 100 字符相同 / 或同一 query 字段),有则跳过写入
优点:同时治理"无上限"和"重复写入"
缺点:
"相似"判断要么粗糙(prefix 匹配)要么贵(向量比对)
与问题 4(写入策略粗放)有重叠,不如把"去重"留给问题 4 专门处理
推荐:方案 A
理由:

最小改动——只动 save_memory 一处,加 ~10 行代码
MVP 友好——无新字段、无后台任务、无管理 API
失败模式安全——cap 删除失败不影响主写
向量库同步——删除 PG 行的同时调 vector_store.delete(ids=[...]),失败也吞掉
不与问题 4 冲突——去重逻辑留给问题 4
后续如果运营反馈"老记忆需要保留",再升级到"按 created_at + 重要性打分"的混合策略。

改动点
config/settings.py + config/config.yaml — MemorySettings 新增:


long_term_max_per_user: int = 200
YAML 加 memory.long_term.max_per_user: 200,扁平化映射加一行。
值为 0 时表示不限制(关闭 cap,作为关闭开关)。

core/memory/long_term.py — LongTermMemory.__init__ 接收 max_per_user: int = 0(默认 0 = 不限,向后兼容)。

save_memory 在 INSERT 完成后:


if self._max_per_user > 0:
    self._enforce_user_cap(user_id)
新方法 _enforce_user_cap(user_id):


DELETE FROM memories
WHERE id IN (
  SELECT id FROM memories
  WHERE user_id = :user_id
  ORDER BY created_at DESC
  OFFSET :max_per_user
)
RETURNING id
拿到被删的 id 列表
调 vector_store.delete(ids=[f"memory_{i}" for i in deleted_ids])(若有向量库),失败吞掉
整体 try/except,失败 _logger.debug 不阻塞主写
注意:PG 支持 DELETE ... RETURNING,SQLite 不支持 DELETE ... OFFSET 子查询,但支持 DELETE WHERE id IN (SELECT id ... LIMIT -1 OFFSET N)——SQLite 的 LIMIT -1 OFFSET N 语法可用。或者用更通用的两步:先 SELECT 出要删的 id,再 DELETE WHERE id IN (...)。两步法对 dialect 完全透明,更稳。

get_long_term_memory() 从 settings 读 max_per_user 注入。

测试(tests/unit/test_long_term_memory.py):

test_save_memory_enforces_cap:max_per_user=3,写 5 条同 user,断言只剩 3 条最新的
test_save_memory_cap_deletes_vector:同上 + mock vector store,断言被删 id 也调用了 vector_store.delete
test_save_memory_cap_zero_means_unlimited:max_per_user=0,写 10 条仍保留 10 条
test_save_memory_cap_isolated_per_user:user1 写 5 条、user2 写 5 条,max_per_user=3,两个用户各剩 3 条(不串扰)
不做的事
不引入 TTL / expires_at 字段(等运营有需求再加)
不做相似度去重(留给问题 4)
不引入后台 worker / cron(等数据量真大再加)
reports 表不做 cap(报告是审计资产,要长期保留;问题 11 也会论证 reports 与 memories 应该拆开)
影响面
默认 max_per_user: 200(配置文件)/ 0(代码默认,向后兼容):默认行为不变,只有显式开启才生效
写路径多一次 SELECT + DELETE(只在超 cap 时触发,通常无操作)
不动表结构
决策点
max_per_user 默认值该设多少?我建议:

代码里 LongTermMemory.__init__ 默认 0(完全向后兼容,测试也不受影响)
config.yaml 里默认 200(生产合理值)
确认后实施。如果你想要不同的默认值或者倾向方案 B/C,告诉我。


