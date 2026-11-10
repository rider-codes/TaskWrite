"""MCP client: connect to user-configured local MCP servers over stdio.

Servers come from a Claude-Desktop-style JSON file ({"mcpServers": {...}}). TaskWrite
never edits that file and never starts a server the user did not list. Each discovery
or call starts the server process, talks to it, and shuts it down again.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jsonschema
from pydantic import BaseModel, Field, ValidationError

from .safety import forbidden_reason, redact
from .tools import MCP_TOOL_RE, mcp_tool_id, split_mcp_id

SERVER_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
CONNECT_TIMEOUT_S = 90.0  # first run of an npx server may download it
CALL_TIMEOUT_S = 60.0
MAX_RESULT_CHARS = 4000
MAX_TOOLS_PER_SERVER = 100
MAX_ARGS_CHARS = 8000


class McpError(Exception):
    """A user-facing MCP problem (config, connection, validation, or tool failure)."""


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ServerConfig(BaseModel):
    name: str
    command: str = Field(default="", max_length=500)
    url: str = Field(default="", max_length=300)  # a local HTTP server (loopback only), instead of a command
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class McpTool(BaseModel):
    id: str
    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    read_only: bool | None = None  # True only when the server marks it readOnlyHint


class ServerStatus(BaseModel):
    name: str
    command: str
    status: str  # not_connected | connected | error
    error: str = ""
    tools: list[McpTool] = Field(default_factory=list)
    hidden: list[str] = Field(default_factory=list)  # tools TaskWrite blocks outright (send, share, delete, shell)


class CallResult(BaseModel):
    ok: bool
    text: str
    truncated: bool = False


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(values: Any) -> dict[str, str]:
    """Fill in ${NAME} from the environment (which holds the .env file), so secrets stay out of the config file.

    Raises KeyError(name) for a name that is not set; the caller turns that into a problem without echoing any value.
    """
    if not isinstance(values, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in values.items():
        if not isinstance(raw, str):
            out[key] = raw  # left for the model validation to reject
            continue
        out[key] = _ENV_REF.sub(lambda m: _lookup_env(m.group(1)), raw)
    return out


def _lookup_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise KeyError(name)
    return value


def load_config(path: Path) -> tuple[dict[str, ServerConfig], list[str]]:
    """Parse the config file. Returns (servers, problems); a missing file is not a problem."""
    if not path.is_file():
        return {}, []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, [f"Could not read {path.name}: {type(exc).__name__}"]
    entries = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}, [f"{path.name} must contain an 'mcpServers' object."]
    servers: dict[str, ServerConfig] = {}
    problems: list[str] = []
    for name, entry in entries.items():
        if not SERVER_NAME_RE.match(str(name)):
            problems.append(f"Server name '{name}' must be 1-32 characters of a-z, 0-9, _ or -.")
            continue
        if not isinstance(entry, dict) or (("command" in entry) == ("url" in entry)):
            problems.append(f"Server '{name}': give exactly one of 'command' (a local program) or 'url' "
                            "(a server on this computer, loopback addresses only).")
            continue
        try:
            entry = {**entry, "env": _expand_env(entry.get("env") or {})}
        except KeyError as missing:
            problems.append(f"Server '{name}': {missing.args[0]} is not set. Add it to your .env file, then restart TaskWrite.")
            continue
        if "url" in entry:
            if not _is_loopback_url(entry["url"]):
                problems.append(f"Server '{name}': only loopback addresses are allowed for 'url' "
                                "(http://127.0.0.1:PORT/..., http://localhost:PORT/... or http://[::1]:PORT/...).")
                continue
            servers[name] = ServerConfig(name=name, url=entry["url"].strip(), env=entry.get("env", {}) or {})
            continue
        try:
            servers[name] = ServerConfig(name=name, command=entry["command"], args=entry.get("args", []),
                                         env=entry.get("env", {}))
        except ValidationError:
            problems.append(f"Server '{name}': 'command' must be text, 'args' a list of text, 'env' text values.")
    return servers, problems


def _is_loopback_url(value: Any) -> bool:
    """Only http(s) addresses that stay on this computer, with no login in the address."""
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower()
        parsed.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and host in LOOPBACK_HOSTS and not parsed.username and not parsed.password


@asynccontextmanager
async def _transport(cfg: ServerConfig):
    """Open the server the way its config says: a local program over stdio, or a loopback HTTP server."""
    if cfg.url:
        import httpx
        from mcp.client.streamable_http import streamablehttp_client

        def no_redirects(headers=None, timeout=None, auth=None):  # a loopback server may not bounce us elsewhere
            return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, follow_redirects=False)

        async with streamablehttp_client(cfg.url, httpx_client_factory=no_redirects) as (read, write, _):
            yield read, write
    else:
        from mcp.client.stdio import stdio_client

        async with stdio_client(_params(cfg)) as (read, write):
            yield read, write


def _short(exc: BaseException) -> str:
    """Unwrap anyio/asyncio exception groups into one short line."""
    inner = getattr(exc, "exceptions", None)
    while inner:
        exc = inner[0]
        inner = getattr(exc, "exceptions", None)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timed out"
    return f"{type(exc).__name__}: {str(exc)[:160]}".rstrip(": ")


def _params(cfg: ServerConfig):
    from mcp import StdioServerParameters

    return StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env or None)


def _to_tool(server: str, t: Any) -> McpTool | None:
    tool_id = mcp_tool_id(server, t.name)
    if not MCP_TOOL_RE.match(tool_id):
        return None
    if forbidden_reason(t.name):
        return None  # never offered, never callable
    ann = getattr(t, "annotations", None)
    hint = getattr(ann, "readOnlyHint", None) if ann is not None else None
    read_only = hint if isinstance(hint, bool) else None
    return McpTool(id=tool_id, server=server, name=t.name, description=(t.description or "")[:300],
                   input_schema=dict(t.inputSchema or {}), read_only=read_only)


def validate_arguments(tool: McpTool, arguments: Any) -> dict[str, Any]:
    """Check model-proposed arguments against the tool's own JSON schema before any call."""
    if not isinstance(arguments, dict):
        raise McpError("Tool arguments must be a JSON object.")
    if len(json.dumps(arguments, default=str)) > MAX_ARGS_CHARS:
        raise McpError("Tool arguments are too large.")
    try:
        jsonschema.validate(arguments, tool.input_schema or {"type": "object"})
    except jsonschema.ValidationError as exc:
        raise McpError(f"Arguments do not match the tool's schema: {exc.message[:160]}") from exc
    except jsonschema.SchemaError:
        pass  # a malformed server schema should not block a read-only look-up
    return arguments


