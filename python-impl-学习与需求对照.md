# 多 Agent 智能客服系统学习文档（基于 `multi-agent-smart-cs-guide-zh.pdf`）

> 目标：用一份文档完成「快速上手 + 架构理解 + 核心代码学习 + 需求对照验收」。
> 适用范围：当前仓库中的 `python-impl` 实现。

---

## 1. 系统是什么

这是一个面向金融/电商客服场景的多 Agent 系统，采用 **Supervisor 编排模式**，由中心节点统一调度子 Agent 完成：
- 意图识别与路由
- 知识检索与问答（RAG）
- 工单处理
- 合规审查

整体目标是让系统能在“自动化效率”和“金融合规”之间取得平衡。

---

## 2. 核心架构总览

### 2.1 编排模式（Supervisor Pattern）

请求主流程：
1. 用户请求进入 `FastAPI` 接口
2. Supervisor 判断应路由到哪个业务 Agent
3. 业务 Agent 执行（知识问答 or 工单处理）
4. 合规 Agent 对业务结果统一审查
5. Supervisor 汇总输出最终回复

### 2.2 状态总线（AgentState）

全局共享状态包含：
- `messages`：对话消息
- `user_id` / `session_id`
- `intent`
- `sub_results`：子 Agent 结果
- `compliance_passed`
- `final_response`
- `current_agent` / `retry_count`

### 2.3 三层记忆

- **工作记忆**：进程内，单请求级推理上下文
- **短期记忆**：Redis（失败自动降级内存），维护最近对话窗口
- **长期记忆**：FAISS 向量检索，支持知识库检索与持久化

### 2.4 MCP 工具协议

采用 JSON-RPC 2.0 风格实现工具注册、发现和调用，默认工具：
- `order_query`
- `knowledge_search`
- `ticket_create`
- `risk_check`

### 2.5 可观测性

通过 OpenTelemetry 装饰器记录 Agent 调用 Span，包含耗时、成功率和错误信息，可接 Jaeger。

---

## 3. 代码结构与学习路径

建议按以下顺序学习（从系统骨架到细节）：

1. `python-impl/api/main.py`
   - 看入口、接口契约、状态初始化和图执行。
2. `python-impl/agents/supervisor.py`
   - 看 StateGraph 的节点/边、条件路由和汇总逻辑。
3. `python-impl/agents/knowledge_rag.py`
   - 看完整 RAG 管线（改写、检索、重排、生成）。
4. `python-impl/agents/compliance_checker.py`
   - 看“两阶段合规”与高风险拦截策略。
5. `python-impl/agents/ticket_handler.py`
   - 看工单创建/查询与状态字段组织。
6. `python-impl/memory/*`
   - 看三层记忆如何配合。
7. `python-impl/mcp/mcp_server.py`
   - 看工具注册模式与 JSON-RPC 分发。
8. `python-impl/tracing/otel_config.py`
   - 看统一追踪装饰器如何无侵入接入。

---

## 4. 核心模块拆解

## 4.1 Supervisor（编排中枢）

关键点：
- 用 `StateGraph` 明确节点关系，而不是散落在 if/else。
- 用 `add_conditional_edges` 做意图分流。
- 强制把业务输出导入 `compliance_check` 汇聚节点后再返回。
- 使用 `MemorySaver` 支持 checkpoint（断点恢复能力基础）。

## 4.2 Knowledge RAG（知识检索）

流程：
1. Query 改写（口语化 -> 检索友好）
2. 向量检索 Top-5
3. LLM 重排序到 Top-3
4. 基于文档生成回复

收益：
- 提高召回和精确度平衡
- 控制上下文注入长度和 Token 消耗

## 4.3 Compliance Checker（合规审查）

两阶段：
1. 规则快筛（敏感词 + PII）
2. LLM 深审（语义级越权/误导检测）

策略：
- 高风险（`high/critical`）直接拦截，不再调用 LLM 审查
- 发现问题时对内容做脱敏后再流转

## 4.4 Ticket Handler（工单处理）

- 先由 LLM 解析动作/类型/优先级
- 内存工单库创建或查询工单
- 统一返回可读工单信息

## 4.5 Memory（分层记忆）

- `working_memory.py`：进程内上下文，低延迟读写
- `short_term.py`：Redis + fallback，支持历史窗口裁剪
- `long_term.py`：FAISS 语义检索 + 文档分块 + 本地持久化

## 4.6 MCP（工具协议）

- 装饰器注册工具（声明 schema）
- `tools/list` / `tools/call` / `ping` 方法分发
- 记录调用日志，便于监控和问题分析

## 4.7 Tracing（追踪）

- `@trace_agent_call` 给每个 Agent 方法挂 Span
- 记录：方法名、耗时、成功状态、异常

