"""FastAPI routes and application setup."""

from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .agent import Agent, AgentError, to_public
from .config import Settings, load_dotenv
from .db import Database, SessionNotFound
from .exporters import ExportError, export
from .extract import ExtractionError
from .folder_dialog import pick_folder
from .keys import KEYED_PROVIDERS
from .llm import SUPPORTED_PROVIDERS, selected_model
from .models import (AnswerRequest, ConfirmRequest, ContinueRequest, EmailOpenRequest, KeyUpdate, Requirement, RunRequest,
                     SettingsUpdate, ToolApprovals, WorkspaceRequest, NoteRequest, PublishRequest)

class NoCacheStatic(StaticFiles):
    """Always revalidate, so an updated page never runs against a stale cached script."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SITE_DIR = Path(__file__).resolve().parent.parent / "site"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _split_host(value: str) -> tuple[str, int | None]:
    """'localhost:8000' -> ('localhost', 8000); '[::1]:8000' -> ('::1', 8000). Anything unparsable gives ('', None)."""
    try:
        parsed = urlparse("//" + value.strip())
        return (parsed.hostname or "").lower(), parsed.port
    except ValueError:
        return "", None


def _same_origin(origin: str, host: str, port: int | None, scheme: str) -> bool:
    """The Origin must be this very page: same scheme family, same host, same port. 'null' and file:// never match."""
    try:
        parsed = urlparse(origin.strip())
        o_host, o_port = (parsed.hostname or "").lower(), parsed.port
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not o_host:
        return False
    default = {"http": 80, "https": 443}
    return o_host == host and (o_port or default[parsed.scheme]) == (port or default.get(scheme, 80))


