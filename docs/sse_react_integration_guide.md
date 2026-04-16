# ReAct 流式（Phase 2）联调指南

> 适用范围：前端独立自测 + 前后端真实联调
> 配套文档：[sse_react_backend.md](./sse_react_backend.md) / [sse_react_frontend.md](./sse_react_frontend.md) / [sse_react_backend_answers.md](./sse_react_backend_answers.md)

## 1. Mock SSE Fixture（前端独立自测）

四份 `.jsonl` 文件位于 [tests/integration/fixtures/](../tests/integration/fixtures/)，每行一个 SSE `data:` JSON 事件，可直接灌入前端 mock SSE server 回放。

| 文件 | 场景 | 关键验证点 |
|---|---|---|
| `sse_react_agent_final_normal.jsonl` | 正常流程：1 次 tool 调用 + 1 轮 text turn | chunk 顺序、index 递增、末帧 eos=true、`node="agent_final"` 全程 |
| `sse_react_agent_final_retry.jsonl` | tenacity 重试：首次推 2 帧后重置 → 第二轮重新推 | index 回退到 0 触发前端 buffer 清空 + 重新累加 |
| `sse_react_agent_final_rollback.jsonl` | 混输 rollback：首 chunk 误判 text 后冒出 tool_call | 后端发 `index=0, delta=""` 重置帧，前端清 buffer |
| `sse_react_agent_final_error.jsonl` | 错误终态：推到一半 → `done(status=error)` | 无 `eos=true` 帧，前端必须以 `done` 为最终停止信号 |

**快速回放**（Node.js mock 示例）：

```javascript
const fs = require('fs');
const http = require('http');
http.createServer((req, res) => {
  if (!req.url.endsWith('/events')) return res.end();
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
  });
  const lines = fs.readFileSync('./fixtures/sse_react_agent_final_normal.jsonl', 'utf8').split('\n');
  let i = 0;
  const tick = setInterval(() => {
    if (i >= lines.length || !lines[i].trim()) {
      clearInterval(tick);
      return res.end();
    }
    res.write(`data: ${lines[i]}\n\n`);
    i++;
  }, 30);  // 30ms/帧，模拟真实节奏
}).listen(8001);
```

前端连 `http://localhost:8001/api/v1/ptp-agent/analyze/tasks/FIX-NORMAL/events` 即可消费。

## 2. 后端本地联调环境

### 2.1 启动后端

