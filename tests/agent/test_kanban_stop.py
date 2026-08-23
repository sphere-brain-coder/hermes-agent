"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import sqlite3

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_run_is_over,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_STOP_NUDGE",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _tool_pair(name: str, call_id: str = "1") -> list[dict]:
    """An assistant tool_call plus its tool result, as the loop sees them."""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": name, "tool_call_id": call_id, "content": "ok"},
    ]


def _board_db(
    tmp_path,
    task_id: str,
    *,
    current_run_id,
    ended_at=None,
    outcome=None,
):
    """Minimal board DB with the two columns the guard reads."""
    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, current_run_id INTEGER)")
    conn.execute(
        "CREATE TABLE task_runs ("
        "  id INTEGER PRIMARY KEY, task_id TEXT, ended_at INTEGER, outcome TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks (id, current_run_id) VALUES (?, ?)",
        (task_id, current_run_id),
    )
    if current_run_id is not None:
        conn.execute(
            "INSERT INTO task_runs (id, task_id, ended_at, outcome) VALUES (?, ?, ?, ?)",
            (current_run_id, task_id, ended_at, outcome),
        )
    conn.commit()
    conn.close()
    return path


def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        *_tool_pair("kanban_heartbeat"),
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_nudge_names_all_four_terminal_verbs(clear_kanban_env):
    """The nudge must not push a reviewer toward complete/block only.

    Naming just those two verbs is what made a correct reviewer either
    falsify an approval (``kanban_complete``) or stall the board
    (``kanban_block``) on a review-gated card.
    """
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review_gated")
    nudge = build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}])
    assert nudge is not None
    for verb in (
        "kanban_complete",
        "kanban_block",
        "kanban_request_review",
        "kanban_request_changes",
    ):
        assert verb in nudge, f"nudge does not offer {verb}"


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = _tool_pair("kanban_complete")
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_no_nudge_after_kanban_block(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = _tool_pair("kanban_block")
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


# ── Regression: review-lane terminals are terminal ───────────────────
# t_0c81933d hit this twice on board `requests`. Run 43 ended with
# kanban_request_review and run 54 with kanban_request_changes; both closed
# their run row, and the guard nudged anyway claiming the task was still
# `running`.


def test_no_nudge_after_request_review(clear_kanban_env):
    """Implementer run ending in kanban_request_review exits clean."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_0c81933d")
    messages = _tool_pair("kanban_request_review")
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is None


def test_no_nudge_after_request_changes(clear_kanban_env):
    """Reviewer run ending in kanban_request_changes exits clean."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_0c81933d")
    messages = _tool_pair("kanban_request_changes")
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is None


@pytest.mark.parametrize(
    "tool",
    [
        "kanban_complete",
        "kanban_block",
        "kanban_request_review",
        "kanban_request_changes",
    ],
)
def test_every_terminal_tool_suppresses_the_nudge(clear_kanban_env, tool):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    assert build_kanban_stop_nudge(messages=_tool_pair(tool)) is None


def test_non_terminal_kanban_tool_still_nudges(clear_kanban_env):
    """Widening the set must not swallow genuine violations."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    for tool in ("kanban_comment", "kanban_heartbeat", "kanban_create"):
        messages = _tool_pair(tool)
        assert session_called_kanban_terminal(messages) is False
        assert build_kanban_stop_nudge(messages=messages) is not None


# ── DB is authoritative over the in-memory transcript ────────────────


def test_run_is_over_when_current_run_id_null(clear_kanban_env, tmp_path):
    db = _board_db(tmp_path, "t_0c81933d", current_run_id=None)
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_0c81933d")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert kanban_run_is_over() is True


def test_run_is_over_when_run_row_ended(clear_kanban_env, tmp_path):
    db = _board_db(
        tmp_path,
        "t_0c81933d",
        current_run_id=54,
        ended_at=1787480000,
        outcome="changes_requested",
    )
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_0c81933d")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert kanban_run_is_over() is True


def test_run_still_open_when_run_row_live(clear_kanban_env, tmp_path):
    db = _board_db(tmp_path, "t_live", current_run_id=83, ended_at=None, outcome=None)
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_live")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert kanban_run_is_over() is False


def test_db_closed_run_suppresses_nudge_despite_empty_transcript(
    clear_kanban_env, tmp_path
):
    """The exact t_0c81933d shape: DB says the run ended, transcript doesn't.

    Task `ready`, current_run_id NULL, run 54 outcome changes_requested with
    ended_at set. The old guard nudged anyway because it only looked at the
    conversation.
    """
    db = _board_db(tmp_path, "t_0c81933d", current_run_id=None)
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_0c81933d")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    messages = [{"role": "user", "content": "work kanban task"}]
    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is None


def test_unknown_db_falls_back_to_transcript(clear_kanban_env, tmp_path):
    """An unreadable board must not silently disable the guard."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(tmp_path / "nope.db"))
    assert kanban_run_is_over() is None
    assert build_kanban_stop_nudge(messages=[{"role": "user", "content": "hi"}])


def test_task_absent_from_board_is_unknown(clear_kanban_env, tmp_path):
    db = _board_db(tmp_path, "t_other", current_run_id=None)
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_missing")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert kanban_run_is_over() is None


def test_corrupt_db_is_unknown_not_over(clear_kanban_env, tmp_path):
    bad = tmp_path / "kanban.db"
    bad.write_bytes(b"not a sqlite file at all")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(bad))
    assert kanban_run_is_over() is None


def test_run_is_over_needs_a_task_id(clear_kanban_env):
    assert kanban_run_is_over() is None


# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.


def test_nudge_budget_is_respected(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [{"role": "user", "content": "go"}]
    assert build_kanban_stop_nudge(messages=messages, attempts=1) is not None
    assert build_kanban_stop_nudge(messages=messages, attempts=2) is None
