"""Requirements analysis, tool requests, and plan generation (deterministic)."""

from __future__ import annotations

import re

from .models import ActionKind, Plan, PlanStep, Requirement, RequirementKind, ToolRequest
from .publish import (estimate_publish_calls, is_create_tool_id, is_doc_tool_id, is_mail_tool_id, is_publish_tool_id,
                      offer_mcp_tools)
from .tools import EMAIL_RE, MAX_PAGES_PER_STEP, MAX_SEARCHES_PER_STEP, MAX_TOOL_CALLS_PER_STEP, MCP_TOOL_RE, find_arithmetic

_QUESTION_START = re.compile(r"^\s*(?:q(?:uestion)?\s*)?(\d{1,3})\s*[\.\):\-]\s+\S", re.I)
# "Question 1", "Question 2 (12 marks)", "Q3:", "Question 4 [6 marks] Explain ..." - an explicit header word
_QUESTION_HEADER = re.compile(
    r"^\s*(?:question|q|task|problem|exercise)\s*\.?\s*(\d{1,3})\b\s*(?:[\(\[][^)\]]{0,30}[\)\]])?\s*[:.\-]?(?:\s|$)", re.I)
_WRITE_CODE = re.compile(
    r"\b(write|implement|create|define|build|code|complete|fix|debug|develop|program)\b[^.\n]{0,80}"
    r"\b(functions?|class(?:es)?|programs?|scripts?|methods?|generators?|algorithms?|modules?|code|queries|query)\b", re.I)
_CONSTRAINT = re.compile(
    r"\b(words?|pages?|font|spacing|cite|citations?|references?|sources?|apa|mla|chicago|format|"
    r"due|must|should|required|do not|don't|at least|at most|maximum|minimum|no more than)\b", re.I)
_DELIVERABLE = re.compile(r"\b(submit|deliverables?|write an?|produce|prepare|create an?)\b", re.I)
_CODE = re.compile(r"\b(python|java|javascript|c\+\+|sql|code|program|function|implement|algorithm|script)\b", re.I)
_SOLVE = re.compile(r"\b(calculate|compute|solve|evaluate|find the (value|sum|product)|simplify|prove|derive)\b|\d\s*[+\-*/^]\s*\d", re.I)
_DATA = re.compile(r"\b(data ?set|data|table|regression|mean|median|variance|correlation|chart)\b", re.I)
_DRAFT = re.compile(r"\b(essay|report|passages?|letter|article|composition|story|speech|paragraphs?|discuss|reflection|summari[sz]e|argue|critique|compare)\b", re.I)
_WORD = re.compile(r"\b(word document|\.docx|ms word|microsoft word)\b", re.I)
_RESEARCH = re.compile(r"\b(research|sources?|cite|citations?|references?|essay|report|investigate|literature|evidence|bibliograph\w*)\b", re.I)
_EMAIL = re.compile(r"\b(e-?mail|send (it )?to (the|your) (teacher|professor|tutor|instructor))\b", re.I)
_EXPLAIN = re.compile(r"\b(explain|describe|define|why|how does|what is|what are)\b", re.I)

MAX_QUESTIONS = 50
MAX_CONSTRAINTS = 20


def question_starts(lines: list[str]) -> list[int]:
    """Line numbers where a question begins.

    Explicit "Question N" headers win when there are at least two, so numbered sub-lists or (a)/(b)
    parts inside a question are not mistaken for new questions. Otherwise "1." / "2)" style numbering.
    """
    headers = [i for i, line in enumerate(lines) if _QUESTION_HEADER.match(line)]
    if len(headers) >= 2:
        return headers
    numbered = [i for i, line in enumerate(lines) if _QUESTION_START.match(line)]
    return numbered or headers  # a lone 'Question 1 (20 marks)' header still starts the question


