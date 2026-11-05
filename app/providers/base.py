"""Shared provider contract. Vendor SDK details stay inside adapters."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class ProviderError(Exception):
    """Normalized provider failure.

    kind: unreachable|timeout|model_missing|bad_response|unsupported|auth|rate_limited|billing.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class StructuredOutputError(Exception):
    pass


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class GenerateRequest(BaseModel):
    prompt: str
    system: str = ""
    max_tokens: int = 1500
    temperature: float = 0.2
    json_mode: bool = False


class GenerateResponse(BaseModel):
    text: str
    model: str
    provider: str
    usage: Usage = Usage()


class HealthStatus(BaseModel):
    ok: bool
    message: str
    models: list[str] = []


class ModelProvider(ABC):
    name: str = ""
    is_cloud: bool = False
    model: str = ""

    @abstractmethod
    def generate(self, request: GenerateRequest) -> GenerateResponse: ...

    @abstractmethod
    def health_check(self) -> HealthStatus: ...


def parse_structured(text: str, schema: type[T]) -> T:
    """Bounded parse-and-validate: extract the first JSON object and validate it."""
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.S)
    if fenced:
        candidate = fenced.group(1).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise StructuredOutputError("No JSON object found in model output.")
    try:
        return schema.model_validate(json.loads(candidate[start : end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise StructuredOutputError(f"Model output failed validation: {type(exc).__name__}") from exc
