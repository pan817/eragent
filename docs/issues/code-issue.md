# 技术债记录

## ISSUE-001: ReportAgent 独立轻量模型

**状态**: 待实施
**优先级**: 中
**创建日期**: 2026-04-14

### 问题描述

当前 ReportAgent 与主 Agent 共用同一 LLM 模型（如 qwen3-max）。
报告生成本质是纯文本格式化/汇总，不需要大模型的推理能力，
使用重型模型导致不必要的延迟和成本。

### 现状

- 已通过 `disable_thinking` 参数关闭 thinking 模式（方案2），
  降低了 qwen3/zhipu/minimax 等支持 thinking 的模型的推理延迟。
- 但模型本身的参数规模仍然是主模型级别，响应速度和成本未达最优。

### 建议方案

在 `config.yaml` 中新增 `llm.report_model` 配置项，
允许为 ReportAgent 指定独立的轻量模型（如 `qwen-turbo`、`qwen-plus`）。

```yaml
llm:
  model: "qwen3-max"          # 主模型（ReAct Agent 使用）
  report_model: "qwen-plus"   # 报告生成模型（可选，默认 fallback 到 model）
```

### 改动范围

- `config/settings.py`: `LLMSettings` 新增 `report_model` 字段
- `config/config.yaml`: 新增配置项
- `modules/p2p/model_factory.py`: 支持构建报告专用模型实例
- `modules/p2p/report_agent.py`: 使用 `report_model` 构建 LLM
- 测试覆盖

### 预期收益

- 报告生成延迟从 30s+ 降至 5-10s（轻量模型 TTFT 更低、生成速度更快）
- API 调用成本降低（轻量模型单价更低）
