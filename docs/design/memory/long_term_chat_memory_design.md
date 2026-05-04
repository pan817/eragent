# 长程对话记忆方案设计 — 已废弃

> **DEPRECATED — 2026-05-01**
>
> 本文档是 [agent_enhancement_gaps.md §4.5](agent_enhancement_gaps.md) 的初版方案，但**未先核查 v1 memory 设计**就独立产出，存在与现有 memory 体系的多处对齐缺口（绕过 MemoryManager 单一入口、未套用"精确性优先"的双通道检索、未对齐双 trace 架构、未走 MemoryType 封闭枚举扩展等）。
>
> **请勿按本文实施。**
>
> 已被 **[docs/memory/p2p_agent_memory_design_v2_refresh.md](memory/p2p_agent_memory_design_v2_refresh.md) §3 J 章** 取代。
>
> v2 refresh 在原 v1 memory 设计的哲学和架构基础上重新设计了"跨会话长程对话记忆"，新方案：
> - chat 索引、SESSION_RECAP 抽取、检索全部通过 MemoryManager 统一入口
> - 检索套用 v1 `_dual_channel_search` 模式（精确通道 + 语义补充）
> - 新增 `MemoryType.SESSION_RECAP`（封闭枚举扩展）
> - 写入 v1 已有的 memory trace（双 trace 架构）
> - 配置项遵循 v1 G15 规范，含完整降级路径
>
> 详细对齐缺口分析见 v2 refresh §1.2 + §2 表格。
>
> 原文已通过 git 历史保留，需要查阅时执行 `git log -- docs/long_term_chat_memory_design.md`。
