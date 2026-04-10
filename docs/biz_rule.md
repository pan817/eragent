## 基于本体 + 大模型的 ERP 采购 Agent：Oracle EBS 工具与规则全景分析

---

## 一、Oracle EBS 采购域的模块版图

Oracle EBS 采购涉及五个核心模块，每个模块暴露不同的数据接口和业务规则：

**iProcurement（员工自助采购）** 是需求源头，员工通过购物车发起采购申请，内置了目录管理、预算检查、审批路由。这个模块的数据是 Agent 理解"需求意图"的起点。

**Purchasing（核心采购）** 是整个 P2P 的控制中枢，管理 PR→RFQ→PO 的完整流程。Oracle EBS Purchasing 模块的 API 层（PO_HEADERS_ALL、PO_LINES_ALL 等核心表）是 Agent 工具层最主要的数据来源。

**Payables（应付账款）** 负责发票验证和付款执行，三路匹配（Three-Way Match）的核心逻辑在此模块。Payables 中的 AP_INVOICES_ALL 和 AP_INVOICE_LINES_ALL 与采购数据的关联是合规分析的关键路径。

**Inventory（库存）** 提供收货凭证（Receiving）数据，RCV_TRANSACTIONS 和 RCV_SHIPMENT_LINES 是三路匹配的第三条腿。

**Supplier Portal / iSupplier** 是供应商侧的数据来源，供应商绩效、资质状态、银行信息都在这里维护。

---

## 二、可实现的工具清单（按业务场景分类）

### 2.1 采购数据查询类工具

**PO 状态追踪工具**：基于 PO_HEADERS_ALL 和 PO_DISTRIBUTIONS_ALL，能够查询任意采购订单的完整生命周期状态——从 INCOMPLETE 到 APPROVED、CLOSED FOR RECEIVING、FINALLY CLOSED 的每一步。Oracle EBS 的 PO 状态字段（AUTHORIZATION_STATUS、CLOSED_CODE）组合判断比单一字段复杂，本体需要把这些状态组合的语义翻译清楚，才能让 Agent 正确理解"一张 PO 是否已经可以付款"。

**采购申请追溯工具**：从 PR（PO_REQUISITION_HEADERS_ALL）出发，追溯到对应的 PO，再到 GR（收货），再到发票。Oracle EBS 中这条链路通过 PO_REQ_DISTRIBUTIONS_ALL 与 PO_DISTRIBUTIONS_ALL 的关联实现，链路追踪是 Agent 做根因分析的基础能力。

**供应商档案查询工具**：AP_SUPPLIERS 和 AP_SUPPLIER_SITES_ALL 存储供应商主数据。关键字段包括 HOLD_FLAG（是否被冻结）、VENDOR_TYPE_LOOKUP_CODE（供应商类型）、PAY_GROUP_LOOKUP_CODE（付款组）。当 Agent 发现一张发票被 HOLD 时，需要能查到是供应商级别的 HOLD 还是发票级别的 HOLD，两者的处理路径完全不同。

**价格历史分析工具**：PO_LINE_LOCATIONS_ALL 记录了每次采购的单价，结合 MTL_SYSTEM_ITEMS_B 中的 STANDARD_COST，可以计算 PPV（采购价格差异）。Oracle EBS 中标准成本存在组织层面（ORGANIZATION_ID 关联），跨组织的价格比对需要先统一成本视角。

**预算执行查询工具**：Oracle EBS 的 Budgetary Control 通过 GL_BC_PACKETS 和 GL_BALANCES 实现。Agent 在分析采购申请审批时，需要能查到当前预算余额，判断申请金额是否会导致超预算。这个工具的价值在于把财务视角引入采购流程分析。

**合同覆盖率分析工具**：POK_K_HEADERS_B（Oracle Contracts）记录了框架合同信息。采购订单是否关联了有效合同，合同价格与实际 PO 价格的偏差，是采购合规性分析的重要维度。没有合同支撑的大额采购是典型的内控风险信号。

### 2.2 三路匹配与发票验证工具

**三路匹配状态查询工具**：Oracle EBS 中三路匹配的结果存在 AP_INVOICE_DISTRIBUTIONS_ALL 的 MATCH_STATUS_FLAG 字段。但这个字段只反映最终状态，Agent 需要的是能深入到"为什么不匹配"的层面——是数量差异（RCV 收货数量 vs PO 订单数量）、还是价格差异（发票单价 vs PO 单价）、还是税务处理差异。本体需要把这三类差异的判断逻辑分别建模。

