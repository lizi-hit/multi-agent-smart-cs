"""
MCP工具协议服务端 — Model Context Protocol实现
遵循Anthropic MCP标准，通过JSON-RPC 2.0提供工具注册/发现/调用能力。
支持动态工具扩展，Agent通过统一接口调用外部系统。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
from datetime import datetime

import httpx

@dataclass
class ToolDefinition:
    """MCP工具定义"""
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    category: str = "general"
    requires_auth: bool = False


@dataclass
class ToolCallResult:
    """工具调用结果"""
    tool_name: str
    success: bool
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class AuditRecord:
    action: str
    target: str
    success: bool
    actor_id: str
    actor_role: str
    message: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    duration_ms: float = 0.0


class MCPToolServer:
    """
    MCP工具服务端。

    实现 Model Context Protocol 的核心功能：
    1. 工具注册 (Tool Registration)
    2. 工具发现 (Tool Discovery) - Agent可查询可用工具列表
    3. 工具调用 (Tool Invocation) - 通过JSON-RPC 2.0协议调用
    4. 结果返回 (Result Delivery)

    遵循MCP规范：
    - 使用JSON-RPC 2.0消息格式
    - 支持工具的inputSchema声明
    - 提供标准化的错误码
    """

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._call_log: list[ToolCallResult] = []
        self._audit_log: list[AuditRecord] = []
        self._resources: dict[str, dict[str, Any]] = {
            "kb://refund_policy": {
                "uri": "kb://refund_policy",
                "name": "退款政策",
                "description": "退款相关规则与时效",
                "mimeType": "text/plain",
                "content": "7天无理由退款，超过7天需提供合理原因，3-5个工作日原路退回。",
            },
            "kb://account_open": {
                "uri": "kb://account_open",
                "name": "开户流程",
                "description": "用户开户步骤",
                "mimeType": "text/plain",
                "content": "准备身份证->填写申请->视频认证->设置密码->风险测评。",
            },
        }
        self._prompts: dict[str, dict[str, Any]] = {
            "customer_reply_template": {
                "name": "customer_reply_template",
                "description": "客服标准回复模板",
                "arguments": [
                    {"name": "tone", "required": False, "description": "回复语气"},
                    {"name": "topic", "required": True, "description": "问题主题"},
                ],
                "template": "请用{tone}语气回答用户关于{topic}的问题，要求专业、清晰、避免承诺收益。",
            }
        }
        self._tool_policies: dict[str, set[str]] = {
            "admin": {"*"},
            "agent": {"order_query", "knowledge_search", "ticket_create", "risk_check"},
            "auditor": {"order_query", "knowledge_search", "risk_check"},
            "viewer": {"knowledge_search"},
        }

    def register_tool(self, tool: ToolDefinition) -> None:
        """注册一个MCP工具"""
        self._tools[tool.name] = tool

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        category: str = "general",
        requires_auth: bool = False,
    ) -> Callable:
        """工具注册装饰器"""
        def decorator(func: Callable[..., Awaitable[Any]]) -> Callable:
            tool = ToolDefinition(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=func,
                category=category,
                requires_auth=requires_auth,
            )
            self._tools[name] = tool
            return func
        return decorator

    def list_tools(self, category: str | None = None) -> list[dict]:
        """
        工具发现：列出所有可用工具。
        对应MCP的 tools/list 方法。
        """
        tools = []
        for tool in self._tools.values():
            if category and tool.category != category:
                continue
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "category": tool.category,
            })
        return tools

    def list_resources(self) -> list[dict]:
        return [
            {
                "uri": r["uri"],
                "name": r["name"],
                "description": r["description"],
                "mimeType": r["mimeType"],
            }
            for r in self._resources.values()
        ]

    def read_resource(self, uri: str) -> dict[str, Any]:
        resource = self._resources.get(uri)
        if resource is None:
            raise ValueError(f"Resource not found: {uri}")
        return {
            "uri": resource["uri"],
            "mimeType": resource["mimeType"],
            "content": resource["content"],
        }

    def list_prompts(self) -> list[dict]:
        return [
            {
                "name": p["name"],
                "description": p["description"],
                "arguments": p["arguments"],
            }
            for p in self._prompts.values()
        ]

    def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        prompt = self._prompts.get(name)
        if prompt is None:
            raise ValueError(f"Prompt not found: {name}")

        arguments = arguments or {}
        topic = arguments.get("topic", "")
        if not topic:
            raise ValueError("Prompt argument 'topic' is required")
        tone = arguments.get("tone", "专业")
        content = prompt["template"].format(tone=tone, topic=topic)
        return {
            "name": name,
            "description": prompt["description"],
            "messages": [{"role": "system", "content": content}],
        }

    @staticmethod
    def _validate_type(value: Any, schema_type: str) -> bool:
        if schema_type == "string":
            return isinstance(value, str)
        if schema_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if schema_type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if schema_type == "boolean":
            return isinstance(value, bool)
        if schema_type == "object":
            return isinstance(value, dict)
        if schema_type == "array":
            return isinstance(value, list)
        return True

    def _validate_arguments(self, input_schema: dict[str, Any], arguments: dict[str, Any]) -> None:
        required = input_schema.get("required", [])
        properties = input_schema.get("properties", {})

        for field_name in required:
            if field_name not in arguments:
                raise ValueError(f"Missing required field: {field_name}")

        for field_name, value in arguments.items():
            field_schema = properties.get(field_name)
            if field_schema is None:
                continue
            expected_type = field_schema.get("type")
            if expected_type and not self._validate_type(value, expected_type):
                raise ValueError(f"Invalid type for '{field_name}', expected {expected_type}")
            enum_values = field_schema.get("enum")
            if enum_values and value not in enum_values:
                raise ValueError(f"Invalid value for '{field_name}', expected one of {enum_values}")

    def _authorize(self, tool_name: str, actor_role: str) -> bool:
        allowed = self._tool_policies.get(actor_role, set())
        return "*" in allowed or tool_name in allowed

    def _sanitize_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        redacted = {}
        sensitive_pattern = re.compile(r"(key|token|password|secret)", re.IGNORECASE)
        for k, v in arguments.items():
            if sensitive_pattern.search(k):
                redacted[k] = "***"
            else:
                redacted[k] = v
        return redacted

    def _record_audit(
        self,
        action: str,
        target: str,
        success: bool,
        actor_id: str,
        actor_role: str,
        message: str = "",
        duration_ms: float = 0.0,
    ) -> None:
        self._audit_log.append(
            AuditRecord(
                action=action,
                target=target,
                success=success,
                actor_id=actor_id,
                actor_role=actor_role,
                message=message,
                duration_ms=duration_ms,
            )
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        actor_id: str = "system",
        actor_role: str = "agent",
    ) -> ToolCallResult:
        """
        工具调用：执行指定工具。
        对应MCP的 tools/call 方法。
        """
        import time

        tool = self._tools.get(name)
        if tool is None:
            result = ToolCallResult(
                tool_name=name,
                success=False,
                error=f"Tool '{name}' not found. Available: {list(self._tools.keys())}",
            )
            self._call_log.append(result)
            self._record_audit(
                action="tools/call",
                target=name,
                success=False,
                actor_id=actor_id,
                actor_role=actor_role,
                message=result.error or "",
            )
            return result

        if not self._authorize(name, actor_role):
            result = ToolCallResult(
                tool_name=name,
                success=False,
                error=f"Access denied for role '{actor_role}' on tool '{name}'",
            )
            self._call_log.append(result)
            self._record_audit(
                action="tools/call",
                target=name,
                success=False,
                actor_id=actor_id,
                actor_role=actor_role,
                message=result.error or "",
            )
            return result

        start = time.time()
        try:
            self._validate_arguments(tool.input_schema, arguments)
            output = await tool.handler(**arguments)
            duration_ms = (time.time() - start) * 1000

            result = ToolCallResult(
                tool_name=name,
                success=True,
                result=output,
                duration_ms=duration_ms,
            )
            self._record_audit(
                action="tools/call",
                target=name,
                success=True,
                actor_id=actor_id,
                actor_role=actor_role,
                message=json.dumps(self._sanitize_arguments(arguments), ensure_ascii=False),
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start) * 1000
            result = ToolCallResult(
                tool_name=name,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )
            self._record_audit(
                action="tools/call",
                target=name,
                success=False,
                actor_id=actor_id,
                actor_role=actor_role,
                message=str(e),
                duration_ms=duration_ms,
            )

        self._call_log.append(result)
        return result

    async def handle_jsonrpc(self, request: dict) -> dict:
        """
        处理JSON-RPC 2.0请求。
        MCP协议传输层实现。
        """
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id", 1)

        try:
            if method == "tools/list":
                result = self.list_tools(category=params.get("category"))
            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                actor = params.get("actor", {})
                call_result = await self.call_tool(
                    tool_name,
                    arguments,
                    actor_id=actor.get("id", "system"),
                    actor_role=actor.get("role", "agent"),
                )
                result = {
                    "success": call_result.success,
                    "result": call_result.result,
                    "error": call_result.error,
                }
            elif method == "resources/list":
                result = self.list_resources()
            elif method == "resources/read":
                result = self.read_resource(params.get("uri", ""))
            elif method == "prompts/list":
                result = self.list_prompts()
            elif method == "prompts/get":
                result = self.get_prompt(
                    params.get("name", ""),
                    params.get("arguments", {}),
                )
            elif method == "ping":
                result = {"status": "ok"}
            else:
                return {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": req_id,
                }

            return {"jsonrpc": "2.0", "result": result, "id": req_id}

        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": str(e)},
                "id": req_id,
            }

    def get_call_log(self, last_n: int = 100) -> list[dict]:
        """获取最近的工具调用日志"""
        return [
            {
                "tool": r.tool_name,
                "success": r.success,
                "duration_ms": r.duration_ms,
                "timestamp": r.timestamp,
                "error": r.error,
            }
            for r in self._call_log[-last_n:]
        ]

    def get_audit_log(self, last_n: int = 100) -> list[dict]:
        return [
            {
                "action": r.action,
                "target": r.target,
                "success": r.success,
                "actor_id": r.actor_id,
                "actor_role": r.actor_role,
                "message": r.message,
                "duration_ms": r.duration_ms,
                "timestamp": r.timestamp,
            }
            for r in self._audit_log[-last_n:]
        ]


def create_default_tools(server: MCPToolServer) -> MCPToolServer:
    """注册默认的MCP工具集"""
    order_service_url = os.getenv("ORDER_SERVICE_URL", "").strip()
    ticket_service_url = os.getenv("TICKET_SERVICE_URL", "").strip()
    risk_service_url = os.getenv("RISK_SERVICE_URL", "").strip()

    async def call_external_json(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not base_url:
            return None
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return response.json()
        except Exception:
            return None

    @server.register(
        name="order_query",
        description="查询订单信息，支持按订单号或用户ID查询",
        input_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "user_id": {"type": "string", "description": "用户ID"},
            },
        },
        category="order",
    )
    async def order_query(order_id: str = "", user_id: str = "") -> dict:
        external = await call_external_json(
            order_service_url,
            "/query",
            {"order_id": order_id, "user_id": user_id},
        )
        if external is not None:
            return external
        return {
            "order_id": order_id or "ORD-20260401-001",
            "status": "shipped",
            "amount": 299.00,
            "product": "智能理财产品A",
            "created_at": "2026-04-01T10:00:00",
        }

    @server.register(
        name="knowledge_search",
        description="搜索企业知识库，返回相关文档片段",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "top_k": {"type": "integer", "description": "返回数量", "default": 3},
            },
            "required": ["query"],
        },
        category="knowledge",
    )
    async def knowledge_search(query: str, top_k: int = 3) -> list[dict]:
        return [
            {"content": f"关于'{query}'的知识库文档片段", "source": "FAQ.md", "score": 0.95},
        ]

    @server.register(
        name="ticket_create",
        description="创建客服工单",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
                "category": {"type": "string"},
            },
            "required": ["title", "description"],
        },
        category="ticket",
    )
    async def ticket_create(title: str, description: str, priority: str = "medium", category: str = "general") -> dict:
        external = await call_external_json(
            ticket_service_url,
            "/create",
            {
                "title": title,
                "description": description,
                "priority": priority,
                "category": category,
            },
        )
        if external is not None:
            return external
        import uuid
        return {
            "ticket_id": f"TK-{uuid.uuid4().hex[:8].upper()}",
            "title": title,
            "status": "created",
            "priority": priority,
        }

    @server.register(
        name="risk_check",
        description="风控接口 — 检查交易/操作的风险等级",
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "action": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["user_id", "action"],
        },
        category="compliance",
    )
    async def risk_check(user_id: str, action: str, amount: float = 0.0) -> dict:
        external = await call_external_json(
            risk_service_url,
            "/check",
            {"user_id": user_id, "action": action, "amount": amount},
        )
        if external is not None:
            return external
        risk_level = "low"
        if amount > 50000:
            risk_level = "high"
        elif amount > 10000:
            risk_level = "medium"

        return {
            "user_id": user_id,
            "action": action,
            "risk_level": risk_level,
            "requires_manual_review": risk_level == "high",
        }

    return server
