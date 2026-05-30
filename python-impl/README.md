# Python 实现 — LangGraph + FastAPI

基于 LangGraph StateGraph 的多Agent智能客服系统，Python原生实现。

## 技术栈

| 组件 | 技术 |
|------|------|
| Agent编排 | LangGraph StateGraph + MemorySaver |
| HTTP框架 | FastAPI + Uvicorn |
| LLM调用 | LangChain ChatOpenAI |
| 向量检索 | FAISS |
| 短期记忆 | Redis (aioredis) |
| 追踪 | OpenTelemetry + Jaeger |
| 协议 | MCP (JSON-RPC 2.0) |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 OPENAI_API_KEY

# 启动服务
python -m api.main
```

服务启动后访问 http://localhost:8000/docs 查看 Swagger UI。

## 项目结构

```
python-impl/
├── agents/                     # Agent实现
│   ├── supervisor.py           # Supervisor编排Agent（StateGraph核心）
│   ├── intent_router.py        # 意图路由Agent
│   ├── knowledge_rag.py        # RAG知识检索Agent
│   ├── ticket_handler.py       # 工单处理Agent
│   └── compliance_checker.py   # 合规审查Agent
├── memory/                     # 三层记忆系统
│   ├── working_memory.py       # 工作记忆（进程内存）
│   ├── short_term.py           # 短期记忆（Redis，30min TTL）
│   └── long_term.py            # 长期记忆（FAISS向量库）
├── mcp/                        # MCP工具协议
│   ├── mcp_server.py           # JSON-RPC 2.0服务端
│   └── tools/                  # 工具定义
├── tracing/                    # OpenTelemetry追踪
│   └── otel_config.py          # 追踪配置 + Agent装饰器
├── api/                        # FastAPI接口层
│   └── main.py                 # REST API入口
├── requirements.txt
├── Dockerfile
└── .env.example
```

## 核心特性

### Supervisor编排

LangGraph StateGraph构建有向图，条件路由根据意图分发到不同Agent：

```python
graph.add_conditional_edges(
    "supervisor_route",
    route_to_agent,
    {"knowledge_rag": "knowledge_rag", "ticket_handler": "ticket_handler"},
)
graph.add_edge("knowledge_rag", "compliance_check")
graph.add_edge("compliance_check", "synthesize")
```

### RAG管线

完整5步RAG流程：Query改写 → 向量检索(Top-5) → LLM重排序(Top-3) → 上下文注入 → 生成回答。

### 两阶段合规审查

1. **规则引擎**（<2ms）：敏感词匹配 + PII检测
2. **LLM深度审查**（~600ms）：处理越权承诺、隐晦违规等规则无法覆盖的场景
3. 高风险直接拦截不走LLM，LLM失败安全降级为通过

### MCP工具

4个已注册工具：
- `order_query` — 订单查询
- `knowledge_search` — 知识库搜索
- `ticket_create` — 工单创建
- `risk_check` — 风控检查

## API接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/chat` | POST | 聊天 |
| `/api/history/{session_id}` | GET | 对话历史 |
| `/api/tools` | GET | MCP工具列表 |
| `/api/tools/call` | POST | MCP工具调用 |
| `/api/metrics` | GET | 系统指标 |
| `/health` | GET | 健康检查 |

### 测试

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user_001", "message": "理财产品A的收益率是多少？"}'
```

## Docker

```bash
docker build -t smart-cs-python .
docker run -p 8000:8000 --env-file .env smart-cs-python
```