def analyze_requirements(text: str) -> list[Requirement]:
    lines = text.splitlines()
    starts = question_starts(lines)
    questions: list[Requirement] = []
    in_question: set[int] = set()
    for n, start in enumerate(starts[:MAX_QUESTIONS]):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        in_question.update(range(start, end))
        chunk = "\n".join(lines[start:end]).strip()
        questions.append(Requirement(
            id=f"q{n + 1}", kind=RequirementKind.question,
            text=re.sub(r"\s+", " ", chunk)[:500], source_text=chunk[:4000]))

    others = [(i, l.strip()) for i, l in enumerate(lines) if i not in in_question and l.strip()]
    deliverables: list[Requirement] = []
    constraints: list[Requirement] = []
    for _, line in others:
        if len(line) < 8 or len(line) > 500:
            continue
        if _CONSTRAINT.search(line) and len(constraints) < MAX_CONSTRAINTS:
            constraints.append(Requirement(id=f"c{len(constraints) + 1}", text=line, kind=RequirementKind.constraint))
        elif _DELIVERABLE.search(line) and len(deliverables) < 5:
            deliverables.append(Requirement(id=f"d{len(deliverables) + 1}", text=line, kind=RequirementKind.deliverable))

    if not questions:
        body = "\n".join(l for _, l in others)[:4000] or text[:4000]
        questions.append(Requirement(
            id="q1", kind=RequirementKind.question,
            text="Complete the assignment as described: " + re.sub(r"\s+", " ", body)[:400],
            source_text=body))
    return questions + deliverables + constraints


def mcp_tool_request(t) -> ToolRequest:
    if is_mail_tool_id(t.id):
        why = ("Saves your finished answer as a draft in your Gmail account, for you to review and send yourself. TaskWrite "
               "never sends it, never picks a recipient and cannot read your mail. You see the exact text first, and the "
               "box starts unticked.")
        return ToolRequest(name=t.id, group=t.server, read_only=t.read_only, reason=f"{why} (MCP server '{t.server}')")
    if is_doc_tool_id(t.id):
        why = ("Saves your finished essay as a new Google Doc in your account, for you to review. TaskWrite never shares it, "
               "never edits or deletes your other documents and cannot read them. You see the exact text first, and the "
               "box starts unticked.")
        return ToolRequest(name=t.id, group=t.server, read_only=t.read_only, reason=f"{why} (MCP server '{t.server}')")
    if is_publish_tool_id(t.id):
        why = ("Creates your code files in the folder VS Code has open (it must be your workspace folder). You see every "
               "file first and tick the ones you want. Nothing is overwritten, nothing outside the folder is touched, "
               "and each file is read back from disk to confirm."
               if is_create_tool_id(t.id) else
               "Read-only. Lists the top-level names in the folder VS Code has open, so TaskWrite can confirm it is your "
               "workspace folder before creating anything. Approved together with the create tool.")
        return ToolRequest(name=t.id, group=t.server, read_only=t.read_only, reason=f"{why} (MCP server '{t.server}')")
    if t.read_only is True:
        safety = "The server marks it read-only, so it runs once you approve it."
    elif t.read_only is False:
        safety = "It may change things, so every call asks you first."
    else:
        safety = "The server did not say whether it changes anything, so every call asks you first."
    return ToolRequest(name=t.id, group=t.server, read_only=t.read_only,
                       reason=f"{(t.description or t.name)[:160]} (MCP server '{t.server}'). {safety}")


