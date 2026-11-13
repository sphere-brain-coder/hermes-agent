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


def test_closed_run_of_another_task_does_not_suppress(clear_kanban_env, tmp_path):
    """A stale RUN_ID from a DIFFERENT task must never silence the guard.

    The run row is looked up by (id, task_id), so a closed run belonging to
    some other card cannot be mistaken for this worker's own finished run.
    That would be the reported bug inverted, letting a genuine silent exit
    through.
    """
    db, run_id = _worker_db(tmp_path, outcome="completed")
    conn = sqlite3.connect(db)
    # This worker is on a different card whose own run is still open.
    conn.execute("INSERT INTO tasks VALUES ('t_other', 'running', 9)")
    conn.commit()
    conn.close()
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_other")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert current_run_is_terminal() is None
    assert build_kanban_stop_nudge(messages=_tool_round("kanban_comment")) is not None


# ── The dispatcher race: tasks.status is running for SOMEONE ELSE ────
# A hand-built table cannot show this, because the bug only appears once
# the dispatcher has claimed the NEXT run while the previous worker's turn
# is still unwinding. These drive the real lifecycle functions instead.


def _board(tmp_path, monkeypatch):
    """A real kanban board, built by the real lifecycle code."""
    from hermes_cli import kanban_db as kb

    for var in ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb._INITIALIZED_PATHS.clear()
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    return kb, kb.connect(db_path=db), db


def _through_changes_requested(kb, conn, *, respawn):
    """review_requested -> changes_requested, optionally respawning the next run.

    Returns (task_id, reviewer_run_id). With ``respawn`` the dispatcher has
    already claimed a new run under the implementer, so ``tasks.status`` is
    legitimately ``running`` again for a different worker.
    """
    tid = kb.create_task(conn, title="race", assignee="coder")
    impl = kb.claim_task(conn, tid, claimer="coder:1")
    assert kb.request_review(
        conn, tid, summary="done", reviewer="reviewer",
        expected_run_id=impl.current_run_id,
    )
    rev = kb.claim_review_task(conn, tid, claimer="reviewer:1")
    reviewer_run = rev.current_run_id
    ok, _ = kb.request_changes(
        conn, tid, reason="defect on line 147", expected_run_id=reviewer_run,
    )
    assert ok
    if respawn:
        nxt = kb.claim_task(conn, tid, claimer="coder:2")
        assert nxt is not None and nxt.current_run_id != reviewer_run
    return tid, reviewer_run


def test_reviewer_not_nudged_once_next_run_is_live(clear_kanban_env, tmp_path, monkeypatch):
    """The reported race: the card is `running` again under another profile.

    The reviewer's own run is closed, but the dispatcher has already spawned
    the implementer's next run. Keying off ``tasks.status`` sees ``running``
    and nudges; keying off this worker's own run sees ``changes_requested``
    and stays quiet. Complying would complete someone else's live run and
    approve the work the reviewer just rejected.
    """
    kb, conn, db = _board(tmp_path, monkeypatch)
    tid, reviewer_run = _through_changes_requested(kb, conn, respawn=True)
    task = kb.get_task(conn, tid)
    conn.close()

    # Precondition: the task row says running, for a run that is not ours.
    assert task.status == "running"
    assert task.current_run_id != reviewer_run

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(reviewer_run))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert current_run_is_terminal() is True
    assert build_kanban_stop_nudge(messages=_tool_round("kanban_comment")) is None


def test_implementer_not_nudged_once_reviewer_run_is_live(clear_kanban_env, tmp_path, monkeypatch):
    """Same race on the handoff side: request_review, reviewer already claimed."""
    kb, conn, db = _board(tmp_path, monkeypatch)
    tid = kb.create_task(conn, title="handoff", assignee="coder")
    impl = kb.claim_task(conn, tid, claimer="coder:1")
    impl_run = impl.current_run_id
    assert kb.request_review(
        conn, tid, summary="done", reviewer="reviewer",
        expected_run_id=impl_run,
    )
    kb.claim_review_task(conn, tid, claimer="reviewer:1")
    task = kb.get_task(conn, tid)
    conn.close()

    assert task.status == "running" and task.current_run_id != impl_run

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(impl_run))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert current_run_is_terminal() is True
    assert build_kanban_stop_nudge(messages=_tool_round("kanban_comment")) is None


def test_live_run_on_real_board_still_nudges(clear_kanban_env, tmp_path, monkeypatch):
    """Control on the same real board: an open run of our own must nudge.

    Without this, the two tests above could be satisfied by a guard that
    never fires at all.
    """
    kb, conn, db = _board(tmp_path, monkeypatch)
    tid = kb.create_task(conn, title="working", assignee="coder")
    mine = kb.claim_task(conn, tid, claimer="coder:1")
    conn.close()

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(mine.current_run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert current_run_is_terminal() is False
    assert build_kanban_stop_nudge(messages=_tool_round("kanban_comment")) is not None


def test_guard_never_creates_a_board_it_cannot_find(
    clear_kanban_env, tmp_path, monkeypatch
):
    """A missing board must be read, not made.

    The guard runs on EVERY turn end, so it has to be read-only. Reaching
    the board through the initialising connect path instead of a read-only
    open turns each turn end into a write: pointed at a path with no board
    (a renamed or archived board, a path this process cannot see) it
    silently creates a fully-formed empty one, which a later reader can
    mistake for a real board.

    Keep this keyed on files-on-disk rather than on the return value. The
    nudge decision is already correct in this state; it is the side effect
    that is wrong, and nothing else in the suite would notice it.
    """
    kb, conn, db = _board(tmp_path / "real", monkeypatch)
    tid = kb.create_task(conn, title="working", assignee="coder")
    mine = kb.claim_task(conn, tid, claimer="coder:1")
    conn.close()

    probe = tmp_path / "probe"
    probe.mkdir()
    missing = probe / "kanban.db"

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(mine.current_run_id))
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(missing))

    # Fails open: an unreadable board must never silence a genuine violation.
    assert build_kanban_stop_nudge(messages=_tool_round("kanban_comment")) is not None
    # ...and leaves nothing behind.
    assert not missing.exists(), "the guard created a board at a path that had none"
    assert sorted(p.name for p in probe.iterdir()) == []



# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




