# 智能客服多Agent系统 — 三语言实现

面向金融/电商场景的企业级多Agent智能客服系统，包含 Python / Java / Go 三种完整实现。

## 架构概览

```
用户消息 → Supervisor路由 → 意图识别Agent → 专业Agent(知识检索/工单处理) → 合规审查 → 最终回复
```

| Agent | 职责 |
|-------|------|
| Supervisor | 中央编排调度、结果汇总 |
| Intent Router | 意图分类（LLM + 关键词回退） |
| Knowledge RAG | RAG知识检索（Query改写 → Top-5检索 → 重排序Top-3 → 生成回答） |
| Ticket Handler | 工单CRUD操作 |
| Compliance Checker | 两阶段合规审查（规则引擎 + LLM深度审查） |

## 三语言对比

| 维度 | Python | Java | Go |
|------|--------|------|-----|
| 框架 | LangGraph + FastAPI | Spring AI + Spring Boot | Gin + 自研LLM客户端 |
| 端口 | 8000 | 8080 | 8090 |
| LLM调用 | LangChain ChatOpenAI | Spring AI ChatClient | OpenAI兼容HTTP客户端 |
| 向量检索 | FAISS | 64维哈希+余弦相似度 | 64维哈希+余弦相似度 |
| 合规审查 | 规则+LLM双阶段 | 规则+LLM双阶段 + PII脱敏 | 规则+LLM双阶段 + PII脱敏 |
| 适合场景 | AI原型/数据科学团队 | 企业级金融/银行 | 高并发云原生微服务 |

## 快速启动

### Docker Compose 一键启动（推荐）

```bash
# 配置环境变量
export OPENAI_API_KEY=your-key

# 启动所有服务
docker-compose up -d

# 访问
# Python API: http://localhost:8000/docs
# Java API:   http://localhost:8080/api/health
# Go API:     http://localhost:8090/health
# Jaeger UI:  http://localhost:16686
```

### 单独启动

各语言的启动方式见对应目录的 README：
- [Python 实现](./python-impl/README.md)
- [Java 实现](./java-impl/README.md)
- [Go 实现](./go-impl/README.md)

## 统一API

三个实现提供相同的REST API：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/chat` | POST | 聊天接口 |
| `/api/chat/stream` | POST | SSE流式聊天接口（Python实现已支持） |
| `/api/history/{session_id}` | GET | 对话历史 |
| `/api/tools` | GET | MCP工具列表 |
| `/api/tools/call` | POST | MCP工具调用（支持角色权限控制与审计日志） |
| `/api/mcp/jsonrpc` | POST | MCP JSON-RPC统一入口（tools/resources/prompts） |
| `/api/resources` | GET | MCP资源列表 |
| `/api/resources/read` | GET | MCP资源读取 |
| `/api/prompts` | GET | MCP Prompt模板列表 |
| `/api/prompts/get` | POST | MCP Prompt模板渲染 |
| `/api/hitl/pending` | GET | 待人工审核会话列表 |
| `/api/hitl/review/{session_id}` | POST | 审核通过/驳回会话 |
| `/api/eval/routing` | POST | 基于标注集的路由准确率评估 |
| `/api/metrics` | GET | 系统指标 |
| `/health` | GET | 健康检查 |

### 聊天接口示例

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_001", "message": "我想查询订单状态"}'
```

### 流式聊天接口示例（Python）

```bash
curl -N -X POST http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"user_id":"user_001","message":"我想查询订单状态"}'
```

---

## Python最新补全进展（按需求清单）

1. **流式输出能力**
- 新增 `POST /api/chat/stream`，返回 `text/event-stream`
- 包含 `meta/progress/chunk/done` 事件，支持分片输出回复内容

2. **统一超时、重试与降级**
- Graph 调用增加全链路超时（`GRAPH_TIMEOUT_SECONDS`）和重试（`GRAPH_MAX_RETRIES`）
- 失败后自动触发统一降级回复，避免请求直接 500
- Supervisor 子节点增加超时保护，合规节点超时默认“保守不通过并转人工”

3. **Intent Router 纳入主编排图**
- 编排入口改为 `intent_router -> supervisor_route -> business_agent -> compliance -> synthesize`
- Supervisor 优先使用 `intent_router` 结果，异常时再回退到自身 LLM 判定

