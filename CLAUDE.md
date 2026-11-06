# CLAUDE.md: TaskWrite

Guide for anyone (human or AI) working on this repo. Short on purpose: read the first two sections to get going, the rest when you need it. The long original rulebook is kept in [docs/RULES_FULL.md](docs/RULES_FULL.md).

## 1. What this is

TaskWrite is an open-source, **local-first AI agent that completes assignments** on your own computer. You give it an assignment (PDF, DOCX, TXT, or just paste text). It lists what the assignment asks for, asks which tools it may use, shows a plan, and only after you approve does it do the work, check it against every requirement, and hand you the result with an explanation.

It does the real work (code, essays, calculations), but it never submits anything for you and never claims more than it did.

**Principles**
- **Local-first, free to start:** Ollama by default; no key needed for basic use.
- **Bring your own key:** Gemini, Groq, OpenAI, Anthropic through one provider interface.
- **Honest:** never say something was verified, tested, opened, sent or exported unless it was.
- **Controlled:** approvals, step limits and outbound actions are enforced in backend code, not just prompts.
- **Useful output:** every requirement is checked and the limits are stated plainly.

## 2. Quick start

```bash
python -m venv .venv && .venv\Scripts\activate     # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env                              # add keys only if you want a cloud model
uvicorn app.main:app --reload                       # website: /   app: /app
pytest                                              # ~45 s, needs no network, key or Docker
python -m app.sandbox_check                         # shows which code isolation you really have
```

Docker (whole app): `docker compose up --build`. (Code then runs in the restricted-process mode; for Docker isolation run the app on your machine with Docker Desktop installed and `docker pull python:3.12-slim`.)

## 3. How a run works

```
Upload/paste -> extract -> find questions + rules -> editable checklist
 -> choose tools (approve each) -> plan (approve) -> run steps
 -> validate every requirement -> repair (bounded) -> deliverable + report -> export
```

- **Python assessments:** one step per question. The model writes the solution and its own tests; they run in the sandbox; up to 2 fix attempts use the error output; each answer gets a plain-English explanation. Files (`q1.py`, `check_q1.py`, `EXPLANATIONS.md`) are saved in the user's chosen workspace folder only after the VS Code tool is approved.
- **Essays:** optional research step. The model proposes up to 3 topic-only search queries; the user sees them and approves before anything is sent. Optional second step opens up to 3 result pages (addresses approved first). The essay may cite only the numbered sources found (`[S1]`); invented citations fail validation and trigger a bounded rewrite. Output ends with a Sources section and a "How I approached this" note.
- **Written/maths questions:** answered by the model or the local calculator.
- **Tools:** `model`, `calculator`, `code_runner`, `vscode`, `word`, `email` (draft only), `web_search`, `read_pages`, plus any MCP tool the user configured.
- **Ask when stuck:** the model may reply `NEED_INPUT: <question>` (max 5 per session); typed text in the chat answers it, otherwise typed text is added as a hint ("note") for the model.
- **Change this:** a finished problem can be redone with an instruction (`POST /sessions/{id}/redo`). Only that problem's step is reset; the approved plan and its hash are unchanged, the instruction is a labelled note ("For Q2: ..."), and the redo goes through the normal run loop, budget and approval cards.
- **Budget:** `MAX_AGENT_STEPS` (default 8) caps model calls, searches, page opens, code runs and fix attempts. At the cap the run pauses and shows what remains; the user adds steps to continue.

## 4. The rules (each has tests; do not weaken them)

