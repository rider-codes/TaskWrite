"""Tap-to-answer: when the agent asks a question it may offer a few choices. They are suggestions only: the student
can tap one or type anything, and the choice is recorded exactly like a typed answer."""

import pytest

from app.executor import parse_question
from app.models import SessionStatus
from conftest import FakeProvider, default_responder, prepare

DOC = b"1. Write a passage about energy.\n"


def asking(reply):
    prompts = []

    def responder(req):
        prompts.append(req.prompt)
        if "Information the student has given you" not in req.prompt:
            return reply
        return default_responder(req)
    return responder, prompts


def run(make_agent, reply):
    responder, prompts = asking(reply)
    agent, _, _ = make_agent(provider=FakeProvider(responder=responder))
    sid = prepare(agent, DOC, tools=("model",))
    return agent, sid, agent.start_run(sid), prompts


def test_the_question_and_its_choices_reach_the_session(make_agent):
    _, _, state, _ = run(make_agent, "NEED_INPUT: Which source should the passage focus on?\nOPTIONS: Solar | Wind | Hydro")
    assert state.status == SessionStatus.awaiting_input
    assert state.pending_question == "Which source should the passage focus on?"
    assert state.pending_options == ["Solar", "Wind", "Hydro"]


def test_a_question_without_choices_still_works(make_agent):
    _, _, state, _ = run(make_agent, "NEED_INPUT: Which source should the passage focus on?")
    assert state.pending_question.startswith("Which source") and state.pending_options == []


def test_tapping_a_choice_answers_like_typing_it_and_clears_the_choices(make_agent):
    agent, sid, _, prompts = run(make_agent, "NEED_INPUT: Which source?\nOPTIONS: Solar | Wind")
    state = agent.answer_question(sid, "Wind")
    assert state.pending_question == "" and state.pending_options == []
    state = agent.start_run(sid)
    assert state.status == SessionStatus.completed and state.answers[0].answer == "Wind" and "Wind" in prompts[-1]


def test_the_student_can_still_type_something_else(make_agent):
    agent, sid, _, prompts = run(make_agent, "NEED_INPUT: Which source?\nOPTIONS: Solar | Wind")
    agent.answer_question(sid, "Geothermal, please")
    assert agent.start_run(sid).answers[0].answer == "Geothermal, please"


@pytest.mark.parametrize("text,question,options", [
    ("NEED_INPUT: Pick one?\nOPTIONS: A | B | C | D | E | F", "Pick one?", ["A", "B", "C", "D"]),          # at most four
    ("NEED_INPUT: Pick one?\nOPTIONS: Only one", "Pick one?", []),                                          # one choice is no choice
    ("NEED_INPUT: Pick one?\nOPTIONS: A |  | A | b", "Pick one?", ["A", "b"]),                              # blanks and repeats dropped
    ("NEED_INPUT: Pick one?\nOPTIONS: " + "x" * 200 + " | Short", "Pick one?", ["x" * 80, "Short"]),        # long ones cut
    ("need_input: lower case?\noptions: yes | no", "lower case?", ["yes", "no"]),
    ("NEED_INPUT: Same line? OPTIONS: Yes | No", "Same line?", ["Yes", "No"]),
    ("NEED_INPUT:\nOPTIONS: A | B", "More information is needed.", ["A", "B"]),
    ("NEED_INPUT: Q?\nSome other text\nOPTIONS: A | B\nOPTIONS: C | D", "Q?", ["A", "B"]),                  # only the first list
])
def test_choices_are_cleaned_up(text, question, options):
    assert parse_question(text) == (question, options)


def test_the_model_is_told_how_to_offer_choices(make_agent):
    _, _, _, prompts = run(make_agent, "NEED_INPUT: Which?\nOPTIONS: A | B")
    from app.executor import ASK_RULE
    assert "OPTIONS:" in ASK_RULE and "|" in ASK_RULE
