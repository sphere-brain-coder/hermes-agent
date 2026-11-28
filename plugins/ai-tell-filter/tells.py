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

* fenced code blocks, including the fence lines. A fence closes only on a
  run of the same character at least as long as the opener, so a ``` block
  nested inside a ```` block stays code.
* indented code blocks (4 spaces or a tab after a blank line), except
  where the indentation is a list item's own continuation
* blockquote lines and GFM table rows, with or without leading pipes
* inline code spans, URLs, absolute and relative file paths, and
  extension-bearing filenames
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
    # Bare relative paths: ``src/robust``, ``plugins/core.py``, ``docs/``.
    # No leading ``/``, ``./`` or ``~``, so the rule above cannot see them,
    # and the filename rule below rescues only the last extension-bearing
    # segment. A slash-joined token is a path far more often than it is
    # prose, and a missed tell is cheaper than a silently mangled path.
    (re.compile(r"(?<![\w.\-/])(?:[\w.\-]+/)+[\w.\-]*"), True),
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

_FENCE_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>")
_TABLE_ROW_RE = re.compile(r"^\s*\|")
# An indented code block is 4 spaces or a tab (CommonMark).
_INDENTED_CODE_RE = re.compile(r"^(?: {4}|\t)")
# GFM delimiter row: ``--- | ---``, ``| :-- | --: |``. At least one pipe, so
# a ``---`` horizontal rule never looks like a table.
_TABLE_DELIM_RE = re.compile(
    r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$"
)


def _table_row_indices(lines: List[str]) -> set:
    """Indices of lines belonging to a GFM table block.

    A table is a row containing a pipe followed by a delimiter row, then
    every consecutive non-blank pipe-bearing line. GFM does not require a
    leading pipe, so anchoring on ``^|`` alone leaves most real tables
    unprotected.
    """
    rows: set = set()
    i, n = 0, len(lines)
    while i < n - 1:
        if (
            lines[i].strip()
            and "|" in lines[i]
            and _TABLE_DELIM_RE.match(lines[i + 1])
        ):
            j = i
            while j < n and lines[j].strip() and "|" in lines[j]:
                rows.add(j)
                j += 1
            i = j
        else:
            i += 1
    return rows


def _protected_lines(lines: List[str]) -> List[bool]:
    """Mark every line the filter must leave byte-identical.

    Block-level carve-outs, decided per line with the surrounding context:
    fenced code (closing only on a fence at least as long as the opener and
    of the same character), indented code blocks, blockquotes and table
    rows.
    """
    protected = [False] * len(lines)
    fence_char: str = ""
    fence_len = 0
    in_list = False
    in_indented_code = False
    prev_blank = True
    table_rows = _table_row_indices(lines)

    for i, line in enumerate(lines):
        blank = not line.strip()
        fence = _FENCE_RE.match(line)

        if fence_char:
            # Inside a fence: everything is code, including the fence line.
            protected[i] = True
            if fence is not None:
                run = fence.group("fence")
                if (
                    run[0] == fence_char
                    and len(run) >= fence_len
                    and not fence.group("info").strip()
                ):
                    fence_char, fence_len = "", 0
            prev_blank = blank
            continue

        if fence:
            fence_char = fence.group("fence")[0]
            fence_len = len(fence.group("fence"))
            protected[i] = True
            in_indented_code = False
            prev_blank = blank
            continue

        # Track list context before judging indentation: a list item's own
        # continuation paragraph is indented but is prose, not code.
        if not blank:
            indent = len(line) - len(line.lstrip())
            if indent < 4 and _LIST_ITEM_RE.match(line):
                in_list = True
            elif indent == 0:
                in_list = False

        if blank:
            # A blank line neither opens nor closes an indented block.
            prev_blank = True
            continue

        if _INDENTED_CODE_RE.match(line):
            if in_indented_code or (prev_blank and not in_list):
                in_indented_code = True
                protected[i] = True
                prev_blank = blank
                continue
        else:
            in_indented_code = False

        if (
            i in table_rows
            or _BLOCKQUOTE_RE.match(line)
            or _TABLE_ROW_RE.match(line)
        ):
            protected[i] = True

        prev_blank = blank

    return protected


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
    lines = text.split("\n")
    protected = _protected_lines(lines)
    for line, skip in zip(lines, protected):
        if skip:
            out.append(line)
            continue
        masked, store = _mask(line)
        masked = _fix_dashes(masked)
        masked = _swap_words(masked)
        out.append(_unmask(masked, store))
    return "\n".join(out)