---

## 5. 运行与验证

## 5.1 启动

```bash
cd python-impl
pip install -r requirements.txt
cp .env.example .env
python -m api.main
```

## 5.2 最小验证

1. `POST /api/chat`：确认编排主流程可用
2. `POST /api/chat/stream`：确认 SSE 流式输出
3. `GET /api/history/{session_id}`：确认短期记忆
4. `GET /api/tools` + `POST /api/tools/call`：确认 MCP
5. `GET /api/metrics`：确认可观测入口
6. `GET /health`：确认健康检查

---

## 6. 与 PDF 一致的学习要点（面试可讲）

- 为什么是 Supervisor 而不是 Agent 互相调用：集中治理、统一合规、便于追踪
- 为什么三层记忆：速度/上下文持续性/知识持久化三者平衡
- 为什么两阶段合规：规则层保召回、LLM层提精度
- RAG 为什么 Top-5 再 Top-3：先召回后精排，兼顾质量和成本
- MCP 的价值：工具标准化与可扩展性，不把工具调用硬编码进 Agent

---

## 7. PDF 需求 vs `python-impl` 实现度核对

核对口径：以 PDF 中“项目概览 + 架构设计 + 部署/API说明”的“系统需求”作为验收标准；面试题库、简历模板等文档内容不纳入代码实现验收。

## 7.1 已实现（核心能力完整）

- [x] Supervisor 编排（StateGraph + 条件路由 + 汇聚）
- [x] 4 类子 Agent（意图、RAG、工单、合规）均有实现文件
- [x] 三层记忆模块（working/short-term/long-term）均已实现
- [x] RAG 主流程（改写、检索、重排、生成）
- [x] MCP 工具服务与 4 个默认工具
- [x] OpenTelemetry 装饰器式追踪能力
- [x] FastAPI 接口层（`/api/chat`、`/api/history`、`/api/tools`、`/api/tools/call`、`/api/metrics`、`/health`）
- [x] Dockerfile、`docker-compose.yml` 与 `.env` 配置模板

## 7.2 部分实现（有雏形，但未完全达到 PDF 描述）

- [x] **Intent Router 已纳入主编排图（已补全）**
  - 当前主链路为 `intent_router -> supervisor_route -> 业务Agent -> compliance_check -> synthesize`。
- [x] **短期记忆“最近20轮”语义已修正（已补全）**
  - `max_turns=20` 现在按轮次语义裁剪，等价于最多40条消息。
- [x] **指标体系已增强（已补全第一版）**
  - `/api/metrics` 已提供系统级 `p95/p99`、请求错误率、Token估算、合规通过率、路由准确率估算。
  - 仍待补：更精确的 Token 统计（基于模型返回 usage）。
- [x] **MCP 规范核心方法已补全（已补全）**
  - 已支持 `tools/*`、`resources/*`、`prompts/*`、`ping`。
- [x] **长期记忆已接入真实Embedding能力（已补全）**
  - 支持 `EMBEDDING_PROVIDER=openai` + `EMBEDDING_MODEL=text-embedding-3-small`，失败自动回退简单向量。
  - 支持 `VECTOR_STORE_TYPE=faiss|memory` 切换。

## 7.3 未实现/未落地（与 PDF 描述存在明显差距）

- [x] **对话流式输出（SSE）已补全**
  - 已新增 `POST /api/chat/stream`，输出 `meta/progress/chunk/done` 事件。
- [x] **Agent 超时与统一降级已补全第一版**
  - Graph 调用层支持统一超时+重试，节点层支持超时降级；合规节点超时按保守策略转人工。
  - 仍待补：完整熔断器与按错误类型差异化重试策略。
- [x] **Human-in-the-Loop 节点化能力（第一版已补全）**
  - 已实现会话级审核队列与审批恢复接口（`/api/hitl/pending`、`/api/hitl/review/{session_id}`）。
- [x] **工具权限控制与安全治理闭环（第一版已补全）**
  - 已支持角色级工具授权、参数 schema 校验、审计日志与敏感字段脱敏。
- [x] **真实外部系统对接能力（已补全第一版）**
  - `order_query`、`ticket_create`、`risk_check` 支持可配置外部 URL 调用。
  - 未配置或调用失败时自动回退 mock，确保可用性。

## 7.4 结论

`python-impl` **已经实现了 PDF 的主体骨架与主要功能模块**（可作为学习和面试演示版本），但**尚未实现“全部需求”**。  
更准确地说：当前实现是“**高完成度原型/MVP**”，距离 PDF 中强调的“生产级完整能力”还差一批工程化能力（流式、容错重试、完整指标、MCP全方法、HITL、真实系统对接）。

