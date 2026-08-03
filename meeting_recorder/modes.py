"""Data-driven recording modes and their packaged prompt assets."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
import re
from typing import Any

import yaml

from meeting_recorder.errors import MeetingRecorderError

_MODE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_CAPTURE_POLICIES = {"mic-and-system", "system-only", "mic-only"}


@dataclass(frozen=True)
class ModeArtifact:
    filename: str
    title: str
    instruction: str | None = None
    combine: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModeDefinition:
    name: str
    capture: str
    min_mics: int
    max_mics: int | None
    prompt_file: str
    prompt: str
    artifacts: tuple[ModeArtifact, ...]


def _invalid(message: str) -> MeetingRecorderError:
    return MeetingRecorderError(f"Invalid mode manifest: {message}")


def _read_prompt(prompt_file: str) -> str:
    resource = files("meeting_recorder").joinpath("modes", prompt_file)
    if not resource.is_file():
        raise _invalid(f"prompt file 'modes/{prompt_file}' does not exist")
    prompt = resource.read_text(encoding="utf-8").strip()
    if prompt:
        return prompt
    raise _invalid(f"prompt file 'modes/{prompt_file}' is empty")


@lru_cache(maxsize=1)
def load_modes() -> dict[str, ModeDefinition]:
    """Load and validate the packaged mode registry."""
    manifest_resource = files("meeting_recorder").joinpath("modes", "modes.yaml")
    try:
        raw = yaml.safe_load(manifest_resource.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise MeetingRecorderError("Could not read packaged mode manifest.") from exc
    except yaml.YAMLError as exc:
        raise _invalid(f"modes.yaml is not valid YAML: {exc}") from exc
    entries = raw.get("modes") if isinstance(raw, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise _invalid("'modes' must be a non-empty mapping")

    modes: dict[str, ModeDefinition] = {}
    for name, value in entries.items():
        if not isinstance(name, str) or not _MODE_NAME.fullmatch(name):
            raise _invalid(f"mode name {name!r} must use lowercase letters, digits, and hyphens")
        if not isinstance(value, dict):
            raise _invalid(f"mode '{name}' must be a mapping")
        capture = value.get("capture")
        if capture not in _CAPTURE_POLICIES:
            raise _invalid(f"mode '{name}' has unsupported capture policy {capture!r}")
        mic_rule = value.get("mics", {})
        if not isinstance(mic_rule, dict):
            raise _invalid(f"mode '{name}' mics must be a mapping")
        min_mics, max_mics = mic_rule.get("min"), mic_rule.get("max")
        if type(min_mics) is not int or min_mics < 0:
            raise _invalid(f"mode '{name}' mics.min must be a non-negative integer")
        if max_mics is not None and (type(max_mics) is not int or max_mics < min_mics):
            raise _invalid(f"mode '{name}' mics.max must be null or an integer no smaller than min")
        if capture == "system-only" and (min_mics != 0 or max_mics != 0):
            raise _invalid(f"system-only mode '{name}' must forbid microphones")
        if capture == "mic-only" and min_mics < 1:
            raise _invalid(f"mic-only mode '{name}' must require a microphone")
        prompt_file = value.get("prompt")
        if not isinstance(prompt_file, str) or not prompt_file.endswith(".md") or "/" in prompt_file:
            raise _invalid(f"mode '{name}' prompt must name a .md file in modes/")
        raw_artifacts = value.get("artifacts")
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            raise _invalid(f"mode '{name}' must declare at least one artifact")
        artifacts: list[ModeArtifact] = []
        filenames: set[str] = set()
        for artifact in raw_artifacts:
            if not isinstance(artifact, dict):
                raise _invalid(f"mode '{name}' artifact must be a mapping")
            filename, title, instruction = (artifact.get(key) for key in ("filename", "title", "instruction"))
            if (not isinstance(filename, str) or not filename.endswith(".md") or "/" in filename
                    or filename in filenames):
                raise _invalid(f"mode '{name}' artifact filenames must be unique Markdown basenames")
            combine = artifact.get("combine", [])
            if not isinstance(combine, list) or not all(isinstance(item, str) for item in combine):
                raise _invalid(f"mode '{name}' artifact combine must be a list of artifact filenames")
            if not isinstance(title, str) or not title:
                raise _invalid(f"mode '{name}' artifacts require a non-empty title")
            if combine:
                if instruction is not None:
                    raise _invalid(f"mode '{name}' combined artifact cannot also have an instruction")
            elif not isinstance(instruction, str) or not instruction:
                raise _invalid(f"mode '{name}' generated artifact requires an instruction")
            filenames.add(filename)
            artifacts.append(ModeArtifact(filename, title, instruction, tuple(combine)))
        generated_filenames = {
            artifact.filename for artifact in artifacts if not artifact.combine
        }
        for artifact in artifacts:
            if len(artifact.combine) != len(set(artifact.combine)):
                raise _invalid(f"mode '{name}' combined artifact repeats an output")
            if any(item not in generated_filenames for item in artifact.combine):
                raise _invalid(
                    f"mode '{name}' combined artifact must reference generated outputs"
                )
        modes[name] = ModeDefinition(name, capture, min_mics, max_mics, prompt_file,
                                     _read_prompt(prompt_file), tuple(artifacts))
    return modes


def get_mode(name: str) -> ModeDefinition:
    try:
        return load_modes()[name]
    except KeyError as exc:
        available = ", ".join(sorted(load_modes()))
        raise MeetingRecorderError(f"Unknown mode '{name}'. Available modes: {available}.") from exc
