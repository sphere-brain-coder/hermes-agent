"""Create-time spawnability validation for kanban cards.

The dispatcher is deliberately quiet about cards it can never spawn: an
assignee that matches no Hermes profile and no existing board lane lands in
``skipped_nonspawnable`` on every tick, and a pathless ``worktree`` workspace
with no anchor raises inside ``_resolve_worktree_workspace`` and is re-queued
forever. Both were only detectable after the fact via
``hermes kanban diagnostics``. These tests pin the checks at create/assign
time instead.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


def _ksp():
    """Resolve the live kanban_spawnability module.

    Other tests in the suite reload hermes_cli modules, so a module object
    bound at import time here can differ from the one the code under test
    imports. Always go through sys.modules.
    """
    import importlib

    return importlib.import_module("hermes_cli.kanban_spawnability")


class _LiveKsp:
    """Proxy that resolves every attribute against the live module.

    Keeps the readable ``ksp.validate_assignee(...)`` call style in tests while
    guaranteeing the object under test is the same one the production code
    imports, even after an unrelated test reloads hermes_cli modules.
    """

    def __getattr__(self, name):
        return getattr(_ksp(), name)


ksp = _LiveKsp()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    c = kb.connect()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def only_profile_is_coder(monkeypatch):
    # String target on purpose: other tests in the suite reload hermes_cli
    # modules, so a module object bound at import time can be stale by the time
    # the code under test imports it. monkeypatch resolves the string against
    # sys.modules at patch time.
    monkeypatch.setattr(
        "hermes_cli.kanban_spawnability._profiles_on_disk", lambda: ["coder"]
    )


# ---------------------------------------------------------------------------
# Assignee validation
# ---------------------------------------------------------------------------


def test_unknown_assignee_is_rejected_and_error_lists_valid_names(conn, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.kanban_spawnability._profiles_on_disk",
        lambda: ["coder", "researcher"],
    )

    with pytest.raises(ksp.SpawnabilityError) as excinfo:
        ksp.validate_assignee(conn, "codr")

    message = str(excinfo.value)
    assert "codr" in message
    assert "coder" in message
    assert "researcher" in message


def test_assignee_matching_a_profile_on_disk_is_accepted(conn, only_profile_is_coder):
    ksp.validate_assignee(conn, "coder")


def test_assignee_of_an_existing_board_lane_is_accepted(conn, only_profile_is_coder):
    """Control-plane lanes (an ``orion-cc`` terminal) are not profiles on disk
    but are pulled from the board directly, so an assignee already present on
    the board is spawnable in the sense that matters."""
    kb.create_task(conn, title="lane card", assignee="orion-cc")

    ksp.validate_assignee(conn, "orion-cc")


def test_empty_assignee_is_not_a_spawnability_error(conn, only_profile_is_coder):
    """Unassigned cards have their own dispatcher signal (skipped_unassigned)
    and a different operator response; this validator must not double-flag."""
    ksp.validate_assignee(conn, None)
    ksp.validate_assignee(conn, "   ")


def test_unknown_assignee_is_allowed_when_forced(conn, only_profile_is_coder):
    """Registering the first card for a brand-new external lane must stay
    possible, otherwise the check makes new lanes unbootstrappable."""
    ksp.validate_assignee(conn, "brand-new-lane", force=True)


def test_check_fails_open_when_no_assignee_is_discoverable(conn, monkeypatch):
    """With nothing to compare against, the check has no ground truth.

    Profile enumeration reads the filesystem and can legitimately come back
    empty (a fresh HERMES_HOME, a host where the profiles root isn't readable,
    a container). Rejecting every assignee there would break card creation
    entirely, so an empty known-set means "can't tell", not "nobody is valid" —
    the same fail-open stance ``_check_dispatcher_presence`` takes.
    """
    monkeypatch.setattr(
        "hermes_cli.kanban_spawnability._profiles_on_disk", lambda: []
    )

    ksp.validate_assignee(conn, "anything-at-all")


# ---------------------------------------------------------------------------
# Workspace validation
# ---------------------------------------------------------------------------


def test_pathless_worktree_without_project_or_board_default_is_rejected():
    with pytest.raises(ksp.SpawnabilityError) as excinfo:
        ksp.validate_workspace("worktree", None, project_id=None,
                               board_default_workdir=None)

    message = str(excinfo.value)
    assert "worktree" in message
    assert "worktree:<absolute-repo-path>" in message
    assert "--project" in message


def test_worktree_with_explicit_path_is_accepted():
    ksp.validate_workspace("worktree", "/srv/repos/app", project_id=None,
                           board_default_workdir=None)


def test_worktree_with_project_is_accepted():
    ksp.validate_workspace("worktree", None, project_id="proj-1",
                           board_default_workdir=None)


def test_pathless_worktree_is_accepted_when_the_board_has_a_default_workdir():
    """``_resolve_worktree_workspace`` anchors a pathless worktree on the
    board's ``default_workdir``, so rejecting that case would be a false
    positive on a correctly-configured board."""
    ksp.validate_workspace("worktree", None, project_id=None,
                           board_default_workdir="/srv/repos/app")


def test_relative_worktree_path_is_rejected():
    """``_resolve_worktree_workspace`` raises on a non-absolute path at
    dispatch time; catch it at create time instead."""
    with pytest.raises(ksp.SpawnabilityError) as excinfo:
        ksp.validate_workspace("worktree", "repos/app", project_id=None,
                               board_default_workdir=None)

    assert "absolute" in str(excinfo.value)


def test_dir_workspace_without_a_path_is_rejected():
    with pytest.raises(ksp.SpawnabilityError):
        ksp.validate_workspace("dir", None, project_id=None,
                               board_default_workdir=None)


def test_scratch_workspace_needs_no_path():
    ksp.validate_workspace("scratch", None, project_id=None,
                           board_default_workdir=None)


# ---------------------------------------------------------------------------
# `hermes kanban create` wiring
# ---------------------------------------------------------------------------


def _created_titles(conn) -> list[str]:
    return [row["title"] for row in conn.execute("SELECT title FROM tasks")]


def test_create_rejects_a_ghost_assignee_and_writes_no_card(
    kanban_home, only_profile_is_coder
):
    """The 9-card epic stranded on assignee 'coder' in a lane with no such
    profile must fail at the command, not silently on every dispatcher tick."""
    output = kc.run_slash("create 'ghost epic' --assignee no-such-profile")

    assert "no-such-profile" in output
    assert "coder" in output
    with kb.connect_closing() as conn:
        assert "ghost epic" not in _created_titles(conn)


def test_create_accepts_a_known_assignee(kanban_home, only_profile_is_coder):
    output = kc.run_slash("create 'real work' --assignee coder")

    assert "Created" in output
    with kb.connect_closing() as conn:
        assert "real work" in _created_titles(conn)


def test_create_with_force_accepts_an_unknown_lane(kanban_home, only_profile_is_coder):
    output = kc.run_slash("create 'new lane card' --assignee orion-cc --force")

    assert "Created" in output
    with kb.connect_closing() as conn:
        assert "new lane card" in _created_titles(conn)


def test_create_rejects_pathless_worktree_and_writes_no_card(
    kanban_home, only_profile_is_coder
):
    """The 7-card pathless-worktree epic: a bare ``--workspace worktree`` with
    no --project and no board default_workdir can never resolve a checkout."""
    output = kc.run_slash("create 'pathless' --assignee coder --workspace worktree")

    assert "worktree" in output
    with kb.connect_closing() as conn:
        assert "pathless" not in _created_titles(conn)


def test_create_accepts_worktree_with_an_explicit_path(
    kanban_home, only_profile_is_coder, tmp_path
):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = kc.run_slash(
        f"create 'anchored' --assignee coder --workspace worktree:{repo}"
    )

    assert "Created" in output
    with kb.connect_closing() as conn:
        assert "anchored" in _created_titles(conn)


def test_create_exit_code_is_a_usage_error_not_a_crash(
    kanban_home, only_profile_is_coder
):
    args = argparse.Namespace(
        title="ghost", body=None, assignee="no-such-profile", parent=[],
        workspace="scratch", branch=None, project=None, tenant=None, priority=0,
        triage=False, idempotency_key=None, max_runtime=None, created_by="user",
        skills=[], max_retries=None, model_override=None, provider_override=None,
        goal_mode=False, goal_max_turns=None, initial_status="running",
        json=False, force=False,
    )

    assert kc._cmd_create(args) == 2


# ---------------------------------------------------------------------------
# `hermes kanban assign` wiring
# ---------------------------------------------------------------------------


def test_assign_rejects_a_ghost_profile(kanban_home, only_profile_is_coder):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="assignable", assignee="coder")

    output = kc.run_slash(f"assign {tid} no-such-profile")

    assert "no-such-profile" in output
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).assignee == "coder"


def test_assign_accepts_a_known_profile(kanban_home, only_profile_is_coder):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="assignable", assignee=None)

    output = kc.run_slash(f"assign {tid} coder")

    assert "Assigned" in output
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).assignee == "coder"


def test_assign_none_still_unassigns(kanban_home, only_profile_is_coder):
    """'none' is the documented way to clear an assignee; the new check must
    not turn it into an unknown-profile rejection."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="assignable", assignee="coder")

    kc.run_slash(f"assign {tid} none")

    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).assignee is None


# ---------------------------------------------------------------------------
# `hermes kanban swarm` wiring
# ---------------------------------------------------------------------------


def test_swarm_rejects_a_ghost_worker_profile(kanban_home, only_profile_is_coder):
    output = kc.run_slash(
        "swarm 'ship it' --worker no-such-profile:Scan --verifier coder "
        "--synthesizer coder"
    )

    assert "no-such-profile" in output
    with kb.connect_closing() as conn:
        assert _created_titles(conn) == []


def test_swarm_rejects_a_ghost_verifier(kanban_home, only_profile_is_coder):
    output = kc.run_slash(
        "swarm 'ship it' --worker coder:Scan --verifier ghost-reviewer "
        "--synthesizer coder"
    )

    assert "ghost-reviewer" in output
    with kb.connect_closing() as conn:
        assert _created_titles(conn) == []


def test_swarm_accepts_known_profiles(kanban_home, only_profile_is_coder):
    output = kc.run_slash(
        "swarm 'ship it' --worker coder:Scan --verifier coder --synthesizer coder"
    )

    assert "Swarm root:" in output
