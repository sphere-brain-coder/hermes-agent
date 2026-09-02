"""Create-time spawnability validation on the ``kanban_create`` tool.

The CLI gate lives in ``tests/hermes_cli/test_kanban_spawnability.py``. This
module covers the sibling call path: orchestrator agents fan out through the
``kanban_create`` model tool, not through ``hermes kanban create``, so a gate
that only covers the CLI would not have caught the stranded epics that
motivated it.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_spawnability as ksp


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


@pytest.fixture
def only_profile_is_coder(monkeypatch):
    # String target on purpose: other tests in the full suite reload hermes_cli
    # modules, so a module object bound at import time here can be stale by the
    # time kanban_tools does its own import. monkeypatch resolves the string
    # against sys.modules at patch time, which is the same object the code
    # under test will get.
    monkeypatch.setattr(
        "hermes_cli.kanban_spawnability._profiles_on_disk", lambda: ["coder"]
    )


def _titles():
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        return [row["title"] for row in conn.execute("SELECT title FROM tasks")]
    finally:
        conn.close()


def test_create_tool_rejects_a_ghost_assignee(worker_env, only_profile_is_coder):
    """An orchestrator fanning out to a profile that doesn't exist in its lane
    is exactly how the 9-card ghost-assignee epic stranded."""
    from tools import kanban_tools as kt

    out = kt._handle_create({"title": "ghost child", "assignee": "no-such-profile"})

    d = json.loads(out)
    assert d.get("ok") is not True
    assert "no-such-profile" in json.dumps(d)
    assert "ghost child" not in _titles()


def test_create_tool_error_names_the_valid_assignees(worker_env, only_profile_is_coder):
    from tools import kanban_tools as kt

    out = kt._handle_create({"title": "ghost child", "assignee": "no-such-profile"})

    assert "coder" in json.dumps(json.loads(out))


def test_create_tool_accepts_a_profile_on_disk(worker_env, only_profile_is_coder):
    from tools import kanban_tools as kt

    out = kt._handle_create({"title": "real child", "assignee": "coder"})

    assert json.loads(out)["ok"] is True
    assert "real child" in _titles()


def test_create_tool_accepts_an_assignee_already_on_the_board(
    worker_env, only_profile_is_coder
):
    """``test-worker`` is not a profile on disk but owns the parent card, so it
    is a real lane and must stay usable."""
    from tools import kanban_tools as kt

    out = kt._handle_create({"title": "lane child", "assignee": "test-worker"})

    assert json.loads(out)["ok"] is True


def test_create_tool_rejects_pathless_worktree(worker_env, only_profile_is_coder):
    """The 7-card pathless-worktree epic: a worktree card with no path and no
    project loops on spawn_failed forever."""
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "pathless child",
        "assignee": "coder",
        "workspace_kind": "worktree",
    })

    d = json.loads(out)
    assert d.get("ok") is not True
    assert "worktree" in json.dumps(d)
    assert "pathless child" not in _titles()


def test_create_tool_accepts_worktree_with_a_path(
    worker_env, only_profile_is_coder, tmp_path
):
    from tools import kanban_tools as kt

    repo = tmp_path / "repo"
    repo.mkdir()
    out = kt._handle_create({
        "title": "anchored child",
        "assignee": "coder",
        "workspace_kind": "worktree",
        "workspace_path": str(repo),
    })

    assert json.loads(out)["ok"] is True


def test_create_tool_force_allows_a_new_lane(worker_env, only_profile_is_coder):
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "new lane child",
        "assignee": "brand-new-lane",
        "force": True,
    })

    assert json.loads(out)["ok"] is True
