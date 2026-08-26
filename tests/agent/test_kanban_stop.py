"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import sqlite3

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_run_is_over,
    kanban_stop_nudge_enabled,
    kanban_task_assignee,
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
    assignee=None,
):
    """Minimal board DB with the columns the guard reads."""
    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks ("
        "  id TEXT PRIMARY KEY, current_run_id INTEGER, assignee TEXT)"
    )
    conn.execute(
        "CREATE TABLE task_runs ("
        "  id INTEGER PRIMARY KEY, task_id TEXT, ended_at INTEGER, outcome TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks (id, current_run_id, assignee) VALUES (?, ?, ?)",
        (task_id, current_run_id, assignee),
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


# ── The nudge must not assert a status it did not read ───────────────
# t_3139ebfa: kanban_run_is_over() is three-valued but the nudge text was
# not. On None (DB missing, locked, foreign board) the guard still opened
# with a hardcoded "is still `running`", which is the same false assertion
# the guard was fixed to stop making.


def test_nudge_does_not_claim_running_when_board_unreadable(
    clear_kanban_env, tmp_path
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_3139ebfa")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(tmp_path / "nope.db"))
    assert kanban_run_is_over() is None
    nudge = build_kanban_stop_nudge(messages=[{"role": "user", "content": "hi"}])
    assert nudge is not None, "unknown board state must still nudge"
    assert "is still `running`" not in nudge
    assert "could not read" in nudge.lower()


def test_nudge_still_says_running_when_db_confirms_a_live_run(
    clear_kanban_env, tmp_path
):
    """A DB-confirmed open run is the one case where `running` is a fact."""
    db = _board_db(tmp_path, "t_live", current_run_id=83, assignee="coder")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_live")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    clear_kanban_env.setenv("HERMES_PROFILE", "coder")
    assert kanban_run_is_over() is False
    nudge = build_kanban_stop_nudge(messages=[{"role": "user", "content": "hi"}])
    assert nudge is not None
    assert "is still `running`" in nudge


# ── The remedy must not offer a verb the worker does not own ─────────
# t_3139ebfa point 3: on t_8add7d0c and t_003e373f the card was already
# assigned to `cooper` when the nudge fired, and the remedy list still led
# with `kanban_complete`. Under the never-self-promote rule that is the one
# verb the implementer must not call, so suggesting it first points the
# pressure at the unsafe action.


def test_assignee_read_from_board(clear_kanban_env, tmp_path):
    db = _board_db(tmp_path, "t_handed_off", current_run_id=80, assignee="cooper")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_handed_off")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    assert kanban_task_assignee() == "cooper"


def test_assignee_is_none_when_board_unreadable(clear_kanban_env, tmp_path):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(tmp_path / "nope.db"))
    assert kanban_task_assignee() is None


def test_nudge_drops_complete_when_card_assigned_elsewhere(
    clear_kanban_env, tmp_path
):
    db = _board_db(tmp_path, "t_handed_off", current_run_id=80, assignee="cooper")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_handed_off")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    clear_kanban_env.setenv("HERMES_PROFILE", "coder")
    nudge = build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}])
    assert nudge is not None
    # Not offered as a remedy. Naming it in the negative is fine, and safer
    # than silence, but it must not appear as something to call.
    assert "`kanban_complete(" not in nudge
    assert "not yours to call" in nudge
    assert "cooper" in nudge
    for verb in ("kanban_request_review", "kanban_request_changes", "kanban_block"):
        assert verb in nudge


def test_nudge_keeps_complete_when_worker_owns_the_card(clear_kanban_env, tmp_path):
    db = _board_db(tmp_path, "t_mine", current_run_id=80, assignee="coder")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_mine")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    clear_kanban_env.setenv("HERMES_PROFILE", "coder")
    nudge = build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}])
    assert nudge is not None
    assert "kanban_complete" in nudge


def test_nudge_keeps_complete_when_ownership_unknown(clear_kanban_env, tmp_path):
    """Unknown assignee must not silently remove a legitimate verb."""
    db = _board_db(tmp_path, "t_anon", current_run_id=80, assignee=None)
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_anon")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    clear_kanban_env.setenv("HERMES_PROFILE", "coder")
    assert kanban_task_assignee() is None
    nudge = build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}])
    assert nudge is not None
    assert "kanban_complete" in nudge


def test_nudge_keeps_complete_when_worker_identity_unknown(
    clear_kanban_env, tmp_path
):
    """No HERMES_PROFILE means we cannot say the card is not ours."""
    db = _board_db(tmp_path, "t_handed_off", current_run_id=80, assignee="cooper")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_handed_off")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    clear_kanban_env.delenv("HERMES_PROFILE", raising=False)
    nudge = build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}])
    assert nudge is not None
    assert "kanban_complete" in nudge


def test_legacy_schema_without_assignee_still_answers_run_state(
    clear_kanban_env, tmp_path
):
    """Adding the assignee column must not break boards that lack it.

    The run-state answer comes from tasks/task_runs; a board too old to have
    tasks.assignee should still get a definite answer there, and simply an
    unknown assignee.
    """
    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, current_run_id INTEGER)")
    conn.execute(
        "CREATE TABLE task_runs ("
        "  id INTEGER PRIMARY KEY, task_id TEXT, ended_at INTEGER, outcome TEXT)"
    )
    conn.execute("INSERT INTO tasks (id, current_run_id) VALUES ('t_legacy', NULL)")
    conn.commit()
    conn.close()
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_legacy")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(path))
    assert kanban_run_is_over() is True
    assert kanban_task_assignee() is None


def test_nudge_opens_the_board_once(clear_kanban_env, tmp_path, monkeypatch):
    """The guard runs on the worker's exit path; one DB open, not two."""
    import agent.kanban_stop as ks

    db = _board_db(tmp_path, "t_handed_off", current_run_id=80, assignee="cooper")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_handed_off")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(db))
    clear_kanban_env.setenv("HERMES_PROFILE", "coder")

    calls = []
    real = ks._read_task_row

    def counting(task_id=None):
        calls.append(task_id)
        return real(task_id)

    monkeypatch.setattr(ks, "_read_task_row", counting)
    assert ks.build_kanban_stop_nudge(messages=[{"role": "user", "content": "go"}])
    assert len(calls) == 1, f"expected one board read, got {len(calls)}"


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
