"""Saving the finished essay as a Google Doc through an MCP server. Create only: nothing is read, edited, shared or deleted."""

import json
import sys
from pathlib import Path

import pytest

from app.agent import AgentError
from app.mcp_client import McpError, McpManager
from app.models import SessionStatus
from conftest import FakeLauncher, FakeProvider

FAKE = str(Path(__file__).parent / "fake_gdocs_mcp.py")
FAKE_GMAIL = str(Path(__file__).parent / "fake_gmail_mcp.py")
DOC_TOOL = "mcp:gdocs:create_doc"
MAIL_TOOL = "mcp:gmail:draft_gmail_message"
ESSAY = b"Bees essay\n1. Write a short essay on why bees matter to farming.\n"
ACCOUNT = "me@example.com"
BLOCKED = ("search_docs", "get_doc_content", "modify_doc_text", "list_docs_in_folder", "share_doc", "delete_doc", "start_google_auth")


class Env:
    def __init__(self, tmp_path, make_agent, monkeypatch, fail=False, account=True, with_gmail=False, provider=None):
        self.log = tmp_path / "gdocs.log"
        self.gmail_log = tmp_path / "gmail.log"
        monkeypatch.setenv("TW_TEST_LOG", str(self.log))
        monkeypatch.setenv("TW_TEST_GMAIL_LOG", str(self.gmail_log))
        monkeypatch.setenv("TW_TEST_EMAIL", ACCOUNT)
        env = {"FAKE_GDOCS_LOG": "${TW_TEST_LOG}", "FAKE_GDOCS_FAIL": "1" if fail else "0"}
        if account:
            env["USER_GOOGLE_EMAIL"] = "${TW_TEST_EMAIL}"
        servers = {"gdocs": {"command": sys.executable, "args": [FAKE], "env": env}}
        if with_gmail:
            servers["gmail"] = {"command": sys.executable, "args": [FAKE_GMAIL], "env": {
                "FAKE_GMAIL_LOG": "${TW_TEST_GMAIL_LOG}", "USER_GOOGLE_EMAIL": "${TW_TEST_EMAIL}", "FAKE_GMAIL_FAIL": "0"}}
        cfg = tmp_path / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        self.manager = McpManager(cfg)
        self.status, self.problems = self.manager.refresh()
        self.agent, _, _ = make_agent(provider=provider or FakeProvider(), cap=30, mcp=self.manager, launcher=FakeLauncher())

    def calls(self, log=None):
        log = log or self.log
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()] if log.exists() else []

    def names_called(self):
        return [c["name"] for c in self.calls()]

    def session(self, doc=ESSAY, tools=("model", DOC_TOOL), name="bees.txt"):
        state = self.agent.create_session(name, doc)
        offered = {t.name for t in state.tool_requests}
        self.agent.set_tool_approvals(state.id, {t: True for t in tools if t in offered})
        self.agent.generate_plan(state.id)
        self.agent.approve_plan(state.id)
        return state.id

    def run_to_card(self, **kw):
        sid = self.session(**kw)
        return sid, self.agent.start_run(sid)

    def approve_and_run(self, sid, item_ids=None, approve=True):
        state = self.agent.db.load_session(sid)
        ids = item_ids if item_ids is not None else [i.id for i in state.publish_items if i.checked]
        self.agent.confirm_publish(sid, approve, ids)
        return self.agent.start_run(sid)


@pytest.fixture
def env(tmp_path, make_agent, monkeypatch):
    return Env(tmp_path, make_agent, monkeypatch)


# --- what is offered: only create_doc -----------------------------------------------------------------------------

def test_only_create_doc_is_offered_and_share_and_delete_are_hidden(env):
    server = next(s for s in env.status if s.name == "gdocs")
    assert server.status == "connected"
    assert {"share_doc", "delete_doc"} <= set(server.hidden)
    state = env.agent.create_session("b.txt", ESSAY)
    assert {t.name for t in state.tool_requests if t.group == "gdocs"} == {DOC_TOOL}  # no reading, editing, listing or sign-in tools


def test_the_offer_says_it_only_creates_a_document_and_never_shares(env):
    state = env.agent.create_session("b.txt", ESSAY)
    why = next(t.reason for t in state.tool_requests if t.name == DOC_TOOL).lower()
    assert "google doc" in why and "never shares" in why and "cannot read" in why


@pytest.mark.parametrize("name", ["share_doc", "delete_doc"])
def test_share_and_delete_tools_can_never_be_called(env, name):
    with pytest.raises(McpError, match="blocked"):
        env.manager.call(f"mcp:gdocs:{name}", {"document_id": "x", "email": "a@b.com"})
    state = env.agent.create_session("b.txt", ESSAY)
    with pytest.raises(AgentError):
        env.agent.set_tool_approvals(state.id, {f"mcp:gdocs:{name}": True})
    assert env.names_called() == []


def test_reading_and_editing_tools_are_not_part_of_publishing(env):
    from app.publish import is_doc_tool_id
    assert is_doc_tool_id(DOC_TOOL)
    for name in BLOCKED:
        assert not is_doc_tool_id(f"mcp:gdocs:{name}")


# --- plain text conversion ------------------------------------------------------------------------------------------

def test_markdown_markers_are_turned_into_plain_text():
    from app.publish import to_plain_text
    out = to_plain_text("# Deliverable: bees.txt\n\n### Q1\nBees **matter** because `x`.\n\n- one\n- two\n\n## Sources\n- [S1] A page. https://example.com/a")
    assert "#" not in out and "**" not in out and "`" not in out
    assert "Q1" in out and "Bees matter because x." in out and "one" in out and "https://example.com/a" in out


