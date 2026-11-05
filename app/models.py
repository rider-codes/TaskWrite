"""Pydantic API and domain models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ActionKind(str, Enum):
    analyze_assignment = "analyze_assignment"
    check_requirements = "check_requirements"
    retrieve_context = "retrieve_context"
    explain = "explain"
    solve = "solve"
    generate_code = "generate_code"
    analyze_data = "analyze_data"
    draft_document = "draft_document"
    review_output = "review_output"
    validate_output = "validate_output"
    export_deliverable = "export_deliverable"


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    incomplete = "incomplete"
    failed = "failed"
    needs_input = "needs_input"
    needs_confirmation = "needs_confirmation"


class SessionStatus(str, Enum):
    awaiting_review = "awaiting_review"
    plan_ready = "plan_ready"
    plan_approved = "plan_approved"
    running = "running"
    paused_budget = "paused_budget"
    interrupted = "interrupted"
    cancelled = "cancelled"
    failed = "failed"
    completed = "completed"
    awaiting_input = "awaiting_input"
    awaiting_confirmation = "awaiting_confirmation"


class RequirementKind(str, Enum):
    question = "question"
    deliverable = "deliverable"
    constraint = "constraint"


class Requirement(BaseModel):
    id: str = Field(min_length=1, max_length=16)
    text: str = Field(min_length=1, max_length=2000)
    kind: RequirementKind = RequirementKind.question
    source_text: str = Field(default="", max_length=4000)


class ToolRequest(BaseModel):
    name: str
    reason: str
    approved: bool | None = None
    group: str = ""  # MCP server name, empty for built-in tools
    read_only: bool | None = None  # MCP tools only; None means unknown


class PlanStep(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    kind: ActionKind
    objective: str = Field(min_length=1, max_length=500)
    inputs: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    expected_output: str = Field(min_length=1, max_length=500)
    status: StepStatus = StepStatus.pending
    requirement_ids: list[str] = Field(default_factory=list)
    output: str = ""
    notes: str = ""
    attempts: int = 0
    repair_attempts: int = 0
    tool_calls: int = 0
    retryable: bool = True


class Plan(BaseModel):
    steps: list[PlanStep]


class CheckResult(BaseModel):
    requirement_id: str
    # met = verified automatically; addressed = answer present, correctness not
    # verified; not_met = missing/violated; unverified = cannot be checked automatically
    status: Literal["met", "addressed", "not_met", "unverified"]
    detail: str


class ValidationReport(BaseModel):
    results: list[CheckResult] = Field(default_factory=list)
    summary: str = ""


class InjectionFinding(BaseModel):
    line_no: int
    text: str
    pattern: str


class QA(BaseModel):
    question: str
    answer: str


class EmailDraft(BaseModel):
    to: str = ""
    subject: str = ""
    body: str = ""
    attachment: str = ""
    status: Literal["prepared", "opened"] = "prepared"


class PendingCall(BaseModel):
    """An MCP tool call waiting for the user's decision on the approval screen."""
    tool: str  # mcp:<server>:<tool>
    server: str
    arguments: dict = Field(default_factory=dict)
    why: str = ""
    read_only: bool | None = None
    decision: Literal["approved", "declined"] | None = None


from .web import Source  # noqa: E402  (plain data model, no app imports)


class CodeResult(BaseModel):
    """One generated solution, how it was run and what that does (and does not) show."""
    requirement_id: str
    filename: str
    code: str = ""
    explanation: str = ""
    tests: str = ""
    # not_run | ran_ok | tests_passed | failed | no_code
    status: Literal["not_run", "ran_ok", "tests_passed", "failed", "no_code"] = "not_run"
    sandbox_level: str = ""
    attempts: int = 0  # generation attempts (1 = first try, no fixes)
    stdout: str = ""
    stderr: str = ""
    note: str = ""


