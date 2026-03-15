"""Bridge between Anthropic tool_use API and the CML MCP server."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


class MCPBridge:
    """Manages a long-lived connection to the cml-mcp stdio server."""

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._tools: list[dict] = []
        self._tool_names: set[str] = set()

    async def connect(self) -> None:
        env = {
            **os.environ,
            "CML_URL": os.getenv("CML_URL", ""),
            "CML_USERNAME": os.getenv("CML_USERNAME", ""),
            "CML_PASSWORD": os.getenv("CML_PASSWORD", ""),
            "CML_VERIFY_SSL": os.getenv("CML_VERIFY_SSL", "false"),
        }

        params = StdioServerParameters(command="cml-mcp", env=env)

        self._exit_stack = AsyncExitStack()
        stdio_transport = await self._exit_stack.enter_async_context(stdio_client(params))
        read_stream, write_stream = stdio_transport
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()

        result = await self._session.list_tools()
        self._tools = []
        self._tool_names = set()
        for tool in result.tools:
            self._tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            })
            self._tool_names.add(tool.name)

        logger.info(f"MCP bridge connected: {len(self._tools)} tools available")

    async def disconnect(self) -> None:
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None
        logger.info("MCP bridge disconnected")

    def get_anthropic_tools(self) -> list[dict]:
        return self._tools

    def has_tool(self, name: str) -> bool:
        return name in self._tool_names

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if not self._session:
            raise RuntimeError("MCP bridge not connected")

        logger.info(f"MCP call: {name}({list(arguments.keys())})")
        try:
            result = await self._session.call_tool(name, arguments)
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                else:
                    parts.append(str(block))
            text = "\n".join(parts)
            logger.info(f"MCP result: {name} -> {text[:200]}")
            return text
        except Exception as e:
            logger.error(f"MCP tool error: {name}: {e}")
            if "Closed" in str(e) or "closed" in str(e):
                logger.info("MCP session lost, reconnecting...")
                try:
                    await self.disconnect()
                    await self.connect()
                except Exception:
                    pass
            return f"Error calling {name}: {e}"
