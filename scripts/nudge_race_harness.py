#!/usr/bin/env python3
"""Compare candidate ``agent/kanban_stop.py`` implementations against the
dispatcher race that the stop-guard nudge misfires into.

Why this exists
---------------
``tests/agent/test_kanban_stop.py`` pins the correct behaviour for whichever
implementation is on the branch. This harness answers a different question:
given several competing candidate implementations living on different
branches, which of them actually survive the race, and which only look
correct because the conversation transcript still holds the terminal tool
call?

The scenarios are built by driving the real ``hermes_cli.kanban_db``
lifecycle functions against a real SQLite board, not by hand-writing rows.
That matters: the race only appears once the dispatcher has claimed the
*next* run while the previous worker's turn is still unwinding, and a
hand-built fixture sets ``tasks.current_run_id`` to NULL exactly when an
``outcome`` exists, which is the one combination that hides the bug.

Usage
-----
Compare every candidate branch against pre-fix ``main``::

    python3 scripts/nudge_race_harness.py \
        --candidate main=origin/main \
        --candidate scoped=origin/agent/t_5eb60eba-nudge-run-outcome-scoped

Each ``--candidate`` is ``label=<git-ref>``; the harness extracts
``agent/kanban_stop.py`` from that ref. ``label=/path/to/kanban_stop.py``
loads a file from disk instead, which is how the mutation check below
feeds in deliberately broken variants.

With no ``--candidate`` the working tree's own ``agent/kanban_stop.py`` is
used, labelled ``worktree``.

``--mutation-check`` additionally applies a fixed set of textual mutations
to the working tree's module and asserts each one is caught by at least one
scenario. A mutation that nothing catches means the scenarios are not
actually constraining that part of the implementation.

Exit status is 0 only when every candidate marked ``--expect-pass`` passes
every scenario, and (with ``--mutation-check``) every mutation whose
expectation is ``caught`` was in fact caught.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# The repo root is resolved from THIS FILE's own location, so the harness
# works from any clone or worktree and keeps working after the directory it
# was first written in is deleted. Never hardcode a scratch path here.
REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_REL_PATH = "agent/kanban_stop.py"


# ─────────────────────────── candidate loading ───────────────────────────


def _module_source(spec: str) -> tuple[str, str]:
    """Return (label, source) for a ``label=ref-or-path`` candidate spec."""
    label, _, target = spec.partition("=")
    if not target:
        label, target = Path(spec).stem, spec
    as_path = Path(target)
    if as_path.exists() and as_path.is_file():
        return label, as_path.read_text(encoding="utf-8")
    out = subprocess.run(
        ["git", "show", f"{target}:{MODULE_REL_PATH}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(
            f"cannot read {MODULE_REL_PATH} from {target!r}: {out.stderr.strip()}"
        )
    return label, out.stdout


def _load_module(label: str, source: str) -> types.ModuleType:
    """Import a candidate's source as a standalone module."""
    tmp = Path(tempfile.mkdtemp(prefix="nudge_cand_")) / "kanban_stop.py"
    tmp.write_text(source, encoding="utf-8")
    name = f"_nudge_candidate_{abs(hash(label))}"
    spec = importlib.util.spec_from_file_location(name, tmp)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────────── board scenarios ────────────────────────────


@dataclass
class Scenario:
    """One board state, plus what a correct guard must do in it."""

    name: str
    why: str
    expect_nudge: bool
    build: Callable[[Path], dict]
    # Message transcript handed to the guard. Deliberately NON-terminal: the
    # point is to test the DB check, not the transcript scan.
    messages: list = field(default_factory=list)
    # When set, the scenario also fails if the guard left anything new on
    # disk. The guard runs on EVERY turn end, so it must be read-only.
    forbid_side_effects: bool = False


def _tool_round(name: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": name, "arguments": "{}"}}
            ],
        },
        {"role": "tool", "name": name, "tool_call_id": "1", "content": "ok"},
    ]


def _board(tmp: Path):
    """A real kanban board, initialised by the real lifecycle code."""
    sys.path.insert(0, str(REPO_ROOT))
    from hermes_cli import kanban_db as kb

    for var in ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_BOARD"):
        os.environ.pop(var, None)
    os.environ["HERMES_HOME"] = str(tmp / ".hermes")
    kb._INITIALIZED_PATHS.clear()
    db = tmp / "kanban.db"
    return kb, kb.connect(db_path=db), db