def build_tool_requests(requirements: list[Requirement], provider_label: str, mcp_tools=()) -> list[ToolRequest]:
    questions = [r for r in requirements if r.kind == RequirementKind.question]
    requests = [ToolRequest(
        name="model",
        reason=f"Needed to write answers. Only the relevant assignment sections are sent to {provider_label}; "
               "flagged injection lines are removed first.")]
    if any(find_arithmetic(q.text) for q in questions):
        requests.append(ToolRequest(
            name="calculator",
            reason="Some questions contain plain arithmetic that can be computed exactly, also without a model."))
    if any(_CODE.search(q.text) for q in questions):
        requests.append(ToolRequest(
            name="code_runner",
            reason="Some questions ask for code; this runs generated Python in a separate process to check it executes."))
    full = " ".join(r.text for r in requirements)
    if any(_CODE.search(q.text) for q in questions):
        requests.append(ToolRequest(
            name="vscode",
            reason="Saves the code as files in a workspace folder and opens it in VS Code for you to review and run."))
    if any(_DRAFT.search(q.text) for q in questions) or _WORD.search(full):
        requests.append(ToolRequest(
            name="word",
            reason="Saves the written work as a .docx in the workspace folder and opens it in Word for you to review."))
    drafts = [q for q in questions if classify(q) == ActionKind.draft_document]
    if drafts and (_RESEARCH.search(full) or any(_RESEARCH.search(q.text) for q in drafts)):
        requests.append(ToolRequest(
            name="web_search",
            reason="Looks up sources for the written work. Only a few short search queries (not your assignment text) "
                   "are sent to DuckDuckGo, and you approve the exact queries first. Only snippets come back; "
                   "pages are not opened."))
        requests.append(ToolRequest(
            name="read_pages",
            reason="After the search, opens up to 3 of the result pages and reads the start of their text so the essay can use "
                   "more than a snippet. Each site is visited from your computer, so it can see your IP address. You approve "
                   "the exact addresses first. Optional: without it, only snippets are used."))
    if _EMAIL.search(full) or EMAIL_RE.search(full):
        requests.append(ToolRequest(
            name="email",
            reason="The assignment mentions emailing someone. This only prepares a draft; you approve the "
                   "recipient and content, and you press Send yourself."))
    requests.extend(mcp_tool_request(t) for t in offer_mcp_tools(list(mcp_tools), requirements))
    return requests


def classify(requirement: Requirement) -> ActionKind:
    text = requirement.text
    if _WRITE_CODE.search(text) or (_CODE.search(text) and not _EXPLAIN.search(text)):
        return ActionKind.generate_code
    if _SOLVE.search(text):
        return ActionKind.solve
    if _DATA.search(text):
        return ActionKind.analyze_data
    if _DRAFT.search(text):
        return ActionKind.draft_document
    if _EXPLAIN.search(text):
        return ActionKind.explain
    return ActionKind.solve


def _step_tools(kind: ActionKind, approved: set[str]) -> list[str]:
    wanted = {
        ActionKind.solve: ["model", "calculator"],
        ActionKind.generate_code: ["model", "code_runner"],
    }.get(kind, ["model"])
    return [t for t in wanted if t in approved]


def step_units(step: PlanStep) -> int:
    """Budget units a step can consume: one per model/tool invocation."""
    if step.kind == ActionKind.export_deliverable:
        return sum(1 for t in step.tools if t in LAUNCH_TOOLS)
    if step.kind == ActionKind.retrieve_context and "read_pages" in step.tools:
        return MAX_PAGES_PER_STEP
    if step.kind == ActionKind.retrieve_context and "web_search" in step.tools:
        return 1 + MAX_SEARCHES_PER_STEP  # one model call to choose queries, then up to N searches
    if step.kind == ActionKind.retrieve_context and any(MCP_TOOL_RE.match(t) for t in step.tools):
        return 2 * MAX_TOOL_CALLS_PER_STEP
    if step.kind in (ActionKind.validate_output, ActionKind.check_requirements, ActionKind.analyze_assignment):
        return 0
    if step.kind == ActionKind.generate_code:  # one generation per question, plus one run each when approved
        return max(1, len(step.requirement_ids)) * (2 if "code_runner" in step.tools else 1)
    return 1


EXPORT_TOOLS = ("vscode", "word", "email")
LAUNCH_TOOLS = ("vscode", "word")  # tools that cost budget when used at delivery


def estimated_units(plan: Plan, publish_calls: int = 0) -> int:
    return sum(step_units(s) for s in plan.steps) + publish_calls


def _merge_groups(groups: list[list[Requirement]], limit: int, approved: set[str]) -> list[list[Requirement]]:
    """Merge neighbouring non-code questions while the plan is over budget.

    Python questions are never merged: each keeps its own step, so its code, run result and
    explanation stay separate. If that is still over budget the run pauses at the step cap and
    asks the user for more steps, as the rules require.
    """
    def units(group: list[Requirement]) -> int:
        if classify(group[0]) == ActionKind.generate_code:
            return len(group) * (2 if "code_runner" in approved else 1)
        return 1

    def mergeable(i: int) -> bool:
        return not (classify(groups[i][0]) == ActionKind.generate_code or classify(groups[i + 1][0]) == ActionKind.generate_code)

    while len(groups) > 1 and sum(units(g) for g in groups) > limit:
        candidates = [i for i in range(len(groups) - 1) if mergeable(i)]
        if not candidates:
            break
        best = min(candidates, key=lambda i: (
            classify(groups[i][0]) != classify(groups[i + 1][0]), len(groups[i]) + len(groups[i + 1])))
        groups[best : best + 2] = [groups[best] + groups[best + 1]]
    return groups


