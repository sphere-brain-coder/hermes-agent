"""Deterministic lexical cleanup of AI tells in assistant text.

Two edits only, both substitutions:

1. **Dashes.** Em dash (U+2014), en dash (U+2013) and horizontal bar
   (U+2015) used as a prose splice become a comma. An en dash between two
   digits becomes a hyphen (number ranges). ``--`` is left alone: in a
   coding assistant's output it is a CLI flag far more often than a dash.
2. **Tier 1A word swaps** from the ``avoid-ai-writing`` skill, restricted
   to entries with a safe one-for-one replacement that keeps the sentence
   grammatical (``robust`` -> ``reliable``). Entries whose skill guidance
   is "rewrite entirely" (``tapestry``, ``beacon``, ``game-changer``) are
   left for a human, as are words whose honest sense is common in
   technical prose (``landscape``, ``unpack``, ``underscore``).

Everything else is out of scope on purpose. The filter never deletes a
word, never reflows, re-splits or re-orders a sentence, and never changes
line structure, so rhythm and paragraph length come out byte-identical
outside the spans it swaps. Over-enforcement would re-introduce the
uniform register the skill's "Never inject" guardrails warn about.

Carve-outs, never touched:

* fenced code blocks (``` and ~~~), including the fence lines
* blockquote lines and markdown table rows
* inline code spans, URLs, file paths and extension-bearing filenames
* double-quoted material, straight or curly

The module is pure text in, text out, with no I/O and no inference, so it
is cheap enough to run on every turn.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

__all__ = ["strip_ai_tells", "WORD_SWAPS"]


# ---------------------------------------------------------------------------
# Tier 1A word swaps
# ---------------------------------------------------------------------------

# Lowercase surface form -> lowercase replacement. Inflections are listed
# explicitly rather than derived: a stemmer would turn "delved" into
# "digged". Keep this table conservative; a wrong swap is worse than a
# missed tell, because the human never sees the original.
WORD_SWAPS: Dict[str, str] = {
    # delve
    "delve": "dig", "delves": "digs", "delved": "dug", "delving": "digging",
    # realm / paradigm
    "realm": "area", "realms": "areas",
    "paradigm": "model", "paradigms": "models",
    # embark
    "embark": "start", "embarks": "starts",
    "embarked": "started", "embarking": "starting",
    # quality adjectives
    "robust": "reliable", "robustly": "reliably",
    "comprehensive": "thorough", "comprehensively": "thoroughly",
    "seamless": "smooth", "seamlessly": "smoothly",
    "meticulous": "careful", "meticulously": "carefully",
    "cutting-edge": "latest",
    "pivotal": "key",
    "daunting": "hard",
    "intricate": "complex", "intricacies": "details",
    "holistic": "complete", "holistically": "completely",
    "actionable": "practical",
    "impactful": "effective",
    "enduring": "lasting",
    "ever-evolving": "changing",
    "bustling": "busy",
    "thriving": "growing",
    "interplay": "interaction",
    "learnings": "lessons",
    # leverage (verb)
    "leverage": "use", "leverages": "uses",
    "leveraged": "used", "leveraging": "using",
    # showcase
    "showcase": "show", "showcases": "shows",
    "showcased": "showed", "showcasing": "showing",
}

_WORD_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(
        re.escape(w) for w in sorted(WORD_SWAPS, key=len, reverse=True)
    ),
    re.IGNORECASE,
)


def _match_case(original: str, replacement: str) -> str:
    """Carry the original token's capitalisation onto the replacement."""
    if original.isupper() and len(original) > 1:
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _swap_words(text: str) -> str:
    def _sub(m: "re.Match[str]") -> str:
        return _match_case(m.group(0), WORD_SWAPS[m.group(0).lower()])
    return _WORD_RE.sub(_sub, text)


# ---------------------------------------------------------------------------
# Dashes
# ---------------------------------------------------------------------------

_DASHES = "\u2013\u2014\u2015"  # en dash, em dash, horizontal bar
_DASH_RE = re.compile(r"(?P<pre>[ \t]*)(?P<dash>[%s])(?P<post>[ \t]*)" % _DASHES)

# "- **Term** — description" / "- [label](url) — description" is list
# typography, not a prose splice (avoid-ai-writing, Formatting).
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<rest>.*)$", re.DOTALL)
_LEAD_TERM_RE = re.compile(r"(?:\*\*[^*]+\*\*|\[[^\]]*\]\([^)]*\))\s*$")


def _is_list_typography(before: str) -> bool:
    m = _LIST_ITEM_RE.match(before)
    if not m:
        return False
    return bool(_LEAD_TERM_RE.search(m.group("rest")))


