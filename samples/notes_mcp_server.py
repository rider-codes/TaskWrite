"""Demo MCP server: read and write plain-text notes in the NOTES_DIR folder.

Try it by copying mcp_servers.example.json to mcp_servers.json. Only started if you list it there."""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("notes")
ROOT = Path(os.environ.get("NOTES_DIR", "."))


def _safe(name: str) -> Path:
    return ROOT / Path(name).name


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def read_note(name: str) -> str:
    """Read a note by file name."""
    path = _safe(name)
    return path.read_text(encoding="utf-8") if path.is_file() else f"No note called {name}"


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def write_note(name: str, text: str) -> str:
    """Write a note (changes files on disk)."""
    _safe(name).write_text(text, encoding="utf-8")
    return f"Wrote {name}"


@server.tool()
def unannotated_tool(x: int) -> str:
    """A tool with no safety annotations."""
    return f"got {x}"


if __name__ == "__main__":
    server.run()
