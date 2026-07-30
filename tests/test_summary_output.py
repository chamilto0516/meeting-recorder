from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meeting_recorder import summarize


class SummaryOutputTests(unittest.TestCase):
    def test_summary_uses_markdown_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = summarize.save_summary("## Summary\n\nHello", Path(temporary))
            self.assertEqual(path.name, "summary.md")
            self.assertEqual(path.read_text(encoding="utf-8"), "## Summary\n\nHello")

    def test_summary_prompt_requires_markdown_sections(self) -> None:
        self.assertIn("Return Markdown only", summarize.SUMMARY_SYSTEM_PROMPT)
        self.assertIn("## Summary", summarize.SUMMARY_SYSTEM_PROMPT)