**发票 HOLD 分析工具**：AP_HOLDS 表记录了所有发票被 HOLD 的原因，Oracle EBS 内置了几十种 HOLD 类型（PRICE、QTY REC、QTY ORD、CANT CLOSE PO 等）。Agent 的工具需要把这些 HOLD 代码翻译成业务语言，并按根因分类（供应商问题、采购订单问题、收货问题），而不是直接把原始代码暴露给分析结论。

**发票到期日预测工具**：Oracle EBS 的付款条款存在 AP_TERMS 和 AP_TERMS_LINES，到期日的计算规则（基于发票日期、收货日期、或系统录入日期）因条款不同而异。Agent 需要一个能准确计算每张发票到期日的工具，而不是简单地用发票日期加固定天数——这种简化在 Oracle EBS 环境下会产生大量错误预测。

**重复发票检测工具**：AP_INVOICES_ALL 中的 VENDOR_ID + INVOICE_NUM + INVOICE_AMOUNT 组合是 Oracle EBS 的重复发票检测基准，但实际上同一张发票可能以不同金额多次录入（部分付款场景）。本体需要建立"同一发票"的精确定义，结合时间窗口和金额容忍度进行模糊匹配。

### 2.3 供应商绩效评估工具

**准时交货率计算工具**：Oracle EBS 收货模块的 RCV_SHIPMENT_HEADERS.SHIPPED_DATE 和 PO_LINE_LOCATIONS_ALL.NEED_BY_DATE 的对比是准时交货率的数据基础。但 Oracle EBS 允许 PO 行设置多个预计交货日期（通过 PO_LINE_LOCATIONS_ALL 的 PROMISED_DATE 和 NEED_BY_DATE 双字段），Agent 工具需要明确以哪个日期为准，这个判断逻辑应该来自本体规则而非工具内部的硬编码。

**供应商风险评分工具**：结合多维度数据——信用评级（自建或外部）、HOLD 历史、发票匹配率、价格稳定性、交货准时率——综合计算供应商风险分数。Oracle EBS 本身没有这个汇总视图，这是 Agent 能超越原生 ERP 的核心价值之一。

**供应商集中度分析工具**：分析采购金额在供应商之间的分布，识别过度依赖单一供应商的风险。Oracle EBS 的 PO_HEADERS_ALL.VENDOR_ID 关联是数据基础，但"集中度风险"的阈值定义（如单一供应商超过某品类采购额的 40%）需要在本体中显式定义，而不是让 Agent 自己判断。

### 2.4 合规与审计类工具

**审批路径合规性验证工具**：Oracle EBS 的审批历史存在 PO_ACTION_HISTORY，记录了每一步审批动作、审批人、时间戳。Agent 工具可以对比"实际审批路径"与本体中定义的"标准审批流"，识别越级审批、审批人与申请人为同一人（自我审批）等内控漏洞。

**职责分离检查工具**：Oracle EBS 的 FND_USER 和 FND_RESPONSIBILITY 记录了用户权限分配。采购领域的职责分离要求申请人不能同时是采购员、采购员不能同时审批自己下的 PO。这个检查在 Oracle EBS 标准功能中往往需要定制开发，而 Agent 可以通过分析 PO_ACTION_HISTORY 中的操作者与 FND_USER 的权限映射来自动实现。

**无合同采购识别工具**：识别超过金额阈值但没有关联框架合同的采购订单。Oracle EBS 中 PO 与 Contract 的关联通过 PO_HEADERS_ALL.CONTRACT_ID 字段体现，NULL 值配合金额阈值就能找出需要关注的无合同采购。

**历史价格基准对比工具**：同一物料（ITEM_ID）、同一供应商在过去 12 个月的采购历史价格，用于判断当前 PO 价格是否合理。Oracle EBS 的 PO_LINE_LOCATIONS_ALL 保存了完整的历史价格数据，这个工具让 Agent 能做到"价格合理性判断"，而不只是"价格差异计算"。

---

## 三、可实现的业务规则体系

### 3.1 采购流程合规规则