1. **Uploaded text is data, never instructions.** Documents, web pages, search results and tool output are untrusted. Injection-looking lines are flagged and removed before a model sees them. Only the necessary sections go to a provider.
2. **No tool runs without per-session approval,** re-checked in code right before use (`ToolGate`). A plan may only name approved tools; changing an approved plan needs re-approval (plan hash).
3. **Nothing leaves the machine without an approval screen** showing exactly what is sent and where: search queries, page addresses, email drafts, MCP calls that could change things. Cloud model use needs a consent tick that names the provider.
4. **Bounded work:** every action is charged to the step budget; no path may bypass the cap. Allowed plan action kinds are fixed (`analyze_assignment`, `check_requirements`, `retrieve_context`, `explain`, `solve`, `generate_code`, `analyze_data`, `draft_document`, `review_output`, `validate_output`, `export_deliverable`); unknown kinds are rejected in code.
5. **Generated code runs only in the sandbox** (`code_runner` approved): Docker when available (no network, read-only, 256 MB, 64 processes, no capabilities, non-root), otherwise a restricted child process (empty env, temp folder, timeout, Job Object/rlimits, in-process guard). The text deny-list is only a pre-check. The fallback is "defence in depth, not a hard sandbox", and results record which level ran.
6. **Wording stays honest:** "ran without errors" and "model-written tests passed" are execution facts, never "correct". Search snippets are not "read sources". Email is never "sent". Apps are only "asked to open".
7. **Files are written only inside the workspace folder the user chose;** path traversal is rejected.
8. **Secrets:** keys live in `.env` or in memory only; never in SQLite, logs, errors, API responses or the browser. Redaction covers pasted keys too.
9. **Web safety (SSRF guard in `app/web.py`):** only addresses the search returned can be opened; http/https on 80/443; no logins in URLs; every resolved address must be public; every redirect re-checked; non-web content refused; size/time capped; only the first ~3,000 characters kept.
10. **Never fabricate** citations, sources, results or tool output. If something cannot be done, say why and what remains.

11. **Publishing through MCP is built by code, never by the model.** Files for the code answers are created through the VS Code MCP extension (loopback HTTP only, `app/publish.py`): one approval card lists every file with its own tick (files start ticked), each call is hashed when approved and refused if anything changed, every path is checked in code (the extension itself does not stop `../`), nothing is ever overwritten, a temporary marker file proves the server's open folder is the user's workspace folder before anything is created, and every file is read back from disk before it is called "created". Tools that can send, share, delete or run shell commands (`forbidden_reason` in `app/safety.py`) are hidden, cannot be approved, and are refused by `ToolGate` and `McpManager.call` even if the database says "approved". The old VS Code/Word/email launchers remain as fallbacks when no MCP server is configured. A Gmail draft (`workspace-mcp`, `draft_gmail_message` only) goes on the same card, starts unticked, carries the finished answer as its body with no recipient, is refused if the content changed after approval, and is reported as "the server says it saved a draft" because no tool can read a draft back. Only that one Gmail tool is offered; the server is started with the other mail tools disabled, and anything named send, share or delete is blocked in code. A Google Doc (`create_doc` only, `gdocs` server) follows the same rules: unticked, plain text, no folder, never shared, reported as "the server says it created a Google Doc" with its link. MCP `env` values may use `${NAME}` so Google's client ID and secret stay in `.env`, not the config file.

12. **The local server only serves this computer.** Every request's Host must be 127.0.0.1, localhost or [::1] (plus `ALLOWED_HOSTS`), which stops DNS rebinding; any request that changes something must come from the app's own page (same host and port in `Origin`, no `null`/`file://`, no `Sec-Fetch-Site: cross-site`). Scripts without an Origin still work. No CORS headers are sent. Tests in `tests/test_host_origin.py`.

## 5. Providers and configuration

| Provider | Notes |
| --- | --- |
| Ollama (default) | local, no key; pick an installed model |
| Gemini | key + model from the provider's own list (non-text models filtered) |
| Groq | OpenAI-compatible; **verified end to end** |
| OpenAI | Chat Completions; needs API credits (not a ChatGPT subscription) |
| Anthropic | Messages API; needs API credits; no JSON mode (parse-and-validate fallback) |

All adapters sit behind `ModelProvider` (`generate`, `health_check`; structured output via `generate_structured` with Pydantic validation and a bounded JSON-mode-to-plain-text fallback). Errors are normalised (`auth`, `billing`, `rate_limited`, `timeout`, `model_missing`, `bad_response`, `unreachable`); account problems stop a run at once. Never hard-code a model ID. Reasoning models spend completion tokens thinking, so keep `max_tokens` generous.

