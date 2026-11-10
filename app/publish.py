"""Publishing finished work through MCP servers. Step 3a covers the code files (VS Code extension).

Everything here is built by code from the validated results, never by the model:
exact paths, exact content, a hash of each call, and a read-back from disk to confirm what was created.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .code_steps import build_files, explanations_markdown
from .mcp_client import McpTool
from .models import ActionKind, PublishItem, Requirement, RequirementKind, SessionState
from .safety import forbidden_reason
from .tools import MCP_TOOL_RE, safe_filename, split_mcp_id

# Which kinds of publish start ticked on the approval card. Files are low risk; a Google Doc and a Gmail
# draft live in an online account, so they start unticked and the user must choose them.
DEFAULT_CHECKED = {"file": True, "doc": False, "mail": False}

MAX_PATH_CHARS = 240
MAX_SEGMENT_CHARS = 100
PREVIEW_CHARS = 400
# A Gmail draft or Google Doc carries the whole approved answer, so these code-built calls get a higher size cap than
# the one for arguments a model proposes (MAX_ARGS_CHARS in mcp_client).
PUBLISH_MAX_CHARS = 60_000
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


class PublishError(Exception):
    """A path or call that publishing refuses; the message is shown to the user as-is."""


# --- paths: nothing leaves the workspace folder -------------------------------------------------------------

def safe_relative_path(raw: str) -> str:
    """A relative path that stays inside the workspace, with forward slashes. Raises PublishError otherwise.

    The VS Code extension itself does not stop `../`, so this is what keeps every file inside the folder.
    """
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise PublishError("The file path is empty or invalid.")
    path = raw.replace("\\", "/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise PublishError("Only paths inside your workspace folder are allowed (no absolute or drive paths).")
    if path.endswith("/"):
        raise PublishError("That path is a folder, not a file.")
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                raise PublishError("That path would leave your workspace folder.")
            parts.pop()
            continue
        if (_BAD_CHARS.search(segment) or segment != segment.strip() or segment.endswith(".")
                or len(segment) > MAX_SEGMENT_CHARS or segment.split(".")[0].upper() in _RESERVED):
            raise PublishError(f"'{segment[:40]}' is not a safe file name.")
        parts.append(segment)
    if not parts:
        raise PublishError("The file path is empty.")
    clean = "/".join(parts)
    if len(clean) > MAX_PATH_CHARS:
        raise PublishError("That path is too long.")
    return clean


def resolve_inside(workspace: Path, relative: str) -> Path:
    """The on-disk location of a relative path, refusing anything that resolves outside the workspace."""
    root = workspace.resolve()
    target = (root / safe_relative_path(relative)).resolve()
    if target != root and root not in target.parents:
        raise PublishError("That path would leave your workspace folder.")
    return workspace / safe_relative_path(relative)


# --- which tools are publish tools ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    name: str
    create: str  # the tool that creates one file
    check: str  # a read-only tool that lists the folder the server has open


VSCODE_EXT = Profile("vscode_ext", create="create_file_code", check="list_files_code")
PROFILES = (VSCODE_EXT,)
MAIL_DRAFT = "draft_gmail_message"  # the one Gmail tool TaskWrite will ever call: it saves a draft and cannot send
DOC_CREATE = "create_doc"  # the one Google Docs tool TaskWrite will ever call: it creates a new document, nothing else
PUBLISH_TOOL_NAMES = {p.create for p in PROFILES} | {p.check for p in PROFILES} | {MAIL_DRAFT, DOC_CREATE}


def is_publish_tool_id(tool_id: str) -> bool:
    return bool(MCP_TOOL_RE.match(tool_id)) and split_mcp_id(tool_id)[1] in PUBLISH_TOOL_NAMES


def is_mail_tool_id(tool_id: str) -> bool:
    return bool(MCP_TOOL_RE.match(tool_id)) and split_mcp_id(tool_id)[1] == MAIL_DRAFT


def is_doc_tool_id(tool_id: str) -> bool:
    return bool(MCP_TOOL_RE.match(tool_id)) and split_mcp_id(tool_id)[1] == DOC_CREATE


def doc_server_tool(tools: list[McpTool]) -> McpTool | None:
    """The create tool of a Google Docs server, judged by its real argument schema (title and content are text)."""
    tool = next((t for t in tools if t.name == DOC_CREATE), None)
    if tool is None:
        return None
    props = (tool.input_schema or {}).get("properties", {})
    return tool if all(isinstance(props.get(k), dict) and props[k].get("type") == "string" for k in ("title", "content")) else None


def mail_server_tool(tools: list[McpTool]) -> McpTool | None:
    """The draft tool of a Gmail server, judged by its real argument schema (subject and body are text)."""
    tool = next((t for t in tools if t.name == MAIL_DRAFT), None)
    if tool is None:
        return None
    props = (tool.input_schema or {}).get("properties", {})
    return tool if all(isinstance(props.get(k), dict) and props[k].get("type") == "string" for k in ("subject", "body")) else None


def is_create_tool_id(tool_id: str) -> bool:
    return bool(MCP_TOOL_RE.match(tool_id)) and split_mcp_id(tool_id)[1] in {p.create for p in PROFILES}


def server_profile(tools: list[McpTool]) -> Profile | None:
    """The publish profile a server fits, judged by its tools and their real argument schemas."""
    by_name = {t.name: t for t in tools}
    for profile in PROFILES:
        create, check = by_name.get(profile.create), by_name.get(profile.check)
        if create is None or check is None:
            continue
        props = (create.input_schema or {}).get("properties", {})
        if all(isinstance(props.get(k), dict) and props[k].get("type") == "string" for k in ("path", "content")):
            return profile
    return None


def companion_ids(tool_id: str) -> list[str]:
    """The read-only check tool that is approved together with a create tool."""
    server, name = split_mcp_id(tool_id)
    return [f"mcp:{server}:{p.check}" for p in PROFILES if p.create == name]


def offer_mcp_tools(tools: list[McpTool], requirements: list[Requirement]) -> list[McpTool]:
    """Which discovered MCP tools to offer the user.

    Blocked tools (send, share, delete, shell) are never offered. A server that fits a publish profile offers only
    its create tool and folder check, and only when the assignment has code to write.
    """
    wants_code = any(r.kind == RequirementKind.question and _is_code(r) for r in requirements)
    by_server: dict[str, list[McpTool]] = {}
    for t in tools:
        if not forbidden_reason(t.name):
            by_server.setdefault(t.server, []).append(t)
    offered: list[McpTool] = []
    for server_tools in by_server.values():
        if (draft := mail_server_tool(server_tools)) is not None:
            offered.append(draft)  # a Gmail server offers only the draft tool: no reading, labels or sign-in tools
            continue
        if (create := doc_server_tool(server_tools)) is not None:
            offered.append(create)  # a Docs server offers only create_doc: no reading, editing, listing or sign-in tools
            continue
        profile = server_profile(server_tools)
        if profile is None:
            offered.extend(server_tools)
        elif wants_code:
            offered.extend(t for t in server_tools if t.name in (profile.create, profile.check))
    return offered


def _is_code(requirement: Requirement) -> bool:
    from .planner import classify

    return classify(requirement) == ActionKind.generate_code


# --- what will be created ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FileSpec:
    path: str  # relative to the workspace folder, forward slashes
    content: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def build_file_specs(state: SessionState) -> list[FileSpec]:
    """The exact files for the code answers: each solution, its tests, and the explanations."""
    results = [c for c in state.code_results if c.code.strip()]
    if not results:
        return []
    folder = safe_filename(Path(state.filename).stem, "assessment")
    specs: list[FileSpec] = []
    for c in results:
        files, _ = build_files(c)
        specs += [FileSpec(safe_relative_path(f"{folder}/{name}"), body.rstrip() + "\n") for name, body in files.items()]
    specs.append(FileSpec(safe_relative_path(f"{folder}/EXPLANATIONS.md"), explanations_markdown(state.filename, results)))
    return specs


def create_args(spec: FileSpec) -> dict[str, str]:
    """Only `path` and `content`: overwrite and ignoreIfExists are never sent, so an existing file is never touched."""
    return {"path": spec.path, "content": spec.content}


def args_hash(tool_id: str, args: dict) -> str:
    return hashlib.sha256(json.dumps([tool_id, args], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def file_items(state: SessionState, create_id: str, check_id: str, specs: list[FileSpec]) -> list[PublishItem]:
    server = split_mcp_id(create_id)[0]
    items = [PublishItem(
        id="check", role="check", tool=check_id, server=server, required=True, checked=True,
        label="Check that VS Code has your workspace folder open (read-only: lists the folder's top-level names)")]
    for n, spec in enumerate(specs, start=1):
        items.append(PublishItem(
            id=f"f{n}", role="file", tool=create_id, server=server, path=spec.path, size=len(spec.content.encode("utf-8")),
            sha256=spec.sha256, preview=spec.content[:PREVIEW_CHARS], args_hash=args_hash(create_id, create_args(spec)),
            checked=DEFAULT_CHECKED["file"], label=f"Create {spec.path}"))
    return items


def mail_args(state: SessionState, tool: McpTool | None, account: str) -> dict[str, str]:
    """The exact draft call: a subject and the finished answer as the body. No recipient is ever chosen by TaskWrite
    (the user adds one in Gmail), and nothing but plain text is sent. If the server's tool asks which Google account to
    use (the real one does), it is the address the user put in the server's USER_GOOGLE_EMAIL setting."""
    args = {"subject": "Draft: " + Path(state.filename).stem, "body": state.deliverable.strip(), "body_format": "plain"}
    _add_account(args, tool, account, "Gmail")
    if len(json.dumps(args, default=str)) > PUBLISH_MAX_CHARS:
        raise PublishError(f"The answer is too long to save as one Gmail draft (the limit is about {PUBLISH_MAX_CHARS:,} characters).")
    return args


