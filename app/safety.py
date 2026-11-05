"""Injection scan, secret redaction, tool approval and plan validation."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import ValidationError

from .models import ActionKind, InjectionFinding, Plan, PlanStep
from .tools import MCP_TOOL_RE, TOOL_CATALOG

MAX_PLAN_STEPS = 50


class ToolNotApproved(Exception):
    """A tool was used without current-session approval."""


class PlanValidationError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


# --- prompt-injection scan --------------------------------------------------

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("override-instructions", re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|above|prior|earlier|all|system|your)\b.{0,30}\b(instructions?|rules?|prompts?|guidelines?)", re.I)),
    ("role-hijack", re.compile(r"\byou are now\b|\bact as (?:an? )?(?:unrestricted|jailbroken|dan)\b|\bnew instructions?\s*:", re.I)),
    ("prompt-exfiltration", re.compile(r"\b(reveal|print|show|repeat|leak|output)\b.{0,30}\b(system prompt|your prompt|your instructions|api[ _-]?keys?|secrets?|passwords?)", re.I)),
    ("command-execution", re.compile(r"\b(run|execute|launch|invoke)\b.{0,30}\b(this |the following |these )?(command|script|macro|shell|powershell|terminal|code)\b|\brm\s+-rf\b|\bcurl\b[^\n]*\|\s*(sh|bash)", re.I)),
    ("approval-bypass", re.compile(r"\b(auto[- ]?approve|approve all|skip (the )?approval|without (asking|approval|permission)|disable (the )?(safety|limits?|step cap))", re.I)),
    ("role-markers", re.compile(r"<\|im_start\|>|\[/?INST\]|^\s*(system|assistant)\s*:", re.I)),
    ("exfiltrate-data", re.compile(r"\b(send|email|e-mail|upload|post|forward)\b.{0,50}\b(conversation|chat|prompts?|system prompt|instructions|keys?|secrets?|passwords?|credentials|history|everything|contents?|files?|data)\b.{0,40}\b(to|at)\b\s+\S+@\S+|https?://\S+.{0,20}\b(send|upload)\b", re.I)),
]


def scan_for_injection(text: str) -> list[InjectionFinding]:
    findings: list[InjectionFinding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for name, pattern in _INJECTION_PATTERNS:
            if pattern.search(line):
                findings.append(InjectionFinding(line_no=line_no, text=line.strip()[:200], pattern=name))
                break
    return findings


def neutralize(text: str, findings: list[InjectionFinding]) -> str:
    """Replace flagged lines so they are never sent to a model."""
    flagged = {f.line_no for f in findings}
    if not flagged:
        return text
    lines = text.splitlines()
    return "\n".join(
        "[REMOVED: suspected prompt injection]" if i in flagged else line
        for i, line in enumerate(lines, start=1)
    )


# --- secret redaction -------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgsk_[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\b(\s*[=:]\s*)\S+"),
]

# Keys the user pasted into Settings at runtime. Held in memory only; redact() always scrubs them.
_RUNTIME_SECRETS: set[str] = set()


def register_secret(secret: str) -> None:
    if secret and len(secret) >= 8:
        _RUNTIME_SECRETS.add(secret)


def forget_secret(secret: str) -> None:
    _RUNTIME_SECRETS.discard(secret)


def redact(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    for secret in (*extra_secrets, *_RUNTIME_SECRETS):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS[:-1]:
        text = pattern.sub("[REDACTED]", text)
    return _SECRET_PATTERNS[-1].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


# --- tools that never run ----------------------------------------------------

# A tool whose name contains one of these words is blocked in code, whatever the user or the database says.
# It covers sending, sharing, deleting and running shell commands, for any MCP server.
FORBIDDEN_WORDS = ("send", "share", "delete", "trash", "remove", "destroy", "purge", "permission", "permissions",
                   "access", "shell", "exec", "execute")


def _name_words(name: str) -> list[str]:
    tail = name.rsplit(":", 1)[-1]
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tail)
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


def forbidden_reason(name: str) -> str | None:
    """Why a tool may never run (or None). Accepts a bare tool name or an `mcp:server:tool` id."""
    hits = [w for w in _name_words(name) if w in FORBIDDEN_WORDS]
    return f"its name says it can {hits[0]}" if hits else None


# --- tool approval ----------------------------------------------------------

def require_tool_approved(approved: set[str], tool: str) -> None:
    if tool not in TOOL_CATALOG and not MCP_TOOL_RE.match(tool):
        raise ToolNotApproved(f"Unknown tool '{tool}'.")
    if MCP_TOOL_RE.match(tool) and (why := forbidden_reason(tool)):
        raise ToolNotApproved(f"Tool '{tool}' is blocked by TaskWrite and never runs ({why}).")
    if tool not in approved:
        raise ToolNotApproved(f"Tool '{tool}' has not been approved for this session.")


# --- plan validation --------------------------------------------------------

def validate_plan_payload(raw: Any, approved_tools: set[str]) -> Plan:
    """Validate an untrusted plan (e.g. from the UI or a model) in code."""
    problems: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        raise PlanValidationError(["Plan must be an object with a 'steps' list."])
    raw_steps = raw["steps"]
    if not raw_steps:
        raise PlanValidationError(["Plan has no steps."])
    if len(raw_steps) > MAX_PLAN_STEPS:
        raise PlanValidationError([f"Plan has more than {MAX_PLAN_STEPS} steps."])

    allowed_kinds = {k.value for k in ActionKind}
    steps: list[PlanStep] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_steps, start=1):
        label = f"step {index}"
        if not isinstance(item, dict):
            problems.append(f"{label}: must be an object.")
            continue
        kind = item.get("kind")
        if kind not in allowed_kinds:
            problems.append(f"{label}: unknown action kind {kind!r}.")
            continue
        try:
            step = PlanStep.model_validate(item)
        except ValidationError as exc:
            fields = ", ".join(str(e["loc"][0]) for e in exc.errors() if e["loc"])
            problems.append(f"{label}: invalid or missing fields ({fields}).")
            continue
        if step.id in seen:
            problems.append(f"{label}: duplicate step id '{step.id}'.")
        seen.add(step.id)
        for tool in step.tools:
            if tool not in TOOL_CATALOG and not MCP_TOOL_RE.match(tool):
                problems.append(f"{label}: unknown tool '{tool}'.")
            elif MCP_TOOL_RE.match(tool) and forbidden_reason(tool):
                problems.append(f"{label}: tool '{tool}' is blocked by TaskWrite and never runs.")
            elif tool not in approved_tools:
                problems.append(f"{label}: tool '{tool}' is not approved.")
        steps.append(step)
    if problems:
        raise PlanValidationError(problems)
    return Plan(steps=steps)


def plan_hash(plan: Plan) -> str:
    """Hash the approvable part of a plan (not runtime status or output)."""
    canonical = [
        {
            "id": s.id,
            "kind": s.kind.value,
            "objective": s.objective,
            "inputs": s.inputs,
            "tools": sorted(s.tools),
            "expected_output": s.expected_output,
            "requirement_ids": s.requirement_ids,
        }
        for s in plan.steps
    ]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
