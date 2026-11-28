"""Tests for the ai-tell-filter plugin.

Covers the bundled plugin at ``plugins/ai-tell-filter/``:

  * ``tells.strip_ai_tells``: em/en dash replacement and the Tier 1A
    single-word swaps taken from the ``avoid-ai-writing`` skill.
  * The carve-outs: fenced code, inline code, URLs, file paths,
    blockquotes, table rows and double-quoted material are never touched.
  * The non-goals: the filter substitutes, it never deletes, reflows,
    re-splits or re-orders anything, so sentence and paragraph structure
    survives byte-for-byte outside the spans it swaps.
  * Plugin ``__init__``: the ``transform_llm_output`` hook returns a
    replacement string only when something actually changed, honours the
    ``AI_TELL_FILTER_DISABLE`` kill switch, and is registered on the hook
    the host dispatches.
  * Bundled-plugin discovery via ``PluginManager.discover_and_load``.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "ai-tell-filter"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("AI_TELL_FILTER_DISABLE", raising=False)
    yield hermes_home


def _load_lib():
    """Import the plugin's library module directly from the repo path."""
    spec = importlib.util.spec_from_file_location(
        "ai_tell_filter_tells_under_test", PLUGIN_DIR / "tells.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_init():
    """Import the plugin's __init__.py (which depends on the library)."""
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.ai_tell_filter",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.ai_tell_filter"
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules["hermes_plugins.ai_tell_filter"] = mod
    spec.loader.exec_module(mod)
    return mod


def strip(text):
    return _load_lib().strip_ai_tells(text)


# ---------------------------------------------------------------------------
# Dashes
# ---------------------------------------------------------------------------

class TestDashes:
    def test_spaced_em_dash_becomes_comma(self):
        assert strip("It works \u2014 mostly.") == "It works, mostly."

    def test_unspaced_em_dash_becomes_comma(self):
        assert strip("two\u2014three") == "two, three"

    def test_em_dash_after_comma_drops_the_dash(self):
        """No double punctuation: the comma that is already there wins."""
        assert strip("Yes, \u2014 and no.") == "Yes, and no."

    def test_en_dash_between_digits_becomes_hyphen(self):
        assert strip("ran 2020\u20132024 without a restart") == (
            "ran 2020-2024 without a restart"
        )

    def test_en_dash_in_prose_becomes_comma(self):
        assert strip("good ideas \u2013 the cheap ones") == (
            "good ideas, the cheap ones"
        )

    def test_horizontal_bar_becomes_comma(self):
        assert strip("here \u2015 there") == "here, there"

    def test_double_hyphen_is_left_alone(self):
        """``--`` is a CLI flag far more often than a dash. Out of scope."""
        assert strip("pass --dry-run first") == "pass --dry-run first"

    def test_markdown_horizontal_rule_survives(self):
        assert strip("a\n\n---\n\nb") == "a\n\n---\n\nb"

    def test_list_item_separator_dash_is_typography_not_a_splice(self):
        src = "- **Term** \u2014 what it means\n- [docs](https://x.test) \u2014 the page"
        assert strip(src) == src

    def test_mid_sentence_splice_in_a_list_item_still_fixed(self):
        assert strip("- it ran fine \u2014 twice") == "- it ran fine, twice"


# ---------------------------------------------------------------------------
# Tier 1A word swaps
# ---------------------------------------------------------------------------

class TestWordSwaps:
    def test_robust_becomes_reliable(self):
        assert strip("a robust parser") == "a reliable parser"

    def test_seamless_becomes_smooth(self):
        assert strip("a seamless handoff") == "a smooth handoff"

    def test_adverb_form_swapped(self):
        assert strip("it ran seamlessly") == "it ran smoothly"

    def test_capitalisation_preserved(self):
        assert strip("Robust, comprehensive logging.") == (
            "Reliable, thorough logging."
        )

    def test_all_caps_preserved(self):
        assert strip("ROBUST") == "RELIABLE"

    def test_irregular_inflection_handled(self):
        assert strip("we delved into it") == "we dug into it"
        assert strip("delving into the logs") == "digging into the logs"

    def test_leverage_verb_forms_swapped(self):
        assert strip("leveraging the cache") == "using the cache"

    def test_word_boundaries_respected(self):
        """A longer word that merely contains a flagged word is left alone."""
        assert strip("robustness testing") == "robustness testing"
        assert strip("a paradigmatic case") == "a paradigmatic case"

    def test_listed_plural_swapped(self):
        assert strip("two realms") == "two areas"

    def test_hyphenated_entry_swapped(self):
        assert strip("cutting-edge tooling") == "latest tooling"

    def test_entries_needing_a_rewrite_are_left_alone(self):
        """``tapestry``/``beacon``/``game-changer`` have no safe 1:1 swap."""
        src = "a tapestry of beacon services, a real game-changer"
        assert strip(src) == src

    def test_clean_text_is_returned_byte_identical(self):
        src = (
            "The deploy finished at 14:02. Two nodes lagged.\n\n"
            "I rolled the second one back by hand.\n"
        )
        assert strip(src) == src


# ---------------------------------------------------------------------------
# Carve-outs
# ---------------------------------------------------------------------------

class TestCarveOuts:
    def test_fenced_code_block_untouched(self):
        src = (
            "Before robust text.\n"
            "```python\n"
            "robust = compute()  # seamless \u2014 really\n"
            "```\n"
            "After robust text.\n"
        )
        out = strip(src)
        assert "robust = compute()  # seamless \u2014 really" in out
        assert out.startswith("Before reliable text.\n")
        assert out.endswith("After reliable text.\n")

    def test_tilde_fenced_block_untouched(self):
        src = "~~~\nrobust \u2014 here\n~~~\n"
        assert strip(src) == src

    def test_inline_code_untouched(self):
        assert strip("pass `--robust` to it") == "pass `--robust` to it"
        assert strip("use `a \u2014 b` verbatim") == "use `a \u2014 b` verbatim"

    def test_url_untouched(self):
        src = "see https://example.test/robust\u2014page?x=1 for more"
        assert strip(src) == src

    def test_markdown_link_target_untouched_but_prose_cleaned(self):
        assert strip("a robust [doc](https://x.test/robust) here") == (
            "a reliable [doc](https://x.test/robust) here"
        )

    def test_file_path_untouched(self):
        src = "edit /opt/robust/seamless.py and ./robust_helper.py now"
        assert strip(src) == src

    def test_blockquote_line_untouched(self):
        src = "> robust \u2014 as quoted\n"
        assert strip(src) == src

    def test_table_row_untouched(self):
        src = "| robust | seamless \u2014 yes |\n| --- | --- |\n"
        assert strip(src) == src

    def test_double_quoted_material_untouched(self):
        assert strip('He said "this is robust \u2014 truly" today.') == (
            'He said "this is robust \u2014 truly" today.'
        )

    def test_curly_quoted_material_untouched(self):
        src = "She wrote \u201ca robust \u2014 design\u201d in the ticket."
        assert strip(src) == src

    def test_prose_after_a_closed_quote_is_still_cleaned(self):
        assert strip('"robust" is a robust word') == (
            '"robust" is a reliable word'
        )

    def test_url_ending_a_sentence_keeps_the_full_stop_outside(self):
        assert strip("a robust doc at https://x.test/a. Next.") == (
            "a reliable doc at https://x.test/a. Next."
        )

    def test_url_containing_balanced_parens_is_kept_whole(self):
        src = "see https://en.wikipedia.test/wiki/Robust_(statistics) now"
        assert strip(src) == src

    def test_path_before_a_comma_is_protected(self):
        assert strip("in ./robust/seamless.py, a robust call") == (
            "in ./robust/seamless.py, a reliable call"
        )


# ---------------------------------------------------------------------------
# Carve-out leaks found in review round 1 (each of these rewrote protected
# material at bf2f3e31d6). One regression test per defect, plus the
# over-protection boundary each fix must not cross: a carve-out that is too
# wide silently switches the filter off, which is the same class of bug.
# ---------------------------------------------------------------------------

class TestRelativePathCarveOut:
    def test_bare_directory_with_trailing_slash_untouched(self):
        src = "The seamless/ directory holds the adapters."
        assert strip(src) == src

    def test_bare_multi_segment_path_untouched(self):
        src = "open plugins/seamless/core.py now"
        assert strip(src) == src

    def test_extensionless_relative_path_untouched(self):
        src = "cd into src/robust then build"
        assert strip(src) == src

    def test_relative_markdown_link_target_untouched(self):
        assert strip("a robust [guide](robust/guide.md) here") == (
            "a reliable [guide](robust/guide.md) here"
        )

    def test_absolute_path_still_protected(self):
        src = "edit /opt/robust/seamless.py and ./robust_helper.py now"
        assert strip(src) == src

    def test_plain_prose_word_is_still_swapped(self):
        """The path rule must not swallow an ordinary sentence."""
        assert strip("a robust design, seamless too") == (
            "a reliable design, smooth too"
        )


class TestIndentedCodeCarveOut:
    def test_four_space_indented_block_untouched(self):
        src = "Run this:\n\n    robust = compute()\n"
        assert strip(src) == src

    def test_tab_indented_block_untouched(self):
        src = "Run this:\n\n\trobust = compute()\n"
        assert strip(src) == src

    def test_prose_after_the_block_dedents_and_is_cleaned(self):
        src = "Run this:\n\n    robust = compute()\n\nThat is robust.\n"
        assert strip(src) == (
            "Run this:\n\n    robust = compute()\n\nThat is reliable.\n"
        )

    def test_indented_list_continuation_is_prose_not_code(self):
        """List content is indented too. Over-protecting it mutes the filter."""
        src = "- first item\n\n    a robust follow-on paragraph\n"
        assert strip(src) == (
            "- first item\n\n    a reliable follow-on paragraph\n"
        )


class TestFenceLengthCarveOut:
    def test_inner_shorter_fence_does_not_close_the_block(self):
        src = "````\n```\nrobust = 1\n```\n````"
        assert strip(src) == src

    def test_longer_closing_fence_closes_the_block(self):
        src = "```\nrobust = 1\n````\n\nThat is robust.\n"
        assert strip(src) == "```\nrobust = 1\n````\n\nThat is reliable.\n"

    def test_tilde_fence_is_not_closed_by_a_backtick_fence(self):
        src = "~~~\n```\nrobust = 1\n```\n~~~"
        assert strip(src) == src

    def test_prose_after_a_closed_fence_is_still_cleaned(self):
        src = "```\nrobust = 1\n```\n\nThat is robust.\n"
        assert strip(src) == "```\nrobust = 1\n```\n\nThat is reliable.\n"


class TestTableCarveOut:
    def test_table_without_leading_pipes_untouched(self):
        src = "col | note\n--- | ---\nparser | robust \u2014 yes\n"
        assert strip(src) == src

    def test_header_row_above_the_delimiter_untouched(self):
        src = "robust | note\n--- | ---\nparser | yes\n"
        assert strip(src) == src

    def test_prose_after_the_table_is_cleaned(self):
        src = "col | note\n--- | ---\nparser | robust \u2014 yes\n\nThat is robust.\n"
        assert strip(src) == (
            "col | note\n--- | ---\nparser | robust \u2014 yes\n\nThat is reliable.\n"
        )

    def test_prose_containing_a_pipe_outside_a_table_is_cleaned(self):
        """Only rows in a real table block are protected, not any pipe."""
        assert strip("run a | b and it is robust") == (
            "run a | b and it is reliable"
        )

    def test_consecutive_pipe_prose_lines_without_a_delimiter_are_cleaned(self):
        """Pipes alone do not make a table: a delimiter row is required."""
        src = "run a | b and it is robust\nthen a seamless | pipe line\n"
        assert strip(src) == (
            "run a | b and it is reliable\nthen a smooth | pipe line\n"
        )


class TestIdempotence:
    def test_running_twice_matches_running_once(self):
        src = (
            "A robust \u2014 seamless \u2014 design.\n"
            "```\nrobust \u2014 code\n```\n"
            "| robust | x |\n"
            "> quoted \u2014 line\n"
            "`robust` and https://x.test/robust\u2014y and \"robust \u2014 q\"\n"
            "- **Term** \u2014 desc\n"
        )
        once = strip(src)
        assert strip(once) == once

    def test_known_output_for_the_mixed_document(self):
        src = (
            "A robust \u2014 seamless \u2014 design.\n"
            "```\nrobust \u2014 code\n```\n"
            "- **Term** \u2014 desc\n"
        )
        assert strip(src) == (
            "A reliable, smooth, design.\n"
            "```\nrobust \u2014 code\n```\n"
            "- **Term** \u2014 desc\n"
        )


# ---------------------------------------------------------------------------
# Structure preservation (the explicit non-goal)
# ---------------------------------------------------------------------------

class TestStructurePreserved:
    def test_line_count_and_blank_lines_unchanged(self):
        src = "Robust one.\n\nSeamless two.\n\n\nThree.\n"
        out = strip(src)
        assert out.count("\n") == src.count("\n")
        assert out.splitlines()[1] == ""

    def test_sentence_count_unchanged(self):
        src = "A robust thing \u2014 it works. Another one. A third."
        assert strip(src).count(".") == src.count(".")

    def test_no_trailing_whitespace_introduced(self):
        out = strip("robust \u2014 fine\n")
        assert out == "reliable, fine\n"


# ---------------------------------------------------------------------------
# Hook behaviour
# ---------------------------------------------------------------------------

class TestHook:
    def test_hook_returns_transformed_text(self):
        mod = _load_plugin_init()
        out = mod._on_transform_llm_output(
            response_text="a robust plan \u2014 yes",
            session_id="s1",
            model="m",
            platform="cli",
        )
        assert out == "a reliable plan, yes"

    def test_hook_returns_none_when_nothing_changes(self):
        mod = _load_plugin_init()
        assert mod._on_transform_llm_output(
            response_text="nothing to fix here",
            session_id="s1",
            model="m",
            platform="cli",
        ) is None

    def test_hook_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setenv("AI_TELL_FILTER_DISABLE", "1")
        mod = _load_plugin_init()
        assert mod._on_transform_llm_output(
            response_text="a robust plan",
            session_id="s1",
            model="m",
            platform="cli",
        ) is None

    def test_hook_survives_non_string_input(self):
        mod = _load_plugin_init()
        assert mod._on_transform_llm_output(
            response_text=None, session_id="", model="", platform="",
        ) is None

    def test_register_wires_transform_llm_output(self):
        mod = _load_plugin_init()
        registered = []

        class _Ctx:
            def register_hook(self, name, fn):
                registered.append((name, fn))

        mod.register(_Ctx())
        assert [n for n, _ in registered] == ["transform_llm_output"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_manifest_declares_the_hook(self):
        manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
        assert manifest["name"] == "ai-tell-filter"
        assert manifest["hooks"] == ["transform_llm_output"]

    def test_discovered_as_bundled_but_not_enabled_by_default(self, _isolate_env):
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        assert "ai-tell-filter" in mgr._plugins
        loaded = mgr._plugins["ai-tell-filter"]
        assert loaded.manifest.source == "bundled"
        assert not loaded.enabled

    def test_enabled_plugin_transforms_through_invoke_hook(self, _isolate_env):
        (_isolate_env / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["ai-tell-filter"]}}),
            encoding="utf-8",
        )
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        results = mgr.invoke_hook(
            "transform_llm_output",
            response_text="a robust plan \u2014 yes",
            session_id="s1",
            model="m",
            platform="cli",
        )
        assert results == ["a reliable plan, yes"]
