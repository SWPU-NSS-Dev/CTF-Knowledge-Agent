"""Async client for a local Ollama server. No model downloads or cloud fallback."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import AsyncIterator
from typing import Any, Self

import httpx

from ctfka.llm.base import LLMError, LLMTimeout, LLMUnavailable, ModelNotFound


class OllamaProvider:
    def __init__(self, config: Any, *, client: httpx.AsyncClient | None = None):
        if str(getattr(config, "provider", "ollama")).lower() != "ollama":
            raise LLMError(f"unsupported LLM provider: {getattr(config, 'provider', None)}")
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=str(config.base_url).rstrip("/"),
            timeout=httpx.Timeout(float(config.timeout_seconds)),
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @staticmethod
    def _detail(payload: Any) -> str:
        if isinstance(payload, dict):
            return str(payload.get("error") or payload.get("message") or payload)
        return str(payload)

    @classmethod
    def _json(cls, response: httpx.Response, endpoint: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMError(f"Ollama {endpoint} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise LLMError(f"Ollama {endpoint} returned a non-object JSON response")
        if payload.get("error"):
            detail = str(payload["error"])
            if "model" in detail.lower() and "not found" in detail.lower():
                raise ModelNotFound(detail)
            raise LLMError(f"Ollama {endpoint} failed: {detail}")
        return payload

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise LLMTimeout(f"Ollama request timed out: {exc}") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise LLMUnavailable(f"Ollama is unavailable at {self.config.base_url}: {exc}") from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            detail = self._detail(payload)
            if response.status_code == 404 or "model" in detail.lower() and "not found" in detail.lower():
                raise ModelNotFound(detail)
            raise LLMError(f"Ollama HTTP {response.status_code}: {detail}")
        return response

    async def _models_and_version(self) -> tuple[list[dict[str, Any]], str | None]:
        tags_response = await self._request("GET", "/api/tags")
        tags_payload = self._json(tags_response, "/api/tags")
        raw_models = tags_payload.get("models", [])
        if not isinstance(raw_models, list):
            raise LLMError("Ollama /api/tags returned an invalid models list")
        models = [
            {
                "name": str(item.get("name") or item.get("model") or ""),
                "digest": item.get("digest"),
                "details": item.get("details") or {},
                "size": item.get("size"),
                "modified_at": item.get("modified_at"),
            }
            for item in raw_models
            if isinstance(item, dict)
        ]
        version_payload = self._json(await self._request("GET", "/api/version"), "/api/version")
        version = version_payload.get("version")
        return models, version

    async def list_models(self) -> list[dict[str, Any]]:
        models, _ = await self._models_and_version()
        return models

    async def status(self) -> dict[str, Any]:
        try:
            models, version = await self._models_and_version()
            names = [m["name"] for m in models]
            configured = {str(self.config.chat_model), str(self.config.embedding_model)}
            return {
                "reachable": True,
                "version": version,
                "models": names,
                "chat_model_available": str(self.config.chat_model) in names,
                "embedding_model_available": str(self.config.embedding_model) in names,
                "available": configured.issubset(set(names)),
                "error": None,
            }
        except LLMError as exc:
            return {
                "reachable": False,
                "version": None,
                "models": [],
                "chat_model_available": False,
                "embedding_model_available": False,
                "available": False,
                "error": str(exc),
            }

    async def _identity_for(self, model: str) -> tuple[str, str]:
        models = await self.list_models()
        requested = model if ":" in model else f"{model}:latest"
        item = next((entry for entry in models if entry["name"] == requested), None)
        if item is None:
            raise ModelNotFound(f"Ollama model '{model}' is not installed")
        digest = item.get("digest")
        if not digest:
            raise LLMError(f"Ollama did not report an immutable digest for model '{item['name']}'")
        return str(item["name"]), str(digest)

    async def embedding_identity(self) -> dict[str, Any]:
        model, digest = await self._identity_for(str(self.config.embedding_model))
        dimensions = getattr(self.config, "embedding_dimensions", None)
        identity = {
            "model": model,
            "digest": digest,
            "embedding_options": dict(getattr(self.config, "embedding_options", {}) or {}),
            "dimensions": dimensions,
        }
        model_key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return {"model": model, "digest": digest, "model_key": model_key}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = str(self.config.embedding_model)
        payload: dict[str, Any] = {
            "model": model,
            "input": texts,
            "truncate": False,
            "options": dict(getattr(self.config, "embedding_options", {}) or {}),
            "keep_alive": getattr(self.config, "keep_alive", "5m"),
        }
        dimensions = getattr(self.config, "embedding_dimensions", None)
        if dimensions is not None:
            payload["dimensions"] = int(dimensions)
        response = await self._request("POST", "/api/embed", json=payload)
        data = self._json(response, "/api/embed")
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise LLMError("Ollama embedding response cardinality does not match input")
        normalized: list[list[float]] = []
        dimension = int(dimensions) if dimensions is not None else None
        for vector in embeddings:
            if not isinstance(vector, list) or not vector:
                raise LLMError("Ollama returned an empty or malformed embedding")
            try:
                values = [float(value) for value in vector]
            except (TypeError, ValueError, OverflowError) as exc:
                raise LLMError("Ollama returned a non-numeric embedding value") from exc
            if dimension is None:
                dimension = len(values)
            if len(values) != dimension or any(not math.isfinite(value) for value in values):
                raise LLMError("Ollama returned inconsistent or non-finite embeddings")
            if not any(values):
                raise LLMError("Ollama returned a zero embedding")
            normalized.append(values)
        return normalized

    def _chat_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None,
        options: dict[str, Any] | None,
        stream: bool,
        format: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        merged_options = dict(getattr(self.config, "options", {}) or {})
        merged_options.update(options or {})
        max_context = getattr(self.config, "max_context_tokens", None)
        if max_context is not None:
            merged_options.setdefault("num_ctx", int(max_context))
        thinking = getattr(self.config, "structured_think", False) if format is not None else getattr(self.config, "think", False)
        payload: dict[str, Any] = {
            "model": model or str(self.config.chat_model),
            "messages": messages,
            "stream": stream,
            "think": bool(thinking),
            "options": merged_options,
            "keep_alive": getattr(self.config, "keep_alive", "5m"),
        }
        if format is not None:
            payload["format"] = format
        return payload

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        format: dict[str, Any] | str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._chat_payload(messages, model=model, options=options, stream=False, format=format)
        response = await self._request("POST", "/api/chat", json=payload)
        data = self._json(response, "/api/chat")
        if data.get("done") is not True:
            raise LLMError("Ollama non-stream chat response is missing terminal done=true")
        if not isinstance(data.get("message"), dict) or not isinstance(data["message"].get("content"), str):
            raise LLMError("Ollama chat response has no message object")
        return data

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        payload = self._chat_payload(messages, model=model, options=options, stream=True)
        terminal = False
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    try:
                        parsed = json.loads(body)
                    except (ValueError, TypeError):
                        parsed = body.decode("utf-8", errors="replace")
                    detail = self._detail(parsed)
                    if response.status_code == 404 or "model" in detail.lower() and "not found" in detail.lower():
                        raise ModelNotFound(detail)
                    raise LLMError(f"Ollama HTTP {response.status_code}: {detail}")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise LLMError(f"Malformed Ollama NDJSON frame: {exc}") from exc
                    if not isinstance(item, dict):
                        raise LLMError("Ollama stream frame must be a JSON object")
                    if item.get("error"):
                        raise LLMError(f"Ollama stream failed: {item['error']}")
                    message = item.get("message")
                    if (
                        not isinstance(message, dict)
                        or not isinstance(message.get("role"), str)
                        or not isinstance(message.get("content"), str)
                    ):
                        raise LLMError("Ollama stream frame has invalid message.content")
                    if not isinstance(item.get("done"), bool):
                        raise LLMError("Ollama stream frame is missing a boolean done field")
                    if item["done"] is True:
                        terminal = True
                        yield item
                        break
                    yield item
            if not terminal:
                raise LLMError("Ollama stream ended without a terminal done frame")
        except httpx.TimeoutException as exc:
            raise LLMTimeout(f"Ollama stream timed out: {exc}") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise LLMUnavailable(f"Ollama stream unavailable: {exc}") from exc