# --- one approval card, unticked, nothing created before it ---------------------------------------------------------

def test_the_run_stops_at_a_card_with_the_doc_unticked_and_nothing_is_called(env):
    sid, state = env.run_to_card()
    assert state.status == SessionStatus.awaiting_confirmation and env.names_called() == []
    doc = [i for i in state.publish_items if i.role == "doc"]
    assert len(doc) == 1 and doc[0].checked is False and doc[0].required is False
    assert doc[0].server == "gdocs" and doc[0].tool == DOC_TOOL and len(doc[0].args_hash) == 64
    assert "Answer for Q1" in doc[0].preview and ACCOUNT in doc[0].label
    with pytest.raises(AgentError):
        env.agent.start_run(sid)


def test_approving_without_ticking_the_doc_creates_nothing(env):
    sid, _ = env.run_to_card()
    state = env.approve_and_run(sid)
    assert state.status == SessionStatus.completed and env.names_called() == []
    assert next(r for r in state.publish_results if r.role == "doc").status == "declined"


def test_declining_creates_nothing(env):
    sid, _ = env.run_to_card()
    env.approve_and_run(sid, item_ids=["d1"], approve=False)
    assert env.names_called() == []


def test_ticking_the_doc_creates_one_document_with_the_essay_as_plain_text(env):
    sid, card = env.run_to_card()
    state = env.approve_and_run(sid, item_ids=["d1"])
    assert env.names_called() == ["create_doc"]
    sent = env.calls()[0]["args"]
    assert sent["user_google_email"] == ACCOUNT and sent["title"] == "bees"
    assert "Answer for Q1" in sent["content"] and "###" not in sent["content"] and "# " not in sent["content"]
    assert sent["folder_id"] is None  # no folder is ever chosen, and nothing is shared
    assert state.status == SessionStatus.completed


def test_the_result_is_honest_about_what_could_and_could_not_be_checked(env):
    sid, _ = env.run_to_card()
    state = env.approve_and_run(sid, item_ids=["d1"])
    result = next(r for r in state.publish_results if r.role == "doc")
    assert result.status == "created" and result.read_back is False
    assert "https://docs.google.com/document/d/1AbCdEf/edit" in result.detail and "could not" in result.detail.lower()
    said = " ".join(state.tool_results).lower()
    assert "google doc" in said and "nothing was shared" in said
    assert "verified" not in said and "confirmed" not in said


def test_a_server_error_is_reported_as_failed_not_created(tmp_path, make_agent, monkeypatch):
    e = Env(tmp_path, make_agent, monkeypatch, fail=True)
    sid, _ = e.run_to_card()
    state = e.approve_and_run(sid, item_ids=["d1"])
    assert next(r for r in state.publish_results if r.role == "doc").status == "failed"
    assert "Nothing was created" in " ".join(state.tool_results)


def test_content_that_changes_after_approval_is_refused(env):
    sid, _ = env.run_to_card()
    env.agent.confirm_publish(sid, True, ["d1"])
    state = env.agent.db.load_session(sid)
    next(s for s in state.plan.steps if s.output.strip()).output += "\nAn extra line nobody approved."
    env.agent.db.save_session(state)
    state = env.agent.start_run(sid)
    result = next(r for r in state.publish_results if r.role == "doc")
    assert result.status == "refused" and "changed" in result.detail and env.names_called() == []


def test_without_a_configured_account_no_doc_is_offered_and_nothing_is_called(tmp_path, make_agent, monkeypatch):
    e = Env(tmp_path, make_agent, monkeypatch, account=False)
    sid, state = e.run_to_card()
    assert state.status == SessionStatus.completed and not state.publish_items
    assert "USER_GOOGLE_EMAIL" in " ".join(state.tool_results) and "Nothing was created" in " ".join(state.tool_results)
    assert e.names_called() == []


def test_an_essay_too_long_for_one_call_is_not_offered_and_says_why(tmp_path, make_agent, monkeypatch):
    from conftest import default_responder
    e = Env(tmp_path, make_agent, monkeypatch, provider=FakeProvider(lambda req: "### Q1\n" + "word " * 2500))
    sid, state = e.run_to_card()
    assert not state.publish_items and "too long" in " ".join(state.tool_results).lower()
    assert e.names_called() == []


def test_the_doc_tool_is_called_only_when_approved_for_the_session(tmp_path, make_agent, monkeypatch):
    e = Env(tmp_path, make_agent, monkeypatch)
    sid, state = e.run_to_card(tools=("model",))
    assert state.status == SessionStatus.completed and not state.publish_items and e.names_called() == []


def test_the_budget_counts_the_doc(env):
    sid, _ = env.run_to_card()
    before = env.agent.db.load_session(sid).steps_used
    assert env.approve_and_run(sid, item_ids=["d1"]).steps_used == before + 1


# --- a draft and a doc on the same card -----------------------------------------------------------------------------

def test_a_gmail_draft_and_a_doc_share_one_card_and_neither_starts_ticked(tmp_path, make_agent, monkeypatch):
    e = Env(tmp_path, make_agent, monkeypatch, with_gmail=True)
    sid, state = e.run_to_card(tools=("model", DOC_TOOL, MAIL_TOOL))
    roles = {i.role: i.checked for i in state.publish_items}
    assert roles == {"mail": False, "doc": False}
    state = e.approve_and_run(sid, item_ids=["d1", "m1"])
    assert e.names_called() == ["create_doc"] and e.calls(e.gmail_log)[0]["name"] == "draft_gmail_message"
    assert {r.role: r.status for r in state.publish_results} == {"mail": "created", "doc": "created"}
