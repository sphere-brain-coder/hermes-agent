"""Create-time spawnability validation for kanban cards.

The dispatcher is deliberately quiet about cards it cannot spawn. A card whose
assignee matches no Hermes profile lands in ``skipped_nonspawnable`` on every
tick, and a ``worktree`` card with neither a path nor an anchor raises inside
``_resolve_worktree_workspace`` and gets re-queued forever. Both are correct
dispatcher behaviour (a crash loop would be worse) but they leave the operator
with a card that silently never runs.

``hermes kanban diagnostics`` already detects both after the fact. This module
moves the same checks to create time so the card is never written in the first
place, which is the only point where the operator is still looking at the
command that caused it.

The checks fail closed but stay escapable: ``force=True`` (``--force`` on the
CLI) bypasses the assignee check so the first card for a brand-new external
lane can still be registered.
"""

from __future__ import annotations

import sqlite3
from typing import Optional


class SpawnabilityError(ValueError):
    """A card configuration that could never be dispatched.

    Subclasses ``ValueError`` so existing callers that already funnel
    ``create_task``'s ``ValueError``s into a usage error keep working.
    """


def _profiles_on_disk() -> list[str]:
    """Return Hermes profile names configured on this host.

    Wrapped in a module-level function (rather than called inline) so tests can
    patch it without touching the filesystem.
    """
    from hermes_cli import kanban_db as kb

    try:
        return list(kb.list_profiles_on_disk())
    except Exception:
        return []


def _board_assignees(conn: sqlite3.Connection) -> list[str]:
    """Return assignee names already present on non-archived cards.

    An assignee that is not a profile on disk but IS already on the board names
    a control-plane lane (an interactive terminal that pulls work via
    ``claim_task``). Those are legitimately non-spawnable by the dispatcher and
    must not be rejected here.
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT assignee FROM tasks "
            "WHERE status != 'archived' AND assignee IS NOT NULL AND assignee != ''"
        ).fetchall()
    except Exception:
        return []
    out: list[str] = []
    for row in rows:
        try:
            name = row["assignee"]
        except Exception:
            name = row[0]
        if name:
            out.append(str(name))
    return out


def known_assignee_names(conn: sqlite3.Connection) -> list[str]:
    """Every assignee a card may legally name: profiles on disk + board lanes.

    Same union ``hermes kanban assignees`` prints, so the error message and the
    discovery command never disagree.
    """
    return sorted({*_profiles_on_disk(), *_board_assignees(conn)})


def validate_assignee(
    conn: sqlite3.Connection,
    assignee: Optional[str],
    *,
    force: bool = False,
) -> None:
    """Raise :class:`SpawnabilityError` when ``assignee`` can never be spawned.

    No-ops when the assignee is empty: an unassigned card has its own
    dispatcher signal (``skipped_unassigned``) and a different operator
    response, so flagging it here would double-report one condition.
    """
    if force:
        return
    name = (assignee or "").strip()
    if not name:
        return

    try:
        from hermes_cli.profiles import normalize_profile_name

        canonical = normalize_profile_name(name)
    except Exception:
        canonical = name.lower()

    known = known_assignee_names(conn)
    if not known:
        # No ground truth to compare against. Profile enumeration reads the
        # filesystem and legitimately returns nothing on a fresh HERMES_HOME
        # or a host where the profiles root is unreadable. Rejecting every
        # assignee there would break card creation outright, so fail OPEN:
        # "can't tell" is not "nobody is valid". The dispatcher's own
        # skipped_nonspawnable path and `kanban diagnostics` still catch the
        # card after the fact, which is exactly the pre-existing behaviour.
        return

    known_canonical = {k.strip().lower() for k in known}
    if canonical.lower() in known_canonical or name.lower() in known_canonical:
        return

    valid = ", ".join(known) if known else "(none — run `hermes -p <name> setup`)"
    raise SpawnabilityError(
        f"assignee {name!r} matches no Hermes profile and no lane on this "
        f"board, so the dispatcher would skip the card forever. "
        f"Valid assignees: {valid}. "
        f"Pass --force to register a card for a new external lane."
    )


def validate_workspace(
    workspace_kind: Optional[str],
    workspace_path: Optional[str],
    *,
    project_id: Optional[str] = None,
    board_default_workdir: Optional[str] = None,
) -> None:
    """Raise :class:`SpawnabilityError` for a workspace that cannot resolve.

    Mirrors the failure conditions in ``kanban_db._resolve_worktree_workspace``
    and ``kanban_db.resolve_workspace``:

    * ``worktree`` with no path, no project to derive one from, and no board
      ``default_workdir`` to anchor on.
    * ``worktree`` / ``dir`` with a relative path (rejected at dispatch as a
      confused-deputy risk).
    * ``dir`` with no path at all.
    """
    kind = (workspace_kind or "scratch").strip() or "scratch"
    path = (workspace_path or "").strip()

    if kind == "worktree":
        if not path:
            if project_id or (board_default_workdir or "").strip():
                return
            raise SpawnabilityError(
                "--workspace worktree needs somewhere to put the worktree: "
                "pass worktree:<absolute-repo-path>, link the card to a repo "
                "with --project, or set a default workdir on the board "
                "(`hermes kanban boards set-workdir <path>`). Without one the "
                "dispatcher cannot resolve a checkout and the card never runs."
            )
        if not _is_absolute(path):
            raise SpawnabilityError(
                f"worktree path {path!r} must be absolute; the dispatcher "
                "rejects relative paths because they would resolve against "
                "its own working directory."
            )
        return

    if kind == "dir":
        if not path:
            raise SpawnabilityError(
                "--workspace dir needs a path: use dir:<absolute-path>."
            )
        if not _is_absolute(path):
            raise SpawnabilityError(
                f"workspace path {path!r} must be absolute; the dispatcher "
                "rejects relative paths because they would resolve against "
                "its own working directory."
            )
        return

    if path and not _is_absolute(path):
        raise SpawnabilityError(
            f"workspace path {path!r} must be absolute."
        )


def _is_absolute(path: str) -> bool:
    import os

    return os.path.isabs(os.path.expanduser(path))


def board_default_workdir(board: Optional[str] = None) -> Optional[str]:
    """Return the board's configured ``default_workdir``, or ``None``.

    Never raises: an unreadable board.json means "no anchor configured", which
    is the conservative answer for a create-time gate.
    """
    from hermes_cli import kanban_db as kb

    try:
        meta = kb.read_board_metadata(board if board else kb.get_current_board())
    except Exception:
        return None
    value = (meta.get("default_workdir") or "").strip()
    return value or None
