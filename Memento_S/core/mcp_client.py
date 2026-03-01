"""MCP tool manager – connects to one or more FastMCP servers with LangChain integration.

Uses ``fastmcp.Client`` for in-memory MCP transport and wraps discovered
tools as LangChain ``StructuredTool`` instances for use with
LangChain / LangGraph agents.

Supports multiple in-process FastMCP servers via the ``extra_servers`` param.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from fastmcp import Client, FastMCP
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from core.mcp_server import mcp as _mcp_server, configure as _configure_server
from core.utils.logging_utils import log_event


class MCPToolManager:
    """Wraps one or more in-process FastMCP servers, exposes LangChain tools.

    Args:
        extra_servers: Optional list of ``(FastMCP, configure_fn | None)`` tuples.
            Each extra server is connected alongside the core server.
    """

    def __init__(
        self,
        extra_servers: list[tuple[FastMCP, Callable | None]] | None = None,
    ) -> None:
        self._extra_servers = extra_servers or []
        self._clients: list[Client] = []
        self._tool_to_client: dict[str, Client] = {}
        self._langchain_tools: list[StructuredTool] = []
        self._openai_tools: list[dict[str, Any]] = []
        # Store configure functions for reconfigure()
        self._configure_fns: list[Callable | None] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, *, base_dir: Path | None = None) -> None:
        """Start all MCP servers and discover tools."""
        # 1. Core server
        _configure_server(base_dir=base_dir)
        self._configure_fns = [_configure_server]
        await self._connect_server(_mcp_server)

        # 2. Extra servers
        for server_obj, configure_fn in self._extra_servers:
            if configure_fn is not None:
                configure_fn(base_dir=base_dir)
            self._configure_fns.append(configure_fn)
            await self._connect_server(server_obj)

    async def _connect_server(self, server_obj: FastMCP) -> None:
        """Connect to a single FastMCP server instance and merge its tools."""
        client = Client(server_obj)
        await client.__aenter__()
        self._clients.append(client)

        raw_tools = await client.list_tools()

        # Map tool names to the client that owns them
        for t in raw_tools:
            name = t.name if hasattr(t, "name") else str(t.get("name", ""))
            self._tool_to_client[name] = client

        # Merge into unified tool lists
        self._langchain_tools.extend(_mcp_tools_to_langchain(raw_tools, client))
        self._openai_tools.extend(_mcp_tools_to_openai(raw_tools))

    async def shutdown(self) -> None:
        """Close all MCP client connections."""
        for client in self._clients:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass
        self._clients.clear()
        self._tool_to_client.clear()
        self._langchain_tools.clear()
        self._openai_tools.clear()
        self._configure_fns.clear()

    # ------------------------------------------------------------------
    # Tool interfaces
    # ------------------------------------------------------------------

    def get_langchain_tools(self) -> list[StructuredTool]:
        """Return tools as LangChain ``StructuredTool`` instances."""
        return list(self._langchain_tools)

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions in OpenAI function-calling format."""
        return list(self._openai_tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool by name, routing to the correct server client."""
        client = self._tool_to_client.get(tool_name)
        if client is None:
            return f"ERR: unknown tool '{tool_name}'"
        result = await client.call_tool(tool_name, arguments)
        return _extract_text(result)

    def reconfigure(self, *, base_dir: Path | None = None) -> None:
        """Update all server contexts without restart."""
        for fn in self._configure_fns:
            if fn is not None:
                fn(base_dir=base_dir)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _coerce_tool_args(kwargs: dict[str, Any], schema: dict) -> dict[str, Any]:
    """Coerce stringified JSON values back to native types before FastMCP validates.

    LLMs often serialize array/object parameters as JSON strings (e.g.
    ``"[1, 50]"`` instead of ``[1, 50]``).  FastMCP's pydantic validation
    rejects these, so we parse them here.
    """
    props = schema.get("properties", {})
    out = dict(kwargs)
    for key, value in out.items():
        if not isinstance(value, str):
            continue
        prop_type = props.get(key, {}).get("type")
        if prop_type in ("array", "object"):
            stripped = value.strip()
            if stripped.startswith(("[", "{")):
                try:
                    out[key] = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    pass
    return out


def _extract_text(result: Any) -> str:
    """Extract text content from an MCP tool result."""
    if isinstance(result, list):
        parts: list[str] = []
        for block in result:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(result)


def _mcp_tools_to_langchain(
    tools: list,
    client: Client,
) -> list[StructuredTool]:
    """Convert MCP tools to LangChain ``StructuredTool`` instances."""
    lc_tools: list[StructuredTool] = []
    for t in tools:
        name = t.name if hasattr(t, "name") else str(t.get("name", ""))
        description = (
            t.description if hasattr(t, "description") else str(t.get("description", ""))
        )
        schema = (
            t.inputSchema if hasattr(t, "inputSchema") else t.get("inputSchema", {})
        )

        # Build async coroutine bound to this tool name
        _tool_name = name  # capture in closure
        _tool_schema = schema  # capture for coercion

        async def _call(
            _client_ref: Client = client,
            _name: str = _tool_name,
            _schema: dict = _tool_schema,
            **kwargs: Any,
        ) -> str:
            kwargs = _coerce_tool_args(kwargs, _schema)
            log_event("tool_call", tool_name=_name, arguments=kwargs)
            try:
                result = await _client_ref.call_tool(_name, kwargs)
                text = _extract_text(result)
                log_event("tool_result", tool_name=_name, result=text)
                return text
            except Exception as exc:
                log_event("tool_error", tool_name=_name, error=str(exc))
                raise

        lc_tools.append(
            StructuredTool(
                name=name,
                description=description,
                coroutine=_call,
                func=None,  # async-only
                args_schema=_json_schema_to_pydantic(name, schema),
            )
        )
    return lc_tools


def _json_schema_to_pydantic(tool_name: str, schema: dict) -> type[BaseModel]:
    """Convert a JSON schema dict to a Pydantic model class for ``args_schema``."""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}

    for prop_name, prop_schema in properties.items():
        prop_type_str = prop_schema.get("type", "string")
        python_type: type = str
        if prop_type_str == "integer":
            python_type = int
        elif prop_type_str == "number":
            python_type = float
        elif prop_type_str == "boolean":
            python_type = bool
        elif prop_type_str == "array":
            python_type = list
        elif prop_type_str == "object":
            python_type = dict

        field_desc = prop_schema.get("title", "") or prop_schema.get("description", "")
        default = prop_schema.get("default", ...)

        if prop_name in required:
            fields[prop_name] = (python_type, Field(description=field_desc))
        else:
            if default is ...:
                default = None
                python_type = python_type | None  # type: ignore[assignment]
            fields[prop_name] = (
                python_type,
                Field(default=default, description=field_desc),
            )

    model_name = f"{tool_name.title().replace('_', '')}Input"
    return create_model(model_name, **fields)


def _mcp_tools_to_openai(tools: list) -> list[dict[str, Any]]:
    """Convert MCP tool list to OpenAI function-calling format."""
    out: list[dict[str, Any]] = []
    for t in tools:
        name = t.name if hasattr(t, "name") else str(t.get("name", ""))
        description = (
            t.description if hasattr(t, "description") else str(t.get("description", ""))
        )
        schema = (
            t.inputSchema if hasattr(t, "inputSchema") else t.get("inputSchema", {})
        )
        parameters: dict[str, Any] = dict(schema) if isinstance(schema, dict) else {}
        parameters.setdefault("type", "object")
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            }
        )
    return out
