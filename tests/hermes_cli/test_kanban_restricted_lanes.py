"""Restricted lanes: a lane's per-task artifacts stay private on a shared board.

A shared board root exists so several OS identities can dispatch against one
board. The cost is that every per-task artifact the dispatcher creates
inherits its umask — under a 002 umask on a 2775 root, a worker's workspace,
its log (the whole stdout+stderr capture of the run, tool output included)
and its attachments are readable by every identity in the group.

These tests pin the fix and, just as importantly, its boundaries: the shared
parents stay traversable, ``dir:`` workspaces are left alone, and kanban.db is
not covered.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure the worktree (not a stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="POSIX mode bits are the mechanism under test"
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _task(**kw):
    base: dict[str, Any] = dict(
        id="t_restricted",
        title="draft",
        body=None,
        assignee="paris-board",
        status="running",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )
    base.update(kw)
    return kb.Task(**base)


@pytest.fixture
def lanes(monkeypatch):
    """Declare ``paris-*`` restricted via the env override."""
    monkeypatch.setenv(kb.RESTRICTED_LANES_ENV, "paris-board, paris-fo")
    return None


@pytest.fixture
def no_lanes(monkeypatch):
    monkeypatch.setenv(kb.RESTRICTED_LANES_ENV, "")
    return None


# ---------------------------------------------------------------------------
# Pattern resolution
# ---------------------------------------------------------------------------

class TestLaneMatching:
    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv(kb.RESTRICTED_LANES_ENV, "alpha,beta")
        assert kb.restricted_lane_patterns() == ["alpha", "beta"]

    def test_env_empty_string_disables(self, monkeypatch):
        """An explicitly empty env var means off, not 'fall back to config'."""
        monkeypatch.setenv(kb.RESTRICTED_LANES_ENV, "")
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"kanban": {"restricted_lane_assignees": ["paris-board"]}},
        )
        assert kb.restricted_lane_patterns() == []
        assert kb.is_restricted_lane("paris-board") is False

    def test_config_used_when_env_unset(self, monkeypatch):
        monkeypatch.delenv(kb.RESTRICTED_LANES_ENV, raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"kanban": {"restricted_lane_assignees": ["paris-*"]}},
        )
        assert kb.is_restricted_lane("paris-fo") is True

    def test_glob_and_case(self, monkeypatch):
        monkeypatch.setenv(kb.RESTRICTED_LANES_ENV, "paris-*")
        assert kb.is_restricted_lane("PARIS-Board") is True
        assert kb.is_restricted_lane("paris-brain") is True
        assert kb.is_restricted_lane("coder") is False

    def test_unconfigured_matches_nothing(self, no_lanes):
        assert kb.is_restricted_lane("paris-board") is False
        assert kb.is_restricted_lane(None) is False

    def test_broken_config_does_not_raise(self, monkeypatch):
        monkeypatch.delenv(kb.RESTRICTED_LANES_ENV, raising=False)

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("hermes_cli.config.load_config", _boom)
        assert kb.restricted_lane_patterns() == []


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

@posix_only
class TestWorkspace:
    def test_scratch_workspace_is_private(self, lanes, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "ws"))
        ws = kb.resolve_workspace(_task())
        assert _mode(ws) == 0o700

    def test_existing_group_readable_workspace_is_rehardened(
        self, lanes, tmp_path, monkeypatch
    ):
        """``mkdir(exist_ok=True)`` does not chmod — the exposure this fixes.

        A workspace created by an earlier run (or before the lane was
        restricted) must be tightened on the next resolve, not left as-is.
        """
        root = tmp_path / "ws"
        stale = root / "t_restricted"
        stale.mkdir(parents=True)
        os.chmod(stale, 0o775)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(root))

        ws = kb.resolve_workspace(_task())

        assert ws == stale
        assert _mode(ws) == 0o700

    def test_ordinary_lane_workspace_unchanged(self, lanes, tmp_path, monkeypatch):
        """A Sphere-lane task keeps whatever the umask gives it."""
        root = tmp_path / "ws"
        existing = root / "t_restricted"
        existing.mkdir(parents=True)
        os.chmod(existing, 0o775)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(root))

        ws = kb.resolve_workspace(_task(assignee="paris-brain"))

        assert _mode(ws) == 0o775

    def test_shared_parent_stays_traversable(self, lanes, tmp_path, monkeypatch):
        """Only the per-task dir is restricted.

        The shared ``workspaces/`` parent must stay group-writable or other
        identities' dispatchers can no longer create their own tasks' dirs
        under the same root.
        """
        root = tmp_path / "ws"
        root.mkdir()
        os.chmod(root, 0o2775)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(root))

        kb.resolve_workspace(_task())

        assert _mode(root) & 0o070 == 0o070

    def test_dir_workspace_left_alone(self, lanes, tmp_path):
        """An operator-chosen ``dir:`` workspace may be shared on purpose."""
        shared = tmp_path / "shared-checkout"
        shared.mkdir()
        os.chmod(shared, 0o775)

        ws = kb.resolve_workspace(
            _task(workspace_kind="dir", workspace_path=str(shared))
        )

        assert _mode(ws) == 0o775


@posix_only
def test_harden_reports_a_filesystem_that_ignores_modes(lanes, tmp_path, monkeypatch):
    """chmod is verified, not trusted.

    On a filesystem that ignores mode bits the chmod call succeeds and
    changes nothing. Silently continuing there would leave the lane exposed
    while looking fixed, so it must raise.
    """
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setattr(kb.os, "chmod", lambda *a, **k: None)

    with pytest.raises(kb.RestrictedLaneError, match="does not enforce POSIX"):
        kb.harden_private_path(target, is_dir=True)


# ---------------------------------------------------------------------------
# Worker logs — the half a workspace-only fix leaves behind
# ---------------------------------------------------------------------------

@posix_only
class TestWorkerLog:
    @pytest.fixture
    def spawn(self, tmp_path, monkeypatch):
        """Run ``_default_spawn`` against a fake Popen; return the log dir."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        os.chmod(log_dir, 0o2775)

        class _Proc:
            pid = 4321

        monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: _Proc())
        monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda _root: None)
        monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: log_dir)
        return log_dir

    def test_log_is_private_for_a_restricted_lane(self, lanes, spawn, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        kb._default_spawn(_task(), str(ws))
        assert _mode(spawn / "t_restricted.log") == 0o600

    def test_existing_group_readable_log_is_rehardened(self, lanes, spawn, tmp_path):
        """The 664 log left by earlier runs is the ongoing half of the leak."""
        stale = spawn / "t_restricted.log"
        stale.write_bytes(b"prior transcript\n")
        os.chmod(stale, 0o664)

        ws = tmp_path / "ws"
        ws.mkdir()
        kb._default_spawn(_task(), str(ws))

        assert _mode(stale) == 0o600
        # Appended to, not clobbered: re-runs on unblock keep their history.
        assert stale.read_bytes().startswith(b"prior transcript")

    def test_rotated_generation_is_hardened_too(self, lanes, spawn, tmp_path):
        rotated = spawn / "t_restricted.log.1"
        rotated.write_bytes(b"older transcript\n")
        os.chmod(rotated, 0o664)

        ws = tmp_path / "ws"
        ws.mkdir()
        kb._default_spawn(_task(), str(ws))

        assert _mode(rotated) == 0o600

    def test_ordinary_lane_log_unchanged(self, lanes, spawn, tmp_path):
        existing = spawn / "t_restricted.log"
        existing.write_bytes(b"x")
        os.chmod(existing, 0o664)

        ws = tmp_path / "ws"
        ws.mkdir()
        kb._default_spawn(_task(assignee="paris-brain"), str(ws))

        assert _mode(existing) == 0o664

    def test_dispatcher_pins_its_own_lane_policy_into_worker_env(
        self, lanes, spawn, tmp_path, monkeypatch
    ):
        """The worker must not re-decide this from its own profile config."""
        captured = {}

        class _Proc:
            pid = 99

        def _fake_popen(cmd, **kwargs):
            captured["env"] = kwargs["env"]
            return _Proc()

        monkeypatch.setattr("subprocess.Popen", _fake_popen)
        ws = tmp_path / "ws"
        ws.mkdir()

        kb._default_spawn(_task(), str(ws))

        assert (
            captured["env"][kb.RESTRICTED_LANES_ENV] == "paris-board,paris-fo"
        )


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

@posix_only
class TestAttachments:
    def test_attachment_dir_is_private(self, lanes, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
        monkeypatch.setenv(
            "HERMES_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "attachments")
        )
        with kb.connect_closing() as conn:
            task_id = kb.create_task(conn, title="draft", assignee="paris-board")
            kb.store_attachment_bytes(conn, task_id, "note.txt", b"content")
            dest = kb.task_attachments_dir(task_id)
        assert _mode(dest) == 0o700

    def test_ordinary_lane_attachment_dir_unchanged(
        self, lanes, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
        root = tmp_path / "attachments"
        monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(root))
        with kb.connect_closing() as conn:
            task_id = kb.create_task(conn, title="draft", assignee="paris-brain")
            dest = kb.task_attachments_dir(task_id)
            dest.mkdir(parents=True)
            os.chmod(dest, 0o775)
            kb.store_attachment_bytes(conn, task_id, "note.txt", b"content")
        assert _mode(dest) == 0o775