def _fix_dashes(line: str) -> str:
    def _sub(m: "re.Match[str]") -> str:
        before = line[: m.start()]
        after = line[m.end():]
        # A dash with nothing but whitespace in front of it on the line is
        # a bullet or a continuation marker, not a splice.
        if not before.strip():
            return m.group(0)
        if _is_list_typography(before):
            return m.group(0)
        # Number range: 2020–2024 -> 2020-2024.
        if (
            m.group("dash") == "\u2013"
            and not m.group("pre")
            and not m.group("post")
            and before[-1:].isdigit()
            and after[:1].isdigit()
        ):
            return "-"
        # Never stack punctuation: the comma already there does the job.
        if before.rstrip()[-1:] in ",;:":
            return " " if after else ""
        if not after:
            return ","
        return ", "
    return _DASH_RE.sub(_sub, line)


# ---------------------------------------------------------------------------
# Carve-outs
# ---------------------------------------------------------------------------

_SENTINEL = "\x00"

# (pattern, trim_tail). Order matters: code spans first so a URL inside
# backticks is masked once. ``trim_tail`` is set for the greedy
# non-whitespace patterns, whose match can run past the end of the thing
# being protected and into the punctuation around it, for example the
# closing paren of a markdown link.
_PROTECTED_SPANS: Tuple[Tuple[re.Pattern, bool], ...] = (
    (re.compile(r"``[^`]*``|`[^`\n]*`"), False),             # inline code
    (re.compile(r"(?:https?|ftp|file|mailto):\S+"), True),   # URLs
    (re.compile(r"\bwww\.\S+"), True),                       # bare www URLs
    (re.compile(r"(?<![\w.])(?:~|\.{1,2})?/[\w.\-/]*[\w.\-]"), True),  # paths
    (re.compile(
        r"\b[\w.\-]+\.(?:py|js|mjs|ts|tsx|jsx|md|json|ya?ml|toml|ini|cfg|sh|"
        r"bash|zsh|txt|csv|log|rs|go|java|rb|php|c|h|cc|cpp|hpp|sql|html|css)\b"
    ), True),                                                 # filenames
    (re.compile("\"[^\"\n]*\"|\u201c[^\u201d\n]*\u201d"), False),  # quoted
)

_TRAILING_PUNCT = ".,;:!?'\""
_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _split_tail(span: str) -> Tuple[str, str]:
    """Split trailing punctuation that belongs to the sentence, not the span.

    A closing bracket only comes off when the span contains no matching
    opener, so ``.../Robust_(statistics)`` keeps its paren while
    ``[docs](https://x.test)`` gives the paren back to the line.
    """
    tail = ""
    while span:
        last = span[-1]
        if last in _TRAILING_PUNCT:
            pass
        elif last in _CLOSERS and span.count(_CLOSERS[last]) >= span.count(last):
            break
        elif last in _CLOSERS:
            pass
        else:
            break
        tail = last + tail
        span = span[:-1]
    return span, tail

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>")
_TABLE_ROW_RE = re.compile(r"^\s*\|")


def _mask(line: str) -> Tuple[str, List[str]]:
    """Replace protected spans with sentinel tokens carrying no letters."""
    store: List[str] = []

    def _make_grab(trim_tail: bool):
        def _grab(m: "re.Match[str]") -> str:
            span = m.group(0)
            tail = ""
            if trim_tail:
                span, tail = _split_tail(span)
                if not span:
                    return m.group(0)
            store.append(span)
            return f"{_SENTINEL}{len(store) - 1}{_SENTINEL}{tail}"
        return _grab

    for pattern, trim_tail in _PROTECTED_SPANS:
        line = pattern.sub(_make_grab(trim_tail), line)
    return line, store


def _unmask(line: str, store: List[str]) -> str:
    for idx in range(len(store) - 1, -1, -1):
        line = line.replace(f"{_SENTINEL}{idx}{_SENTINEL}", store[idx])
    return line


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def strip_ai_tells(text: str) -> str:
    """Return ``text`` with lexical AI tells replaced. Structure is preserved.

    Returns the input unchanged when there is nothing to swap, and is
    idempotent: running it twice gives the same result as running it once.
    """
    if not text or not isinstance(text, str):
        return text

    out: List[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or _BLOCKQUOTE_RE.match(line) or _TABLE_ROW_RE.match(line):
            out.append(line)
            continue
        masked, store = _mask(line)
        masked = _fix_dashes(masked)
        masked = _swap_words(masked)
        out.append(_unmask(masked, store))
    return "\n".join(out)