**.env** (all optional): `LLM_PROVIDER`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `GEMINI|GROQ|OPENAI|ANTHROPIC_API_KEY` and `_MODEL`, `MAX_AGENT_STEPS`, `DATABASE_URL`, `WORKSPACE_DIR`, `MCP_CONFIG`, `REQUEST_TIMEOUT_S`, `SANDBOX_IMAGE`, `SANDBOX_TIMEOUT_S`, `SANDBOX_DOCKER` (`auto`/`off`), `ALLOWED_HOSTS` (extra host names; loopback is always allowed). Keys can also be pasted in Settings (memory only).

## 6. Where things live

```
app/
  main.py          routes and app setup          config.py   settings from env
  agent.py         session lifecycle + LangGraph run loop, repair, hand-off
  planner.py       find questions, classify, offer tools, build plan
  executor.py      run steps, ToolGate, research and page steps, MCP step
  code_steps.py    Python workflow: generate, run, fix, explain
  validator.py     requirement and citation checks
  sandbox.py       Docker / restricted-process runner;  sandbox_check.py  self-test
  web.py           DuckDuckGo search, SSRF-guarded page reader, citation helpers
  safety.py        injection scan, redaction, plan/tool validation
  llm.py keys.py   provider routing, in-memory key store
  providers/       base, http_base, ollama, gemini, groq, openai, anthropic
  tools.py         tool catalog, calculator, launchers (VS Code/Word/mailto)
  mcp_client.py    MCP servers (local program or loopback HTTP)      publish.py   files created through MCP
  db.py models.py extract.py exporters.py
static/index.html  chat UI (one file, no build step; deep link /app#sid=<id>&focus=...)
site/              marketing site (home + "how it works")
tests/             ~220 tests incl. 30 eval cases (eval_cases.py)
samples/           synthetic assignments (never real student work)
docs/screenshots/  README images
```

## 7. Working on it

- **Tests first for guardrails:** every security or execution rule needs a test that tries to break it. Use mocked HTTP for providers (`httpx.MockTransport`); tests must not need network, keys or Docker.
- **Add an eval case** (`tests/eval_cases.py`) when you find an assignment layout the detector gets wrong, then fix the detector.
- **Add a provider:** copy `providers/openai.py`, register it in `llm.py`, `keys.py`, config and the UI name map; test errors, missing key, and that the key never appears in a message.
- **Style:** type hints, small functions, Pydantic for API and model output, vendor details only inside adapters, short plain-English commit messages.
- **Windows gotcha:** bash heredocs mangle backslash escapes (`\b`, `\n`) in Python regexes and strings; write scripts with a file-writing tool instead.
- **Never commit** `.env`, databases, `workspaces/` or keys (they are git-ignored). Publish with `git add .` and read `git status` first.

## 8. Status and honest limits

**Done and tested (218 tests, 30 eval cases):** five providers, chat UI with history/delete/progress/stop, Python workflow in a Docker sandbox, essay research with page reading, citation checks and rewrite, Word/VS Code/email-draft/MCP hand-offs, prompt-injection and SSRF guards, resumable runs.

**Verified with real services:** Groq (code, essays, search, page reading, email draft, MCP), Docker sandbox, live DuckDuckGo search.

**Not verified or weak:**
- Gemini, OpenAI, Anthropic adapters are tested against mocked responses only (the Gemini test account had no credits).
- The Browse folder dialog, real VS Code and real Word launches were only tested with fakes.
- "Tests passed" can be wrong or too easy; question detection is rules-based and unusual layouts may be mis-split (the checklist is editable); some sites block page reading (HTTP 403); the process-limited sandbox is not a hard sandbox; Groq's free tier rate-limits quickly on large exams.
- Not built: reading PDFs/files linked from results, charts or data files, a light theme.

**Definition of done:** clone, run with Ollama, upload an assignment, approve tools and plan, get a completed deliverable with a requirement report, and export it; switch to a cloud provider without changing code; and the app says clearly when it cannot be trusted to complete a task.