4. **指标体系增强**
- `/api/metrics` 增加系统级指标：`p95/p99`、请求错误率、合规通过率、平均Token估算、路由准确率估算
- 保留工具调用日志用于排障

5. **MCP标准方法补全**
- 已支持 `resources/list`、`resources/read`、`prompts/list`、`prompts/get`
- 新增 `/api/mcp/jsonrpc` 统一入口，同时保留 REST 便捷接口

6. **工具权限模型 + 审计日志**
- `tools/call` 支持按角色授权（admin/agent/auditor/viewer）
- 增加参数 schema 校验与敏感字段脱敏审计
- `/api/metrics` 返回 `tool_audit_log`

7. **长期记忆升级为可配置真实Embedding**
- `LongTermMemory` 支持 `EMBEDDING_PROVIDER=openai`（失败自动回退简单向量）
- 支持 `VECTOR_STORE_TYPE=faiss|memory` 切换（为生产扩展预留）

8. **Human-in-the-Loop 审批流**
- 合规不通过的会话自动进入待审核队列
- 新增审核接口：`/api/hitl/pending`、`/api/hitl/review/{session_id}`
- 支持 `approve/reject` 决策并写回会话历史

9. **路由准确率标注集评估**
- 新增 `/api/eval/routing`，输入带 `expected_intent` 的标注样本，输出准确率和逐条明细

10. **短期记忆20轮语义修正**
- `ShortTermMemory` 由“20条消息”改为“20轮对话（40条消息）”裁剪
- 更符合文档里“最近20轮”的定义

11. **外部系统对接能力**
- `order_query`、`ticket_create`、`risk_check` 支持通过环境变量直连外部服务
- 支持 `ORDER_SERVICE_URL` / `TICKET_SERVICE_URL` / `RISK_SERVICE_URL`
- 外部接口不可用时自动回退本地 mock 结果，保证服务可用

12. **Knowledge RAG 升级为完整可落地链路**
- `knowledge_rag.py` 从“纯向量召回”升级为“混合检索”：
  - 相似度检索（vector）
  - 关键词检索（keyword）
  - 分数归一化后按权重融合（默认 vector:0.7 / keyword:0.3）
  - LLM 重排序（Top-N 候选 -> Top-3）
  - 检索内容与引用编号注入生成 Prompt
- Query 改写增加失败降级（失败时回退原始 query）
- 返回 `knowledge_rag_debug` 调试信息（改写query、候选数、来源）

13. **Knowledge RAG 生产化增强（第二轮）**
- 关键词检索从“简单词频”升级为 **BM25-like 打分**（含 IDF 与文档长度归一）
- 融合策略从“单一加权”升级为 **加权融合 + RRF 融合** 双通道
- 重排序结果解析增强：同时支持 `0,2,4` 与 JSON 数组格式
- 新增检索置信度门控（低置信度主动拒答转人工，降低幻觉风险）
- 生成阶段新增上下文预算控制（`max_context_docs` + `max_context_chars`）
- `knowledge_rag_debug` 增加 `confidence` 与 `confidence_blocked`

14. **最小 Harness 工程落地**
- 新增 `harness/` 目录，包含数据集、执行脚本、结果模板与阈值配置
- 支持两类评测：
  - Routing 准确率（调用 `/api/eval/routing`）
  - RAG 关键词命中率（调用 `/api/chat`）
- 生成标准产物：
  - `harness/results/latest.json`
  - `harness/results/latest.md`
- 新增 CI：`.github/workflows/harness.yml`
  - 在 PR / 手动触发时执行
  - 自动上传评测报告 artifact

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENAI_API_KEY` | LLM API密钥 | 无 |
| `OPENAI_BASE_URL` | API端点 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `MODEL_NAME` | 模型名称 | `qwen-plus` |
| `EMBEDDING_MODEL` | 向量模型 | `text-embedding-v3` |
| `REDIS_URL` | Redis地址（Python/Go） | `redis://localhost:6379/0` |
| `REDIS_HOST` / `REDIS_PORT` | Redis地址（Java） | `localhost` / `6379` |
| `OTEL_SERVICE_NAME` | 追踪服务名 | `smart-cs-multi-agent` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP端点 | `http://localhost:4317` |

## 项目结构

