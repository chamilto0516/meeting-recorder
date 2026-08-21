"""Safe, shared selection of PipeWire/Pulse microphone and output sources."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, replace
from typing import Literal

from meeting_recorder import audio
from meeting_recorder.config import LLMConfig
from meeting_recorder.errors import (
    AmbiguousDeviceSelectionError,
    DeviceNotFoundError,
    DeviceSelectionError,
)

DeviceKind = Literal["mic", "output"]
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class DeviceCandidate:
    """One live, selectable Pulse source with a compact public selector."""

    kind: DeviceKind
    token: str
    source: str
    index: str
    label: str
    is_default: bool
    aliases: tuple[str, ...] = ()


def _label(source: str, kind: DeviceKind) -> str:
    value = source
    for prefix in ("alsa_input.", "alsa_output."):
        if value.startswith(prefix):
            value = value[len(prefix):]
    if kind == "output" and value.endswith(".monitor"):
        value = value[: -len(".monitor")]
    return value.replace("_", " ").replace(".", " ").replace("-", " ").strip()


def _normal(value: str) -> str:
    return _NORMALIZE_RE.sub("", value.casefold())


def discover(aliases: dict[str, str] | None = None) -> list[DeviceCandidate]:
    """Take one live source snapshot and assign deterministic short tokens."""
    aliases = aliases or {}
    sources = audio.list_sources()
    default_mic = audio.get_default_source()
    default_output = audio.get_default_sink_monitor()

    def build(kind: DeviceKind) -> list[DeviceCandidate]:
        default = default_mic if kind == "mic" else default_output
        filtered = [source for source in sources if (not source.is_monitor) == (kind == "mic")]
        filtered.sort(key=lambda source: (source.name != default, source.name.casefold()))
        prefix = "m" if kind == "mic" else "o"
        candidates = [
            DeviceCandidate(
                kind=kind,
                token=f"{prefix}{number}",
                source=source.name,
                index=source.index,
                label=_label(source.name, kind),
                is_default=source.name == default,
            )
            for number, source in enumerate(filtered, start=1)
        ]
        return candidates

    candidates = [*build("mic"), *build("output")]
    aliases_by_source: dict[str, list[str]] = {}
    for alias, source in aliases.items():
        aliases_by_source.setdefault(source, []).append(alias)
    return [
        replace(candidate, aliases=tuple(sorted(aliases_by_source.get(candidate.source, ()))))
        for candidate in candidates
    ]


def _available_text(candidates: list[DeviceCandidate], kind: DeviceKind) -> str:
    eligible = [candidate for candidate in candidates if candidate.kind == kind]
    if not eligible:
        return "  (none)"
    return "\n".join(
        f"  {candidate.token}: {candidate.label} [{candidate.source}]"
        + (f" (aliases: {', '.join(candidate.aliases)})" if candidate.aliases else "")
        for candidate in eligible
    )


def _ambiguous(selector: str, matches: list[DeviceCandidate], kind: DeviceKind) -> AmbiguousDeviceSelectionError:
    return AmbiguousDeviceSelectionError(
        f"{kind} selector {selector!r} is ambiguous. Choose one of:\n"
        + "\n".join(
            f"  {candidate.token}: {candidate.label} [{candidate.source}]"
            + (f" (aliases: {', '.join(candidate.aliases)})" if candidate.aliases else "")
            for candidate in matches
        )
    )


def _local_matches(selector: str, candidates: list[DeviceCandidate]) -> list[DeviceCandidate]:
    query = _normal(selector)
    if not query:
        return []
    exact = []
    for candidate in candidates:
        values = (candidate.label, candidate.source, *candidate.aliases)
        if any(query == _normal(value) for value in values):
            exact.append(candidate)
    if exact:
        return exact
    contains = []
    for candidate in candidates:
        values = (candidate.label, candidate.source, *candidate.aliases)
        if any(query in _normal(value) for value in values):
            contains.append(candidate)
    if contains:
        return contains
    if len(query) < 3:
        return []
    scored: list[tuple[float, DeviceCandidate]] = []
    for candidate in candidates:
        values = (candidate.label, candidate.source, *candidate.aliases)
        score = max(difflib.SequenceMatcher(a=query, b=_normal(value)).ratio() for value in values)
        if score >= 0.72:
            scored.append((score, candidate))
    scored.sort(key=lambda item: (-item[0], item[1].token))
    if not scored:
        return []
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.12:
        return [candidate for _, candidate in scored]
    return [scored[0][1]]


def resolve_local(
    selector: str, kind: DeviceKind, candidates: list[DeviceCandidate], aliases: dict[str, str]
) -> DeviceCandidate:
    """Resolve a selector without a network/model call, or raise a useful error."""
    value = selector.strip()
    eligible = [candidate for candidate in candidates if candidate.kind == kind]
    all_by_source = {candidate.source: candidate for candidate in candidates}
    source_of_wrong_kind = all_by_source.get(value)
    if source_of_wrong_kind is not None and source_of_wrong_kind.kind != kind:
        raise DeviceSelectionError(
            f"Source {value!r} is an {source_of_wrong_kind.kind} source and cannot be used for {kind} selection."
        )
    exact_source = [candidate for candidate in eligible if candidate.source == value]
    if exact_source:
        return exact_source[0]
    token = [candidate for candidate in eligible if candidate.token.casefold() == value.casefold()]
    if token:
        return token[0]
    alias_source = aliases.get(value.casefold())
    if alias_source is not None:
        target = all_by_source.get(alias_source)
        if target is None:
            raise DeviceSelectionError(
                f"Alias {value!r} points to unavailable source {alias_source!r}. Reconnect it or run 'meeting-recorder list-devices'."
            )
        if target.kind != kind:
            raise DeviceSelectionError(
                f"Alias {value!r} names an {target.kind} source and cannot be used for {kind} selection."
            )
        return target
    matches = _local_matches(value, eligible)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise _ambiguous(value, matches, kind)
    raise DeviceNotFoundError(
        f"No live {kind} source matched {value!r}. Available {kind} selectors:\n{_available_text(candidates, kind)}"
    )


def _response_text(response: object) -> str:
    choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
    if choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else getattr(choices[0], "message", None)
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
        raise ValueError("response contained no assistant text")
    output = response.get("output") if isinstance(response, dict) else getattr(response, "output", None)
    text = ""
    for item in output or []:
        item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
        if item_type != "message" or role != "assistant" or not isinstance(content, (list, tuple)):
            continue
        for part in content:
            part_type = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
            part_text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if part_type == "output_text" and isinstance(part_text, str):
                text += part_text
    if not text.strip():
        raise ValueError("response contained no assistant text")
    return text.strip()


def resolve_with_llm(
    hint: str, kind: DeviceKind, candidates: list[DeviceCandidate], llm: LLMConfig
) -> DeviceCandidate:
    """Ask the configured model to choose a *validated* candidate token."""
    eligible = [candidate for candidate in candidates if candidate.kind == kind]
    if not eligible:
        raise DeviceSelectionError(f"No live {kind} sources are available.")
    payload = [
        {"selector": candidate.token, "label": candidate.label, "source": candidate.source,
         "aliases": list(candidate.aliases)}
        for candidate in eligible
    ]
    try:
        import litellm

        response = litellm.completion(
            model=llm.model,
            api_base=llm.endpoint,
            api_key=llm.api_key,
            messages=[
                {"role": "system", "content": (
                    "Choose the one device that best matches the user's hint. "
                    "Return only JSON in the form {\"selector\": \"m1\"}. "
                    "The selector must be copied exactly from the supplied candidates."
                )},
                {"role": "user", "content": json.dumps({"hint": hint, "candidates": payload})},
            ],
        )
        parsed = json.loads(_response_text(response))
        selector = parsed.get("selector") if isinstance(parsed, dict) else None
    except Exception as exc:  # noqa: BLE001 - provider errors are shown as selection errors
        raise DeviceSelectionError(
            f"LLM device selection failed for {hint!r}: {exc}. Choose a local selector instead."
        ) from exc
    if not isinstance(selector, str):
        raise DeviceSelectionError("LLM device selection returned no selector. Choose a local selector instead.")
    selected = next((candidate for candidate in eligible if candidate.token == selector), None)
    if selected is None:
        raise DeviceSelectionError(
            f"LLM device selection returned invalid selector {selector!r}. Choose a local selector instead."
        )
    return selected


def resolve(
    selector: str,
    kind: DeviceKind,
    candidates: list[DeviceCandidate],
    aliases: dict[str, str],
    *,
    allow_llm: bool,
    llm: LLMConfig,
) -> DeviceCandidate:
    """Resolve locally first; only a true no-match may use the opt-in LLM path."""
    try:
        return resolve_local(selector, kind, candidates, aliases)
    except DeviceNotFoundError:
        if not allow_llm:
            raise
    return resolve_with_llm(selector, kind, candidates, llm)


def render_device_config(candidates: list[DeviceCandidate]) -> str:
    """Return an editable initial alias file for a live device snapshot."""
    aliases: dict[str, str] = {}
    for candidate in candidates:
        name = f"mic-{candidate.token[1:]}" if candidate.kind == "mic" else f"output-{candidate.token[1:]}"
        aliases[name] = candidate.source
    lines = [
        "# Device aliases for meeting-recorder.",
        "# Change the names on the left to names you will remember; keep the exact source on the right.",
        "# m1/m2 and o1/o2 are live selectors and do not need to be listed here.",
        "version: 1",
        "aliases:",
    ]
    if aliases:
        lines.extend(f"  {alias}: {source}" for alias, source in aliases.items())
    else:
        lines.append("  {}")
    return "\n".join(lines) + "\n"
