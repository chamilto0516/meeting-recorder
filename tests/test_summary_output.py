from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from meeting_recorder import config, modes, summarize
from meeting_recorder.errors import SummarizationError


class SummaryOutputTests(unittest.TestCase):
    def test_summary_uses_markdown_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = summarize.save_summary("## Summary\n\nHello", Path(temporary))
            self.assertEqual(path.name, "summary.md")
            self.assertEqual(path.read_text(encoding="utf-8"), "## Summary\n\nHello")

    def test_meeting_prompt_requires_markdown_sections(self) -> None:
        prompt = modes.get_mode("meeting").prompt
        self.assertIn("Return Markdown only", prompt)
        self.assertIn("## Summary", prompt)

    def test_game_mode_generates_and_saves_two_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            response = (
                "# DM Continuity Brief\n\n- Hook\n\n"
                "# Player Recap\n\nPreviously..."
            )
            mode = modes.get_mode("game")
            with patch.object(summarize, "_call_llm", return_value=response) as call_llm:
                result = summarize.summarize_mode(
                    "The party enters the ruin.", config.LLMConfig(), mode
                )
            self.assertEqual(result, [
                ("dm-continuity-brief.md", "# DM Continuity Brief\n\n- Hook"),
                ("player-recap.md", "# Player Recap\n\nPreviously..."),
            ])
            call_llm.assert_called_once()
            system_prompt, user_prompt, _ = call_llm.call_args.args
            self.assertIn("DM Continuity Brief", system_prompt)
            self.assertIn("Player Recap", system_prompt)
            self.assertEqual(user_prompt, "The party enters the ruin.")
            dm_path, player_path, combined_path = summarize.save_mode_summaries(
                result, Path(temporary), mode
            )
            self.assertEqual((dm_path.name, player_path.name, combined_path.name), (
                "dm-continuity-brief.md", "player-recap.md", "game-summary.md"
            ))
            self.assertTrue(
                combined_path.read_text(encoding="utf-8").startswith("# Game Summary Archive")
            )
            self.assertIn("# Player Recap", combined_path.read_text(encoding="utf-8"))

    def test_long_transcript_is_sent_intact_in_one_call(self) -> None:
        long_transcript = "BEGIN " + ("middle " * 12000) + "END"
        mode = modes.get_mode("game")
        response = "# DM Continuity Brief\n\nNotes\n\n# Player Recap\n\nRecap"
        with patch.object(summarize, "_call_llm", return_value=response) as call_llm:
            summarize.summarize_mode(long_transcript, config.LLMConfig(), mode)
        call_llm.assert_called_once()
        self.assertEqual(call_llm.call_args.args[1], long_transcript)

    def test_single_artifact_mode_uses_one_full_context_call(self) -> None:
        mode = modes.get_mode("meeting")
        transcript = "Opening fact. Middle decision. Closing action."
        with patch.object(
            summarize, "_call_llm", return_value="# Meeting Summary\n\nComplete."
        ) as call_llm:
            result = summarize.summarize_mode(transcript, config.LLMConfig(), mode)
        call_llm.assert_called_once()
        self.assertEqual(call_llm.call_args.args[1], transcript)
        self.assertEqual(result, [("summary.md", "# Meeting Summary\n\nComplete.")])

    def test_empty_transcript_is_rejected_without_calling_llm(self) -> None:
        with patch.object(summarize, "_call_llm") as call_llm:
            with self.assertRaisesRegex(SummarizationError, "empty"):
                summarize.summarize_mode("  \n", config.LLMConfig(), modes.get_mode("game"))
        call_llm.assert_not_called()

    def test_malformed_artifact_headings_are_rejected(self) -> None:
        malformed_responses = {
            "missing": "# DM Continuity Brief\n\nOnly one artifact.",
            "duplicate": (
                "# DM Continuity Brief\n\nOne\n\n"
                "# DM Continuity Brief\n\nTwo\n\n"
                "# Player Recap\n\nThree"
            ),
            "unknown": "# DM Continuity Brief\n\nOne\n\n# Other\n\nTwo",
            "out of order": "# Player Recap\n\nOne\n\n# DM Continuity Brief\n\nTwo",
            "preamble": "Here are the files:\n\n# DM Continuity Brief\n\nOne\n\n# Player Recap\n\nTwo",
        }
        for name, response in malformed_responses.items():
            with self.subTest(name=name):
                with patch.object(summarize, "_call_llm", return_value=response):
                    with self.assertRaisesRegex(SummarizationError, "invalid artifact headings"):
                        summarize.summarize_mode(
                            "Transcript", config.LLMConfig(), modes.get_mode("game")
                        )

    def test_malformed_response_does_not_write_partial_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                summarize,
                "_call_llm",
                return_value="# DM Continuity Brief\n\nMissing the player recap.",
            ):
                with self.assertRaises(SummarizationError):
                    summaries = summarize.summarize_mode(
                        "Transcript", config.LLMConfig(), modes.get_mode("game")
                    )
                    summarize.save_mode_summaries(summaries, root, modes.get_mode("game"))
            self.assertEqual(list(root.iterdir()), [])

    def test_context_limit_error_recommends_a_larger_context_model(self) -> None:
        fake_litellm = types.ModuleType("litellm")
        fake_litellm.completion = Mock(
            side_effect=RuntimeError("maximum context length exceeded")
        )
        with patch.dict("sys.modules", {"litellm": fake_litellm}):
            with self.assertRaisesRegex(
                SummarizationError, "context window can hold the full transcript"
            ):
                summarize._call_llm("Prompt", "Transcript", config.LLMConfig())

    def test_call_llm_extracts_chat_completion_content(self) -> None:
        fake_litellm = types.ModuleType("litellm")
        fake_litellm.completion = Mock(return_value={
            "choices": [{"message": {"content": "  # Meeting Summary\n\nDone.  "}}]
        })
        with patch.dict("sys.modules", {"litellm": fake_litellm}):
            result = summarize._call_llm("Prompt", "Transcript", config.LLMConfig())
        self.assertEqual(result, "# Meeting Summary\n\nDone.")

    def test_call_llm_extracts_responses_api_output_without_reasoning(self) -> None:
        fake_litellm = types.ModuleType("litellm")
        fake_litellm.completion = Mock(return_value={
            "object": "response",
            "output": [
                {"type": "reasoning", "summary": [{"text": "Do not return this."}]},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "  # Meeting Summary\n\n"},
                        {"type": "output_text", "text": "Visible text.  "},
                    ],
                },
            ],
        })
        with patch.dict("sys.modules", {"litellm": fake_litellm}):
            result = summarize._call_llm("Prompt", "Transcript", config.LLMConfig())
        self.assertEqual(result, "# Meeting Summary\n\nVisible text.")

    def test_call_llm_rejects_reasoning_only_response(self) -> None:
        fake_litellm = types.ModuleType("litellm")
        fake_litellm.completion = Mock(return_value={
            "object": "response",
            "output": [{"type": "reasoning", "summary": [{"text": "Internal work."}]}],
        })
        with patch.dict("sys.modules", {"litellm": fake_litellm}):
            with self.assertRaisesRegex(SummarizationError, "no assistant output text"):
                summarize._call_llm("Prompt", "Transcript", config.LLMConfig())

    def test_legacy_chunk_limit_is_ignored_and_not_persisted(self) -> None:
        cfg = config.LLMConfig.from_dict({
            "model": "remote/model",
            "endpoint": "https://example.test",
            "chunk_char_limit": 1234,
        })
        self.assertEqual(cfg.model, "remote/model")
        self.assertNotIn("chunk_char_limit", cfg.to_dict())
