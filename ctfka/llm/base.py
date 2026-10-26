"""Provider contract and errors shared by local language model adapters."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable


class LLMError(RuntimeError):
    """Base class for provider failures that can be shown to an operator."""


class LLMUnavailable(LLMError):
    """The configured local inference service cannot be reached."""


class LLMTimeout(LLMError):
    """A local inference request exceeded its configured timeout."""


class ModelNotFound(LLMError):
    """The requested model is not installed in the local provider."""


def context_input_budget(config: Any, options: dict[str, Any] | None = None) -> int | None:
    """Conservatively reserve context for output using effective Ollama options."""
    merged = dict(getattr(config, "options", {}) or {})
    merged.update(options or {})
    num_ctx = merged.get("num_ctx", getattr(config, "max_context_tokens", None))
    if num_ctx is None:
        return None
    try:
        num_ctx = int(num_ctx)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("effective num_ctx must be a positive integer") from exc
    if num_ctx <= 0:
        raise ValueError("effective num_ctx must be a positive integer")
    reserve = math.ceil(num_ctx * 0.25)
    try:
        num_predict = int(merged.get("num_predict", 0))
    except (TypeError, ValueError, OverflowError):
        num_predict = 0
    if num_predict > 0:
        reserve = max(reserve, num_predict)
    return max(0, num_ctx - reserve)


@runtime_checkable
class LLMProvider(Protocol):
    async def list_models(self) -> list[dict[str, Any]]: ...
    async def status(self) -> dict[str, Any]: ...
    async def embedding_identity(self) -> dict[str, Any]: ...
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        format: dict[str, Any] | str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...