---

## 8. 下一步优化建议（按优先级）

1. 更精确 Token 统计（基于模型 usage）
2. HITL 状态持久化（当前为进程内存）
3. 外部系统对接增加签名鉴权、幂等重试、熔断策略
4. 路由评估接入固定评测集与持续回归

---

## 9. 本轮落地变更记录

1. **流式接口**
- 新增 `POST /api/chat/stream`，采用 SSE 输出阶段与内容分片。

2. **超时、重试、降级**
- Graph 入口调用新增统一超时与重试。
- `supervisor` 编排节点新增 `with_timeout_retry` 包装器；知识、工单、合规节点均纳入。
- 合规超时默认不放行并转人工（保守策略）。

3. **Intent Router 主链路化**
- 图入口改为 `intent_router`。
- Supervisor 优先复用 `intent_router` 结果，异常时回退 LLM 判定。

4. **指标增强**
- 指标聚合新增 `p95/p99`、请求错误率、合规通过率、Token/请求估算、路由准确率估算。

5. **MCP 标准方法 + 权限审计**
- 新增 `resources/list`、`resources/read`、`prompts/list`、`prompts/get`。
- 新增统一入口 `/api/mcp/jsonrpc`。
- `tools/call` 增加角色授权、schema 校验、敏感字段脱敏审计。

6. **真实Embedding + 向量存储切换**
- `LongTermMemory` 支持 `EMBEDDING_PROVIDER=openai` 与 `EMBEDDING_MODEL`。
- 支持 `VECTOR_STORE_TYPE=faiss|memory`。

7. **HITL 审批流**
- 合规不通过会话自动进入待审核队列。
- 新增人工审核接口：`/api/hitl/pending`、`/api/hitl/review/{session_id}`。

8. **标注集路由评估**
- 新增 `/api/eval/routing`，按 `expected_intent` 输出真实评估准确率与逐条结果。

9. **短期记忆轮次语义修正**
- `max_turns=20` 由原“20条消息”修正为“20轮（40条消息）”。

10. **真实外部系统对接能力**
- `order_query` / `ticket_create` / `risk_check` 可按环境变量调用外部服务。
- 调用失败自动回退 mock，兼顾生产接入与本地可跑性。

11. **Knowledge RAG 完整链路升级**
- `knowledge_rag.py` 已实现完整可落地流程：
  1) Query改写（失败降级）
  2) 相似度检索（vector）
  3) 关键词检索（keyword）
  4) 归一化后加权融合（默认 0.7/0.3）
  5) LLM重排序（Top候选 -> Top-3）
  6) 检索内容 + 引用编号注入生成Prompt
- 新增 `knowledge_rag_debug` 输出，便于排障与线上观测。

12. **Knowledge RAG 生产化增强（第二轮）**
- 关键词检索升级为 BM25-like（引入 IDF 与文档长度归一），提升关键词命中质量。
- 融合升级为“加权融合 + RRF 融合”双通道，降低单一排序策略偏差。
- 重排序索引解析更稳健（支持逗号格式与 JSON 数组格式）。
- 新增低置信度门控：检索置信度不足时拒答并转人工，降低幻觉风险。
- 新增上下文预算控制（文档数/字符数上限），避免提示词过长与噪声注入。
- 调试信息补充 `confidence` / `confidence_blocked`，支持线上分析和告警。

13. **默认模型配置切换到百炼兼容模式**
- `python-impl/.env.example` 默认值调整为：
  - `OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`
  - `MODEL_NAME=qwen-plus`
  - `EMBEDDING_MODEL=text-embedding-v3`
- Harness CI 默认值同步切换（未设置 `vars` 时走百炼兼容端点）。

14. **Windows 一键启动脚本**
- 新增 `scripts/start-all.ps1`，可一键拉起：
  - Redis（Docker）
  - Python API 服务
  - Harness 评测（可选）
- 支持参数化传入 API Key、模型名、端口，适合求职演示场景。

15. **前端现状核对**
- 新增内置 Web 单页前端（`python-impl/web/index.html`），由 FastAPI 直接托管。
- 前端支持聊天、流式回复、会话历史、MCP工具调用与指标查看，和后端接口一一对应。

---

## 10. RAG 核对结果（当前实现）

按“可落地RAG必备能力”逐项核对：

- [x] 相似度检索
- [x] 关键词检索
- [x] 打分加权融合
- [x] 重排序
- [x] 检索内容注入答案生成Prompt

结论：`python-impl/agents/knowledge_rag.py` 已满足完整可落地 RAG 的核心链路要求。

进一步结论：当前实现已从“可落地”提升到“具备生产防护能力的可落地版本”（含置信度门控、预算控制和稳健融合）。

