"""Prompt assets and instructions for tabletop-RPG game mode."""

from importlib.resources import files


GAME_SYSTEM_PROMPT = (
    files("meeting_recorder").joinpath("prompts/game_mode.txt").read_text(encoding="utf-8").strip()
)

GAME_PARTIAL_PROMPT = (
    "Extract dense, factual tabletop-RPG continuity notes from this transcript portion. "
    "Separate likely canon from OOC discussion, preserve names, places, NPC status, "
    "decisions, combat consequences, loot, rulings, deadlines, and unresolved hooks. "
    "Mark uncertainty as [UNCLEAR: ...]. Do not invent facts. These notes will be reduced "
    "with other portions."
)

GAME_REDUCTION_PROMPT = (
    "Combine these tabletop-RPG continuity notes into a compact, accurate campaign record. "
    "Preserve canon, uncertainty markers, DM prep hooks, and player-visible events; remove "
    "duplication and OOC chatter that has no continuing relevance. Do not invent facts."
)
