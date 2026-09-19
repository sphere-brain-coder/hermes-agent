"""The dispatcher must never spawn a card's implementer as its own reviewer.

Observed twice on a live board: a worker requested review with no ``reviewer``
(because the intended human reviewer has no worker profile), the task stayed
assigned to its implementer, and the review lane dispatched that same profile
back onto the card. Every terminal action then available to that run is
dishonest: approving is self-review, completing promotes unmerged work, and
``request_changes`` files a routing problem as a code verdict.

These tests pin the three halves of the fix:

* the review lane identifies the implementer from the run that produced the
  ``review_requested`` event, not from ``task.assignee``, and refuses to spawn
  that profile as reviewer;
* a card with no eligible reviewer stays in ``review`` and is surfaced as
  needing a human rather than silently re-queued; and
* a review-claimed run can still park the card with ``kanban_block`` instead of
  being forced into a code verdict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def review_dispatch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every assignee spawnable, review dispatch enabled."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        cfgmod,
        "load_config",
        lambda *args, **kwargs: {"kanban": {"review_dispatch": True}},
    )


def _events(conn, task_id: str, kind: str):
    return [event for event in kb.list_events(conn, task_id) if event.kind == kind]


def _implementer_requested_review(conn, *, title: str, profile: str) -> str:
    """Create a card, run it as ``profile``, hand it off with no reviewer."""
    task_id = kb.create_task(conn, title=title, assignee=profile)
    implementation = kb.claim_task(conn, task_id, claimer=f"{profile}:1")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="PR open, awaiting a human merge",
        expected_run_id=implementation.current_run_id,
    )
    assert kb.get_task(conn, task_id).status == "review"
    return task_id


def test_review_lane_does_not_spawn_the_implementer_as_reviewer(
    kanban_home: Path, review_dispatch_on: None
) -> None:
    """AC1/AC4: profile X requests review with no reviewer; X is not spawned."""
    with kb.connect() as conn:
        task_id = _implementer_requested_review(
            conn, title="self-review loop", profile="coder"
        )

        result = kb.dispatch_once(conn, dry_run=True)

        assert task_id not in [spawn[0] for spawn in result.spawned]
        assert kb.get_task(conn, task_id).status == "review"


def test_implementer_is_read_from_the_review_requested_run_not_the_assignee(
    kanban_home: Path, review_dispatch_on: None
) -> None:
    """AC1: the guard must not be fooled by a later assignee change.

    Reassigning the card to a different profile after the handoff does not make
    the original implementer eligible; conversely a card whose assignee happens
    to equal the implementer only because of the fallback is still caught.
    """
    with kb.connect() as conn:
        task_id = _implementer_requested_review(
            conn, title="assignee moved after handoff", profile="coder"
        )
        # A board operator points the card at the implementer again by hand.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = 'coder' WHERE id = ?", (task_id,)
            )

        assert kb.review_assignee_is_implementer(conn, task_id) is True

        # Handing it to a genuinely different profile clears the guard.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = 'reviewer' WHERE id = ?", (task_id,)
            )
        assert kb.review_assignee_is_implementer(conn, task_id) is False

        result = kb.dispatch_once(conn, dry_run=True)
        assert task_id in [spawn[0] for spawn in result.spawned]


def test_card_with_no_eligible_reviewer_is_surfaced_for_a_human(
    kanban_home: Path, review_dispatch_on: None
) -> None:
    """AC2: stays in review, recorded once as needing a human reviewer."""
    with kb.connect() as conn:
        task_id = _implementer_requested_review(
            conn, title="needs a human reviewer", profile="coder"
        )

        first = kb.dispatch_once(conn)
        assert task_id in first.needs_human_reviewer
        assert kb.get_task(conn, task_id).status == "review"
        assert len(_events(conn, task_id, "review_needs_human")) == 1

        # A second tick must not spam the event log; the card is still parked.
        second = kb.dispatch_once(conn)
        assert task_id in second.needs_human_reviewer
        assert len(_events(conn, task_id, "review_needs_human")) == 1
        assert kb.get_task(conn, task_id).status == "review"


def test_dispatch_cli_surfaces_cards_awaiting_a_human_reviewer(
    kanban_home: Path, review_dispatch_on: None, capsys
) -> None:
    """AC2: a board operator must see the parked card, not silence."""
    import argparse

    from hermes_cli import kanban as kanban_cli

    with kb.connect() as conn:
        task_id = _implementer_requested_review(
            conn, title="awaiting a human", profile="coder"
        )

    args = argparse.Namespace(
        json=False, dry_run=True, max_spawn=None, board=None,
        max_in_progress=None, max_in_progress_per_profile=None,
        ttl_seconds=None, failure_limit=None, default_assignee=None,
    )
    kanban_cli._cmd_dispatch(args)
    text = capsys.readouterr().out
    assert task_id in text
    assert "human reviewer" in text.lower()
    assert "implementer" in text.lower()


def test_a_review_claimed_run_can_park_the_card_with_block(
    kanban_home: Path, review_dispatch_on: None
) -> None:
    """AC3: kanban_block works from a review claim, and returns to review."""
    with kb.connect() as conn:
        task_id = _implementer_requested_review(
            conn, title="reviewer escalates", profile="coder"
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = 'reviewer' WHERE id = ?", (task_id,)
            )
        review = kb.claim_review_task(conn, task_id, claimer="reviewer:1")
        assert review is not None

        assert kb.block_task(
            conn,
            task_id,
            reason="no eligible reviewer; a human must merge the PR",
            kind="needs_input",
            expected_run_id=review.current_run_id,
        )
        blocked = kb.get_task(conn, task_id)
        assert blocked.status == "blocked"
        assert _events(conn, task_id, "blocked")[-1].payload["source_status"] == "review"

        assert kb.unblock_task(conn, task_id)
        assert kb.get_task(conn, task_id).status == "review"
