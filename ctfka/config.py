from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
        return cls.model_validate(values)