**PR-PO 关联完整性规则**：每一张超过授权金额的采购订单（Oracle EBS 中通过 PO_HEADERS_ALL.AUTHORIZATION_STATUS 和金额阈值判断）必须能追溯到至少一张已批准的采购申请。规则的边界条件需要明确：跨事业部采购、紧急采购绕过 PR 的例外场景如何处理，必须在本体中显式定义，否则 Agent 会产生大量误报。

**三级审批权限规则**：Oracle EBS 的采购审批权限（APPROVAL_AMOUNT_LIMIT）存在用户级别，但实际业务中往往有"组织层面的金额分段"——1万以下采购员可批，1万到10万部门经理批，10万以上总监批。本体需要把这个分段逻辑建模，Agent 才能识别审批权限超限的情况。

**竞价要求规则**：超过一定金额的采购必须经过询价（RFQ）和竞标流程。Oracle EBS 的 PO_HEADERS_ALL.QUOTE_TYPE_LOOKUP_CODE 记录了报价类型，但是否进行了充分竞价（至少几家供应商参与）需要结合 PO_QUOTATIONS_INTERFACE 来判断。这条规则的违反在 Oracle EBS 原生报表中很难发现，但对内控审计非常重要。

**供应商资质有效性规则**：采购订单不能下给资质已过期的供应商。Oracle EBS 的供应商资质管理往往通过自定义扩展表实现，本体需要把"有效资质"的定义标准化，Agent 工具才能做一致的检查。

### 3.2 三路匹配规则

**标准三路匹配容差规则**：Oracle EBS 中的三路匹配容差（PRICE_TOLERANCE、QTY_TOLERANCE）在 AP_SYSTEM_PARAMETERS_ALL 中配置。本体应该把这些系统配置的容差值引入，让 Agent 的分析与 Oracle EBS 的自动处理逻辑保持一致，避免 Agent 标记为"异常"的情况实际上已经在 EBS 的容差范围内被自动放行了。

**两路匹配例外规则**：服务类采购（非库存物料）通常只需要两路匹配（PO+Invoice），不需要 GR。Oracle EBS 中通过 PO_LINES_ALL.ITEM_TYPE = 'SERVICE' 区分。本体需要把"哪类采购适用几路匹配"的规则显式化，否则 Agent 会错误地对服务采购的"缺少 GR"报警。

**数量接受规则**：Oracle EBS 允许按发票数量（Invoice Match）或按收货数量（Receipt Match）进行匹配，具体设置在 PO_LINE_LOCATIONS_ALL.MATCH_OPTION 字段。Agent 必须先查询这个字段，再决定用哪种方式做匹配分析，而不是统一按一种方式处理。

**发票日期有效性规则**：发票日期不能早于 PO 日期，不能晚于当前会计期间关闭日期。Oracle EBS 的会计期间状态存在 GL_PERIOD_STATUSES，Agent 工具在分析发票时效性时需要联动查询。

### 3.3 供应商管理规则

**单一来源采购规则**：对于垄断性物料或特殊原因必须从单一供应商采购的情况，Oracle EBS 中通过 PO_HEADERS_ALL.SOLE_SOURCE_INDICATOR 标记。本体需要建立"单一来源采购的审批要求"规则——这类采购通常需要额外的业务说明和更高层级的审批。Agent 可以识别出没有单一来源标记但实际上只有一家供应商报价的采购，作为潜在的合规风险点。

**供应商付款条款一致性规则**：采购订单上的付款条款必须与供应商主数据（AP_SUPPLIERS.TERMS_ID）保持一致，或者有明确的差异说明。实践中经常出现采购员在 PO 上随意填写付款条款的情况，这既是内控风险也是供应商关系风险。

**黑名单和制裁筛查规则**：这是合规领域越来越重要的规则，要求所有供应商都要通过制裁名单筛查。Oracle EBS 本身没有内置这个功能，需要通过自定义表或外部系统集成实现。Agent 可以在分析新供应商或大额采购时触发这个检查。

**供应商绩效阈值告警规则**：准时交货率低于 85%、发票匹配率低于 95%、质量退货率高于 5% 的供应商需要进入改善计划。这些阈值应该在本体中定义，而不是硬编码在 Agent 逻辑里，这样业务方可以根据行业特点和内部政策调整阈值，而不需要修改代码。

### 3.4 财务控制规则

**预算可用性规则**：在 Oracle EBS 开启 Budgetary Control 的情况下，采购申请和采购订单的提交会触发预算检查（Funds Check）。Agent 可以在预算检查通过之前预测"当前所有待处理 PR/PO 占用了多少预算余额"，帮助预算管理者提前发现超预算风险，而不是等到 EBS 自动拒绝后再处理。

