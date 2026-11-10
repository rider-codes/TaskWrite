"""Typing an instruction while a run is going: it is queued and picked up when the next step starts. It is context
only, like every note: it cannot approve tools, change the plan or add budget, and it is never lost."""

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentError
from app.main import create_app
from app.models import SessionStatus
from conftest import FakeProvider, default_responder, prepare

NOTE = "use bullet points everywhere"
DOC = b"Quiz\n1. Explain why the sky is blue.\n2. Explain why grass is green.\n3. Explain why snow is white.\n"


class Typist:
    """A fake model that 'types' a note into the session while a chosen model call is in progress."""

    def __init__(self, on_call):
        self.on_call, self.calls, self.agent, self.sid, self.error = on_call, 0, None, None, None

    def __call__(self, req):
        self.calls += 1
        if self.calls == self.on_call:
            try:
                self.agent.add_note(self.sid, NOTE)
            except Exception as exc:  # noqa: BLE001 - reported by the test
                self.error = exc
        return default_responder(req)


def start(make_agent, on_call, cap=30):
    typist = Typist(on_call)
    agent, provider, settings = make_agent(provider=FakeProvider(typist), cap=cap)
    sid = prepare(agent, DOC, tools=("model",))
    typist.agent, typist.sid = agent, sid
    return agent, provider, settings, sid, typist


def running_session(make_agent):
    agent, _, settings = make_agent()
    sid = prepare(agent, DOC, tools=("model",))
    state = agent.db.load_session(sid)
    state.status = SessionStatus.running
    agent.db.save_session(state)
    return agent, settings, sid


def test_a_note_typed_during_a_run_is_queued_not_refused(make_agent):
    agent, _, sid = running_session(make_agent)
    state = agent.add_note(sid, NOTE)
    assert state.user_notes == [] and agent.db.queued_notes(sid) == [NOTE]  # the running step's state is not touched


def test_the_next_step_gets_the_note_and_earlier_steps_did_not(make_agent):
    agent, provider, _, sid, typist = start(make_agent, on_call=1)
    state = agent.start_run(sid)
    assert typist.error is None and state.status == SessionStatus.completed
    assert len(provider.prompts) >= 2
    assert NOTE not in provider.prompts[0]  # that call had already been sent
    assert all(NOTE in p for p in provider.prompts[1:])
    assert state.user_notes == [NOTE] and agent.db.queued_notes(sid) == []


def test_a_note_typed_during_the_last_step_is_kept_for_later_not_lost(make_agent):
    agent, provider, _, sid, typist = start(make_agent, on_call=99)
    typist.on_call = None
    agent.start_run(sid)
    last = len(provider.prompts)
    agent2, provider2, _, sid2, typist2 = start(make_agent, on_call=last)
    state = agent2.start_run(sid2)
    assert typist2.error is None and state.status == SessionStatus.completed
    assert state.user_notes == [NOTE] and agent2.db.queued_notes(sid2) == []


def test_a_queued_note_changes_nothing_that_was_approved(make_agent):
    agent, _, _, sid, _ = start(make_agent, on_call=1)
    before = agent.db.load_session(sid)
    state = agent.start_run(sid)
    assert state.approved_plan_hash == before.approved_plan_hash and state.step_cap == before.step_cap
    assert {t.name: t.approved for t in state.tool_requests} == {t.name: t.approved for t in before.tool_requests}


def test_the_limit_counts_queued_notes_too(make_agent):
    agent, _, sid = running_session(make_agent)
    for i in range(10):
        agent.add_note(sid, f"note {i}")
    with pytest.raises(AgentError) as err:
        agent.add_note(sid, "one too many")
    assert err.value.code == 409 and len(agent.db.queued_notes(sid)) == 10


def test_an_empty_note_is_refused_and_a_long_one_is_cut(make_agent):
    agent, _, sid = running_session(make_agent)
    with pytest.raises(AgentError):
        agent.add_note(sid, "   ")
    agent.add_note(sid, "x" * 5000)
    assert len(agent.db.queued_notes(sid)[0]) <= 600


def test_a_stranded_queued_note_is_picked_up_by_the_next_action(make_agent):
    agent, _, sid = running_session(make_agent)
    agent.add_note(sid, NOTE)
    state = agent.db.load_session(sid)
    state.status = SessionStatus.plan_approved  # the run ended without seeing it (for example, the app restarted)
    agent.db.save_session(state)
    state = agent.add_note(sid, "second note")
    assert state.user_notes == [NOTE, "second note"] and agent.db.queued_notes(sid) == []


def test_the_api_shows_queued_notes_and_deleting_a_session_clears_them(make_agent):
    agent, settings, sid = running_session(make_agent)
    c = TestClient(create_app(settings, agent))
    r = c.post(f"/api/sessions/{sid}/notes", json={"text": NOTE})
    assert r.status_code == 200 and r.json()["queued_notes"] == [NOTE] and r.json()["user_notes"] == []
    assert c.get(f"/api/sessions/{sid}").json()["queued_notes"] == [NOTE]
    state = agent.db.load_session(sid)
    state.status = SessionStatus.completed
    agent.db.save_session(state)
    assert c.delete(f"/api/sessions/{sid}").status_code == 200
    assert agent.db.queued_notes(sid) == []
