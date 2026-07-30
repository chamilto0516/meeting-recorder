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
from dataclasses import dataclass

from meeting_recorder.config import LLMConfig
from meeting_recorder.errors import SummarizationError
from meeting_recorder.game_prompt import (
    GAME_PARTIAL_PROMPT,
    GAME_REDUCTION_PROMPT,
    GAME_SYSTEM_PROMPT,
)

SUMMARY_SYSTEM_PROMPT = (
    "You are an assistant that writes clear, concise meeting summaries in Markdown from "
    "raw speech-to-text transcripts. Transcripts may contain minor "
    "transcription errors, filler words, or missing punctuation -- do your "
    "best to infer intended meaning. Return Markdown only, using these level-two "
    "headings: `## Summary`, `## Key discussion points`, `## Decisions`, and "
    "`## Action items`. Use Markdown bullet lists for the final three sections; "
    "write `None` beneath a section when there is nothing to report."
)

PARTIAL_SYSTEM_PROMPT = (
    "You are an assistant that condenses a portion of a longer meeting "
    "transcript into a dense, factual summary preserving names, decisions, "
    "and action items. This summary will later be combined with summaries of "
    "other portions of the same meeting."
)


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


def summarize_transcript(transcript: str, config: LLMConfig) -> str:
    """Produce the standard Markdown meeting summary."""
    condensed = _hierarchical_summary(
        transcript,
        config,
        PARTIAL_SYSTEM_PROMPT,
        "Combine these meeting notes accurately and compactly, preserving decisions, owners, and action items.",
    )
    return _call_llm(SUMMARY_SYSTEM_PROMPT, condensed, config)


@dataclass(frozen=True)
class GameSummaries:
    dm_brief: str
    player_recap: str


def summarize_game(transcript: str, config: LLMConfig) -> GameSummaries:
    """Generate distinct DM and player documents from a mixed game transcript."""
    condensed = _hierarchical_summary(
        transcript, config, GAME_PARTIAL_PROMPT, GAME_REDUCTION_PROMPT
    )
    dm_brief = _call_llm(
        GAME_SYSTEM_PROMPT + "\n\nProduce OUTPUT 1 only. Start exactly with '# DM Continuity Brief'.",
        condensed,
        config,
    )
    player_recap = _call_llm(
        GAME_SYSTEM_PROMPT + "\n\nProduce OUTPUT 2 only. Start exactly with '# Player Recap'.",
        condensed,
        config,
    )
    return GameSummaries(dm_brief=dm_brief, player_recap=player_recap)


def save_summary(summary: str, session_dir: Path, filename: str = "summary.md") -> Path:
    path = session_dir / filename
    path.write_text(summary, encoding="utf-8")
    return path


def save_game_summaries(summaries: GameSummaries, session_dir: Path) -> tuple[Path, Path, Path]:
    """Save separate game deliverables plus a combined archival report."""
    dm_path = save_summary(summaries.dm_brief, session_dir, "dm-continuity-brief.md")
    player_path = save_summary(summaries.player_recap, session_dir, "player-recap.md")
    combined = f"{summaries.dm_brief.strip()}\n\n---\n\n{summaries.player_recap.strip()}\n"
    combined_path = save_summary(combined, session_dir, "game-summary.md")
    return dm_path, player_path, combined_path