**付款折扣窗口规则**：很多供应商提供早付折扣（如 2/10 NET30，10天内付款享受 2% 折扣）。Oracle EBS 的 AP_TERMS_LINES 中记录了折扣条款，但实际执行中财务部门经常错过折扣窗口。Agent 可以主动识别即将错过折扣窗口的发票，并按折扣金额排序优先推送处理。

**汇率风险规则**：Oracle EBS 中涉及外币采购时，PO 汇率（PO_HEADERS_ALL.RATE）与发票录入时的实时汇率往往存在差异，产生汇兑损益。本体需要定义"汇率差异超过一定比例需要额外审批"的规则，Agent 可以在分析外币发票时自动触发这个检查。

---

## 四、本体层对 Oracle EBS 的映射价值

Oracle EBS 是一个极度复杂的系统——仅采购模块就涉及超过 200 张数据库表，字段命名晦涩（AUTHORIZATION_STATUS、CLOSED_CODE、MATCH_OPTION 等），状态机隐藏在枚举代码中，业务逻辑分散在多个模块之间。本体的核心价值正是在于把这些复杂性封装起来，为大模型提供一个干净的语义层。

具体体现在三个方面：

其一是**状态语义标准化**。Oracle EBS 的 PO 状态是 AUTHORIZATION_STATUS 和 CLOSED_CODE 的组合，"可以付款"的 PO 可能是 APPROVED + CLOSED FOR INVOICING，也可能是 APPROVED + 空。本体把这些组合映射为"PaymentEligible"、"FullyClosed"等语义清晰的状态，Agent 不需要理解 Oracle EBS 的底层状态机。

其二是**跨模块关联语义化**。三路匹配涉及 Purchasing 的 PO_LINE_LOCATIONS_ALL、Receiving 的 RCV_SHIPMENT_LINES、Payables 的 AP_INVOICE_DISTRIBUTIONS_ALL 三个模块的数据，通过外键关联链路极长。本体把这条关联链路建模为直接的"Invoice matches PurchaseOrder through GoodsReceipt"语义关系，Agent 发现不匹配时可以直接沿关系路径定位原因。

其三是**业务规则与系统配置解耦**。Oracle EBS 的很多业务规则是通过系统参数配置的（AP_SYSTEM_PARAMETERS_ALL 中的匹配容差、GL_PERIOD_STATUSES 中的期间状态等），本体把这些配置值引入规则定义，让 Agent 的分析结论始终与当前系统配置保持一致，避免规则固化后系统配置变更导致的分析偏差。

---

## 五、Oracle EBS 特有的挑战与应对

**多组织（Multi-Org）复杂性**：Oracle EBS 通过 Operating Unit 和 Organization 实现多组织数据隔离，几乎所有核心表都有 ORG_ID 字段。Agent 的工具层必须正确处理 MO_GLOBAL.INIT 的组织上下文设置，否则查询结果会出现跨组织的数据混淆。本体需要把"组织范围"作为所有分析场景的必要参数建模。

**数据量与性能**：大型 Oracle EBS 实例的 AP_INVOICES_ALL 可能有数千万条记录，PO_HEADERS_ALL 也是百万级别。Agent 工具不能直接全表扫描，必须有完善的索引策略和数据分层机制——建议在 Oracle EBS 外部建立分析专用的数据层（ODS 或 Data Warehouse），Agent 查询分析库而不是直接访问 EBS 生产库。

**EBS 定制化差异**：不同企业对 Oracle EBS 的定制程度差异巨大，标准表的字段可能被重用（字段语义被改变），自定义扩展表（通常以 XX_ 开头）承载了大量业务关键数据。本体设计必须针对具体企业的 EBS 实例做调研，而不能完全依赖 Oracle 标准数据字典。这也是为什么本体层比 Agent 本身更难一蹴而就——它需要深度的业务和 EBS 技术双重知识积累。

**审计轨迹不可篡改**：Oracle EBS 环境中，Agent 的分析结论如果要进入正式的审计报告，必须能完整追溯到原始数据来源（表名、行 ID、查询时间戳）。本体中的每个分析结论都应该携带"证据链"——不只是结论本身，还有支撑这个结论的原始 EBS 数据引用，这是合规场景下的硬性要求。