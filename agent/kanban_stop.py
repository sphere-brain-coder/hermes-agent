"""Turn-end guard for kanban workers.

Kanban workers must end a run with a terminal board tool. Models (especially
GLM / Qwen families) sometimes narrate the next step ("Let me write the report
now") and stop with ``finish_reason=stop`` and no tool calls. Hermes treats
that as a clean exit → ``rc=0`` → dispatcher ``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.

Four tools end a run, not two. ``kanban_complete`` and ``kanban_block`` are
the classic pair, but ``kanban_request_review`` (implementer hands off) and
``kanban_request_changes`` (reviewer returns rework) also close the run row
and move the card off ``running``. Treating those two as non-terminal pushed
a correct worker toward the only verbs the nudge named, and on a review-gated
card both are wrong: ``kanban_complete`` from a reviewer that just rejected
the work records a false approval, and ``kanban_block`` stalls a correctly
queued rework card behind a human unblock.

The message scan is a heuristic over this process's own conversation. The
authoritative answer lives in the board DB: a run with ``task_runs.ended_at``
set, or a task whose ``current_run_id`` is NULL, is over no matter what the
transcript looks like. :func:`kanban_run_is_over` consults that first and the
guard defers to it, so a stale in-memory view can no longer manufacture a
nudge against a finished run.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


#: Board tools that legitimately end a worker run. ``kanban_complete`` and
#: ``kanban_block`` finish the card; ``kanban_request_review`` and
#: ``kanban_request_changes`` hand it to the next lane. All four close the
#: run row, so none of them is a protocol violation.
_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_block",
    "kanban_request_review",
    "kanban_request_changes",
})

#: Run outcomes the dispatcher records for those tools. Kept alongside the
#: tool names so DB-side and transcript-side checks agree on what "terminal"
#: means.
_TERMINAL_RUN_OUTCOMES = frozenset({
    "completed",
    "blocked",
    "review_requested",
    "changes_requested",
})

_DEFAULT_MAX_ATTEMPTS = 2

#: Seconds to wait for the board DB before giving up. The guard runs on the
#: worker's exit path, so it must never hang there; an unavailable DB just
#: falls back to the transcript scan.
_DB_TIMEOUT_SECONDS = 2.0


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool.

    Terminal means any of :data:`_TERMINAL_KANBAN_TOOLS` — completion, block,
    review handoff, or rework return.
    """
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                return True
    return False


def _kanban_db_path() -> Optional[str]:
    """Resolve the board DB file for this worker, or None if unresolvable."""
    pinned = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    if pinned:
        return pinned
    try:
        from hermes_cli.kanban_db import kanban_db_path

        board = (os.environ.get("HERMES_KANBAN_BOARD") or "").strip() or None
        return str(kanban_db_path(board=board))
    except Exception:
        logger.debug("kanban stop-guard: could not resolve board DB", exc_info=True)
        return None


def kanban_run_is_over(task_id: Optional[str] = None) -> Optional[bool]:
    """Ask the board DB whether this worker's run has already ended.

    Returns ``True`` when the run row is closed (``task_runs.ended_at`` is
    set) or the task carries no current run (``tasks.current_run_id IS
    NULL``), ``False`` when a run is demonstrably still open, and ``None``
    when the question cannot be answered (no task id, DB missing, unreadable,
    or a schema without the columns). ``None`` means "unknown", so callers
    must fall back to their own signal rather than assume either answer.

    Read-only by construction: opened with ``mode=ro`` and never written.
    """
    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not tid:
        return None
    path = _kanban_db_path()
    if not path or not os.path.exists(path):
        return None

    import sqlite3
    from urllib.parse import quote

    conn = None
    try:
        uri = f"file:{quote(path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=_DB_TIMEOUT_SECONDS)
        row = conn.execute(
            """
            SELECT t.current_run_id AS current_run_id,
                   r.ended_at       AS ended_at,
                   r.outcome        AS outcome
              FROM tasks t
              LEFT JOIN task_runs r ON r.id = t.current_run_id
             WHERE t.id = ?
            """,
            (tid,),
        ).fetchone()
    except Exception:
        # Locked, missing, WAL sidecar unreadable, older schema — all mean
        # "no authoritative answer", never "the run is still open".
        logger.debug("kanban stop-guard: board DB read failed", exc_info=True)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if row is None:
        # Task not on this board. Unknown, not "running".
        return None
    current_run_id, ended_at, outcome = row[0], row[1], row[2]
    if current_run_id is None:
        return True
    if ended_at is not None:
        return True
    if outcome is not None and str(outcome) in _TERMINAL_RUN_OUTCOMES:
        return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire: not a kanban worker, the
    nudge budget is spent, a terminal board tool was already called
    (completed, blocked, review requested, or changes requested), or the board
    DB says the run row is already closed.
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None
    # The DB outranks the transcript. A closed run row means the worker did
    # transition the board, even if this process cannot see the call.
    if kanban_run_is_over(task_id) is True:
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no terminal kanban "
        "call).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. End the run with exactly one terminal board tool:\n"
        "   - `kanban_complete(summary=..., artifacts=[...])` if the work is "
        "done (on a review-gated board this is the approval verb, so never "
        "use it to record a rejection);\n"
        "   - `kanban_request_review(summary=...)` if you implemented the "
        "change and a reviewer must sign it off;\n"
        "   - `kanban_request_changes(reason=...)` if you are the reviewer "
        "and the work needs rework;\n"
        "   - `kanban_block(reason=...)` only if you are genuinely blocked on "
        "something outside your control.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_run_is_over",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
