"""Summarization of the meeting transcript via LiteLLM.

LiteLLM is used purely as an abstraction layer: today `config.llm` points at
a local Ollama server (`ollama/<model>` + `http://localhost:11434`, which
LiteLLM turns into the appropriate `/api/generate` or `/api/chat` call), but
swapping to a hosted LiteLLM proxy, OpenAI-compatible endpoint, or any other
LiteLLM-supported provider later is just a matter of changing `model`,
`endpoint`, and `api_key` -- no code changes required here.
"""

from __future__ import annotations

from pathlib import Path

from meeting_recorder.config import LLMConfig
from meeting_recorder.errors import SummarizationError
from meeting_recorder.modes import ModeDefinition


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split `text` into whitespace-respecting chunks, each at most
    `max_chars` characters (best-effort, on word boundaries)."""
    words = text.split()
    if not words:
        return [""]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        added_len = len(word) + (1 if current else 0)
        if current and current_len + added_len > max_chars:
            chunks.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += added_len
    if current:
        chunks.append(" ".join(current))
    return chunks


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
        raise SummarizationError(
            f"LiteLLM summarization call failed (model={config.model!r}, "
            f"endpoint={config.endpoint!r}): {exc}"
        ) from exc


def _group_texts(texts: list[str], max_chars: int) -> list[str]:
    groups: list[str] = []
    current: list[str] = []
    current_length = 0
    for text in texts:
        added = len(text) + (2 if current else 0)
        if current and current_length + added > max_chars:
            groups.append("\n\n".join(current))
            current = [text]
            current_length = len(text)
        else:
            current.append(text)
            current_length += added
    if current:
        groups.append("\n\n".join(current))
    return groups


def _hierarchical_summary(
    transcript: str,
    config: LLMConfig,
    partial_prompt: str,
    reduction_prompt: str,
) -> str:
    """Map-reduce while keeping every individual LLM input bounded in size."""
    transcript = transcript.strip()
    if not transcript:
        raise SummarizationError("Transcript is empty; nothing to summarize.")
    chunks = chunk_text(transcript, config.chunk_char_limit)
    if len(chunks) == 1:
        return chunks[0]
    summaries = [
        _call_llm(
            partial_prompt,
            f"Transcript part {i + 1} of {len(chunks)}:\n\n{chunk}",
            config,
        )
        for i, chunk in enumerate(chunks)
    ]
    while len(summaries) > 1:
        groups = _group_texts(summaries, config.chunk_char_limit)
        if len(groups) == len(summaries):
            # A single model response can exceed our preferred bound. It is
            # still safer to reduce it than to send every prior result at once.
            groups = ["\n\n".join(summaries)]
        summaries = [
            _call_llm(reduction_prompt, group, config)
            for group in groups
        ]
    return summaries[0]


def summarize_mode(
    transcript: str, config: LLMConfig, mode: ModeDefinition
) -> list[tuple[str, str]]:
    """Generate every LLM-produced artifact declared by a mode."""
    partial_prompt = (
        f"{mode.prompt}\n\n"
        f"For this partial {mode.name} transcript, extract dense factual notes rather "
        "than producing the final artifact. "
        "Preserve important names, facts, decisions, steps, questions, and uncertainty. "
        "Do not invent facts; these notes will be combined with other portions."
    )
    reduction_prompt = (
        f"{mode.prompt}\n\n"
        f"Combine these partial {mode.name} notes accurately and compactly. Preserve uncertainty "
        "and important details while removing duplication. Do not invent facts."
    )
    condensed = _hierarchical_summary(transcript, config, partial_prompt, reduction_prompt)
    results: list[tuple[str, str]] = []
    for artifact in mode.artifacts:
        if artifact.combine:
            continue
        prompt = (
            f"{mode.prompt}\n\n{artifact.instruction}\n"
            f"Start exactly with '# {artifact.title}'."
        )
        results.append((artifact.filename, _call_llm(prompt, condensed, config)))
    return results


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
