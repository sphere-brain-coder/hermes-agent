"""``kanban_block`` must stay usable, and honest, from a review-claimed run.

Two live runs on one card concluded "kanban_block is refused from a
review-claimed run" and fell back to ``kanban_request_changes``, which files a
routing fault as if it were a code verdict. The DB layer actually permits the
block; what those runs hit was a SUPERSEDED run (the dispatcher had already
spawned a newer worker on the same card) behind an error message that names
only "unknown id or not in running/ready".

So the fix AC3 needs is not a new permission, it is a truthful refusal: a
worker must be able to tell "my run was superseded" apart from "this card is
in the wrong state", because the two demand opposite responses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kb


def _handed_off_for_review(kb, conn, *, reviewer: str | None = "reviewer"):
    """A card implemented by ``coder`` and parked in review."""
    tid = kb.create_task(conn, title="deploy gate", assignee="coder")
    impl = kb.claim_task(conn, tid, claimer="coder:1")
    assert kb.request_review(
        conn, tid, summary="PR open, CI green",
        reviewer=reviewer, expected_run_id=impl.current_run_id,
    )
    return tid, impl.current_run_id


def test_block_succeeds_from_a_live_review_claim(board, monkeypatch):
    """AC3: a reviewer with no honest verdict can park the card."""
    kb = board
    conn = kb.connect()
    try:
        tid, _ = _handed_off_for_review(kb, conn)
        review = kb.claim_review_task(conn, tid, claimer="reviewer:1")
        assert review is not None
        run_id = review.current_run_id
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_block({
        "reason": "no eligible reviewer; a human must merge the PR",
        "kind": "needs_input",
    }))
    assert out.get("ok") is True, out
    assert out["status"] == "blocked"


def test_block_from_a_superseded_run_says_so(board, monkeypatch):
    """The refusal must name the real cause: a newer run owns this card.

    Without this, the message reads as "the card is in the wrong state" and a
    worker reasonably concludes review claims forbid blocking at all.
    """
    kb = board
    conn = kb.connect()
    try:
        tid, stale_run = _handed_off_for_review(kb, conn, reviewer=None)
        # The dispatcher spawns a fresh run on the same card; the old worker
        # process is still alive and still believes it owns the task.
        newer = kb.claim_review_task(conn, tid, claimer="coder:2")
        assert newer is not None
        assert newer.current_run_id != stale_run
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(stale_run))
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_block({"reason": "cannot review my own PR"}))
    assert out.get("ok") is not True
    error = out["error"]
    assert "superseded" in error.lower(), error
    # It must point at the run that actually holds the card, and must NOT
    # imply the worker did something wrong with the card's status.
    assert str(newer.current_run_id) in error, error
