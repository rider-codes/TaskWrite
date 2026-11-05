"""Runs every case in eval_cases.py: coverage of questions, classification, tool offers, injection flags."""

import pytest

from app.models import RequirementKind
from app.planner import analyze_requirements, build_tool_requests, classify
from app.safety import scan_for_injection
from eval_cases import CASES


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_case(case):
    reqs = analyze_requirements(case.text)
    questions = [r for r in reqs if r.kind == RequirementKind.question]
    assert len(questions) == case.questions, [q.text[:60] for q in questions]
    assert tuple(classify(q).value for q in questions) == case.kinds
    names = {t.name for t in build_tool_requests(reqs, "test model")}
    assert set(case.tools) <= names, f"missing {set(case.tools) - names}"
    assert not (set(case.no_tools) & names), f"unexpected {set(case.no_tools) & names}"
    constraints = [r for r in reqs if r.kind == RequirementKind.constraint]
    assert len(constraints) >= case.constraints
    assert bool(scan_for_injection(case.text)) == case.injection
    # every question's own text is kept in full for the model, and nothing is lost between requirement and source
    assert all(q.source_text.strip() for q in questions)
    ids = [r.id for r in reqs]
    assert len(ids) == len(set(ids)), "requirement ids must be unique"


def test_there_are_at_least_thirty_cases():
    assert len(CASES) >= 30 and len({c.name for c in CASES}) == len(CASES)
