"""ai-tell-filter plugin: strip lexical AI tells from the final response.

Wires one behaviour:

* ``transform_llm_output`` hook, fired once per turn after the
  tool-calling loop produces a final response and before it is delivered.
  The callback runs ``tells.strip_ai_tells`` over the text and returns the
  result only when something actually changed, so a clean turn costs one
  regex pass and no replacement.

Scope is lexical and deterministic: em/en dash replacement plus the Tier
1A word swaps from the ``avoid-ai-writing`` skill. No LLM call, no
rhythm/structure rewriting, and code, inline code, URLs, paths, quotes,
blockquotes and table rows are carved out. See ``tells.py`` for the table
and the carve-out list, and ``README.md`` for why the scope stops there.

Kill switch: ``AI_TELL_FILTER_DISABLE=1`` loads the plugin but makes the
hook a no-op, which is the fastest way to compare raw against filtered
output without editing config.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .tells import strip_ai_tells

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

# A transform that has to walk a novel is not worth the latency, and a
# response this long is almost always a pasted artifact rather than prose.
_MAX_CHARS = 200_000


def _disabled() -> bool:
    return os.environ.get("AI_TELL_FILTER_DISABLE", "").strip().lower() in _TRUTHY


def _on_transform_llm_output(
    response_text: Any = None,
    session_id: str = "",
    model: str = "",
    platform: str = "",
    **_: Any,
) -> Optional[str]:
    """Return cleaned text, or None to leave the response untouched."""
    if _disabled():
        return None
    if not isinstance(response_text, str) or not response_text:
        return None
    if len(response_text) > _MAX_CHARS:
        return None
    try:
        cleaned = strip_ai_tells(response_text)
    except Exception as exc:  # never break delivery over a cosmetic pass
        logger.warning("ai-tell-filter: transform failed, passing through: %s", exc)
        return None
    if cleaned == response_text:
        return None
    return cleaned


def register(ctx) -> None:
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)
