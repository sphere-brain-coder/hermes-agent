"""Turn-end guard for kanban workers.

Kanban workers must end with a terminal board transition: ``kanban_complete``,
``kanban_block``, ``kanban_request_review`` or ``kanban_request_changes``.
Models (especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.

Two independent signals say the run is already over, and either one suppresses
the nudge:

* the recorded state of this worker's own run in the kanban DB (``outcome``
  non-null means the run is closed, whatever ``tasks.status`` says, and the
  card has usually been reassigned to someone else by then);
* a terminal board tool in this session's messages.

The DB check is authoritative and deliberately keys off the run's ``outcome``
rather than ``tasks.status``: after ``kanban_request_changes`` the card is
``ready`` and owned by the implementer, so a status-based check mis-describes
the world and nags a reviewer to "complete" work they just rejected.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Iterable, Optional


logger = logging.getLogger(__name__)


_TERMINAL_KANBAN_TOOLS = frozenset(
    {
        "kanban_complete",
        "kanban_block",
        "kanban_request_review",
        "kanban_request_changes",
    }
)

_DEFAULT_MAX_ATTEMPTS = 2


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
    """True if this conversation already invoked a terminal kanban tool."""
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


def current_run_is_terminal() -> Optional[bool]:
    """Has this worker's own run already been closed in the kanban DB?

    ``True``:  the run row carries an ``outcome``, so some board tool already
               ended it (completed, blocked, review_requested,
               changes_requested, ...). Nothing more is owed.
    ``False``: the run row exists and its ``outcome`` is still NULL, so the run
               is genuinely open.
    ``None``:  unknown (no run pinned in the env, DB missing or unreadable,
               schema older than ``task_runs``). Callers fall back to the
               message scan; never treat unknown as terminal.

    Read-only and best-effort by design: this runs on every turn end and must
    never take a write lock, raise, or hold the worker up.
    """
    raw_run = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    db_path = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not raw_run or not db_path:
        return None
    try:
        run_id = int(raw_run)
    except ValueError:
        return None
    if not os.path.exists(db_path):
        return None
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=2.0
        )
        if task_id:
            row = conn.execute(
                "SELECT outcome FROM task_runs WHERE id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT outcome FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
    except Exception:
        logger.debug("kanban run-state lookup failed", exc_info=True)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if row is None:
        return None
    outcome = row[0]
    return bool(outcome is not None and str(outcome).strip())


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker, the
    run is already closed in the DB, a terminal board tool was already called,
    or the nudge budget is exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if current_run_is_terminal():
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Run for task `{tid}` is still open. Ending now without a board tool "
        "causes a protocol violation (clean exit with no terminal kanban "
        "call).\n\n"
        "Do this immediately in your next response, do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. End the run with exactly ONE of these, whichever matches the work:\n"
        "   - `kanban_complete(summary=..., artifacts=[...])`: the work is done;\n"
        "   - `kanban_block(reason=...)`: a genuine external blocker;\n"
        "   - `kanban_request_review(summary=...)`: done and awaiting review;\n"
        "   - `kanban_request_changes(reason=...)`: you are the reviewer and "
        "the work needs rework.\n\n"
        "Pick the one that is true. Do not call `kanban_complete` on work you "
        "rejected, and do not invent a blocker that does not exist.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "current_run_is_terminal",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
