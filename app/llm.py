"""Provider routing, health checks, and a logged, budget-aware model service."""

from __future__ import annotations

from typing import Callable, TypeVar

from pydantic import BaseModel

from .config import Settings
from .db import Database
from .providers.base import (GenerateRequest, GenerateResponse, HealthStatus, ModelProvider,
                             ProviderError, StructuredOutputError, parse_structured)
from .keys import KeyStore
from .providers.anthropic import AnthropicProvider
from .providers.gemini import GeminiProvider
from .providers.groq import GroqProvider
from .providers.ollama import OllamaProvider
from .providers.openai import OpenAIProvider

T = TypeVar("T", bound=BaseModel)

SUPPORTED_PROVIDERS = ("ollama", "gemini", "groq", "openai", "anthropic")
CLOUD_PROVIDERS = ("gemini", "groq", "openai", "anthropic")


class EffectiveConfig(BaseModel):
    llm_provider: str
    ollama_base_url: str
    ollama_model: str
    gemini_model: str = ""
    groq_model: str = ""
    openai_model: str = ""
    anthropic_model: str = ""


def effective_config(settings: Settings, db: Database) -> EffectiveConfig:
    """UI-saved (non-secret) settings override environment defaults."""
    return EffectiveConfig(
        llm_provider=db.get_setting("llm_provider") or settings.llm_provider,
        ollama_base_url=db.get_setting("ollama_base_url") or settings.ollama_base_url,
        ollama_model=(db.get_setting("ollama_model") if db.get_setting("ollama_model") is not None
                      else settings.ollama_model),
        gemini_model=db.get_setting("gemini_model") or settings.gemini_model,
        groq_model=db.get_setting("groq_model") or settings.groq_model,
        openai_model=db.get_setting("openai_model") or settings.openai_model,
        anthropic_model=db.get_setting("anthropic_model") or settings.anthropic_model,
    )


def selected_model(cfg: EffectiveConfig) -> str:
    return {"ollama": cfg.ollama_model, "gemini": cfg.gemini_model, "groq": cfg.groq_model,
            "openai": cfg.openai_model, "anthropic": cfg.anthropic_model}.get(cfg.llm_provider, "")


def build_provider(cfg: EffectiveConfig, settings: Settings, keys: KeyStore | None = None) -> ModelProvider:
    if cfg.llm_provider == "ollama":
        return OllamaProvider(cfg.ollama_base_url, cfg.ollama_model, settings.request_timeout_s)
    keys = keys or KeyStore(settings)
    timeout = min(settings.request_timeout_s, 120.0)
    if cfg.llm_provider == "openai":
        return OpenAIProvider(keys.get("openai"), cfg.openai_model, timeout)
    if cfg.llm_provider == "anthropic":
        return AnthropicProvider(keys.get("anthropic"), cfg.anthropic_model, timeout)
    if cfg.llm_provider == "gemini":
        return GeminiProvider(keys.get("gemini"), cfg.gemini_model, min(settings.request_timeout_s, 120.0))
    if cfg.llm_provider == "groq":
        return GroqProvider(keys.get("groq"), cfg.groq_model, min(settings.request_timeout_s, 120.0))
    raise ProviderError("unsupported", f"Provider '{cfg.llm_provider}' has no adapter yet.")


def check_provider(cfg: EffectiveConfig, settings: Settings,
                   keys: KeyStore | None = None) -> tuple[ModelProvider | None, HealthStatus]:
    try:
        provider = build_provider(cfg, settings, keys)
    except ProviderError as exc:
        return None, HealthStatus(ok=False, message=exc.message)
    return provider, provider.health_check()


class LLMService:
    """Every model call goes through here so it is charged to the budget and logged."""

    def __init__(self, provider: ModelProvider, log: Callable[[str, str, str], None],
                 charge: Callable[[str], None]) -> None:
        self.provider = provider
        self._log = log
        self._charge = charge

    def generate(self, request: GenerateRequest, label: str) -> GenerateResponse:
        self._charge(label)
        try:
            response = self.provider.generate(request)
        except ProviderError as exc:
            self._log("model_call", label, f"failed: {exc.kind}: {exc.message}")
            raise
        usage = response.usage
        self._log("model_call", label,
                  f"ok model={response.model} tokens_in={usage.prompt_tokens} tokens_out={usage.completion_tokens}")
        return response

    def generate_structured(self, request: GenerateRequest, schema: type[T], label: str,
                            max_attempts: int = 2) -> T:
        """Parse-and-validate with bounded retries; each attempt is charged.

        JSON mode is tried first. If the provider itself fails to produce JSON (for example an HTTP 400
        "failed to generate JSON"), the next attempt asks for plain text and relies on our own
        parse-and-validate fallback instead of failing the whole question.
        """
        request = request.model_copy(update={"json_mode": True})
        last: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.generate(request, f"{label} (attempt {attempt})")
            except ProviderError as exc:
                if request.json_mode and exc.kind == "bad_response" and "json" in exc.message.lower() and attempt < max_attempts:
                    request = request.model_copy(update={"json_mode": False})
                    self._log("validation", label, "provider could not produce JSON; retrying without JSON mode")
                    last = StructuredOutputError(exc.message)
                    continue
                raise
            try:
                return parse_structured(response.text, schema)
            except StructuredOutputError as exc:
                last = exc
                self._log("validation", label, f"structured output rejected: {exc}")
        raise StructuredOutputError(str(last))