def _reviewer_terminal(tmp: Path, *, respawn: bool) -> dict:
    """review_requested -> changes_requested, optionally respawning the next run.

    With ``respawn`` the dispatcher has already claimed a new run under the
    implementer, so ``tasks.status`` is legitimately ``running`` again, for a
    different worker's run. That is the reported race.
    """
    kb, conn, db = _board(tmp)
    tid = kb.create_task(conn, title="race", assignee="coder")
    impl = kb.claim_task(conn, tid, claimer="coder:1")
    assert kb.request_review(
        conn, tid, summary="done", reviewer="reviewer", expected_run_id=impl.current_run_id
    )
    rev = kb.claim_review_task(conn, tid, claimer="reviewer:1")
    reviewer_run = rev.current_run_id
    ok, _ = kb.request_changes(
        conn, tid, reason="defect on line 147", expected_run_id=reviewer_run
    )
    assert ok
    if respawn:
        nxt = kb.claim_task(conn, tid, claimer="coder:2")
        assert nxt is not None and nxt.current_run_id != reviewer_run
    task = kb.get_task(conn, tid)
    conn.close()
    return {"task": tid, "run": reviewer_run, "db": db, "snapshot": _snapshot(db, task)}


def _implementer_terminal(tmp: Path) -> dict:
    """The handoff side: request_review landed, the reviewer already claimed."""
    kb, conn, db = _board(tmp)
    tid = kb.create_task(conn, title="handoff", assignee="coder")
    impl = kb.claim_task(conn, tid, claimer="coder:1")
    impl_run = impl.current_run_id
    assert kb.request_review(
        conn, tid, summary="done", reviewer="reviewer", expected_run_id=impl_run
    )
    kb.claim_review_task(conn, tid, claimer="reviewer:1")
    task = kb.get_task(conn, tid)
    conn.close()
    return {"task": tid, "run": impl_run, "db": db, "snapshot": _snapshot(db, task)}


def _own_run_open(tmp: Path) -> dict:
    """Control: this worker's own run is genuinely open. Must still nudge."""
    kb, conn, db = _board(tmp)
    tid = kb.create_task(conn, title="working", assignee="coder")
    mine = kb.claim_task(conn, tid, claimer="coder:1")
    task = kb.get_task(conn, tid)
    conn.close()
    return {"task": tid, "run": mine.current_run_id, "db": db, "snapshot": _snapshot(db, task)}


def _stale_run_id(tmp: Path) -> dict:
    """Control: RUN_ID names a CLOSED run belonging to a DIFFERENT task.

    A guard that looks a run up by id alone sees ``outcome`` set and goes
    quiet, disarming itself for a worker whose own run is wide open. That is
    the reported bug inverted, and it must not pass.
    """
    kb, conn, db = _board(tmp)
    other = kb.create_task(conn, title="other card", assignee="coder")
    o = kb.claim_task(conn, other, claimer="coder:1")
    stale_run = o.current_run_id
    assert kb.complete_task(conn, other, summary="done", expected_run_id=stale_run)

    mine = kb.create_task(conn, title="mine", assignee="coder")
    kb.claim_task(conn, mine, claimer="coder:2")
    task = kb.get_task(conn, mine)
    conn.close()
    # Env points at the OTHER task's closed run while we work on `mine`.
    return {"task": mine, "run": stale_run, "db": db, "snapshot": _snapshot(db, task)}


def _board_unreadable(tmp: Path) -> dict:
    """Control: the DB path does not exist. The guard must fail OPEN."""
    kb, conn, db = _board(tmp)
    tid = kb.create_task(conn, title="working", assignee="coder")
    mine = kb.claim_task(conn, tid, claimer="coder:1")
    task = kb.get_task(conn, tid)
    conn.close()
    return {
        "task": tid,
        "run": mine.current_run_id,
        "db": tmp / "does-not-exist.db",
        "snapshot": _snapshot(db, task),
    }


def _readonly_board(tmp: Path) -> dict:
    """Control: the board is on read-only storage, own run open.

    The guard runs on every turn end, so it must never need a write lock.
    A candidate that opens the board read-write (or lets the connect path
    run migrations) raises here, and whether that failure is swallowed into
    "terminal" or "not terminal" decides whether a genuine violation still
    gets caught. The correct answer is to nudge.
    """
    import stat

    kb, conn, db = _board(tmp)
    tid = kb.create_task(conn, title="working", assignee="coder")
    mine = kb.claim_task(conn, tid, claimer="coder:1")
    task = kb.get_task(conn, tid)
    conn.close()
    snapshot = _snapshot(db, task)
    ro = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
    for extra in ("-wal", "-shm", ""):
        p = Path(str(db) + extra)
        if p.exists():
            p.chmod(ro)
    # A read-only *directory* is what actually blocks journal creation.
    Path(tmp).chmod(stat.S_IRUSR | stat.S_IXUSR)
    return {
        "task": tid,
        "run": mine.current_run_id,
        "db": db,
        "snapshot": snapshot,
        "restore": tmp,
    }


