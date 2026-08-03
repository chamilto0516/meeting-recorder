from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from meeting_recorder import cli, config, modes, summarize
from meeting_recorder.errors import SummarizationError


class ModeTests(unittest.TestCase):
    def test_packaged_modes_define_expected_capture_policies(self) -> None:
        registry = modes.load_modes()
        self.assertEqual(set(registry), {"meeting", "game", "lecture", "cbt", "journal"})
        self.assertEqual(registry["lecture"].capture, "system-only")
        self.assertEqual(registry["cbt"].max_mics, 0)
        self.assertEqual(registry["journal"].capture, "mic-only")
        self.assertEqual(
            registry["meeting"].description,
            "General meetings with summaries and action items",
        )
        self.assertEqual([artifact.filename for artifact in registry["game"].artifacts], [
            "dm-continuity-brief.md", "player-recap.md", "game-summary.md",
        ])

    def test_list_modes_prints_compact_alphabetical_table(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            result = cli.cmd_list_modes(Namespace(), config.AppConfig(Path("/tmp"), 48000))

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), """Available recording modes:

MODE     CAPTURE         MICS DESCRIPTION
cbt      system only     0    Computer-based training as practical reference notes
game     mic + system    1    Tabletop RPG sessions with DM and player recaps
journal  mic only        1    Spoken personal reflections as a journal entry
lecture  system only     0    Lectures or classes as study-ready notes
meeting  mic + system    1+   General meetings with summaries and action items
""")

    def test_top_level_help_lists_list_modes(self) -> None:
        self.assertIn("list-modes", cli.build_parser().format_help())

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
