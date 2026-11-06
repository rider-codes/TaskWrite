"""'Change this': redo one finished problem with an instruction. Only that problem is redone; every rule still applies."""

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentError
from app.main import create_app
from app.models import SessionStatus, StepStatus
from conftest import FakeProvider, code_json, default_responder, prepare

INSTRUCTION = "make it exactly two sentences"


def responder(req):
    """Like the default, but answers Q2 differently once the student has asked for a change."""
    if "For Q2: " + INSTRUCTION in req.prompt and "### Q2" in req.prompt:
        return "### Q2\nRevised answer for Q2. Second sentence."
    if "ONE question of an assessment" in req.system and "For Q3:" in req.prompt:
        return code_json(code="def reverse(s):\n    return ''.join(reversed(s))\n", explanation="Revised: uses reversed().")
    return default_responder(req)


@pytest.fixture
def done(make_agent):
    agent, provider, settings = make_agent(provider=FakeProvider(responder), cap=30)
    sid = prepare(agent)
    state = agent.start_run(sid)
    assert state.status == SessionStatus.completed
    return agent, provider, sid, state


def section(state, qid):
    return state.deliverable.split(f"### {qid}")[1].split("###")[0]


def test_redoing_one_problem_reruns_only_that_problem_with_the_instruction(done):
    agent, provider, sid, before = done
    calls_before = len(provider.prompts)
    state = agent.redo_problem(sid, "q2", INSTRUCTION)
    assert state.status == SessionStatus.plan_approved and "Q2" in state.message
    assert ("For Q2: " + INSTRUCTION) in state.user_notes
    state = agent.start_run(sid)
    assert state.status == SessionStatus.completed
    new_prompts = provider.prompts[calls_before:]
    assert new_prompts and all("### Q2" in p for p in new_prompts)  # nothing else was sent to the model
    assert all(INSTRUCTION in p for p in new_prompts)
    assert "Revised answer for Q2" in section(state, "Q2")
    assert section(state, "Q1") == section(before, "Q1") and section(state, "Q3") == section(before, "Q3")


def test_redoing_a_code_problem_regenerates_only_its_code(done):
    agent, provider, sid, before = done
    other = next(c for c in before.code_results if c.requirement_id == "q3")
    agent.redo_problem(sid, "q3", "use reversed() instead of slicing")
    state = agent.start_run(sid)
    q3 = next(c for c in state.code_results if c.requirement_id == "q3")
    assert "reversed" in q3.code and q3.code != other.code and q3.status in ("tests_passed", "ran_ok")
    assert "Answer for Q2" in section(state, "Q2")  # untouched


def test_the_redo_is_charged_to_the_step_budget(done):
    agent, _, sid, before = done
    agent.redo_problem(sid, "q2", INSTRUCTION)
    assert agent.start_run(sid).steps_used > before.steps_used


def test_the_redo_pauses_at_the_cap_like_any_other_work(make_agent):
    agent, _, _ = make_agent(provider=FakeProvider(responder), cap=30)
    sid = prepare(agent)
    state = agent.start_run(sid)
    agent.db.set_budget(sid, cap=state.steps_used)  # no budget left (the budget has its own columns)
    agent.redo_problem(sid, "q2", INSTRUCTION)
    assert agent.start_run(sid).status == SessionStatus.paused_budget


def test_the_approved_plan_is_unchanged_so_no_new_approval_is_needed(done):
    agent, _, sid, before = done
    state = agent.redo_problem(sid, "q2", INSTRUCTION)
    assert state.approved_plan_hash == before.approved_plan_hash
    assert [s.tools for s in state.plan.steps] == [s.tools for s in before.plan.steps]


def test_the_results_are_rebuilt_not_kept_from_before(done):
    agent, _, sid, _ = done
    state = agent.redo_problem(sid, "q2", INSTRUCTION)
    assert state.deliverable == "" and state.validation is None and not state.publish_items
    step = next(s for s in state.plan.steps if "q2" in s.requirement_ids and s.kind.value not in ("validate_output", "export_deliverable"))
    assert step.status == StepStatus.pending and step.output == ""


@pytest.mark.parametrize("rid,text,code", [("q9", INSTRUCTION, 400), ("q2", "   ", 400), ("q2", "x" * 5000, None)])
def test_bad_requests_are_refused(done, rid, text, code):
    agent, _, sid, _ = done
    if code is None:  # a very long instruction is cut, not refused
        state = agent.redo_problem(sid, rid, text)
        assert max(len(n) for n in state.user_notes) <= 620
        return
    with pytest.raises(AgentError) as err:
        agent.redo_problem(sid, rid, text)
    assert err.value.code == code


def test_cannot_redo_while_a_run_is_going(done):
    agent, _, sid, _ = done
    state = agent.db.load_session(sid)
    state.status = SessionStatus.running
    agent.db.save_session(state)
    with pytest.raises(AgentError) as err:
        agent.redo_problem(sid, "q2", INSTRUCTION)
    assert err.value.code == 409


def test_cannot_redo_before_the_plan_is_approved(make_agent):
    agent, _, _ = make_agent()
    state = agent.create_session("hw.txt", b"1. Explain why the sky is blue.\n")
    with pytest.raises(AgentError) as err:
        agent.redo_problem(state.id, "q1", INSTRUCTION)
    assert err.value.code == 409


def test_cannot_redo_while_a_decision_is_waiting(done):
    agent, _, sid, _ = done
    state = agent.db.load_session(sid)
    state.status, state.pending_question = SessionStatus.awaiting_input, "Which language?"
    agent.db.save_session(state)
    with pytest.raises(AgentError) as err:
        agent.redo_problem(sid, "q2", INSTRUCTION)
    assert err.value.code == 409


def test_the_instruction_limit_still_applies(done):
    agent, _, sid, _ = done
    state = agent.db.load_session(sid)
    state.user_notes = [f"note {i}" for i in range(10)]
    agent.db.save_session(state)
    with pytest.raises(AgentError) as err:
        agent.redo_problem(sid, "q2", INSTRUCTION)
    assert err.value.code == 409


def test_the_api_redo_route(make_agent):
    agent, _, settings = make_agent(provider=FakeProvider(responder), cap=30)
    c = TestClient(create_app(settings, agent))
    sid = prepare(agent)
    agent.start_run(sid)
    r = c.post(f"/api/sessions/{sid}/redo", json={"requirement_id": "q2", "instruction": INSTRUCTION})
    assert r.status_code == 200 and r.json()["status"] == "plan_approved"
    assert c.post(f"/api/sessions/{sid}/run", json={"mode": "model"}).json()["status"] == "completed"
    assert c.post(f"/api/sessions/{sid}/redo", json={"requirement_id": "nope", "instruction": "x"}).status_code == 400