def _no_run_id_pinned(tmp: Path) -> dict:
    """Control: no RUN_ID in the env while our own run is open.

    Nothing in the board says this worker is finished, so the guard must
    fire. A candidate that treats "cannot identify my run" as "nothing left
    to do" silently disarms itself on every worker whose env is missing the
    pin.
    """
    kb, conn, db = _board(tmp)
    tid = kb.create_task(conn, title="working", assignee="coder")
    kb.claim_task(conn, tid, claimer="coder:1")
    task = kb.get_task(conn, tid)
    conn.close()
    return {"task": tid, "run": None, "db": db, "snapshot": _snapshot(db, task)}


def _missing_board_path(tmp: Path) -> dict:
    """Control: HERMES_KANBAN_DB names a board file that does not exist.

    Real cause, not a contrivance: a worker's env can name a board that was
    renamed, archived, or lives on a path this process cannot see. The
    correct response is to nudge (fail open) and touch nothing.

    This scenario also sets ``forbid_side_effects``. A guard that reaches
    the board through the *initialising* connect path will CREATE a full
    board at that path instead of reading one, on every turn end, for every
    worker. That is a silent write from a read-only check, and it seeds
    empty boards that later readers can mistake for real ones.
    """
    kb, conn, db = _board(tmp / "real")
    tid = kb.create_task(conn, title="working", assignee="coder")
    mine = kb.claim_task(conn, tid, claimer="coder:1")
    task = kb.get_task(conn, tid)
    conn.close()
    probe = tmp / "probe"
    probe.mkdir()
    return {
        "task": tid,
        "run": mine.current_run_id,
        "db": probe / "kanban.db",
        "snapshot": _snapshot(db, task),
        "watch": probe,
    }


def _snapshot(db: Path, task) -> str:
    import sqlite3

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        runs = conn.execute(
            "SELECT id, task_id, outcome, ended_at FROM task_runs ORDER BY id"
        ).fetchall()
    except Exception:
        runs = []
    finally:
        conn.close()
    lines = [
        f"task status={task.status!r} current_run_id={task.current_run_id} "
        f"assignee={task.assignee!r}"
    ]
    for rid, tid, outcome, ended in runs:
        lines.append(
            f"  run {rid} task={tid} outcome={outcome!r} ended={'yes' if ended else 'no'}"
        )
    return "\n".join(lines)


SCENARIOS = [
    Scenario(
        "reviewer_terminal_no_respawn",
        "reviewer's run closed (changes_requested), card ready, no new run",
        expect_nudge=False,
        build=lambda t: _reviewer_terminal(t, respawn=False),
        messages=_tool_round("kanban_request_changes"),
    ),
    Scenario(
        "reviewer_terminal_RESPAWNED",
        "reviewer run closed; the NEXT run is already live under coder",
        expect_nudge=False,
        build=lambda t: _reviewer_terminal(t, respawn=True),
        messages=_tool_round("kanban_request_changes"),
    ),
    Scenario(
        "respawned_db_only",
        "same race, but the transcript no longer holds the terminal call",
        expect_nudge=False,
        build=lambda t: _reviewer_terminal(t, respawn=True),
        messages=_tool_round("kanban_comment"),
    ),
    Scenario(
        "implementer_review_requested",
        "implementer run closed (review_requested); reviewer run live",
        expect_nudge=False,
        build=_implementer_terminal,
        messages=_tool_round("kanban_comment"),
    ),
    Scenario(
        "control_genuine_violation",
        "own run open, no terminal call: the guard MUST still fire",
        expect_nudge=True,
        build=_own_run_open,
        messages=_tool_round("kanban_comment"),
    ),
    Scenario(
        "control_stale_run_id",
        "RUN_ID points at another task's closed run; own run is open",
        expect_nudge=True,
        build=_stale_run_id,
        messages=_tool_round("kanban_comment"),
    ),
    Scenario(
        "control_board_unreadable",
        "DB unreadable: the guard must stay armed (fail open)",
        expect_nudge=True,
        build=_board_unreadable,
        messages=_tool_round("kanban_comment"),
    ),
    Scenario(
        "control_readonly_board",
        "board on read-only storage, own run open: must not need a write lock",
        expect_nudge=True,
        build=_readonly_board,
        messages=_tool_round("kanban_comment"),
    ),
    Scenario(
        "control_no_run_id_pinned",
        "RUN_ID unset while our own run is open: must still fire",
        expect_nudge=True,
        build=_no_run_id_pinned,
        messages=_tool_round("kanban_comment"),
    ),
    Scenario(
        "control_missing_board_is_readonly",
        "DB path names no board: nudge, and create nothing (runs every turn)",
        expect_nudge=True,
        build=_missing_board_path,
        messages=_tool_round("kanban_comment"),
        forbid_side_effects=True,
    ),
]


