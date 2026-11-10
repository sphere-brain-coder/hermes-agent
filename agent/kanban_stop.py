"""Turn-end guard for kanban workers.

Kanban workers must end their run with a terminal board transition
(``kanban_complete``, ``kanban_block``, ``kanban_request_review`` or
``kanban_request_changes``). Models (especially GLM / Qwen families)
sometimes narrate the next step ("Let me write the report now") and stop
with ``finish_reason=stop`` and no tool calls. Hermes treats that as a
clean exit → ``rc=0`` → dispatcher ``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.

Two things keep the nudge honest, both added after it fired at workers that
had already finished correctly (three occurrences on ``t_00352f44``):

* ``kanban_request_review`` / ``kanban_request_changes`` are terminal for a
  run. They end the run exactly like complete/block, so a session that
  called either must never be nudged, and the nudge text names all four.
* Before nudging we ask the board whether this process's run is still open.
  A run whose row already carries an ``outcome`` (or a task whose
  ``current_run_id`` no longer points at a live run) cannot be closed again:
  the only tools the nudge could name would be rejected, so it is suppressed.
  The check reads ``task_runs.outcome``, not ``tasks.status``.

The budget is one nudge per run: a worker that correctly declines must not
be re-prompted in a loop.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional


logger = logging.getLogger(__name__)


# Every tool that ends a worker's run. All four close the run row via
# ``_end_run``; naming only two of them tells a correctly-behaving worker
# it misbehaved and offers it no valid action.
_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_block",
    "kanban_request_review",
    "kanban_request_changes",
})

# One nudge per run. The dispatcher's bounded protocol-violation retry is
# the next layer; re-prompting a worker that already answered just burns
# tokens on a refusal per cycle.
_DEFAULT_MAX_ATTEMPTS = 1


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


def _env_run_id() -> Optional[int]:
    raw = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def run_already_terminal(
    task_id: Optional[str] = None,
    run_id: Optional[int] = None,
) -> bool:
    """True when the board says this worker's run already ended.

    Decided off the run record, not ``tasks.status``:

    * the run row for ``HERMES_KANBAN_RUN_ID`` has a non-null ``outcome``
      or ``ended_at`` → terminal;
    * no run id is known (or its row is gone) and the task's
      ``current_run_id`` is null → the run this process belonged to is no
      longer the active one, so nothing is left for it to close.

    Fails open (returns ``False``) whenever the board cannot be read, so a
    missing or locked DB never silences the guard for a genuine violation.
    """
    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not tid:
        return False
    rid = run_id if run_id is not None else _env_run_id()
    try:
        # Late import: this module is imported from the agent loop in
        # non-kanban contexts too, and the suite reloads hermes_cli modules.
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            if rid is not None:
                run = kb.get_run(conn, rid)
                if run is not None:
                    return bool(run.outcome) or run.ended_at is not None
            task = kb.get_task(conn, tid)
            if task is None:
                return False
            return task.current_run_id is None
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        logger.debug("kanban stop-guard: board read failed", exc_info=True)
        return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire: not a kanban worker, a
    terminal board tool already called this session, the run already recorded
    a terminal outcome on the board, or the one-nudge budget is spent.
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None
    if run_already_terminal(task_id):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running` and its current run has recorded no "
        "outcome. Ending now without a board tool causes a protocol violation "
        "(clean exit with no terminal kanban call).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Make exactly ONE terminal board call, whichever fits:\n"
        "   - `kanban_complete(summary=..., artifacts=[...])`: work is done\n"
        "   - `kanban_block(reason=...)`: externally blocked\n"
        "   - `kanban_request_review(summary=...)`: implemented, needs review\n"
        "   - `kanban_request_changes(reason=...)`: reviewing, sending it back\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "run_already_terminal",
    "session_called_kanban_terminal",
]
