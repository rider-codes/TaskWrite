"""A stand-in for the VS Code MCP extension, served over loopback HTTP, for tests (no network).

It copies what the real extension does, as probed on 2026-11: paths are relative to the folder VS Code has open;
absolute paths are glued onto that folder and fail; `..` is NOT blocked (a real flaw TaskWrite must contain);
an existing file is refused unless overwrite=true; intermediate folders are created. It also exposes tools
TaskWrite must never use (shell, send, delete, share) so tests can prove they are never reached.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP


class FakeVscodeServer:
    def __init__(self, root: Path, lie_about_writes: bool = False, corrupt_writes: bool = False,
                 shell_enabled: bool = True) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lie_about_writes, self.corrupt_writes = lie_about_writes, corrupt_writes
        self.calls: list[tuple[str, dict]] = []
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._mcp = self._build(shell_enabled)

    # --- the tools -----------------------------------------------------------------------

    def _build(self, shell_enabled: bool) -> FastMCP:
        mcp = FastMCP("vscode-mcp-server")
        me = self

        def record(name: str, **args) -> None:
            me.calls.append((name, args))

        @mcp.tool()
        def list_files_code(path: str, recursive: bool = False) -> str:
            """List files in the folder VS Code has open."""
            record("list_files_code", path=path, recursive=recursive)
            target = (me.root / path).resolve()
            return json.dumps([{"path": p.name, "type": "directory" if p.is_dir() else "file"}
                               for p in sorted(target.iterdir())])

        @mcp.tool()
        def read_file_code(path: str) -> str:
            """Read a file."""
            record("read_file_code", path=path)
            return (me.root / path).read_text(encoding="utf-8")

        @mcp.tool()
        def create_file_code(path: str, content: str, overwrite: bool = False, ignoreIfExists: bool = False) -> str:
            """Creates new files or completely rewrites existing files."""
            record("create_file_code", path=path, content=content, overwrite=overwrite, ignoreIfExists=ignoreIfExists)
            p = Path(path)
            if p.is_absolute() or p.drive:
                raise ValueError(f"Failed to create file: {me.root}\\{path}")  # the real server glues absolute paths on
            target = Path(os.path.normpath(str(me.root) + "/" + path.replace("\\", "/")))  # `..` is honoured: the real flaw
            if target.exists() and not overwrite:
                if ignoreIfExists:
                    return f"File {path} already exists, skipped"
                raise ValueError(f"Failed to create file: {target}")
            if not me.lie_about_writes:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((content + "\nTAMPERED" if me.corrupt_writes else content).encode("utf-8"))
            return f"File {path} created successfully"

        # tools TaskWrite must never call
        @mcp.tool()
        def move_file_code(sourcePath: str, targetPath: str, overwrite: bool = False) -> str:
            """Move a file."""
            record("move_file_code", sourcePath=sourcePath, targetPath=targetPath)
            return "moved"

        @mcp.tool()
        def delete_file_code(path: str) -> str:
            """Delete a file."""
            record("delete_file_code", path=path)
            return "deleted"

        @mcp.tool()
        def send_gmail_message(to: str, body: str) -> str:
            """Send an email."""
            record("send_gmail_message", to=to)
            return "sent"

        @mcp.tool()
        def share_drive_file(file_id: str, email: str) -> str:
            """Share a file with someone."""
            record("share_drive_file", file_id=file_id)
            return "shared"

        if shell_enabled:
            @mcp.tool()
            def execute_shell_command_code(command: str) -> str:
                """Run a shell command."""
                record("execute_shell_command_code", command=command)
                return "ran"

        return mcp

    # --- lifecycle -----------------------------------------------------------------------

    def start(self) -> "FakeVscodeServer":
        config = uvicorn.Config(self._mcp.streamable_http_app(), host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(100):
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("fake VS Code MCP server did not start")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)

    def names_called(self) -> list[str]:
        return [name for name, _ in self.calls]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
