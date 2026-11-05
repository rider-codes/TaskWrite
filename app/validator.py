"""Requirement coverage and output validation."""

from __future__ import annotations

import re

from .web import cited_ids
from .models import (CheckResult, RequirementKind, SessionState, StepStatus, ValidationReport)

_HEADING = re.compile(r"^#{1,6}[ \t]*\**[ \t]*(q\d+)\b[^\n]*$", re.I | re.M)
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_NUM = r"(\d[\d,]*)"
_MAX_WORDS = re.compile(r"\b(?:at most|no more than|maximum(?: of)?|max\.?|under|not exceed(?:ing)?|up to|limit(?:ed)? (?:of|to))\s+" + _NUM + r"\s+words?", re.I)
_MIN_WORDS = re.compile(r"\b(?:at least|no fewer than|no less than|minimum(?: of)?|min\.?)\s+" + _NUM + r"\s+words?", re.I)
_RANGE_WORDS = re.compile(_NUM + r"\s*(?:-|–|to)\s*" + _NUM + r"\s+words?", re.I)
_APPROX_WORDS = re.compile(r"\b(?:approximately|about|around|roughly)\s+" + _NUM + r"\s+words?", re.I)


def split_sections(text: str) -> dict[str, str]:
    """Split step output into {requirement_id: body} using '### Qn' headings."""
    matches = list(_HEADING.finditer(text))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        key = m.group(1).lower()
        sections[key] = (sections.get(key, "") + "\n" + body).strip() if key in sections else body
    return sections


def count_words(text: str) -> int:
    body = _CODE_BLOCK.sub(" ", text)
    body = re.sub(r"^#{1,6}.*$", " ", body, flags=re.M)
    return len(re.findall(r"\b[\w'’-]+\b", body))


def word_bounds(text: str) -> tuple[int, int] | None:
    """Return (min, max) words if the requirement states a checkable limit."""
    to_int = lambda s: int(s.replace(",", ""))
    if m := _RANGE_WORDS.search(text):
        return to_int(m.group(1)), to_int(m.group(2))
    if m := _APPROX_WORDS.search(text):
        n = to_int(m.group(1))
        return int(n * 0.9), int(n * 1.1)
    lo = to_int(m.group(1)) if (m := _MIN_WORDS.search(text)) else 0
    hi = to_int(m.group(1)) if (m := _MAX_WORDS.search(text)) else 10**9
    return (lo, hi) if (lo, hi) != (0, 10**9) else None


def collect_sections(state: SessionState) -> dict[str, str]:
    sections: dict[str, str] = {}
    for step in (state.plan.steps if state.plan else []):
        if step.output:
            sections.update(split_sections(step.output))
    return sections


_URL = re.compile(r"https?://[^\s)\]>\"']+", re.I)


def _norm_url(url: str) -> str:
    return url.rstrip(".,;:").rstrip("/").lower()


def citation_problems(body: str, state: SessionState) -> list[str]:
    """Citations that do not trace back to a real search result. Empty list means nothing suspicious.

    A cited URL must be one of the URLs the search returned, and a marker like [S3] must name a source
    that exists. This never proves a source supports a claim; it only catches invented references.
    """
    known_urls = {_norm_url(s.url) for s in state.sources}
    known_ids = {s.id.upper() for s in state.sources}
    problems = [f"link not in the search results: {u[:80]}" for u in _URL.findall(body)
                if _norm_url(u) not in known_urls]
    problems += [f"[{i}] does not exist" for i in sorted(cited_ids(body)) if i not in known_ids]
    return list(dict.fromkeys(problems))[:4]


def code_check(requirement_id: str, c) -> CheckResult:
    """Execution facts only: nothing here claims a solution is proven correct."""
    if c.status == "failed":
        why = (c.note or c.stderr.strip().splitlines()[-1] if (c.note or c.stderr.strip()) else "see the error output")
        return CheckResult(requirement_id=requirement_id, status="not_met", detail=f"The code failed when run: {why}"[:240])
    if c.status == "no_code":
        return CheckResult(requirement_id=requirement_id, status="not_met", detail="No usable code was produced.")
    detail = {"tests_passed": "The code ran and its model-written tests passed. Correctness against the assignment "
                              "was not independently verified.",
              "ran_ok": "The code ran without errors. There were no tests, so nothing else was checked.",
              "not_run": "Code was produced but not run."}[c.status]
    return CheckResult(requirement_id=requirement_id, status="addressed", detail=detail)


def check_requirements(state: SessionState) -> ValidationReport:
    sections = collect_sections(state)
    step_of = {rid: s for s in (state.plan.steps if state.plan else []) for rid in s.requirement_ids}
    answered_text = "\n\n".join(sections.values())
    results: list[CheckResult] = []
    code = {c.requirement_id: c for c in state.code_results}

    for req in state.requirements:
        if req.kind == RequirementKind.question:
            step = step_of.get(req.id)
            body = sections.get(req.id, "").strip()
            if step is None:
                results.append(CheckResult(requirement_id=req.id, status="not_met", detail="No plan step covers this item."))
            elif not body:
                why = step.notes or f"Step {step.id} is {step.status.value}."
                results.append(CheckResult(requirement_id=req.id, status="not_met", detail=f"No answer produced. {why}"))
            elif step.status in (StepStatus.failed, StepStatus.incomplete) and body.lower().startswith("not completed"):
                results.append(CheckResult(requirement_id=req.id, status="not_met", detail=body[:200]))
            elif req.id not in code and (problems := citation_problems(body, state)):
                results.append(CheckResult(requirement_id=req.id, status="not_met",
                                           detail="Cites something that was not found by the search: " + "; ".join(problems)))
            elif req.id in code:
                results.append(code_check(req.id, code[req.id]))
            elif req.id in state.verified_ids:
                results.append(CheckResult(requirement_id=req.id, status="met",
                                           detail="Answer computed by the calculator tool."))
            else:
                results.append(CheckResult(requirement_id=req.id, status="addressed",
                                           detail="An answer is present. Its correctness was not verified."))
        else:
            bounds = word_bounds(req.text) if req.kind == RequirementKind.constraint else None
            if bounds:
                words = count_words(answered_text)
                ok = bounds[0] <= words <= bounds[1]
                results.append(CheckResult(
                    requirement_id=req.id, status="met" if ok else "not_met",
                    detail=f"Answers total {words} words (limit {bounds[0]}-{bounds[1] if bounds[1] < 10**9 else 'no max'})."))
            else:
                results.append(CheckResult(requirement_id=req.id, status="unverified",
                                           detail="Cannot be checked automatically; review manually."))

    counts = {s: sum(1 for r in results if r.status == s) for s in ("met", "addressed", "not_met", "unverified")}
    summary = (f"{counts['met']} verified, {counts['addressed']} answered but not verified for correctness, "
               f"{counts['not_met']} not met, {counts['unverified']} need manual review.")
    return ValidationReport(results=results, summary=summary)
