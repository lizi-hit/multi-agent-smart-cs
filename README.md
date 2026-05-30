# 多 Agent 电商客服系统（Python）

一个基于 **LangGraph + FastAPI** 的多 Agent 电商客服项目，支持电商场景中的：
- 商品/政策问答（RAG）
- 订单与售后工具调用（MCP）
- 工单流转
- 合规与内容安全审查
- 流式响应与人工复核（HITL）

---

## 1. 系统架构

请求主链路：

`用户消息 -> intent_router -> supervisor_route -> 业务Agent -> compliance_check -> synthesize -> 回复`

核心 Agent：

| Agent | 职责 |
|------|------|
| `Supervisor` | 中央编排、路由汇总、降级兜底 |
| `Intent Router` | 意图识别（知识问答 / 工单处理 / 合规检查） |
| `Knowledge RAG` | 查询改写、混合检索、融合重排、答案生成 |
| `Ticket Handler` | 售后/工单创建与查询 |
| `Compliance Checker` | 规则 + LLM 双阶段审查、PII 脱敏 |

---

## 2. 已实现能力

- **SSE 流式聊天**：`POST /api/chat/stream`
- **统一超时 + 重试 + 降级**：图执行与节点执行双层保护
- **三层记忆**：工作记忆 / 短期记忆（Redis）/ 长期记忆（向量）
- **RAG 完整链路（电商）**：
  - Query 改写（失败降级）
  - 相似度检索 + 关键词检索（BM25-like）
  - 加权融合 + RRF 融合
  - LLM 重排序
  - 上下文预算控制与低置信度门控
- **MCP 能力**：
  - `tools/*`, `resources/*`, `prompts/*`
  - 角色权限控制与审计日志
- **HITL 人工复核**：
  - 待审队列查询
  - 人工 approve/reject
- **Harness 最小评测工程**：
  - 路由准确率
  - RAG 关键词命中
  - 阈值门禁 + 报告产物 + CI
- **内置 Web 控制台**（无 Node 构建）：
  - 页面路径：`/`

---

## 3. 快速启动

### 3.1 手动启动（推荐先跑通）

```bash
cd python-impl
pip install -r requirements.txt
cp .env.example .env
python -m api.main
```

访问：
- Web UI: `http://127.0.0.1:8000/`
- Swagger: `http://127.0.0.1:8000/docs`
- Health: `http://127.0.0.1:8000/health`

### 3.2 Windows 一键启动（Redis + API + Harness）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-all.ps1
```

可选：

```powershell
# 传入 API Key 并自动跑 harness
powershell -ExecutionPolicy Bypass -File .\scripts\start-all.ps1 `
  -ApiKey "你的APIKey" `
  -RunHarness:$true

# 仅拉起 Redis + API
powershell -ExecutionPolicy Bypass -File .\scripts\start-all.ps1 -RunHarness:$false
```

---

## 4. API 总览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 内置 Web 控制台 |
| `/api/chat` | POST | 普通聊天 |
| `/api/chat/stream` | POST | SSE 流式聊天 |
| `/api/history/{session_id}` | GET | 会话历史 |
| `/api/tools` | GET | MCP 工具列表 |
| `/api/tools/call` | POST | MCP 工具调用 |
| `/api/mcp/jsonrpc` | POST | MCP JSON-RPC 统一入口 |
| `/api/resources` | GET | MCP 资源列表 |
| `/api/resources/read` | GET | MCP 资源读取 |
| `/api/prompts` | GET | MCP Prompt 列表 |
| `/api/prompts/get` | POST | MCP Prompt 渲染 |
| `/api/hitl/pending` | GET | 待人工审核会话 |
| `/api/hitl/review/{session_id}` | POST | 人工审核通过/驳回 |
| `/api/eval/routing` | POST | 路由准确率评测 |
| `/api/metrics` | GET | 系统指标 + 工具审计日志 |
| `/health` | GET | 健康检查 |

---

## 5. Harness 使用

运行评测：

```bash
python harness/run_harness.py --base-url http://127.0.0.1:8000 --config harness/config.yaml
```

结果文件：
- `harness/results/latest.json`
- `harness/results/latest.md`

可配置项：
- `harness/datasets/routing_v1.jsonl`
- `harness/datasets/rag_v1.jsonl`
- `harness/config.yaml`

CI 工作流：
- `.github/workflows/harness.yml`

---

## 6. 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENAI_API_KEY` | LLM API Key | 无 |
| `OPENAI_BASE_URL` | OpenAI 兼容端点 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `MODEL_NAME` | 对话模型 | `qwen-plus` |
| `EMBEDDING_PROVIDER` | 向量提供方 | `openai` |
| `EMBEDDING_MODEL` | 向量模型 | `text-embedding-v3` |
| `REDIS_URL` | 短期记忆 Redis 地址 | `redis://localhost:6379/0` |
| `VECTOR_STORE_TYPE` | 向量存储类型 | `faiss` |
| `FAISS_INDEX_PATH` | FAISS 索引路径 | `./vector_store/faiss_index` |
| `GRAPH_TIMEOUT_SECONDS` | 图执行超时 | `20` |
| `GRAPH_MAX_RETRIES` | 图执行重试次数 | `1` |
| `ORDER_SERVICE_URL` | 外部订单服务（可选） | 空（回退 mock） |
| `TICKET_SERVICE_URL` | 外部工单服务（可选） | 空（回退 mock） |
| `RISK_SERVICE_URL` | 外部风险服务（可选） | 空（回退 mock） |

---

## 7. 项目结构

```text
.
├── .github/workflows/harness.yml
├── docker-compose.yml
├── harness/
│   ├── config.yaml
│   ├── datasets/
│   ├── metrics.py
│   ├── run_harness.py
│   ├── templates/
│   └── results/
├── python-impl/
│   ├── agents/
│   ├── api/
│   ├── mcp/
│   ├── memory/
│   ├── tracing/
│   ├── web/
│   │   └── index.html
│   ├── .env.example
│   └── requirements.txt
└── scripts/
    └── start-all.ps1
```

---

## 8. 说明

- 仓库为可运行工程，重点在多 Agent 编排与电商客服落地能力。
- 如需生产部署，建议补充：鉴权、幂等、熔断、评测集扩展、成本监控和持久化 HITL 队列。