```bash
cd eragent
# 必填环境变量
export LLM_API_KEY="<your_dashscope_key>"
export LLM_STREAMING_ENABLED=true   # 默认 true，显式声明便于排查

# 可选（默认 SQLite + 内存）
# export DATABASE_URL="postgresql://..."
# export REDIS_URL="redis://localhost:6379/0"

uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

### 2.2 触发 ReAct 路径的测试 query

ReAct 兜底路径在以下条件触发：

- `intent_kind = data_lookup`（纯事实查询）
- 或 `intent_kind = analysis` 但 L3 置信度 < 0.5

**推荐 query 示例**（已验证走 ReAct）：

| query | 路径 | 说明 |
|---|---|---|
| `"列出最近 7 天创建的前 3 张采购订单和它们的当前状态"` | ReAct | DATA_LOOKUP，会调 query_purchase_orders + 生成回复 |
| `"看看 SUP-001 最近开了哪些发票"` | ReAct | DATA_LOOKUP，单实体查询 |
| `"PO-2024-0001 当前在哪个流程节点"` | ReAct | DATA_LOOKUP，状态查询 |

**会走 DAG 路径（不是 ReAct）的 query**（避免使用）：

- 含 `三路匹配` / `价格异常` / `付款合规` / `供应商绩效` 等关键词 → L1 命中 DAG
- 含具体 PO/SUP 实体 + 综合分析意图 → L3 ANALYSIS + has_entity → DAG

### 2.3 提交 + 订阅完整链路

```bash
# 终端 1：提交任务
TRACE=$(curl -s -X POST http://localhost:8000/api/v1/ptp-agent/analyze/async \
  -H "Content-Type: application/json" \
  -d '{"query":"列出最近 7 天创建的前 3 张采购订单和它们的当前状态","user_id":"dev","auto_persist":false}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['trace_id'])")
echo "trace_id=$TRACE"

# 终端 2：订阅 SSE
curl -N "http://localhost:8000/api/v1/ptp-agent/analyze/tasks/$TRACE/events"

# 终端 3：拉快照（done 之后用）
curl "http://localhost:8000/api/v1/ptp-agent/analyze/tasks/$TRACE" | jq '.result.report_markdown'
```

### 2.4 关闭流式回退验证

```bash
LLM_STREAMING_ENABLED=false uvicorn api.main:app --reload
# 重复上面的提交 + 订阅
# 期望：SSE 流里只有 status / stage / tool / done，无 chunk 事件
# done 后拉快照能拿到完整 report_markdown
```

## 3. 观测 + 排错

### 3.1 后端日志关键字段

```
trace=<trace_id>  [eragent.modules.p2p.agent]  ReAct done: type=... duration=...
[eragent.core.observability.middleware]  model call ok: name=qwen3-max ...
```

启用 DEBUG 日志可看到每个 chunk 的 publish 事件：

```bash
LOG_LEVEL=DEBUG uvicorn api.main:app --reload
```

### 3.2 trace span 查询（含监控指标）

```bash
curl "http://localhost:8000/api/v1/ptp-agent/traces/$TRACE" | jq '.spans[] | select(.span_type == "model" and .name == "p2p_agent.react") | .attributes'
```

期望返回：

```json
{
  "react_streaming": true,
  "first_chunk_ms": 7800.5,
  "text_turns": 1,
  "tool_turns": 1,
  "ambiguous_chunks": 0,
  "rollback_triggered": false
}
```

### 3.3 常见问题排查表

| 现象 | 可能原因 | 排查方向 |
|---|---|---|
| 收到的 chunks 全是 `node="report"` | 走了 DAG 路径，不是 ReAct | 改 query；查 `intent` span 的 `route_level` 与 `intent_kind` |
| 没有 chunk 事件 | streaming 关闭 / EventBus 异常 / trace_id 丢失 | 查后端日志 `react_streaming=false` 字样；确认 `LLM_STREAMING_ENABLED=true` |
| `index` 出现 gap | 网络丢包或代理重组 | 不阻塞，前端按 `chunkBroken` 兜底，最终以 `done` 后快照覆盖 |
| chunk 大量积压在末尾 | micro-batch 阈值偏高 / Qwen 慢 token 速率 | 检查 `flush_interval`/`flush_chars`；检查 first_chunk_ms |
| `eos=true` 之后还来 chunk | 后端 bug | 立即上报，前端忽略并告警 |
| `done(status=error)` 无 `eos` | 正常行为（错误场景不发 eos） | 前端用 `done` 作为终态信号 |

## 4. 联调验收清单（前端 + 后端共同打勾）

| # | 验收点 | 验证方式 |
|---|---|---|
| 1 | 正常流：fixture 回放后 UI 气泡按 chunk 累加显示，typing 动画正常 | 浏览器手动观察 |
| 2 | 重试场景：fixture 回放后 UI buffer 重置无可见跳变 | 浏览器手动观察 |
| 3 | rollback 场景：UI buffer 清空后重新累加 | 浏览器手动观察 |
| 4 | 错误场景：UI 显示错误卡片，无残留 typing 光标 | 浏览器手动观察 |
| 5 | 真实 ReAct query → SSE 通畅，accumulated == done 后快照 | 真实 LLM 跑 |
| 6 | streaming 关闭 → UI 沿用 done 后整段渲染，无 typing 状态 | 后端切配置 |
| 7 | 移动端 / 桌面端打字动画一致 | 双端目视 |
| 8 | Phase 1 DAG 路径流式无回归 | 跑既有 DAG query |

## 5. 联调时间窗 & 联系方式

- **后端代码状态**：Phase 2 代码 + 测试 + 文档已全部就绪（[git log](#)）
- **建议联调节奏**：
  1. 前端先用 fixture 自测（半天）
  2. 前后端联调真实环境（半天）
  3. 跑 §4 验收清单 8 项（半天）
- **后端 owner**：（请填写）
- **前端 owner**：（请填写）