# ───────────────────────────── the run loop ──────────────────────────────


_ENV_KEYS = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_STOP_NUDGE",
    "HERMES_HOME",
)


def run_scenario(mod: types.ModuleType, scenario: Scenario, *, verbose: bool = False) -> dict:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    restore: Optional[Path] = None
    try:
        with tempfile.TemporaryDirectory(prefix="nudge_board_") as tmpdir:
            try:
                state = scenario.build(Path(tmpdir))
                restore = state.get("restore")
                os.environ["HERMES_KANBAN_TASK"] = state["task"]
                if state["run"] is None:
                    os.environ.pop("HERMES_KANBAN_RUN_ID", None)
                else:
                    os.environ["HERMES_KANBAN_RUN_ID"] = str(state["run"])
                os.environ["HERMES_KANBAN_DB"] = str(state["db"])
                os.environ.pop("HERMES_KANBAN_STOP_NUDGE", None)
                nudge = mod.build_kanban_stop_nudge(messages=scenario.messages)
                nudged = nudge is not None
                stray: list[str] = []
                if scenario.forbid_side_effects:
                    watch = Path(state.get("watch") or state["db"]).resolve()
                    if watch.is_file():
                        watch = watch.parent
                    stray = sorted(
                        str(p.relative_to(watch)) for p in watch.rglob("*") if p.is_file()
                    )
                # Candidates name their predicate differently; report whichever
                # one this module exposes, for the "why" column.
                predicate = None
                for fn in ("current_run_is_terminal", "run_already_terminal", "kanban_run_is_over"):
                    f = getattr(mod, fn, None)
                    if callable(f):
                        try:
                            predicate = (fn, f())
                        except TypeError:
                            try:
                                predicate = (fn, f(state["task"]))
                            except Exception:
                                predicate = (fn, "ERR")
                        except Exception:
                            predicate = (fn, "ERR")
                        break
                result = {
                    "scenario": scenario.name,
                    "nudged": nudged,
                    "ok": nudged == scenario.expect_nudge and not stray,
                    "stray_files": stray,
                    "predicate": predicate,
                    "snapshot": state["snapshot"],
                }
                if verbose:
                    print(f"\n=== {scenario.name}")
                    print(textwrap.indent(state["snapshot"], "  "))
                    print(
                        f"  expect: {'nudge' if scenario.expect_nudge else 'silent'}"
                        f"  ({scenario.why})"
                    )
                    pname, pval = predicate or ("<none>", None)
                    print(f"  {pname}()={pval} -> {'nudges' if nudged else 'silent'}")
                    if scenario.forbid_side_effects:
                        print(f"  files created by the guard: {stray or 'none'}")
                return result
            finally:
                # A read-only scenario dir would otherwise defeat cleanup.
                if restore is not None:
                    import stat

                    Path(restore).chmod(stat.S_IRWXU)
                    for p in Path(restore).rglob("*"):
                        try:
                            p.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
                        except OSError:
                            pass
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_candidate(label: str, source: str, *, verbose: bool = False) -> list[dict]:
    mod = _load_module(label, source)
    return [run_scenario(mod, s, verbose=verbose) for s in SCENARIOS]


# ────────────────────────────── mutations ────────────────────────────────

