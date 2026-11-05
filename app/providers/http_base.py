"""Shared HTTP plumbing for cloud adapters.

Normalised errors, one bounded retry on HTTP 429, and key redaction. Adapters never put the
key in a URL and never include it in a message; every provider message passes through
redact() with the live key as a belt-and-braces measure.
"""

from __future__ import annotations

import time
from typing import Callable

import httpx

from ..safety import redact
from .base import HealthStatus, ModelProvider, ProviderError


class HttpProvider(ModelProvider):
    is_cloud = True
    label = "provider"
    base_url = ""
    max_retry_wait_s = 5.0

    def __init__(self, api_key: str, model: str, timeout_s: float = 60.0,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self._api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self._transport = transport
        self._sleep = sleep or time.sleep

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _clean(self, text: str) -> str:
        return redact(text, (self._api_key,))

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self._api_key:
            raise ProviderError("auth", f"No {self.label} API key is set. Add one in Settings or in .env.")
        url = f"{self.base_url}{path}"
        for attempt in (1, 2):
            try:
                with httpx.Client(transport=self._transport, timeout=self.timeout_s) as client:
                    resp = client.request(method, url, headers=self._headers(), json=payload)
            except httpx.TimeoutException as exc:
                raise ProviderError("timeout", f"{self.label} did not respond within {self.timeout_s:g}s.") from exc
            except httpx.HTTPError as exc:
                raise ProviderError("unreachable", f"Could not reach {self.label}.") from exc
            if resp.status_code == 429 and attempt == 1:
                self._sleep(self._retry_wait(resp))
                continue
            return self._parse(resp)
        raise ProviderError("rate_limited", f"{self.label} rate limit reached.")  # pragma: no cover

    def _retry_wait(self, resp: httpx.Response) -> float:
        try:
            return max(0.0, min(float(resp.headers.get("retry-after", "1")), self.max_retry_wait_s))
        except ValueError:
            return 1.0

    def _parse(self, resp: httpx.Response) -> dict:
        code = resp.status_code
        if code == 200:
            try:
                data = resp.json()
            except ValueError as exc:
                raise ProviderError("bad_response", f"{self.label} returned a response that is not JSON.") from exc
            if not isinstance(data, dict):
                raise ProviderError("bad_response", f"{self.label} returned an unexpected response.")
            return data
        if code in (401, 403):
            raise ProviderError("auth", f"{self.label} rejected the API key (HTTP {code}). Check the key.")
        if code == 404:
            detail = self._error_detail(resp)
            raise ProviderError("model_missing", f"{self.label}: model not found or not available to this key."
                                + (f" ({detail})" if detail else ""))
        if code == 402:
            detail = self._error_detail(resp)
            raise ProviderError("billing", f"{self.label}: payment or credits are needed for this account"
                                + (f" ({detail})" if detail else "."))
        if code == 429:
            raise ProviderError("rate_limited", f"{self.label} rate limit or quota reached. "
                                "Wait and retry, or switch provider.")
        detail = self._error_detail(resp)
        raise ProviderError("bad_response", f"{self.label} returned HTTP {code}" + (f": {detail}" if detail else "."))

    def _error_detail(self, resp: httpx.Response) -> str:
        try:
            body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None
            msg = err.get("message", "") if isinstance(err, dict) else ""
        except ValueError:
            msg = ""
        return self._clean(str(msg))[:200]

    def _health_from(self, models: list[str]) -> HealthStatus:
        if not self.model:
            return HealthStatus(ok=False, models=models, message=f"{self.label} key works; choose a model in settings.")
        if models and self.model not in models:
            return HealthStatus(ok=False, models=models, message=f"Model '{self.model}' is not available to this key.")
        return HealthStatus(ok=True, models=models, message=f"{self.label} ready with '{self.model}'.")
