"""MCP server exposing the DJ tools to any MCP client (Claude Desktop, Claude Code, Cursor, ...)."""

from __future__ import annotations

import logging
from typing import Any

from . import __version__
from .guide import DJ_GUIDE
from .runtime import Runtime, build_runtime

log = logging.getLogger(__name__)


def build_server(runtime: Runtime):
    import mcp_types as types
    from mcp.server import Server

    tools = runtime.tools

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=t.name, description=t.description, input_schema=t.schema)
                for t in tools.tools.values()
            ]
        )

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        text, is_error = await tools.call_json(params.name, params.arguments or {})
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)

    return Server(
        "djplusai",
        version=__version__,
        title="DJ+AI for Mixxx",
        instructions=DJ_GUIDE,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve_stdio(**runtime_kwargs: Any) -> None:
    from mcp.server.stdio import stdio_server

    runtime = build_runtime(**runtime_kwargs)
    await runtime.start()
    server = build_server(runtime)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await runtime.close()