```
code/
├── docker-compose.yml          # Docker编排（Redis + 三语言服务 + Jaeger）
├── python-impl/                # Python: LangGraph + FastAPI
│   ├── agents/                 # 5个Agent实现
│   ├── memory/                 # 三层记忆系统
│   ├── mcp/                    # MCP工具协议
│   ├── tracing/                # OpenTelemetry追踪
│   └── api/                    # FastAPI接口
├── harness/                    # 最小评测框架（Harness）
│   ├── config.yaml             # 数据集与阈值配置
│   ├── datasets/               # 标注集（routing/rag）
│   ├── metrics.py              # 指标计算与阈值判断
│   ├── run_harness.py          # 评测执行器
│   ├── templates/              # 报告模板
│   └── results/                # 评测产物（latest.json/md）
├── java-impl/                  # Java: Spring AI + Spring Boot
│   └── src/main/java/com/smartcs/
│       ├── SmartCsApplication.java  # Spring Boot入口
│       ├── agent/              # 5个Agent实现
│       ├── memory/             # 分层记忆（ShortTerm + LongTerm）
│       ├── mcp/                # MCP工具服务端
│       ├── tracing/            # 追踪
│       └── config/             # Spring配置 + ChatController
└── go-impl/                    # Go: Gin + 自研LLM客户端
    ├── agent/                  # 5个Agent实现
    ├── memory/                 # 分层记忆（Working + ShortTerm + LongTerm）
    ├── mcp/                    # MCP工具服务端
    ├── llm/                    # OpenAI兼容HTTP客户端
    ├── tracing/                # 追踪
    └── api/                    # Gin HTTP服务
```

## Harness 使用说明（最小可用）

### 1) 启动服务

```bash
cd python-impl
pip install -r requirements.txt
cp .env.example .env
python -m api.main
```

### 2) 运行 Harness

在项目根目录执行：

```bash
python harness/run_harness.py --base-url http://127.0.0.1:8000 --config harness/config.yaml
```

### 2.1) Windows 一键启动（Redis + API + Harness）

项目已提供脚本：`scripts/start-all.ps1`

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-all.ps1
```

可选参数：

```powershell
# 指定百炼 Key，并启动后自动跑 harness
powershell -ExecutionPolicy Bypass -File .\scripts\start-all.ps1 `
  -ApiKey "你的APIKey" `
  -RunHarness:$true

# 不跑 harness，只拉起 Redis + API
powershell -ExecutionPolicy Bypass -File .\scripts\start-all.ps1 -RunHarness:$false
```

### 3) 查看结果

- 结构化结果：`harness/results/latest.json`
- 人类可读报告：`harness/results/latest.md`

### 4) 自定义数据与门禁阈值

- 路由数据集：`harness/datasets/routing_v1.jsonl`
- RAG数据集：`harness/datasets/rag_v1.jsonl`
- 阈值配置：`harness/config.yaml`
  - `routing_accuracy_min`
  - `rag_keyword_hit_min`
  - `avg_latency_ms_max`

### 5) CI 自动回归

- 工作流文件：`.github/workflows/harness.yml`
- 触发方式：
  - Pull Request 到 `main/master`
  - 手动 `workflow_dispatch`
- 依赖：
  - `secrets.OPENAI_API_KEY`（必需）
  - 可选 `vars.OPENAI_BASE_URL` / `vars.MODEL_NAME` / `vars.EMBEDDING_MODEL`

## 前端说明

当前仓库已新增内置 Web 页面（无需 Node 构建）：
- 页面路径：`/`（文件位置：`python-impl/web/index.html`）
- 页面能力：
  - 普通聊天 `POST /api/chat`
  - 流式聊天 `POST /api/chat/stream`
  - 会话历史 `GET /api/history/{session_id}`
  - MCP 工具调用 `POST /api/tools/call`
  - 工具列表与系统指标查看

本地启动后直接访问：
- `http://127.0.0.1:8000/`
- Swagger 文档：`http://127.0.0.1:8000/docs`

快速本地测试：
1. 启动服务：`python -m api.main`（或执行 `scripts/start-all.ps1`）
2. 打开 `http://127.0.0.1:8000/`
3. 在页面输入问题并点击“发送”
4. 切换“流式聊天”验证 `/api/chat/stream`
5. 在右侧调用 `order_query` 等工具，验证 MCP 接口联通
