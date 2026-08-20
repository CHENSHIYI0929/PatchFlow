from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


@dataclass(frozen=True)
class MCPToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class MCPClient(Protocol):
    def list_tools(self) -> list[MCPToolSpec]: ...
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class _SessionClient:
    async def _with_session(self, operation):
        raise NotImplementedError

    def list_tools(self) -> list[MCPToolSpec]:
        async def operation(session: ClientSession):
            response = await session.list_tools()
            return [
                MCPToolSpec(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema,
                )
                for tool in response.tools
            ]

        return anyio.run(self._with_session, operation)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async def operation(session: ClientSession):
            response = await session.call_tool(name, arguments)
            return response.model_dump(mode="json", by_alias=True)

        return anyio.run(self._with_session, operation)


class StdioMCPClient(_SessionClient):
    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        self.parameters = StdioServerParameters(command=command, args=args or [], env=env)

    async def _with_session(self, operation):
        async with stdio_client(self.parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await operation(session)


class StreamableHTTPMCPClient(_SessionClient):
    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers

    async def _with_session(self, operation):
        async with create_mcp_http_client(headers=self.headers) as http_client:
            async with streamable_http_client(self.url, http_client=http_client) as streams:
                read, write, _ = streams
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await operation(session)
