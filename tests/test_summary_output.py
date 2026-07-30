from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meeting_recorder import config, summarize


class SummaryOutputTests(unittest.TestCase):
    def test_summary_uses_markdown_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = summarize.save_summary("## Summary\n\nHello", Path(temporary))
            self.assertEqual(path.name, "summary.md")
            self.assertEqual(path.read_text(encoding="utf-8"), "## Summary\n\nHello")

    def test_summary_prompt_requires_markdown_sections(self) -> None:
        self.assertIn("Return Markdown only", summarize.SUMMARY_SYSTEM_PROMPT)
        self.assertIn("## Summary", summarize.SUMMARY_SYSTEM_PROMPT)

    def test_game_mode_generates_and_saves_two_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            responses = ["# DM Continuity Brief\n\n- Hook", "# Player Recap\n\nPreviously..."]
            with patch.object(summarize, "_call_llm", side_effect=responses) as call_llm:
                result = summarize.summarize_game("The party enters the ruin.", config.LLMConfig())
            self.assertEqual(result.dm_brief, responses[0])
            self.assertEqual(result.player_recap, responses[1])
            self.assertIn("Produce OUTPUT 1 only", call_llm.call_args_list[0].args[0])
            self.assertIn("Produce OUTPUT 2 only", call_llm.call_args_list[1].args[0])
            dm_path, player_path, combined_path = summarize.save_game_summaries(result, Path(temporary))
            self.assertEqual((dm_path.name, player_path.name, combined_path.name), (
                "dm-continuity-brief.md", "player-recap.md", "game-summary.md"
            ))
            self.assertIn("# Player Recap", combined_path.read_text(encoding="utf-8"))

    def test_game_mode_reduces_a_long_transcript_before_final_outputs(self) -> None:
        long_transcript = " ".join(["adventure"] * 30)
        cfg = config.LLMConfig(chunk_char_limit=25)
        with patch.object(summarize, "_call_llm", return_value="compact notes") as call_llm:
            summarize.summarize_game(long_transcript, cfg)
        prompts = [call.args[0] for call in call_llm.call_args_list]
        self.assertGreater(len(call_llm.call_args_list), 2)
        self.assertIn("Produce OUTPUT 1 only", prompts[-2])
        self.assertIn("Produce OUTPUT 2 only", prompts[-1])
        self.assertIn("Open Threads for Prep", summarize.GAME_SYSTEM_PROMPT)
