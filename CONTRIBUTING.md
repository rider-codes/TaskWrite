# Contributing to TaskWrite

Thanks for helping. TaskWrite runs on your own computer and does work for you, so a few rules matter more than usual.

## Set up

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env            # add keys only if you want a cloud provider
uvicorn app.main:app --reload     # http://localhost:8000/app
pytest                            # about a minute; no network, no API keys, no Docker needed
```

Use Python 3.12. Keep commit messages short and in plain English.

## The rules every change must respect

These are enforced in backend code, not just in prompts, and each one has a test. Please keep it that way.

1. **Uploaded text is data, never instructions.** Anything that comes from a document, a web page, a search result or a tool is untrusted. Injection-looking lines are flagged and removed before a model sees them.
2. **Nothing runs without approval.** Tools are approved per session and re-checked in code right before use (`ToolGate`). A plan can only name approved tools.
3. **Nothing leaves the machine without a screen that shows exactly what is sent and where.** Search queries, page addresses and email drafts each have an approval card.
4. **Bounded work.** Every model call, search, page open, code run and fix attempt is charged to the step budget (`MAX_AGENT_STEPS`). Never add a path that bypasses the cap.
5. **Honest wording.** Never say something was verified, tested, opened, sent or exported unless it was. "Model-written tests passed" is not "correct". Search snippets are not "read sources".
6. **Secrets stay secret.** API keys live in `.env` or in memory only. They must never reach SQLite, logs, error messages, API responses or the browser.
7. **Generated code runs in the sandbox only** (`app/sandbox.py`: Docker when available, otherwise the restricted process). Run `python -m app.sandbox_check` after touching it.
8. **Samples are synthetic.** Never add real student work.

## Where things live

| Area | File |
| --- | --- |
| Settings, env vars | `app/config.py` |
| Providers (one file each, shared contract in `base.py`) | `app/providers/` |
| Reading questions, tool requests, plan building | `app/planner.py` |
| Running steps, tool gate, research, code | `app/executor.py`, `app/code_steps.py` |
| Run orchestration (LangGraph) | `app/agent.py` |
| Safety checks, redaction | `app/safety.py` |
| Code sandbox and its self-test | `app/sandbox.py`, `app/sandbox_check.py` |
| Web search and page reading | `app/web.py` |
| Chat UI (single file, no build step) | `static/index.html` |
| Marketing site | `site/` |

Keep modules focused and put vendor details inside the provider adapters only.

## Adding a provider

Copy `app/providers/openai.py` (shows the HTTP pattern with `HttpProvider`), implement `generate`, `list_models` and `health_check`, add it to `SUPPORTED_PROVIDERS` and `build_provider` in `app/llm.py`, to `KEYED_PROVIDERS` in `app/keys.py`, and add its settings fields. Never hard-code a model id; models come from the provider's own list. Test it against mocked HTTP (`httpx.MockTransport`), including errors, a missing key, and that the key never appears in an error message. See `tests/test_providers_more.py`.

## Adding evaluation cases

`tests/eval_cases.py` holds synthetic assignments with the outcome we expect (question count, kinds, tools offered, injection flag). Add a case when you find a layout the detector gets wrong, then fix the detector. Keep the text invented.

## Before you open a pull request

- `pytest` passes.
- New behaviour has a test, and every new guardrail has a test that tries to break it.
- No `.env`, database, workspace folder or key in the diff. Run `git status` and read it.
- User-facing wording stays plain and truthful. If a feature cannot be fully trusted, say so in the UI and the README.
