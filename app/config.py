"""Settings and environment configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_STEPS = 8
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing values."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _parse_steps(raw: str | None) -> int:
    try:
        return max(1, int(raw)) if raw else DEFAULT_MAX_STEPS
    except ValueError:
        return DEFAULT_MAX_STEPS


@dataclass(frozen=True)
class Settings:
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = ""
    max_agent_steps: int = DEFAULT_MAX_STEPS
    database_url: str = "sqlite:///./taskwrite.db"
    workspace_dir: str = "./workspaces"
    mcp_config: str = "./mcp_servers.json"
    max_upload_bytes: int = 10 * 1024 * 1024
    request_timeout_s: float = 180.0
    sandbox_image: str = "python:3.12-slim"
    sandbox_timeout_s: float = 10.0
    sandbox_docker: bool = True
    gemini_api_key: str = field(default="", repr=False)
    groq_api_key: str = field(default="", repr=False)
    gemini_model: str = ""
    groq_model: str = ""
    openai_model: str = ""
    anthropic_model: str = ""
    openai_api_key: str = field(default="", repr=False)
    anthropic_api_key: str = field(default="", repr=False)
    # Host names this server answers to. Anything else is refused, which stops DNS-rebinding attacks.
    allowed_hosts: tuple[str, ...] = LOCAL_HOSTS

    @property
    def db_path(self) -> str:
        url = self.database_url
        return url[len("sqlite:///"):] if url.startswith("sqlite:///") else url

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (self.gemini_api_key, self.groq_api_key, self.openai_api_key, self.anthropic_api_key) if s)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            llm_provider=(env("LLM_PROVIDER") or "ollama").strip().lower(),
            ollama_base_url=(env("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/"),
            ollama_model=(env("OLLAMA_MODEL") or "").strip(),
            max_agent_steps=_parse_steps(env("MAX_AGENT_STEPS")),
            database_url=env("DATABASE_URL") or "sqlite:///./taskwrite.db",
            workspace_dir=env("WORKSPACE_DIR") or "./workspaces",
            mcp_config=env("MCP_CONFIG") or "./mcp_servers.json",
            request_timeout_s=float(env("REQUEST_TIMEOUT_S") or 180),
            sandbox_image=(env("SANDBOX_IMAGE") or "python:3.12-slim").strip(),
            sandbox_timeout_s=float(env("SANDBOX_TIMEOUT_S") or 10),
            sandbox_docker=(env("SANDBOX_DOCKER") or "auto").strip().lower() != "off",
            gemini_api_key=env("GEMINI_API_KEY") or "",
            groq_api_key=env("GROQ_API_KEY") or "",
            gemini_model=(env("GEMINI_MODEL") or "").strip(),
            groq_model=(env("GROQ_MODEL") or "").strip(),
            openai_model=(env("OPENAI_MODEL") or "").strip(),
            anthropic_model=(env("ANTHROPIC_MODEL") or "").strip(),
            openai_api_key=env("OPENAI_API_KEY") or "",
            anthropic_api_key=env("ANTHROPIC_API_KEY") or "",
            allowed_hosts=LOCAL_HOSTS + tuple(h.strip().lower() for h in (env("ALLOWED_HOSTS") or "").split(",") if h.strip()),
        )
