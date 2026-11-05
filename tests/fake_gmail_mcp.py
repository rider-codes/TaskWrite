"""A tiny stand-in for the Google Workspace MCP server (stdio), used only by tests. Never started by the app itself.

It copies the tool names and argument names that the real `workspace-mcp --permissions gmail:drafts` offered when it
was probed, plus a send tool and mailbox-reading tools, so the tests can prove those stay unreachable. Every call is
appended to the file named by FAKE_GMAIL_LOG, together with whether the Google client secret reached this process.
"""

import json
import os
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("google_workspace")
LOG = Path(os.environ["FAKE_GMAIL_LOG"])
FAIL = os.environ.get("FAKE_GMAIL_FAIL") == "1"


def _log(name: str, **args) -> None:
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"name": name, "args": args, "secret_len": len(os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""))}) + "\n")


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def draft_gmail_message(user_google_email: str, subject: str, body: str, to: str | None = None, cc: str | None = None,
                        bcc: str | None = None, body_format: Literal["plain", "html"] = "plain") -> str:
    """Creates a draft email in the user's Gmail account. `user_google_email` is required, as on the real server."""
    _log("draft_gmail_message", user_google_email=user_google_email, subject=subject, body=body, to=to, cc=cc, bcc=bcc,
         body_format=body_format)
    if FAIL:
        raise RuntimeError("quota exceeded")
    return "Draft created! Draft ID: r-1234567890"


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def send_gmail_message(to: str, subject: str, body: str) -> str:
    """Sends an email."""
    _log("send_gmail_message", to=to, subject=subject, body=body)
    return "Sent"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def search_gmail_messages(query: str) -> str:
    """Search the mailbox."""
    _log("search_gmail_messages", query=query)
    return "no messages"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_gmail_message_content(message_id: str) -> str:
    """Read one message."""
    _log("get_gmail_message_content", message_id=message_id)
    return "private mail"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_gmail_labels() -> str:
    """List labels."""
    _log("list_gmail_labels")
    return "INBOX"


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def manage_gmail_label(name: str) -> str:
    """Create or change a label."""
    _log("manage_gmail_label", name=name)
    return "ok"


@server.tool(annotations=ToolAnnotations(readOnlyHint=False))
def start_google_auth(service_name: str) -> str:
    """Begin signing in to Google."""
    _log("start_google_auth", service_name=service_name)
    return "open this link"


if __name__ == "__main__":
    server.run()
