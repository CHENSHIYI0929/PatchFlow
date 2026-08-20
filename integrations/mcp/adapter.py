from __future__ import annotations

import json
import re
from typing import Any

from integrations.mcp.client import MCPClient, MCPToolSpec
from tools.base import BaseTool, ToolRegistry, ToolResult


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)


class MCPToolAdapter(BaseTool):
    def __init__(self, server_name: str, spec: MCPToolSpec, client: MCPClient, max_output_chars: int = 32_000) -> None:
        self.server_name = _safe_name(server_name)
        self.spec = spec
        self.client = client
        self.max_output_chars = max_output_chars

    @property
    def name(self) -> str:
        return f"mcp__{self.server_name}__{_safe_name(self.spec.name)}"

    @property
    def description(self) -> str:
        return self.spec.description or f"MCP tool {self.spec.name} from {self.server_name}"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return self.spec.input_schema

    def execute(self, params: dict[str, Any]) -> ToolResult:
        try:
            response = self.client.call_tool(self.spec.name, params)
            content = response.get("content") or []
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            output = "\n".join(parts)[: self.max_output_chars]
            failed = bool(response.get("isError") or response.get("is_error"))
            return ToolResult(
                success=not failed,
                output=output,
                error=output if failed else None,
                metadata={"mcp_server": self.server_name, "mcp_tool": self.spec.name},
                failure_type="tool_failure" if failed else None,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"MCP tool failed: {exc}",
                metadata={"mcp_server": self.server_name, "mcp_tool": self.spec.name},
                failure_type="tool_failure",
            )


def register_mcp_tools(
    registry: ToolRegistry,
    server_name: str,
    client: MCPClient,
    *,
    allowed_tools: set[str] | None = None,
) -> ToolRegistry:
    for spec in client.list_tools():
        if allowed_tools is None or spec.name in allowed_tools:
            registry.register(MCPToolAdapter(server_name, spec, client))
    return registry
