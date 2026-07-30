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

SUMMARY_SYSTEM_PROMPT = (
    "You are an assistant that writes clear, concise meeting summaries from "
    "raw speech-to-text transcripts. Transcripts may contain minor "
    "transcription errors, filler words, or missing punctuation -- do your "
    "best to infer intended meaning. Structure your response with:\n"
    "1. A short overall summary (2-4 sentences)\n"
    "2. Key discussion points (bullet list)\n"
    "3. Decisions made (bullet list, or 'None' if none)\n"
    "4. Action items with owners if mentioned (bullet list, or 'None' if none)"
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


def summarize_transcript(transcript: str, config: LLMConfig) -> str:
    """Summarize `transcript` via LiteLLM, map-reducing across chunks if the
    transcript is longer than `config.chunk_char_limit`."""
    transcript = transcript.strip()
    if not transcript:
        raise SummarizationError("Transcript is empty; nothing to summarize.")

    chunks = chunk_text(transcript, config.chunk_char_limit)

    if len(chunks) == 1:
        return _call_llm(SUMMARY_SYSTEM_PROMPT, chunks[0], config)

    partial_summaries = [
        _call_llm(
            PARTIAL_SYSTEM_PROMPT,
            f"Transcript part {i + 1} of {len(chunks)}:\n\n{chunk}",
            config,
        )
        for i, chunk in enumerate(chunks)
    ]
    combined = "\n\n".join(
        f"--- Part {i + 1} summary ---\n{summary}"
        for i, summary in enumerate(partial_summaries)
    )
    final_prompt = (
        "The following are summaries of consecutive parts of a single "
        "meeting transcript. Combine them into one cohesive meeting summary "
        f"following the requested structure.\n\n{combined}"
    )
    return _call_llm(SUMMARY_SYSTEM_PROMPT, final_prompt, config)


def save_summary(summary: str, session_dir: Path, filename: str = "summary.txt") -> Path:
    path = session_dir / filename
    path.write_text(summary, encoding="utf-8")
    return path