class McpManager:
    def __init__(self, config_path: str | Path, extra_secrets: tuple[str, ...] = ()) -> None:
        self.config_path = Path(config_path)
        self._extra_secrets = extra_secrets
        self._tools: dict[str, list[McpTool]] = {}
        self._errors: dict[str, str] = {}
        self._hidden: dict[str, list[str]] = {}
        self._lock = threading.Lock()  # one refresh at a time
        self.connecting = False  # True while a background connect is under way

    # --- config and cached state ---------------------------------------------

    def servers(self) -> tuple[dict[str, ServerConfig], list[str]]:
        return load_config(self.config_path)

    def _secrets(self, cfg: ServerConfig | None = None) -> tuple[str, ...]:
        values = tuple(v for v in (cfg.env.values() if cfg else ()) if len(v) >= 6)
        return self._extra_secrets + values

    def all_tools(self) -> list[McpTool]:
        servers, _ = self.servers()
        return [t for name in servers for t in self._tools.get(name, [])]

    def get_tool(self, tool_id: str) -> McpTool | None:
        return next((t for t in self.all_tools() if t.id == tool_id), None)

    def status(self) -> tuple[list[ServerStatus], list[str]]:
        servers, problems = self.servers()
        out = []
        for name, cfg in servers.items():
            tools = self._tools.get(name, [])
            state = "error" if name in self._errors else ("connected" if tools else "not_connected")
            shown = cfg.url if cfg.url else " ".join([cfg.command, *cfg.args])[:200]
            out.append(ServerStatus(name=name, command=shown, status=state, error=self._errors.get(name, ""), tools=tools,
                                    hidden=self._hidden.get(name, [])))
        return out, problems

    # --- discovery and calls (synchronous wrappers around the async SDK) ------

    def refresh(self) -> tuple[list[ServerStatus], list[str]]:
        with self._lock:
            servers, _ = self.servers()
            self._tools = {k: v for k, v in self._tools.items() if k in servers}
            self._errors = {}
            self._hidden = {}
            for name, cfg in servers.items():
                try:
                    self._tools[name] = asyncio.run(asyncio.wait_for(self._discover(cfg), CONNECT_TIMEOUT_S))
                except Exception as exc:  # noqa: BLE001 - report any server failure to the user
                    self._tools.pop(name, None)
                    self._errors[name] = redact(_short(exc), self._secrets(cfg))
        return self.status()

    def refresh_in_background(self) -> threading.Thread:
        """Connect the listed servers without holding up start-up. A failure is recorded per server, never raised."""
        self.connecting = True

        def work() -> None:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - start-up must never fail because of a connector
                pass
            finally:
                self.connecting = False

        thread = threading.Thread(target=work, name="mcp-autoconnect", daemon=True)
        thread.start()
        return thread

    async def _discover(self, cfg: ServerConfig) -> list[McpTool]:
        from mcp import ClientSession

        found: list[McpTool] = []
        async with _transport(cfg) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                cursor = None
                while True:
                    page = await session.list_tools(cursor)
                    found.extend(t for t in (_to_tool(cfg.name, x) for x in page.tools) if t)
                    self._hidden[cfg.name] = sorted(set(self._hidden.get(cfg.name, [])) | {x.name for x in page.tools if forbidden_reason(x.name)})
                    cursor = page.nextCursor
                    if not cursor or len(found) >= MAX_TOOLS_PER_SERVER:
                        break
        return found[:MAX_TOOLS_PER_SERVER]

    def call(self, tool_id: str, arguments: dict[str, Any]) -> CallResult:
        servers, _ = self.servers()
        server, name = split_mcp_id(tool_id)
        if (why := forbidden_reason(name)):
            raise McpError(f"Tool '{tool_id}' is blocked by TaskWrite and never runs ({why}).")
        cfg = servers.get(server)
        if cfg is None:
            raise McpError(f"Server '{server}' is not in the MCP config.")
        tool = self.get_tool(tool_id)
        if tool is None:
            raise McpError(f"Tool '{tool_id}' was not discovered on server '{server}'. Refresh MCP first.")
        validate_arguments(tool, arguments)
        try:
            return asyncio.run(asyncio.wait_for(self._call(cfg, name, arguments), CALL_TIMEOUT_S + 15))
        except McpError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise McpError(redact(f"Calling {tool_id} failed: {_short(exc)}", self._secrets(cfg))) from exc

    async def _call(self, cfg: ServerConfig, name: str, arguments: dict[str, Any]) -> CallResult:
        from mcp import ClientSession

        async with _transport(cfg) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments,
                                                 read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT_S))
        parts = [c.text if getattr(c, "type", "") == "text" else f"[{getattr(c, 'type', 'unknown')} content omitted]"
                 for c in result.content]
        text = redact("\n".join(parts), self._secrets(cfg))
        return CallResult(ok=not result.isError, text=text[:MAX_RESULT_CHARS], truncated=len(text) > MAX_RESULT_CHARS)