class PublishItem(BaseModel):
    """One thing TaskWrite will ask an MCP server to create, shown on the approval card before anything happens."""
    id: str
    role: Literal["check", "file", "doc", "mail"]
    tool: str  # mcp:<server>:<tool>
    server: str
    label: str = ""
    path: str = ""  # files: relative to the workspace folder
    size: int = 0
    sha256: str = ""
    preview: str = ""
    args_hash: str = ""  # hash of the exact call that was approved; a different call is refused
    checked: bool = True  # the starting tick on the card
    required: bool = False  # a safety check the user cannot untick
    decision: Literal["approved", "declined"] | None = None


class PublishResult(BaseModel):
    item_id: str
    role: Literal["check", "file", "doc", "mail"] = "file"
    path: str = ""
    # ok: check passed | created: confirmed by reading it back | declined | refused | exists | not_found |
    # mismatch | failed | skipped
    status: Literal["ok", "created", "declined", "refused", "exists", "not_found", "mismatch", "failed", "skipped"]
    detail: str = ""
    read_back: bool = False


class SessionState(BaseModel):
    id: str
    filename: str
    status: SessionStatus = SessionStatus.awaiting_review
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    text: str = ""
    requirements: list[Requirement] = Field(default_factory=list)
    tool_requests: list[ToolRequest] = Field(default_factory=list)
    plan: Plan | None = None
    approved_plan_hash: str | None = None
    injection_findings: list[InjectionFinding] = Field(default_factory=list)
    mode: Literal["model", "rule_based"] | None = None
    provider: str = ""
    model: str = ""
    step_cap: int = 8
    steps_used: int = 0
    cancel_requested: bool = False
    verified_ids: list[str] = Field(default_factory=list)
    deliverable: str = ""
    validation: ValidationReport | None = None
    limitations: list[str] = Field(default_factory=list)
    message: str = ""
    pending_question: str = ""
    pending_step_id: str = ""
    answers: list[QA] = Field(default_factory=list)
    email_draft: EmailDraft | None = None
    workspace_dir: str = ""
    workspace_files: list[str] = Field(default_factory=list)
    tool_results: list[str] = Field(default_factory=list)
    pending_call: PendingCall | None = None
    context_notes: list[str] = Field(default_factory=list)
    code_results: list[CodeResult] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)  # found by web search; snippets only, never opened
    searches: list[str] = Field(default_factory=list)  # queries that were actually sent
    approach_notes: dict[str, str] = Field(default_factory=dict)  # step id -> short explanation by the model
    user_notes: list[str] = Field(default_factory=list)  # the user's own typed hints; context only
    publish_items: list[PublishItem] = Field(default_factory=list)  # what the approval card lists
    publish_results: list[PublishResult] = Field(default_factory=list)  # what really happened, item by item
    publish_check: Literal["", "ok", "failed"] = ""  # did the server's open folder match the workspace folder?

    def approved_tools(self) -> set[str]:
        return {t.name for t in self.tool_requests if t.approved}


class SettingsUpdate(BaseModel):
    llm_provider: str | None = None
    ollama_base_url: str | None = None
    ollama_model: str | None = None
    gemini_model: str | None = None
    groq_model: str | None = None
    openai_model: str | None = None
    anthropic_model: str | None = None


class KeyUpdate(BaseModel):
    provider: str
    key: str = Field(max_length=500)


class ToolApprovals(BaseModel):
    approvals: dict[str, bool]


class PublishRequest(BaseModel):
    approve: bool
    item_ids: list[str] = Field(default_factory=list, max_length=200)


class NoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=600)


class WorkspaceRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)


class RunRequest(BaseModel):
    mode: Literal["model", "rule_based"] = "model"
    consent_cloud: bool = False


class ContinueRequest(BaseModel):
    extra_steps: int = Field(default=8, ge=1, le=100)
    mode: Literal["model", "rule_based"] | None = None
    consent_cloud: bool = False


class AnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


class EmailOpenRequest(BaseModel):
    to: str = Field(max_length=320)
    subject: str = Field(max_length=300)
    body: str = Field(max_length=20000)
    confirm: bool = False


class ConfirmRequest(BaseModel):
    approve: bool
