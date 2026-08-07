"""Summarization of the meeting transcript via LiteLLM.

LiteLLM is used purely as an abstraction layer: today `config.llm` points at
a local Ollama server (`ollama/<model>` + `http://localhost:11434`, which
LiteLLM turns into the appropriate `/api/generate` or `/api/chat` call), but
swapping to a hosted LiteLLM proxy, OpenAI-compatible endpoint, or any other
LiteLLM-supported provider later is just a matter of changing `model`,
`endpoint`, and `api_key` -- no code changes required here.
"""

from __future__ import annotations

import re
from pathlib import Path

from meeting_recorder.config import LLMConfig
from meeting_recorder.errors import SummarizationError
from meeting_recorder.modes import ModeDefinition


_H1_HEADING = re.compile(r"(?m)^# ([^\n]+?)\s*$")


def _is_context_limit_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "context length",
            "context window",
            "maximum context",
            "prompt is too long",
            "too many tokens",
            "token limit",
        )
    )


def _call_llm(system_prompt: str, user_prompt: str, config: LLMConfig) -> str:
    try:
        import litellm
    except ImportError as exc:  # pragma: no cover - environment issue
        raise SummarizationError(
            "litellm is not installed. Install it with: pip install litellm"
        ) from exc

    try:
        response = litellm.completion(
            model=config.model,
            api_base=config.endpoint,
            api_key=config.api_key,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001 - surface as our own error type
        if _is_context_limit_error(exc):
            raise SummarizationError(
                f"The configured model could not accept the complete transcript "
                f"(model={config.model!r}, endpoint={config.endpoint!r}). Choose a "
                "model whose context window can hold the full transcript and mode "
                f"prompt. Provider error: {exc}"
            ) from exc
        raise SummarizationError(
            f"LiteLLM summarization call failed (model={config.model!r}, "
            f"endpoint={config.endpoint!r}): {exc}"
        ) from exc


def _artifact_prompt(mode: ModeDefinition) -> str:
    generated = [artifact for artifact in mode.artifacts if not artifact.combine]
    requirements = "\n".join(
        f"{index}. Start exactly with '# {artifact.title}'. {artifact.instruction}"
        for index, artifact in enumerate(generated, start=1)
    )
    return (
        f"{mode.prompt}\n\n"
        "The user message is the complete transcript. Read it from beginning to "
        "end and produce every requested artifact from that full context.\n\n"
        f"Requested artifacts, in required order:\n{requirements}\n\n"
        "Return all requested artifacts in one Markdown response. Use each exact "
        "level-one heading once, in the order listed. Do not add any other "
        "level-one headings, preamble, epilogue, or Markdown code fence."
    )


def _parse_artifacts(response: str, mode: ModeDefinition) -> list[tuple[str, str]]:
    generated = [artifact for artifact in mode.artifacts if not artifact.combine]
    expected_titles = [artifact.title for artifact in generated]
    response = response.strip()
    headings = list(_H1_HEADING.finditer(response))
    actual_titles = [match.group(1).strip() for match in headings]

    if actual_titles != expected_titles or (headings and headings[0].start() != 0):
        expected = ", ".join(f"# {title}" for title in expected_titles)
        actual = ", ".join(f"# {title}" for title in actual_titles) or "none"
        raise SummarizationError(
            f"Mode '{mode.name}' returned invalid artifact headings. Expected "
            f"exactly, in order: {expected}. Received: {actual}."
        )

    results: list[tuple[str, str]] = []
    for index, (artifact, heading) in enumerate(zip(generated, headings)):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(response)
        content = response[heading.start():end].strip()
        results.append((artifact.filename, content))
    return results


def summarize_mode(
    transcript: str, config: LLMConfig, mode: ModeDefinition
) -> list[tuple[str, str]]:
    """Generate every declared artifact from one full-context LLM call."""
    if not transcript.strip():
        raise SummarizationError("Transcript is empty; nothing to summarize.")
    response = _call_llm(_artifact_prompt(mode), transcript, config)
    return _parse_artifacts(response, mode)


def save_mode_summaries(
    summaries: list[tuple[str, str]], session_dir: Path, mode: ModeDefinition
) -> list[Path]:
    """Write generated artifacts and manifest-declared combined archives."""
    generated_names = {
        artifact.filename for artifact in mode.artifacts if not artifact.combine
    }
    supplied_names = [filename for filename, _ in summaries]
    if len(supplied_names) != len(set(supplied_names)) or set(supplied_names) != generated_names:
        raise SummarizationError(
            f"Mode '{mode.name}' summary outputs did not match its artifact manifest."
        )
    contents = dict(summaries)
    paths: list[Path] = []
    for artifact in mode.artifacts:
        if artifact.combine:
            body = "\n\n---\n\n".join(contents[name].strip() for name in artifact.combine)
            combined = f"# {artifact.title}\n\n{body}\n"
            contents[artifact.filename] = combined
        paths.append(save_summary(contents[artifact.filename], session_dir, artifact.filename))
    return paths


def save_summary(summary: str, session_dir: Path, filename: str = "summary.md") -> Path:
    path = session_dir / filename
    path.write_text(summary, encoding="utf-8")
    return path