def create_app(settings: Settings | None = None, agent: Agent | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if agent is None:
        agent = Agent(Database(settings.db_path, settings.secrets), settings)
    db = agent.db
    app = FastAPI(title="TaskWrite")
    allowed_hosts = {h.lower().strip("[]") for h in settings.allowed_hosts}

    @app.middleware("http")
    async def _local_only(request: Request, call_next):
        """Answer only requests addressed to this computer (stops DNS rebinding), and accept changes only from this
        app's own pages (stops another site making your browser approve, run or delete things here)."""
        host, port = _split_host(request.headers.get("host", ""))
        if host not in allowed_hosts:
            return JSONResponse({"detail": "This address is not allowed. Open TaskWrite at http://127.0.0.1 or http://localhost."},
                                status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None and not _same_origin(origin, host, port, request.url.scheme):
                return JSONResponse({"detail": "Changes are only accepted from TaskWrite's own page."}, status_code=403)
            if origin is None and request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Changes are only accepted from TaskWrite's own page."}, status_code=403)
        return await call_next(request)

    @app.exception_handler(AgentError)
    async def _agent_error(_: Request, exc: AgentError) -> JSONResponse:
        return JSONResponse({"detail": exc.message, "options": exc.options}, status_code=exc.code)

    @app.exception_handler(SessionNotFound)
    async def _not_found(_: Request, exc: SessionNotFound) -> JSONResponse:
        return JSONResponse({"detail": "Session not found."}, status_code=404)

    @app.exception_handler(ExtractionError)
    async def _extraction(_: Request, exc: ExtractionError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # --- settings & health ------------------------------------------------

    @app.get("/api/health")
    def health() -> dict:
        provider, status, cfg = agent.provider_status()
        active = cfg.llm_provider
        return {"config": cfg.model_dump(), "ok": status.ok, "message": status.message, "models": status.models,
                "supported_providers": list(SUPPORTED_PROVIDERS), "max_agent_steps": settings.max_agent_steps,
                "cloud": active in KEYED_PROVIDERS, "active_model": selected_model(cfg),
                "keys": {p: agent.keys.has(p) for p in KEYED_PROVIDERS},
                "vscode_available": shutil.which("code") is not None, "workspace_dir": str(Path(settings.workspace_dir).resolve())}

    @app.put("/api/settings")
    def update_settings(body: SettingsUpdate) -> dict:
        if any(s["status"] == "running" for s in db.list_sessions()):  # the model picker is locked while a run is going
            raise HTTPException(409, "A run is in progress. Change the model after it stops.")
        warning = ""
        if body.llm_provider is not None:
            if body.llm_provider not in SUPPORTED_PROVIDERS:
                raise HTTPException(400, f"Provider '{body.llm_provider}' is not available yet.")
            db.set_setting("llm_provider", body.llm_provider)
        if body.ollama_base_url is not None:
            parsed = urlparse(body.ollama_base_url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise HTTPException(400, "Ollama URL must start with http:// or https://")
            db.set_setting("ollama_base_url", body.ollama_base_url.rstrip("/"))
            if parsed.hostname not in LOCAL_HOSTS:
                warning = "This Ollama host is not on this machine; assignment text will be sent to it."
        if body.ollama_model is not None:
            db.set_setting("ollama_model", body.ollama_model.strip())
        if body.gemini_model is not None:
            db.set_setting("gemini_model", body.gemini_model.strip())
        if body.groq_model is not None:
            db.set_setting("groq_model", body.groq_model.strip())
        if body.openai_model is not None:
            db.set_setting("openai_model", body.openai_model.strip())
        if body.anthropic_model is not None:
            db.set_setting("anthropic_model", body.anthropic_model.strip())
        return {**health(), "warning": warning}

    @app.put("/api/keys")
    def put_key(body: KeyUpdate) -> dict:
        """Store an API key in memory only. The key is never returned, logged or saved to disk."""
        try:
            agent.keys.set(body.provider, body.key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return health()

    # --- sessions ---------------------------------------------------------

    @app.get("/api/sessions")
    def list_sessions() -> list:
        return db.list_sessions()

    @app.post("/api/sessions")
    async def upload(file: UploadFile = File(...)) -> dict:
        data = await file.read(settings.max_upload_bytes + 1)
        return to_public(agent.create_session(file.filename or "upload", data))

    @app.post("/api/sessions/{sid}/publish")
    def post_publish(sid: str, body: PublishRequest) -> dict:
        return to_public(agent.confirm_publish(sid, body.approve, body.item_ids))

    @app.post("/api/sessions/{sid}/notes")
    def post_note(sid: str, body: NoteRequest) -> dict:
        return to_public(agent.add_note(sid, body.text))

    @app.put("/api/sessions/{sid}/workspace")
    def put_workspace(sid: str, body: WorkspaceRequest) -> dict:
        return to_public(agent.set_workspace(sid, body.path))

    @app.get("/api/workspace/browse")
    def browse_workspace() -> dict:
        """Open a native folder dialog on this machine (the backend runs locally). Falls back to typing."""
        return {"path": pick_folder()}

    @app.get("/api/sandbox")
    def sandbox_info() -> dict:
        return agent.sandbox.describe()

    @app.delete("/api/sessions/{sid}")
    def delete_session(sid: str) -> dict:
        agent.delete_session(sid)
        return {"deleted": sid}

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str) -> dict:
        return to_public(db.load_session(sid))

    @app.put("/api/sessions/{sid}/checklist")
    def put_checklist(sid: str, items: list[Requirement]) -> dict:
        return to_public(agent.update_checklist(sid, items))

    @app.post("/api/sessions/{sid}/checklist/refine")
    def refine(sid: str) -> dict:
        return to_public(agent.refine_checklist(sid))

    @app.put("/api/sessions/{sid}/tools")
    def put_tools(sid: str, body: ToolApprovals) -> dict:
        return to_public(agent.set_tool_approvals(sid, body.approvals))

    @app.post("/api/sessions/{sid}/plan")
    def make_plan(sid: str) -> dict:
        return to_public(agent.generate_plan(sid))

    @app.put("/api/sessions/{sid}/plan")
    def put_plan(sid: str, body: dict) -> dict:
        return to_public(agent.update_plan(sid, body))

    @app.post("/api/sessions/{sid}/plan/approve")
    def approve(sid: str) -> dict:
        return to_public(agent.approve_plan(sid))

    @app.post("/api/sessions/{sid}/run")
    def run(sid: str, body: RunRequest) -> dict:
        return to_public(agent.start_run(sid, body.mode, body.consent_cloud))

    @app.post("/api/sessions/{sid}/continue")
    def continue_(sid: str, body: ContinueRequest) -> dict:
        return to_public(agent.continue_run(sid, body.extra_steps, body.mode, body.consent_cloud))

    @app.get("/api/mcp")
    def mcp_status() -> dict:
        servers, problems = agent.mcp.status()
        return {"config_path": str(agent.mcp.config_path.resolve()), "problems": problems,
                "servers": [s.model_dump() for s in servers]}

    @app.post("/api/mcp/refresh")
    def mcp_refresh() -> dict:
        servers, problems = agent.mcp.refresh()
        return {"config_path": str(agent.mcp.config_path.resolve()), "problems": problems,
                "servers": [s.model_dump() for s in servers]}

    @app.post("/api/sessions/{sid}/mcp/sync")
    def mcp_sync(sid: str) -> dict:
        return to_public(agent.sync_mcp_tools(sid))

    @app.post("/api/sessions/{sid}/confirm")
    def confirm(sid: str, body: ConfirmRequest) -> dict:
        return to_public(agent.confirm_call(sid, body.approve))

    @app.post("/api/sessions/{sid}/answer")
    def answer(sid: str, body: AnswerRequest) -> dict:
        return to_public(agent.answer_question(sid, body.answer))

    @app.post("/api/sessions/{sid}/email/open")
    def open_email(sid: str, body: EmailOpenRequest) -> dict:
        return to_public(agent.open_email(sid, body))

    @app.post("/api/sessions/{sid}/cancel")
    def cancel(sid: str) -> dict:
        return to_public(agent.cancel(sid))

    @app.get("/api/sessions/{sid}/log")
    def get_log(sid: str) -> list:
        db.load_session(sid)
        return db.get_log(sid)

    @app.get("/api/sessions/{sid}/export")
    def export_session(sid: str, format: str = Query("md"), report: bool = Query(True)) -> Response:
        state = db.load_session(sid)
        try:
            data, media_type, filename = export(state, format, report)
        except ExportError as exc:
            raise HTTPException(409, str(exc)) from exc
        db.log_action(sid, "export", f"Exported {filename}", f"{len(data)} bytes")
        return Response(data, media_type=media_type,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon_ico() -> Response:
        icon = SITE_DIR / "favicon.svg"
        if icon.is_file():
            return Response(icon.read_bytes(), media_type="image/svg+xml")
        return Response(status_code=404)

    if STATIC_DIR.is_dir():
        app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")

        @app.get("/app")
        def app_ui() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    if SITE_DIR.is_dir():  # marketing site at the root; mounted last so API routes win
        app.mount("/", NoCacheStatic(directory=SITE_DIR, html=True), name="site")

    return app


def _default_app() -> FastAPI:
    load_dotenv()
    return create_app()


app = _default_app()
