"""In-memory API key store. Keys are never written to SQLite, logs or API responses."""

from __future__ import annotations

from .config import Settings
from .safety import forget_secret, register_secret

KEYED_PROVIDERS = ("gemini", "groq", "openai", "anthropic")


class KeyStore:
    """Holds provider keys: environment/.env values first, then keys pasted in Settings."""

    def __init__(self, settings: Settings) -> None:
        self._keys: dict[str, str] = {}
        for provider, key in (("gemini", settings.gemini_api_key), ("groq", settings.groq_api_key),
                              ("openai", settings.openai_api_key), ("anthropic", settings.anthropic_api_key)):
            if key:
                self._set(provider, key.strip())

    def _set(self, provider: str, key: str) -> None:
        old = self._keys.get(provider)
        if old:
            forget_secret(old)
        if key:
            self._keys[provider] = key
            register_secret(key)
        else:
            self._keys.pop(provider, None)

    def set(self, provider: str, key: str) -> None:
        if provider not in KEYED_PROVIDERS:
            raise ValueError(f"'{provider}' does not use an API key.")
        key = key.strip()
        if key and (len(key) < 8 or any(c.isspace() for c in key)):
            raise ValueError("That does not look like an API key.")
        self._set(provider, key)

    def get(self, provider: str) -> str:
        return self._keys.get(provider, "")

    def has(self, provider: str) -> bool:
        return bool(self._keys.get(provider))
