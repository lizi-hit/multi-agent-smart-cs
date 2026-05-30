"""
FastAPI入口 — 提供REST API + SSE流式响应
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.supervisor import create_supervisor_graph
from memory.working_memory import WorkingMemory
from memory.short_term import ShortTermMemory
from memory.long_term import LongTermMemory
from mcp.mcp_server import MCPToolServer, create_default_tools
from tracing.otel_config import init_tracer, AgentMetrics

load_dotenv()


working_memory = WorkingMemory()
short_term_memory = ShortTermMemory(redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
long_term_memory = LongTermMemory(
    index_path=os.getenv("FAISS_INDEX_PATH", "./vector_store/faiss_index"),
    vector_store_type=os.getenv("VECTOR_STORE_TYPE", "faiss"),
    embedding_provider=os.getenv("EMBEDDING_PROVIDER", "openai"),
    embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
)
mcp_server = create_default_tools(MCPToolServer())
metrics = AgentMetrics()
graph = None
pending_reviews: dict[str, dict] = {}

GRAPH_TIMEOUT_SECONDS = float(os.getenv("GRAPH_TIMEOUT_SECONDS", "20"))
GRAPH_MAX_RETRIES = int(os.getenv("GRAPH_MAX_RETRIES", "1"))
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "web"


def _estimate_tokens(*parts: str) -> int:
    text = "".join(parts)
    return max(len(text) // 2, 1)


def _expected_intent(message: str) -> str:
    message = message.lower()
    ticket_keywords = ["退款", "理赔", "开户", "工单", "投诉", "申请"]
    compliance_keywords = ["保证收益", "零风险", "被骗", "盗刷", "风控", "合规", "银行卡"]
    if any(k in message for k in compliance_keywords):
        return "compliance_checker"
    if any(k in message for k in ticket_keywords):
        return "ticket_handler"
    return "knowledge_rag"


async def _run_graph_with_retry(initial_state: dict, config: dict):
    last_error = None
    for attempt in range(GRAPH_MAX_RETRIES + 1):
        try:
            return await asyncio.wait_for(
                graph.ainvoke(initial_state, config=config),
                timeout=GRAPH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            last_error = exc
            if attempt >= GRAPH_MAX_RETRIES:
                raise
            await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
    raise last_error


def _build_fallback_response(session_id: str, user_message: str, user_id: str, reason: str) -> dict:
    fallback_text = (
        "系统暂时繁忙，已为您转人工处理。"
        "请稍后查看处理进度，我们会尽快跟进您的问题。"
    )
    return {
        "messages": [],
        "user_id": user_id,
        "session_id": session_id,
        "intent": "ticket_handler",
        "sub_results": {
            "fallback": f"{fallback_text}（原因: {reason}）",
            "ticket_handler": f"自动降级工单：用户问题「{user_message[:120]}」",
        },
        "compliance_passed": False,
        "final_response": fallback_text,
        "current_agent": "fallback",
        "retry_count": GRAPH_MAX_RETRIES,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global graph

    init_tracer(
        service_name=os.getenv("OTEL_SERVICE_NAME", "smart-cs-multi-agent"),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )

    graph = create_supervisor_graph(
        working_memory=working_memory,
        short_term_memory=short_term_memory,
        long_term_memory=long_term_memory,
    )

    long_term_memory.add_document(
        content="我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，最低投资金额10000元。注意：理财非存款，产品有风险，投资须谨慎。",
        source="product_faq.md",
    )
    long_term_memory.add_document(
        content="退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。退款将在3-5个工作日内原路退回。",
        source="refund_policy.md",
    )
    long_term_memory.add_document(
        content="开户流程：1.准备身份证原件 2.填写开户申请表 3.进行视频认证 4.设置交易密码 5.完成风险评估问卷。整个流程约需15-30分钟。",
        source="account_guide.md",
    )

    yield


app = FastAPI(
    title="智能客服多Agent系统",
    description="基于LangGraph的Supervisor编排多Agent智能客服系统",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIR.exists():
    assets_dir = FRONTEND_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


class ChatRequest(BaseModel):
    message: str
    user_id: str = "anonymous"
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    compliance_passed: bool


class RoutingEvalSample(BaseModel):
    message: str
    expected_intent: str


class RoutingEvalRequest(BaseModel):
    samples: list[RoutingEvalSample]


class ReviewDecisionRequest(BaseModel):
    action: str  # approve | reject
    reviewer: str = "reviewer"
    message: str = ""


@app.get("/")
async def serve_frontend():
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return FileResponse(index_file)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """主聊天接口"""
    if graph is None:
        raise HTTPException(status_code=503, detail="系统初始化中")

    session_id = request.session_id or str(uuid.uuid4())

    await short_term_memory.add_message(session_id, "user", request.message)

    from langchain_core.messages import HumanMessage

    initial_state = {
        "messages": [HumanMessage(content=request.message)],
        "user_id": request.user_id,
        "session_id": session_id,
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
    }

    config = {"configurable": {"thread_id": session_id}}

    start = time.time()
    success = True
    routing_correct = None

    try:
        result = await _run_graph_with_retry(initial_state, config=config)
    except Exception as e:
        success = False
        result = _build_fallback_response(
            session_id=session_id,
            user_message=request.message,
            user_id=request.user_id,
            reason=str(e),
        )

    final_response = result.get("final_response", "系统处理异常，请稍后重试")
    actual_intent = result.get("intent", "unknown")
    expected_intent = _expected_intent(request.message)
    routing_correct = (
        actual_intent == expected_intent
        if actual_intent in {"knowledge_rag", "ticket_handler", "compliance_checker"}
        else None
    )

    await short_term_memory.add_message(session_id, "assistant", final_response)

    if not result.get("compliance_passed", True):
        pending_reviews[session_id] = {
            "session_id": session_id,
            "user_id": request.user_id,
            "user_message": request.message,
            "assistant_response": final_response,
            "status": "pending",
            "created_at": time.time(),
        }
        final_response = (
            f"{final_response}\n\n"
            f"[HITL] 当前会话已进入人工审核队列，session_id={session_id}"
        )

    latency_ms = (time.time() - start) * 1000
    metrics.record_request(
        latency_ms=latency_ms,
        success=success,
        token_estimate=_estimate_tokens(request.message, final_response),
        compliance_passed=result.get("compliance_passed"),
        routing_correct=routing_correct,
    )

    return ChatResponse(
        response=final_response,
        session_id=session_id,
        intent=actual_intent,
        compliance_passed=result.get("compliance_passed", True),
    )


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest):
    """SSE流式聊天接口（阶段事件 + 回复分片）"""
    if graph is None:
        raise HTTPException(status_code=503, detail="系统初始化中")

    session_id = request.session_id or str(uuid.uuid4())
    await short_term_memory.add_message(session_id, "user", request.message)

    async def event_generator() -> AsyncGenerator[str, None]:
        start = time.time()
        success = True
        result = None

        def sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield sse("meta", {"session_id": session_id, "status": "started"})
        yield sse("progress", {"stage": "routing"})

        from langchain_core.messages import HumanMessage

        initial_state = {
            "messages": [HumanMessage(content=request.message)],
            "user_id": request.user_id,
            "session_id": session_id,
            "intent": "",
            "sub_results": {},
            "compliance_passed": True,
            "final_response": "",
            "current_agent": "",
            "retry_count": 0,
        }
        config = {"configurable": {"thread_id": session_id}}

        try:
            result = await _run_graph_with_retry(initial_state, config=config)
        except Exception as e:
            success = False
            result = _build_fallback_response(
                session_id=session_id,
                user_message=request.message,
                user_id=request.user_id,
                reason=str(e),
            )
            yield sse("progress", {"stage": "fallback"})

        final_response = result.get("final_response", "系统处理异常，请稍后重试")
        await short_term_memory.add_message(session_id, "assistant", final_response)

        if not result.get("compliance_passed", True):
            pending_reviews[session_id] = {
                "session_id": session_id,
                "user_id": request.user_id,
                "user_message": request.message,
                "assistant_response": final_response,
                "status": "pending",
                "created_at": time.time(),
            }
            yield sse("progress", {"stage": "hitl_pending", "session_id": session_id})

        # 按固定窗口分片，提供稳定SSE体验
        chunk_size = 48
        for i in range(0, len(final_response), chunk_size):
            chunk = final_response[i:i + chunk_size]
            yield sse("chunk", {"content": chunk, "index": i // chunk_size})
            await asyncio.sleep(0.01)

        latency_ms = (time.time() - start) * 1000
        actual_intent = result.get("intent", "unknown")
        expected_intent = _expected_intent(request.message)
        routing_correct = (
            actual_intent == expected_intent
            if actual_intent in {"knowledge_rag", "ticket_handler", "compliance_checker"}
            else None
        )
        metrics.record_request(
            latency_ms=latency_ms,
            success=success,
            token_estimate=_estimate_tokens(request.message, final_response),
            compliance_passed=result.get("compliance_passed"),
            routing_correct=routing_correct,
        )

        yield sse(
            "done",
            {
                "session_id": session_id,
                "intent": actual_intent,
                "compliance_passed": result.get("compliance_passed", True),
                "latency_ms": latency_ms,
            },
        )

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/hitl/pending")
async def list_pending_reviews():
    return {
        "pending": [v for v in pending_reviews.values() if v.get("status") == "pending"]
    }


@app.post("/api/hitl/review/{session_id}")
async def review_pending(session_id: str, request: ReviewDecisionRequest):
    item = pending_reviews.get(session_id)
    if not item:
        raise HTTPException(status_code=404, detail="未找到待审核会话")
    if item.get("status") != "pending":
        raise HTTPException(status_code=400, detail="该会话不在待审核状态")
    if request.action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="action必须是approve或reject")

    item["status"] = request.action
    item["reviewer"] = request.reviewer
    item["review_message"] = request.message
    item["reviewed_at"] = time.time()

    assistant_msg = (
        "人工审核已通过，之前的自动回复已放行。"
        if request.action == "approve"
        else "人工审核未通过，已转人工客服继续处理。"
    )
    await short_term_memory.add_message(session_id, "assistant", assistant_msg)

    return {
        "session_id": session_id,
        "status": item["status"],
        "reviewer": item["reviewer"],
        "message": assistant_msg,
    }


@app.get("/api/history/{session_id}")
async def get_history(session_id: str):
    """获取对话历史"""
    history = await short_term_memory.get_history(session_id)
    return {"session_id": session_id, "messages": history}


@app.get("/api/tools")
async def list_tools():
    """MCP工具发现接口"""
    return {"tools": mcp_server.list_tools()}


@app.post("/api/tools/call")
async def call_tool(
    request: dict,
    x_user_id: str = Header(default="api-user"),
    x_user_role: str = Header(default="agent"),
):
    """MCP工具调用接口"""
    result = await mcp_server.call_tool(
        name=request.get("name", ""),
        arguments=request.get("arguments", {}),
        actor_id=x_user_id,
        actor_role=x_user_role,
    )
    return {
        "success": result.success,
        "result": result.result,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


@app.post("/api/mcp/jsonrpc")
async def mcp_jsonrpc(request: dict):
    """统一MCP JSON-RPC入口（含tools/resources/prompts）"""
    return await mcp_server.handle_jsonrpc(request)


@app.get("/api/resources")
async def list_resources():
    return {"resources": mcp_server.list_resources()}


@app.get("/api/resources/read")
async def read_resource(uri: str):
    return mcp_server.read_resource(uri)


@app.get("/api/prompts")
async def list_prompts():
    return {"prompts": mcp_server.list_prompts()}


@app.post("/api/prompts/get")
async def get_prompt(request: dict):
    return mcp_server.get_prompt(
        name=request.get("name", ""),
        arguments=request.get("arguments", {}),
    )


@app.post("/api/eval/routing")
async def eval_routing(request: RoutingEvalRequest):
    """
    基于标注集评估路由准确率。
    每条样本都会实际跑完整图并对比expected_intent。
    """
    if graph is None:
        raise HTTPException(status_code=503, detail="系统初始化中")
    if not request.samples:
        raise HTTPException(status_code=400, detail="samples不能为空")

    from langchain_core.messages import HumanMessage

    details = []
    correct = 0
    for sample in request.samples:
        session_id = f"eval-{uuid.uuid4()}"
        initial_state = {
            "messages": [HumanMessage(content=sample.message)],
            "user_id": "evaluator",
            "session_id": session_id,
            "intent": "",
            "sub_results": {},
            "compliance_passed": True,
            "final_response": "",
            "current_agent": "",
            "retry_count": 0,
        }
        config = {"configurable": {"thread_id": session_id}}
        result = await _run_graph_with_retry(initial_state, config=config)
        predicted = result.get("intent", "unknown")
        is_correct = predicted == sample.expected_intent
        if is_correct:
            correct += 1
        details.append(
            {
                "message": sample.message,
                "expected_intent": sample.expected_intent,
                "predicted_intent": predicted,
                "correct": is_correct,
            }
        )

    accuracy = correct / len(request.samples)
    return {
        "total": len(request.samples),
        "correct": correct,
        "accuracy": accuracy,
        "details": details,
    }


@app.get("/api/metrics")
async def get_metrics():
    """获取系统指标"""
    return {
        "metrics": metrics.get_summary(),
        "tool_call_log": mcp_server.get_call_log(last_n=20),
        "tool_audit_log": mcp_server.get_audit_log(last_n=20),
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
