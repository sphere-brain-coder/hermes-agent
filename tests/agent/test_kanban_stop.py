"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    run_already_terminal,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _tool_pair(name):
    """One assistant tool_call + its tool result, as the loop records them."""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": name, "arguments": "{}"}}
            ],
        },
        {"role": "tool", "name": name, "tool_call_id": "1", "content": "ok"},
    ]


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


def test_nudge_names_every_terminal_transition(clear_kanban_env):
    """The nudge must offer review/changes too, or a reviewer has no valid move."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    nudge = build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}])
    assert nudge is not None
    for tool in (
        "kanban_complete",
        "kanban_block",
        "kanban_request_review",
        "kanban_request_changes",
    ):
        assert tool in nudge, f"{tool} missing from nudge text"


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = _tool_pair("kanban_complete")
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


@pytest.mark.parametrize("tool", ["kanban_request_review", "kanban_request_changes"])
def test_no_nudge_after_review_transitions(clear_kanban_env, tool):
    """request_review / request_changes end the run as surely as complete."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = _tool_pair(tool)
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_nudge_budget_is_one_per_run(clear_kanban_env):
    """A worker that already answered one nudge must not be re-prompted."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [{"role": "user", "content": "go"}]
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is not None
    assert build_kanban_stop_nudge(messages=messages, attempts=1) is None


# ── Board-state gate: decided off task_runs.outcome, not tasks.status ──
# Regression for the nudge firing at runs that had already transitioned
# (three occurrences on t_00352f44, the third a repeat within one run).


@pytest.fixture
def board(tmp_path, monkeypatch):
    """An isolated kanban DB; never the live board."""
    db_path = tmp_path / "stop-guard.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "stop-guard-test")
    from hermes_cli import kanban_db as kb

    kb.init_db()
    assert str(tmp_path) in str(kb.kanban_db_path())
    return kb


def _claimed_task(kb, title="guard"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee="coder")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        return tid, run_id
    finally:
        conn.close()


def test_run_still_open_is_not_terminal(board, monkeypatch):
    tid, run_id = _claimed_task(board)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    assert run_already_terminal() is False
    assert build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}]) is not None


def test_no_nudge_once_run_recorded_review_requested(board, monkeypatch):
    """Acceptance 1: a run ended by request_review gets no 'still running' nudge."""
    tid, run_id = _claimed_task(board, title="review handoff")
    conn = board.connect()
    try:
        ok = board.request_review(
            conn, tid, summary="implemented", expected_run_id=run_id
        )
        assert ok
        run = board.get_run(conn, run_id)
        assert run.outcome == "review_requested"
        assert run.ended_at is not None
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    assert run_already_terminal() is True
    # Message history deliberately carries NO terminal tool call: this must be
    # decided by the run's recorded outcome alone (the reported bug fired on a
    # fresh-looking transcript).
    assert build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}]) is None


def test_no_nudge_once_run_recorded_changes_requested(board, monkeypatch):
    """Acceptance 1: same for a reviewer run ended by request_changes."""
    tid, run_id = _claimed_task(board, title="reviewed work")
    conn = board.connect()
    try:
        assert board.request_review(
            conn, tid, summary="implemented", expected_run_id=run_id
        )
        # A reviewer run must be claimed from `review` (claim_review_task
        # stamps source_status=review, which request_changes requires).
        assert board.claim_review_task(conn, tid) is not None
        review_run_id = board.get_task(conn, tid).current_run_id
        ok, _ = board.request_changes(
            conn, tid, reason="fix the thing", expected_run_id=review_run_id
        )
        assert ok
        run = board.get_run(conn, review_run_id)
        assert run.outcome == "changes_requested"
        # The reported board state: task back to a queued status, pointer cleared.
        assert board.get_task(conn, tid).current_run_id is None
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review_run_id))
    assert run_already_terminal() is True
    assert build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}]) is None


def test_no_nudge_when_current_run_pointer_cleared(board, monkeypatch):
    """Run id unknown to this process, pointer null → nothing left to close."""
    tid, run_id = _claimed_task(board, title="pointer cleared")
    conn = board.connect()
    try:
        assert board.complete_task(conn, tid, summary="done")
        assert board.get_task(conn, tid).current_run_id is None
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    assert run_already_terminal() is True
    assert build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}]) is None


def test_closed_run_of_another_task_does_not_suppress(board, monkeypatch):
    """A stale HERMES_KANBAN_RUN_ID from a DIFFERENT card must not suppress.

    Review finding on PR #9: the guard trusted the env run id outright. Task A
    finishes (its run closes), task B is then claimed with its own OPEN run,
    but the worker's environment still names A's run. Reading A's closed row
    and calling it "my run ended" silences the guard for B, which is this
    defect inverted: a genuine silent exit on B goes un-nudged.

    The run must belong to the task before its outcome counts.
    """
    tid_a, run_a = _claimed_task(board, title="other card")
    conn = board.connect()
    try:
        assert board.complete_task(conn, tid_a, summary="done")
        assert board.get_run(conn, run_a).outcome is not None
    finally:
        conn.close()

    tid_b, run_b = _claimed_task(board, title="my card")
    assert run_b != run_a
    conn = board.connect()
    try:
        # B's own run is open and B still points at it.
        assert board.get_run(conn, run_b).outcome is None
        assert board.get_task(conn, tid_b).current_run_id == run_b
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid_b)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_a))
    assert run_already_terminal() is False
    nudge = build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}])
    assert nudge is not None
    assert tid_b in nudge


def test_explicit_run_id_of_another_task_does_not_suppress(board, monkeypatch):
    """Same ownership rule when the run id is passed in, not read from env."""
    tid_a, run_a = _claimed_task(board, title="other card, explicit")
    conn = board.connect()
    try:
        assert board.complete_task(conn, tid_a, summary="done")
    finally:
        conn.close()

    tid_b, run_b = _claimed_task(board, title="my card, explicit")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    assert run_already_terminal(task_id=tid_b, run_id=run_a) is False
    assert run_already_terminal(task_id=tid_b, run_id=run_b) is False


def test_genuine_silent_exit_still_nudged(board, monkeypatch):
    """Acceptance 2: a run that made no board transition still gets the nudge."""
    tid, run_id = _claimed_task(board, title="narrated exit")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    messages = [
        {"role": "user", "content": "work kanban task"},
        *_tool_pair("kanban_heartbeat"),
        {"role": "assistant", "content": "Let me write the report now."},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert tid in nudge


def test_board_read_failure_fails_open(clear_kanban_env, monkeypatch):
    """An unreadable board must not silence the guard for a real violation."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", "/nonexistent/dir/kanban.db")
    assert run_already_terminal() is False
    assert build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}]) is not None


# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (one attempt per run), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.
