"""A tiny stand-in for the Google Workspace MCP server's Docs tools (stdio), used only by tests.

It copies the tool and argument names the real `workspace-mcp --permissions docs:full` offered when it was probed
(`create_doc` requires `user_google_email` and `title`), plus tools that read, edit, share and delete documents, so the
tests can prove those stay unreachable. Every call is appended to the file named by FAKE_GDOCS_LOG.
"""

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("google_workspace")
LOG = Path(os.environ["FAKE_GDOCS_LOG"])
FAIL = os.environ.get("FAKE_GDOCS_FAIL") == "1"


def _log(name: str, **args) -> None:
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"name": name, "args": args}) + "\n")


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def create_doc(user_google_email: str, title: str, content: str = "", folder_id: str | None = None) -> str:
    """Creates a new Google Doc and optionally inserts initial content."""
    _log("create_doc", user_google_email=user_google_email, title=title, content=content, folder_id=folder_id)
    if FAIL:
        raise RuntimeError("quota exceeded")
    return f"Created Google Doc '{title}' (ID: 1AbCdEf) for {user_google_email}. Link: https://docs.google.com/document/d/1AbCdEf/edit"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def search_docs(query: str) -> str:
    """Search your documents."""
    _log("search_docs", query=query)
    return "none"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_doc_content(document_id: str) -> str:
    """Read a document."""
    _log("get_doc_content", document_id=document_id)
    return "private text"


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def modify_doc_text(document_id: str, text: str) -> str:
    """Edit a document."""
    _log("modify_doc_text", document_id=document_id)
    return "edited"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_docs_in_folder(folder_id: str) -> str:
    """List documents."""
    _log("list_docs_in_folder", folder_id=folder_id)
    return "none"


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def share_doc(document_id: str, email: str) -> str:
    """Share a document."""
    _log("share_doc", document_id=document_id, email=email)
    return "shared"


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def delete_doc(document_id: str) -> str:
    """Delete a document."""
    _log("delete_doc", document_id=document_id)
    return "deleted"


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def start_google_auth(service_name: str) -> str:
    """Begin signing in to Google."""
    _log("start_google_auth", service_name=service_name)
    return "open this link"


if __name__ == "__main__":
    server.run()