def build_plan(requirements: list[Requirement], approved_tools: set[str], max_steps: int) -> Plan:
    questions = [r for r in requirements if r.kind == RequirementKind.question]
    reserve = 2 if max_steps >= 5 else 1 if max_steps >= 3 else 0  # headroom for repairs
    export_tools = [t for t in EXPORT_TOOLS if t in approved_tools]
    export_units = sum(1 for t in export_tools if t in LAUNCH_TOOLS)
    publish_ids = sorted(t for t in approved_tools if is_publish_tool_id(t))
    if any(is_create_tool_id(t) for t in publish_ids):
        export_units += estimate_publish_calls(requirements)
    if any(is_mail_tool_id(t) for t in publish_ids):
        export_units += 1  # one draft call
    if any(is_doc_tool_id(t) for t in publish_ids):
        export_units += 1  # one create-document call
    mcp_tools = sorted(t for t in approved_tools if MCP_TOOL_RE.match(t) and not is_publish_tool_id(t))  # research only
    mcp_units = 2 * MAX_TOOL_CALLS_PER_STEP if (mcp_tools and 'model' in approved_tools) else 0
    research = ("web_search" in approved_tools and "model" in approved_tools
                and any(classify(q) == ActionKind.draft_document for q in questions))
    pages = research and "read_pages" in approved_tools
    research_units = ((1 + MAX_SEARCHES_PER_STEP) if research else 0) + (MAX_PAGES_PER_STEP if pages else 0)
    groups = _merge_groups([[q] for q in questions],
                           max(1, max_steps - reserve - export_units - mcp_units - research_units), approved_tools)

    steps: list[PlanStep] = []
    for n, group in enumerate(groups, start=1):
        kind = classify(group[0])
        ids = [r.id for r in group]
        steps.append(PlanStep(
            id=f"s{n}", kind=kind,
            objective=f"Complete {', '.join(i.upper() for i in ids)}",
            inputs=ids, tools=_step_tools(kind, approved_tools),
            expected_output=f"Complete answers under {', '.join('### ' + i.upper() for i in ids)} headings.",
            requirement_ids=ids))
    other_ids = [r.id for r in requirements if r.kind != RequirementKind.question]
    steps.append(PlanStep(
        id=f"s{len(steps) + 1}", kind=ActionKind.validate_output,
        objective="Check every requirement against the produced answers",
        inputs=[r.id for r in requirements], tools=[],
        expected_output="Per-requirement validation report.", requirement_ids=other_ids))
    steps.append(PlanStep(
        id=f"s{len(steps) + 1}", kind=ActionKind.export_deliverable,
        objective="Assemble the deliverable and hand it to the approved apps",
        inputs=[s.id for s in steps], tools=[*export_tools, *publish_ids],
        expected_output="Deliverable document ready for download."))
    if pages:
        steps.insert(0, PlanStep(
            id="r2", kind=ActionKind.retrieve_context,
            objective="Open a few of the sources found and read them (you approve the exact addresses first)",
            inputs=["r1"], tools=["read_pages"],
            expected_output="The start of the text of up to 3 pages, used when writing.", requirement_ids=[]))
    if research:
        steps.insert(0, PlanStep(
            id="r1", kind=ActionKind.retrieve_context,
            objective="Search the web for sources (you approve the exact queries first)",
            inputs=[q.id for q in questions], tools=["model", "web_search"],
            expected_output="A numbered list of sources (titles, links, snippets) to cite.", requirement_ids=[]))
    if mcp_units:
        steps.insert(0, PlanStep(
            id="c1", kind=ActionKind.retrieve_context,
            objective="Gather information with the MCP tools you approved",
            inputs=[q.id for q in questions], tools=["model", *mcp_tools],
            expected_output="Notes gathered with the approved tools.", requirement_ids=[]))
    return Plan(steps=steps)