# Each mutation is (name, old, new, expectation). ``caught`` means at least
# one scenario must flip; ``overlap`` documents a mutation the scenarios
# deliberately do NOT catch, because another check already covers it.
MUTATIONS = [
    (
        "keys_off_task_status",
        "SELECT outcome FROM task_runs WHERE id = ? AND task_id = ?",
        "SELECT CASE WHEN t.status IN ('done','blocked') THEN 'x' END "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.id = ? AND r.task_id = ?",
        "caught",
    ),
    (
        "no_db_check",
        "    if current_run_is_terminal():\n        return None\n",
        "",
        "caught",
    ),
    (
        "unscoped_run_lookup",
        '"SELECT outcome FROM task_runs WHERE id = ? AND task_id = ?",\n                (run_id, task_id),',
        '"SELECT outcome FROM task_runs WHERE id = ?",\n                (run_id,),',
        "caught",
    ),
    (
        "unknown_means_over",
        "    if row is None:\n        return None\n",
        "    if row is None:\n        return True\n",
        "caught",
    ),
    (
        "two_tools_only",
        '        "kanban_complete",\n        "kanban_block",\n        "kanban_request_review",\n        "kanban_request_changes",\n',
        '        "kanban_complete",\n        "kanban_block",\n',
        "overlap",
    ),
    (
        # Reach a missing board through the INITIALISING connect path
        # instead of returning. The nudge decision is unchanged; only the
        # side effect appears, so this mutation is caught by
        # control_missing_board_is_readonly or by nothing at all.
        "init_connect_when_missing",
        "    if not os.path.exists(db_path):\n        return None\n",
        "    if not os.path.exists(db_path):\n"
        "        from pathlib import Path as _P\n\n"
        "        from hermes_cli import kanban_db as _kb\n\n"
        "        try:\n"
        "            _kb.connect(db_path=_P(db_path)).close()\n"
        "        except Exception:\n"
        "            pass\n"
        "        return None\n",
        "caught",
    ),
]


def run_mutation_check(base_source: str) -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True
    for name, old, new, expectation in MUTATIONS:
        if old not in base_source:
            lines.append(f"{name}: NOT APPLICABLE (anchor absent from this module)")
            if expectation == "caught":
                ok = False
            continue
        mutated = base_source.replace(old, new, 1)
        results = run_candidate(f"mut_{name}", mutated)
        broken = [r["scenario"] for r in results if not r["ok"]]
        if broken:
            lines.append(f"{name}: CAUGHT  (fails {', '.join(broken)})")
        else:
            lines.append(f"{name}: MISSED  (every scenario still passes)")
            if expectation == "caught":
                ok = False
    return ok, lines


# ──────────────────────────────── main ───────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--candidate",
        action="append",
        default=[],
        metavar="LABEL=REF_OR_PATH",
        help="candidate implementation; repeatable. Default: the working tree.",
    )
    ap.add_argument(
        "--expect-pass",
        action="append",
        default=[],
        metavar="LABEL",
        help="label that must pass every scenario for exit status 0.",
    )
    ap.add_argument("--mutation-check", action="store_true", help="mutate the working tree module and check the scenarios catch it")
    ap.add_argument("--verbose", action="store_true", help="print each scenario's board state and the predicate's answer")
    ap.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = ap.parse_args(argv)

    specs = args.candidate or [f"worktree={REPO_ROOT / MODULE_REL_PATH}"]
    loaded = [_module_source(s) for s in specs]

    results: dict[str, list[dict]] = {}
    for label, source in loaded:
        results[label] = run_candidate(label, source, verbose=args.verbose)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        width = max(28, *(len(label) + 2 for label in results))
        header = "scenario".ljust(34) + "".join(label[:width - 2].ljust(width) for label in results)
        print(header)
        for i, scenario in enumerate(SCENARIOS):
            row = scenario.name.ljust(34)
            for label in results:
                r = results[label][i]
                verdict = "PASS" if r["ok"] else "FAIL"
                if r.get("stray_files"):
                    verdict = "FAIL*"
                row += f"{'nudges' if r['nudged'] else 'silent'} {verdict}".ljust(width)
            print(row)
        print()
        for label, rs in results.items():
            bad = [r["scenario"] for r in rs if not r["ok"]]
            print(f"{label}: {'ALL PASS' if not bad else 'FAILS ' + ', '.join(bad)}")
            for r in rs:
                if r.get("stray_files"):
                    print(
                        f"    * {r['scenario']}: guard WROTE to disk: "
                        f"{', '.join(r['stray_files'])}"
                    )

    exit_code = 0
    for label in args.expect_pass:
        if label not in results:
            print(f"\n--expect-pass {label!r}: no such candidate", file=sys.stderr)
            exit_code = 1
            continue
        if any(not r["ok"] for r in results[label]):
            print(f"\n--expect-pass {label!r}: FAILED", file=sys.stderr)
            exit_code = 1

    if args.mutation_check:
        base = (REPO_ROOT / MODULE_REL_PATH).read_text(encoding="utf-8")
        ok, lines = run_mutation_check(base)
        print("\n## mutation check (working tree module)")
        for line in lines:
            print("  " + line)
        if not ok:
            print("\nmutation check FAILED: a mutation expected to be caught was not", file=sys.stderr)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
