from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3:4b"
    embedding_model: str = "qwen3-embedding:0.6b"
    timeout_seconds: float = Field(default=120, gt=0)
    max_context_tokens: int | None = Field(default=8192, gt=0)
    options: dict[str, Any] = Field(default_factory=dict)
    embedding_options: dict[str, Any] = Field(default_factory=dict)
    embedding_dimensions: int | None = Field(default=None, gt=0)
    keep_alive: str = "5m"
    think: bool = True
    structured_think: bool = False


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    keyword_top_k: int = Field(default=30, ge=1)
    vector_top_k: int = Field(default=30, ge=1)
    final_top_k: int = Field(default=8, ge=1)
    rrf_k: int = Field(default=60, ge=1)
    exact_match_boost: float = Field(default=0.05, ge=0)
    use_reranker: bool = False
    semantic_enabled: bool = True
    context_chars: int | None = Field(default=24000, gt=0)
    embedding_batch_size: int = Field(default=8, ge=1)

    @field_validator("use_reranker")
    @classmethod
    def reject_unimplemented_reranker(cls, value: bool) -> bool:
        if value:
            raise ValueError("reranker is not implemented")
        return value


class ChunkConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_chars: int = Field(default=1800, gt=0)


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    adapter: str
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)
    concurrency: int = Field(default=4, ge=1)
    requests_per_second: float = Field(default=0, ge=0)
    max_items: int | None = Field(default=None, ge=1)
    max_attempts: int = Field(default=5, ge=1)
    timeout_seconds: float = Field(default=30, gt=0)
    max_response_bytes: int | None = Field(default=33554432, ge=1)


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data_dir: Path = Path("./data")
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    workers: int = Field(default=8, ge=1)
    token: str | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    chunking: ChunkConfig = Field(default_factory=ChunkConfig)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        values: dict[str, Any] = {}
        config_path = Path(path) if path is not None else Path("config.yaml")
        if config_path.is_file():
            try:
                import yaml
            except ImportError as exc:
                raise RuntimeError("PyYAML is required to load YAML configuration") from exc
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError("configuration root must be a mapping")
            values.update(loaded)
        if data_dir := os.getenv("CTFKA_DATA_DIR"):
            values["data_dir"] = data_dir
        token = os.getenv("CTFKA_TOKEN")
        if token is not None:
            values["token"] = token
        llm = dict(values.get("llm") or {})
        for env_name, field_name in (("CTFKA_OLLAMA_BASE_URL", "base_url"), ("CTFKA_CHAT_MODEL", "chat_model"), ("CTFKA_EMBEDDING_MODEL", "embedding_model")):
            if (env_value := os.getenv(env_name)) is not None:
                llm[field_name] = env_value
        if llm:
            values["llm"] = llm
        return cls.model_validate(values)
