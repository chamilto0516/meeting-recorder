"""Layered configuration for meeting-recorder.

Precedence (lowest to highest):
    built-in defaults -> YAML config file -> environment variables -> CLI flags

Keeping this in one place means `start` and `stop` (which can run as two
completely separate invocations, potentially minutes/hours apart) always
agree on where things live and how transcription/summarization should behave,
while still letting a single invocation override anything on the fly.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

import yaml

APP_NAME = "meeting-recorder"

DEFAULTS: dict[str, Any] = {
    "data_dir": None,  # resolved lazily via XDG_DATA_HOME
    "audio": {
        "sample_rate": 48000,
    },
    "whisper": {
        "model_size": "base",
        "device": "cpu",
        "compute_type": "int8",
        "language": None,
    },
    "llm": {
        "model": "ollama/llama3.1",
        "endpoint": "http://localhost:11434",
        "api_key": None,
        "chunk_char_limit": 6000,
    },
}


@dataclass
class WhisperConfig:
    model_size: str = "base"
    device: str = "cpu"
    compute_type: str = "int8"
    language: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_size": self.model_size,
            "device": self.device,
            "compute_type": self.compute_type,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WhisperConfig":
        return cls(**{k: data.get(k, getattr(cls, k, None)) for k in _fields(cls)})


@dataclass
class LLMConfig:
    model: str = "ollama/llama3.1"
    endpoint: str = "http://localhost:11434"
    api_key: Optional[str] = None
    chunk_char_limit: int = 6000

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "endpoint": self.endpoint,
            "api_key": self.api_key,
            "chunk_char_limit": self.chunk_char_limit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMConfig":
        return cls(**{k: data.get(k, getattr(cls, k, None)) for k in _fields(cls)})


def _fields(cls):
    return cls.__dataclass_fields__.keys()


@dataclass
class AppConfig:
    data_dir: Path
    sample_rate: int
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME / "config.yaml"


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME / "sessions"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data or {}


def load_config(args: Any) -> AppConfig:
    """Build the effective config for this invocation.

    `args` is the argparse Namespace; only attributes that exist on it are
    consulted, so this works for subcommands that don't expose every flag
    (e.g. `list-devices` has no `--llm-model`).
    """
    config_path = getattr(args, "config", None) or default_config_path()
    merged = _deep_merge(DEFAULTS, _load_yaml(Path(config_path)))

    def env(name: str) -> Optional[str]:
        return os.environ.get(f"MEETING_RECORDER_{name}")

    if env("DATA_DIR"):
        merged["data_dir"] = env("DATA_DIR")
    if env("SAMPLE_RATE"):
        merged["audio"]["sample_rate"] = int(env("SAMPLE_RATE"))
    if env("WHISPER_MODEL"):
        merged["whisper"]["model_size"] = env("WHISPER_MODEL")
    if env("WHISPER_DEVICE"):
        merged["whisper"]["device"] = env("WHISPER_DEVICE")
    if env("WHISPER_COMPUTE_TYPE"):
        merged["whisper"]["compute_type"] = env("WHISPER_COMPUTE_TYPE")
    if env("WHISPER_LANGUAGE"):
        merged["whisper"]["language"] = env("WHISPER_LANGUAGE")
    if env("LLM_MODEL"):
        merged["llm"]["model"] = env("LLM_MODEL")
    if env("LLM_ENDPOINT"):
        merged["llm"]["endpoint"] = env("LLM_ENDPOINT")
    if env("LLM_API_KEY"):
        merged["llm"]["api_key"] = env("LLM_API_KEY")

    def cli(name: str) -> Optional[Any]:
        return getattr(args, name, None)

    if cli("data_dir"):
        merged["data_dir"] = str(cli("data_dir"))
    if cli("sample_rate"):
        merged["audio"]["sample_rate"] = int(cli("sample_rate"))
    if cli("whisper_model"):
        merged["whisper"]["model_size"] = cli("whisper_model")
    if cli("whisper_device"):
        merged["whisper"]["device"] = cli("whisper_device")
    if cli("whisper_compute_type"):
        merged["whisper"]["compute_type"] = cli("whisper_compute_type")
    if cli("language"):
        merged["whisper"]["language"] = cli("language")
    if cli("llm_model"):
        merged["llm"]["model"] = cli("llm_model")
    if cli("llm_endpoint"):
        merged["llm"]["endpoint"] = cli("llm_endpoint")
    if cli("llm_api_key"):
        merged["llm"]["api_key"] = cli("llm_api_key")

    data_dir = Path(merged["data_dir"]) if merged["data_dir"] else default_data_dir()

    return AppConfig(
        data_dir=data_dir,
        sample_rate=int(merged["audio"]["sample_rate"]),
        whisper=WhisperConfig.from_dict(merged["whisper"]),
        llm=LLMConfig.from_dict(merged["llm"]),
    )


def override_whisper(config: WhisperConfig, args: Any) -> WhisperConfig:
    """Apply stop-time CLI overrides on top of the whisper config captured at start."""
    updates = {}
    if getattr(args, "whisper_model", None):
        updates["model_size"] = args.whisper_model
    if getattr(args, "whisper_device", None):
        updates["device"] = args.whisper_device
    if getattr(args, "whisper_compute_type", None):
        updates["compute_type"] = args.whisper_compute_type
    if getattr(args, "language", None):
        updates["language"] = args.language
    return replace(config, **updates) if updates else config


def override_llm(config: LLMConfig, args: Any) -> LLMConfig:
    """Apply stop-time CLI overrides on top of the LLM config captured at start."""
    updates = {}
    if getattr(args, "llm_model", None):
        updates["model"] = args.llm_model
    if getattr(args, "llm_endpoint", None):
        updates["endpoint"] = args.llm_endpoint
    if getattr(args, "llm_api_key", None):
        updates["api_key"] = args.llm_api_key
    return replace(config, **updates) if updates else config
