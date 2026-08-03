from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meeting_recorder import config, modes, summarize
from meeting_recorder.errors import SummarizationError


class ModeTests(unittest.TestCase):
    def test_packaged_modes_define_expected_capture_policies(self) -> None:
        registry = modes.load_modes()
        self.assertEqual(set(registry), {"meeting", "game", "lecture", "cbt", "journal"})
        self.assertEqual(registry["lecture"].capture, "system-only")
        self.assertEqual(registry["cbt"].max_mics, 0)
        self.assertEqual(registry["journal"].capture, "mic-only")
        self.assertEqual([artifact.filename for artifact in registry["game"].artifacts], [
            "dm-continuity-brief.md", "player-recap.md", "game-summary.md",
        ])

    def test_every_packaged_mode_has_a_substantive_markdown_prompt(self) -> None:
        for name, mode in modes.load_modes().items():
            with self.subTest(mode=name):
                self.assertGreater(len(mode.prompt), 200)
                self.assertIn("Markdown", mode.prompt)
                self.assertIn("invent", mode.prompt.lower())
                self.assertIn("##", mode.prompt)

    def test_mode_summary_writes_combined_game_archive(self) -> None:
        mode = modes.get_mode("game")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(summarize, "_call_llm", side_effect=["# DM Continuity Brief", "# Player Recap"]):
                summaries = summarize.summarize_mode("The party arrives.", config.LLMConfig(), mode)
            paths = summarize.save_mode_summaries(summaries, root, mode)
            self.assertEqual([path.name for path in paths], [
                "dm-continuity-brief.md", "player-recap.md", "game-summary.md",
            ])
            self.assertIn("# Player Recap", paths[-1].read_text(encoding="utf-8"))

    def test_mode_summary_rejects_outputs_that_do_not_match_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(SummarizationError, "did not match"):
                summarize.save_mode_summaries(
                    [("wrong.md", "content")],
                    Path(temporary),
                    modes.get_mode("meeting"),
                )
