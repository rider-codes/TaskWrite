"""Tool catalog and local tool implementations (calculator, code runner)."""

from __future__ import annotations

import ast
import operator
import os
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from urllib.parse import quote
from dataclasses import dataclass
from pathlib import Path

TOOL_CATALOG: dict[str, str] = {
    "model": "Language model: sends the relevant assignment sections to the selected provider.",
    "calculator": "Calculator: evaluates plain arithmetic expressions locally and deterministically.",
    "code_runner": "Code runner: runs generated Python in a separate process with a timeout. "
    "Not a hardened sandbox.",
    "vscode": "VS Code: saves the generated code as files in a workspace folder and opens that folder "
    "in VS Code so you can see, run and edit it.",
    "word": "Word: saves the written deliverable as a .docx in the workspace folder and opens it in "
    "your default .docx app (Word) for you to review.",
    "web_search": "Web search: sends a few short search queries (never your assignment text) to DuckDuckGo and gets back titles, links and snippets to cite. You approve the exact queries before anything is sent.",
    "read_pages": "Page reader: opens up to 3 of the pages the search found and reads the start of their text, so the essay can rely on more than a snippet. Each site is visited from your computer (it can see your IP address). You approve the exact addresses first.",
    "email": "Email: prepares a draft to the teacher. Nothing is sent: you review it on an approval "
    "screen, then it opens as a draft in your mail app and you press Send yourself.",
}

MAX_PAGES_PER_STEP = 3  # result pages one reading step may open
MAX_SEARCHES_PER_STEP = 3  # web searches one research step may send (each is charged to the budget)
MAX_TOOL_CALLS_PER_STEP = 2  # MCP tool calls one plan step may make (each also costs a model call)
MCP_TOOL_RE = re.compile(r"^mcp:[a-z0-9_-]{1,32}:[A-Za-z0-9_.\-]{1,64}$")


def mcp_tool_id(server: str, tool: str) -> str:
    return f"mcp:{server}:{tool}"


def split_mcp_id(tool_id: str) -> tuple[str, str]:
    _, server, tool = tool_id.split(":", 2)
    return server, tool


# --- calculator -------------------------------------------------------------

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
MAX_EXPONENT = 100


class CalculatorError(ValueError):
    pass


def safe_eval(expression: str) -> float | int:
    """Evaluate arithmetic only: numbers, + - * / // % ** and parentheses."""
    if len(expression) > 200:
        raise CalculatorError("Expression too long.")
    try:
        tree = ast.parse(expression.strip().replace("^", "**"), mode="eval")
    except SyntaxError as exc:
        raise CalculatorError("Not a valid arithmetic expression.") from exc

    def walk(node: ast.AST) -> float | int:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](walk(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
                raise CalculatorError("Exponent too large.")
            try:
                return _BIN_OPS[type(node.op)](left, right)
            except ZeroDivisionError as exc:
                raise CalculatorError("Division by zero.") from exc
        raise CalculatorError("Only plain arithmetic is supported.")

    result = walk(tree)
    if isinstance(result, float) and result == int(result) and abs(result) < 1e15:
        return int(result)
    return result


_DATE_RE = re.compile(r"\b\d{1,4}[-/]\d{1,2}[-/]\d{1,4}\b")
_EXPR_RE = re.compile(r"[\d(][\d\s+\-*/().^%]*[\d)]")


def find_arithmetic(text: str) -> str | None:
    """Find the longest arithmetic-looking expression (with an operator) in text."""
    cleaned = _DATE_RE.sub(" ", text)
    best = ""
    for match in _EXPR_RE.finditer(cleaned):
        candidate = match.group(0).strip()
        if re.search(r"\d\s*[+\-*/^%]\s*[\d(]", candidate) and len(candidate) > len(best):
            best = candidate
    return best or None


# --- code runner ------------------------------------------------------------

_RESTRICTED = re.compile(
    r"\b(subprocess|socket|requests|urllib|http\.client|ftplib|smtplib|shutil\.rmtree|"
    r"os\.(system|remove|rmdir|unlink|popen|environ)|ctypes|__import__|eval\(|exec\()"
)


@dataclass
class RunResult:
    ran: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    note: str = ""


def run_python(code: str, timeout_s: float = 10.0, max_output: int = 6000) -> RunResult:
    """Run code in a fresh isolated-mode interpreter in a temp dir with a timeout.

    Defence in depth only: a deny-list pre-check, no inherited environment, a
    throwaway working directory, and a hard timeout. It does not block network or
    filesystem access by itself, so it is only available after explicit approval.
    """
    if _RESTRICTED.search(code):
        return RunResult(ran=False, note="Not run: the code uses modules or calls this runner refuses.")
    with tempfile.TemporaryDirectory(prefix="taskwrite_run_") as tmp:
        script = Path(tmp) / "main.py"
        script.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(script)],
                cwd=tmp,
                env={"SYSTEMROOT": _systemroot(), "PYTHONIOENCODING": "utf-8"},
                capture_output=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return RunResult(ran=True, timed_out=True, note=f"Stopped after {timeout_s:g}s timeout.")
        except OSError as exc:
            return RunResult(ran=False, note=f"Could not start the interpreter: {type(exc).__name__}")
    return RunResult(
        ran=True,
        exit_code=proc.returncode,
        stdout=proc.stdout.decode("utf-8", "replace")[:max_output],
        stderr=proc.stderr.decode("utf-8", "replace")[:max_output],
    )


def _systemroot() -> str:
    import os

    return os.environ.get("SYSTEMROOT", "")


# --- workspace files and app launchers ----------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAILTO_BODY_LIMIT = 1800


class LaunchError(Exception):
    """An app could not be opened; the message is shown to the user as-is."""


def safe_filename(name: str, default: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", Path(name).name).strip("._")
    return cleaned[:80] or default


def write_workspace_file(root: Path, name: str, content: str | bytes) -> Path:
    """Write a file strictly inside root (no path traversal)."""
    root.mkdir(parents=True, exist_ok=True)
    target = (root / safe_filename(name, "file.txt")).resolve()
    if root.resolve() not in target.parents:
        raise LaunchError("Refused to write outside the workspace folder.")
    if isinstance(content, str):
        target.write_text(content, encoding="utf-8")
    else:
        target.write_bytes(content)
    return target


def mailto_url(to: str, subject: str, body: str) -> tuple[str, bool]:
    """Build a mailto: link. Returns (url, body_was_truncated)."""
    truncated = len(body) > MAILTO_BODY_LIMIT
    body = body[:MAILTO_BODY_LIMIT]
    return f"mailto:{quote(to)}?subject={quote(subject)}&body={quote(body)}", truncated


class Launcher:
    """Opens the user's own apps. Each method returns an honest note, or raises LaunchError."""

    def open_vscode(self, folder: Path, files: list[Path]) -> str:
        code = shutil.which("code")
        if not code:
            raise LaunchError("VS Code's 'code' command was not found on PATH; the files were saved but not opened.")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen([code, str(folder), *map(str, files)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        except OSError as exc:
            raise LaunchError(f"Could not start VS Code: {type(exc).__name__}") from exc
        return f"Asked VS Code to open {folder}."

    def open_document(self, path: Path) -> str:
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        except OSError as exc:
            raise LaunchError(f"Could not open {path.name}: {type(exc).__name__}") from exc
        return f"Asked the system to open {path.name} in its default app."

    def open_mail(self, url: str) -> str:
        if not webbrowser.open(url):
            raise LaunchError("No mail app or browser handled the mailto: link.")
        return "Asked the system to open a mail draft."
