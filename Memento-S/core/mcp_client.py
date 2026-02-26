"""MCP tool manager – connects to FastMCP server with LangChain integration.

Uses ``fastmcp.Client`` for in-memory MCP transport and wraps discovered
tools as LangChain ``StructuredTool`` instances for use with
LangChain / LangGraph agents.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastmcp import Client
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from core.mcp_server import mcp as _mcp_server, configure as _configure_server


class MCPToolManager:
    """Wraps the in-process FastMCP server, exposes LangChain tools."""

    def __init__(self, *, event_sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._client: Client | None = None
        self._langchain_tools: list[StructuredTool] = []
        self._openai_tools: list[dict[str, Any]] = []
        self._event_sink = event_sink

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, *, base_dir: Path | None = None) -> None:
        """Start the in-process MCP server and discover tools."""
        _configure_server(base_dir=base_dir)
        self._client = Client(_mcp_server)
        await self._client.__aenter__()
        raw_tools = await self._client.list_tools()
        self._langchain_tools = _mcp_tools_to_langchain(raw_tools, self._client, self._event_sink)
        self._openai_tools = _mcp_tools_to_openai(raw_tools)

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

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
        """Call a tool by name and return the result as a string."""
        if self._client is None:
            return "ERR: MCP client not started"
        result = await self._client.call_tool(tool_name, arguments)
        return _extract_text(result)

    def reconfigure(self, *, base_dir: Path | None = None) -> None:
        """Update server context without restart."""
        _configure_server(base_dir=base_dir)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_event(event_sink: Callable[[dict[str, Any]], None] | None, event: str, **fields: Any) -> None:
    if event_sink is None:
        return
    payload: dict[str, Any] = {"ts": _utc_now_iso(), "event": str(event)}
    payload.update(fields)
    try:
        event_sink(payload)
    except Exception:
        # Tracing must never break tool execution.
        pass


def _preview_jsonable(value: Any, max_len: int = 400) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:
        text = repr(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


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
    event_sink: Callable[[dict[str, Any]], None] | None = None,
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
            _event_sink: Callable[[dict[str, Any]], None] | None = event_sink,
            **kwargs: Any,
        ) -> str:
            kwargs = _coerce_tool_args(kwargs, _schema)
            t0 = time.perf_counter()
            _emit_event(
                _event_sink,
                "tool_call_start",
                tool_name=_name,
                args_preview=_preview_jsonable(kwargs),
            )
            try:
                result = await _client_ref.call_tool(_name, kwargs)
                text = _extract_text(result)
            except Exception as exc:
                _emit_event(
                    _event_sink,
                    "tool_call_error",
                    tool_name=_name,
                    args_preview=_preview_jsonable(kwargs),
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=round((time.perf_counter() - t0) * 1000, 2),
                )
                raise
            _emit_event(
                _event_sink,
                "tool_call_end",
                tool_name=_name,
                args_preview=_preview_jsonable(kwargs),
                result_preview=_preview_jsonable(text),
                duration_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
            return text

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
