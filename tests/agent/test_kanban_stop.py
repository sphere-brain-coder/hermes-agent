"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import sqlite3

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    current_run_is_terminal,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def _tool_round(name: str, result: str = "ok") -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": name, "tool_call_id": "1", "content": result},
    ]


def test_no_nudge_after_kanban_request_changes(clear_kanban_env):
    """A reviewer's only terminal call is request_changes, which ends the run.

    Regression for the false "still running, call kanban_complete" nudge: it
    pushed reviewers to approve the very work they had just rejected.
    """
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_ab35ec7b")
    messages = _tool_round("kanban_request_changes", "changes requested")
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_no_nudge_after_kanban_request_review(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = _tool_round("kanban_request_review", "review requested")
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_nudge_lists_every_terminal_transition(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    nudge = build_kanban_stop_nudge(messages=_tool_round("kanban_heartbeat"))
    assert nudge is not None
    for tool in (
        "kanban_complete",
        "kanban_block",
        "kanban_request_review",
        "kanban_request_changes",
    ):
        assert tool in nudge


# ── Run-state check: the DB outcome, not tasks.status ────────────────


def _worker_db(tmp_path, *, outcome, task_status="running", run_id=7):
    """Minimal kanban DB with one task and one run, as the dispatcher leaves it."""
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, current_run_id INTEGER)")
    conn.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, "
        "outcome TEXT, ended_at INTEGER)"
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?)",
        ("t_abc", task_status, None if outcome else run_id),
    )
    conn.execute(
        "INSERT INTO task_runs VALUES (?, ?, ?, ?)",
        (run_id, "t_abc", outcome, 1794283501 if outcome else None),
    )
    conn.commit()
    conn.close()
    return db, run_id


def test_run_outcome_recorded_is_terminal(clear_kanban_env, tmp_path):
    db, run_id = _worker_db(tmp_path, outcome="changes_requested")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert current_run_is_terminal() is True
    # No terminal tool call in this session's messages at all: the run state
    # alone must suppress the nudge.
    assert build_kanban_stop_nudge(messages=_tool_round("kanban_comment")) is None


def test_open_run_still_nudges(clear_kanban_env, tmp_path):
    db, run_id = _worker_db(tmp_path, outcome=None)
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert current_run_is_terminal() is False
    assert build_kanban_stop_nudge(messages=_tool_round("kanban_comment")) is not None


def test_task_status_ready_does_not_decide(clear_kanban_env, tmp_path):
    """The old premise was tasks.status; an open run must still nudge."""
    db, run_id = _worker_db(tmp_path, outcome=None, task_status="ready")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert current_run_is_terminal() is False


def test_unreadable_db_is_unknown_not_terminal(clear_kanban_env, tmp_path):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "7")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(tmp_path / "missing.db"))
    assert current_run_is_terminal() is None
    # Unknown falls back to the message scan, which still nudges here.
    assert build_kanban_stop_nudge(messages=_tool_round("kanban_comment")) is not None



# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




