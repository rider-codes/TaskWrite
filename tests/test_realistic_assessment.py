"""Regressions found by running a realistic exam: 'Question N (x marks)' headers, (a)/(b) parts,
a written question that mentions Python, and a provider that cannot produce JSON."""

import pytest

from app.llm import LLMService
from app.models import Requirement
from app.planner import analyze_requirements, classify
from app.providers.base import GenerateRequest, ProviderError
from pydantic import BaseModel

EXAM = """CS101 Mid-Term (synthetic)
Time allowed: 90 minutes   Total: 50 marks
Instructions: Answer ALL questions. Python 3 only.
Functions must RETURN their results (do not print or use input()).

Question 1 (12 marks)
(a) Write a function grade_to_letter(score) that converts a score into a letter.
(b) Write a function class_stats(scores) that returns a dict with mean and median.

Question 2 (14 marks)
Write a class BankAccount with deposit and withdraw.

Question 3 (6 marks)
Explain, in no more than five sentences, the difference between a list and a tuple in Python.

Question 4 [8 marks]: Write a generator function first_primes(n) that yields the first n primes.
"""


def test_question_headers_with_marks_and_sub_parts_are_one_question_each():
    reqs = analyze_requirements(EXAM)
    questions = [r for r in reqs if r.kind.value == "question"]
    assert [q.id for q in questions] == ["q1", "q2", "q3", "q4"]
    assert "grade_to_letter" in questions[0].text and "class_stats" in questions[0].text  # (a) and (b) stay together
    assert "BankAccount" in questions[1].text and "first_primes" in questions[3].text
    # nothing inside a question is also listed as a separate deliverable or constraint
    others = [r for r in reqs if r.kind.value != "question"]
    assert all("grade_to_letter" not in r.text and "BankAccount" not in r.text for r in others)
    assert any("RETURN" in r.text for r in others)  # genuine assignment-wide rules are still found


def test_written_question_that_mentions_python_is_not_treated_as_code():
    reqs = {r.id: r for r in analyze_requirements(EXAM)}
    assert classify(reqs["q3"]).value == "explain"
    for qid in ("q1", "q2", "q4"):
        assert classify(reqs[qid]).value == "generate_code"
    assert classify(Requirement(id="q9", text="Explain what this Python function does.")).value == "explain"
    assert classify(Requirement(id="q9", text="Write a Python program that prints hello.")).value == "generate_code"


def test_numbered_lists_without_headers_still_work():
    reqs = analyze_requirements("1. First thing to do here.\n2. Second thing to do here.\n")
    assert [r.id for r in reqs if r.kind.value == "question"] == ["q1", "q2"]


def test_a_single_header_does_not_hijack_numbered_questions():
    text = "Question 1 is below.\n1. Add two numbers together.\n2. Subtract two numbers.\n"
    assert len([r for r in analyze_requirements(text) if r.kind.value == "question"]) == 2


class _Out(BaseModel):
    a: int


class _JsonModeFails:
    """Provider whose JSON mode errors out (like Groq's 'Failed to generate JSON') but plain text works."""
    name, model, is_cloud = "fake", "m", False

    def __init__(self):
        self.modes = []

    def generate(self, request):
        from app.providers.base import GenerateResponse
        self.modes.append(request.json_mode)
        if request.json_mode:
            raise ProviderError("bad_response", "Fake returned HTTP 400: Failed to generate JSON.")
        return GenerateResponse(text='Sure!\n```json\n{"a": 7}\n```', model="m", provider="fake")

    def health_check(self):
        raise NotImplementedError


def test_json_mode_failure_falls_back_to_plain_text_parsing_within_the_budget():
    provider, charged = _JsonModeFails(), []
    service = LLMService(provider, lambda *a: None, charged.append)
    out = service.generate_structured(GenerateRequest(prompt="p"), _Out, "t")
    assert out.a == 7 and provider.modes == [True, False]
    assert len(charged) == 2  # both attempts were charged; the fallback cannot bypass the step cap


def test_other_provider_errors_are_not_swallowed_by_the_json_fallback():
    class Down(_JsonModeFails):
        def generate(self, request):
            raise ProviderError("auth", "bad key")

    service = LLMService(Down(), lambda *a: None, lambda label: None)
    with pytest.raises(ProviderError):
        service.generate_structured(GenerateRequest(prompt="p"), _Out, "t")