def _add_account(args: dict, tool: McpTool | None, account: str, service: str) -> None:
    """If the server's tool asks which Google account to use (the real one does), it is the USER_GOOGLE_EMAIL setting."""
    if "user_google_email" in ((tool.input_schema if tool else {}) or {}).get("properties", {}):
        if not EMAIL_ADDRESS.fullmatch(account):
            raise PublishError(f"Set USER_GOOGLE_EMAIL to your Google address in the {service} server's env in mcp_servers.json "
                               "(the address you signed in with).")
        args["user_google_email"] = account


def to_plain_text(markdown: str) -> str:
    """The Docs server inserts plain text (it does not render Markdown), so drop the Markdown markers."""
    lines = []
    for line in markdown.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)
        lines.append(line.replace("`", ""))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def doc_args(state: SessionState, tool: McpTool | None, account: str) -> dict[str, str]:
    """The exact create call: a title and the finished essay as plain text. No folder is chosen and nothing is shared."""
    args = {"title": Path(state.filename).stem, "content": to_plain_text(state.deliverable)}
    _add_account(args, tool, account, "Google Docs")
    if len(json.dumps(args, default=str)) > PUBLISH_MAX_CHARS:
        raise PublishError(f"The essay is too long to send as one Google Doc (the limit is about {PUBLISH_MAX_CHARS:,} characters). "
                           "Download the .docx instead.")
    return args


def doc_item(state: SessionState, tool_id: str, tool: McpTool | None, account: str) -> PublishItem:
    args = doc_args(state, tool, account)
    where = f" in {args['user_google_email']}" if "user_google_email" in args else ""
    return PublishItem(
        id="d1", role="doc", tool=tool_id, server=split_mcp_id(tool_id)[0], label=f"Save as a Google Doc{where}: {args['title']}",
        size=len(args["content"].encode("utf-8")), sha256=hashlib.sha256(args["content"].encode("utf-8")).hexdigest(),
        preview=args["content"][:PREVIEW_CHARS], args_hash=args_hash(tool_id, args), checked=DEFAULT_CHECKED["doc"])


def mail_item(state: SessionState, tool_id: str, tool: McpTool | None, account: str) -> PublishItem:
    args = mail_args(state, tool, account)
    where = f" in {args['user_google_email']}" if "user_google_email" in args else ""
    return PublishItem(
        id="m1", role="mail", tool=tool_id, server=split_mcp_id(tool_id)[0], label=f"Save a Gmail draft{where}: {args['subject']}",
        size=len(args["body"].encode("utf-8")), sha256=hashlib.sha256(args["body"].encode("utf-8")).hexdigest(),
        preview=args["body"][:PREVIEW_CHARS], args_hash=args_hash(tool_id, args), checked=DEFAULT_CHECKED["mail"])


def estimate_publish_calls(requirements: list[Requirement]) -> int:
    """How many MCP calls publishing the code files will probably need: a folder check, two files per question, one notes file."""
    code_questions = sum(1 for r in requirements if r.kind == RequirementKind.question and _is_code(r))
    return 0 if code_questions == 0 else 1 + 2 * code_questions + 1
