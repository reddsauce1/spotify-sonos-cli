#!/usr/bin/env python3
"""
run_queue.py — a simple task queue runner for agent-loop.

Reads tasks.md (at the project root), finds the first unchecked task, calls
eval_loop.run() with the problem statement, then marks the task:

    - [ ]  unchecked  ->  - [x]  on PASS
                          - [~]  on hard FAIL (MAX_ITERATIONS reached)

An INTERRUPTED verdict (Ctrl+C during a run) leaves the task unchecked and
stops the queue, so an aborted task is not consumed.

The runner loops until every task is marked or the user presses Ctrl+C.

DB logging is inherited for free: eval_loop.run() already logs each run to
eval_loop_db (log_run_start / log_iteration / finalize_run). Nothing extra is
needed here.

--rescue mode: instead of running the queue, find the most recent
eval-loop/failed/* branch (by committer date), cherry-pick its changed files
onto main, run pytest, and — only if green — commit locally and flip the last
`- [~]` (failed) task in tasks.md to `- [x]`. On red, main is rolled back to
pristine and the failed branch is preserved for inspection. The commit is
local-only (never pushed) and the failed branch is never deleted, consistent
with the "never auto-merge or auto-push" workflow.

Stdlib only. Portable across macOS and Linux. Idempotent: re-running after all
tasks are marked is a no-op, and a second --rescue of an already-rescued
branch is a clean no-op.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Make `import eval_loop` resolve regardless of the current working directory,
# mirroring how eval_loop.py imports eval_loop_db from the same scripts/ dir.
SCRIPTS_DIR = Path(__file__).parent.resolve()
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

PROJECT_ROOT = SCRIPTS_DIR.parent
DEFAULT_TASKS_FILE = PROJECT_ROOT / "tasks.md"

# Line-prefix markers. Trailing space is part of the marker so the problem
# statement that follows is preserved exactly.
UNCHECKED = "- [ ] "
DONE = "- [x] "
FAILED = "- [~] "

# Default contents written when tasks.md is missing, so the runner is safe to
# invoke in a fresh checkout (idempotent / self-bootstrapping).
STUB_TASKS = """# Task Queue

# One task per line. Markers:
#   - [ ]  unchecked  — pending, will be picked up by scripts/run_queue.py
#   - [x]  done       — completed with a PASS verdict
#   - [~]  failed      — hard fail after MAX_ITERATIONS
# Blank lines and lines starting with "#" are ignored by the runner.
#
# Run the queue with:  python3 scripts/run_queue.py

- [ ] Add a --version flag to scripts/eval_loop.py that prints the tool version and exits
- [ ] Add a docstring coverage check to tests/ that fails if any public function lacks a docstring
- [ ] Add a helper to eval_loop_db that prunes runs older than N days from eval_loop.db
"""


def read_tasks(tasks_file) -> list[str]:
    """Return the raw lines of tasks.md (without trailing newlines).

    Comments and blank lines are preserved so that rewrites are lossless.
    """
    text = Path(tasks_file).read_text()
    return text.splitlines()


def write_tasks(tasks_file, lines: list[str]) -> None:
    """Atomically write the lines back to tasks.md.

    Writes to a temp file in the same directory then os.replace()s it into
    place, so a Ctrl+C mid-write cannot corrupt tasks.md.
    """
    tasks_file = Path(tasks_file)
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(tasks_file.parent), prefix=".tasks-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_path, tasks_file)
    except BaseException:
        # Clean up the temp file on any failure/interrupt.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def find_next_unchecked(lines: list[str]):
    """Return (index, problem_statement) of the first unchecked task.

    Returns None when there are no more unchecked tasks.
    """
    for i, line in enumerate(lines):
        if line.startswith(UNCHECKED):
            problem = line[len(UNCHECKED):].strip()
            if problem:
                return i, problem
    return None


def mark_task(lines: list[str], index: int, marker: str,
              from_marker: str = UNCHECKED) -> list[str]:
    """Replace the `from_marker` prefix on lines[index] with `marker`.

    The problem text after the prefix is left untouched. Other lines are
    unchanged. Returns the (mutated) list for convenience. `from_marker`
    defaults to UNCHECKED to preserve the original behaviour.
    """
    line = lines[index]
    if line.startswith(from_marker):
        lines[index] = marker + line[len(from_marker):]
    return lines


def pending_tasks(lines: list[str]) -> list[str]:
    """Return the problem statements of all unchecked tasks, in order."""
    out = []
    for line in lines:
        if line.startswith(UNCHECKED):
            problem = line[len(UNCHECKED):].strip()
            if problem:
                out.append(problem)
    return out


def _counts(lines: list[str]):
    done = sum(1 for ln in lines if ln.startswith(DONE))
    failed = sum(1 for ln in lines if ln.startswith(FAILED))
    remaining = sum(
        1 for ln in lines
        if ln.startswith(UNCHECKED) and ln[len(UNCHECKED):].strip()
    )
    return done, failed, remaining


def _alias_suggestion(tasks_file) -> str:
    """Build a shell alias suggestion using a project-relative script path."""
    try:
        script_rel = Path(__file__).resolve().relative_to(PROJECT_ROOT)
    except ValueError:
        script_rel = Path(__file__).resolve()
    return (
        "Tip: add a shell alias so you can start the queue from anywhere.\n"
        f"  alias runqueue='python3 {PROJECT_ROOT / script_rel}'\n"
        "Add it to ~/.bashrc or ~/.zshrc, then run:  runqueue"
    )


def _locate_pending(lines: list[str], problem: str):
    """Index of the first unchecked line whose problem text == problem, else None."""
    for i, line in enumerate(lines):
        if line.startswith(UNCHECKED) and line[len(UNCHECKED):].strip() == problem:
            return i
    return None


def _mark_and_write(tasks_file, problem: str, marker: str) -> bool:
    """Re-read fresh, locate the pending task by text, mark it, and persist.

    Re-reading before each mark means manual edits during the run are not
    clobbered. Returns True if the task was marked, False if its line
    changed/vanished during the run.
    """
    lines = read_tasks(tasks_file)
    located = _locate_pending(lines, problem)
    if located is None:
        print("  [warning] task line changed during run — not marking.")
        return False
    mark_task(lines, located, marker)
    write_tasks(tasks_file, lines)
    return True


def _log_crash(problem: str) -> None:
    """Best-effort record of a queue-level crash to eval_loop_db.

    eval_loop.run() logs its own runs, but if run_fn raises before/around
    returning that logging may never happen, so record the crash here. Fully
    guarded: logging must never abort the queue.
    """
    try:
        import eval_loop_db
        rid = eval_loop_db.log_run_start(problem, "queue-crash")
        eval_loop_db.finalize_run(rid, "CRASH", 0.0)
    except Exception:
        pass


def run_queue(tasks_file, run_fn) -> int:
    """Process tasks one at a time until none remain or the user interrupts.

    `run_fn(problem)` must return a verdict string: "PASS", "FAIL", or
    "INTERRUPTED". Returns a process exit code.
    """
    try:
        while True:
            # Re-read fresh each iteration so manual edits between runs are
            # respected and progress persists even if the process dies.
            lines = read_tasks(tasks_file)
            nxt = find_next_unchecked(lines)
            if nxt is None:
                done, failed, _ = _counts(lines)
                print(f"\nNothing to do — all tasks are marked "
                      f"({done} done, {failed} failed).")
                return 0

            index, problem = nxt
            done, failed, remaining = _counts(lines)
            print(f"\n{'#'*60}")
            print(f"# Task {done + failed + 1} (line {index + 1}), "
                  f"{remaining} remaining")
            print(f"# {problem}")
            print(f"{'#'*60}")

            try:
                verdict = run_fn(problem)
            except Exception as e:
                # One crashing task must not nuke the whole queue. Deliberate
                # policy: treat an unhandled crash as a hard fail ([~]) and keep
                # going so the queue still makes progress. KeyboardInterrupt is a
                # BaseException (not Exception), so it is NOT caught here — it
                # propagates to the outer handler and stops the queue cleanly.
                print(f"\n[ERROR] run_fn crashed on task:\n  {problem}\n  {e!r}")
                _log_crash(problem)
                if _mark_and_write(tasks_file, problem, FAILED):
                    print(f"  Marked as FAILED (crashed): {problem}")
                continue

            # Explicit, total mapping from verdict -> action. Only the two
            # sentinels below are ever written to disk; INTERRUPTED stops the
            # queue cleanly; anything else is treated as a bug and halts the
            # queue LOUDLY without guessing (never auto-mark as failed).
            if verdict == "INTERRUPTED":
                print("\nRun was interrupted — leaving task unchecked and "
                      "stopping the queue.")
                break

            if verdict == "PASS":
                marker = DONE
            elif verdict == "FAIL":
                marker = FAILED
            else:
                # None, an unknown string, or an unexpected type. We refuse to
                # invent a result: leave the task unchecked and halt so a human
                # can investigate. This is the only non-zero exit path.
                print(f"\n[ERROR] Unexpected verdict {verdict!r} from run_fn "
                      f"for task:\n  {problem}")
                print("Expected one of: PASS, FAIL, INTERRUPTED. "
                      "Halting the queue; task left unchecked.")
                lines = read_tasks(tasks_file)
                done, failed, remaining = _counts(lines)
                print(f"\nQueue summary: {done} done, {failed} failed, "
                      f"{remaining} remaining.")
                return 2

            if _mark_and_write(tasks_file, problem, marker):
                print(f"  Marked as {'DONE' if marker == DONE else 'FAILED'}: "
                      f"{problem}")

    except KeyboardInterrupt:
        print("\n\nInterrupted — the in-flight task was left unchecked.")

    lines = read_tasks(tasks_file)
    done, failed, remaining = _counts(lines)
    print(f"\nQueue summary: {done} done, {failed} failed, "
          f"{remaining} remaining.")
    return 0


def _ensure_tasks_file(tasks_file) -> None:
    """Create tasks.md with placeholder tasks if it doesn't exist."""
    tasks_file = Path(tasks_file)
    if not tasks_file.exists():
        print(f"tasks.md not found at {tasks_file} — creating a stub with "
              "example tasks.")
        tasks_file.write_text(STUB_TASKS)


# -- rescue mode ----------------------------------------------------------------

# Paths never cherry-picked onto main by --rescue. Mirrors the exclusion list
# in eval_loop.commit_iteration() (kept as a local copy on purpose: rescue must
# work without importing eval_loop, which reads CLAUDE.md/rubric at import
# time). Purely defensive — these should never land on failed branches anyway.
RESCUE_EXCLUDE = (
    "scripts/eval_loop.py",
    "scripts/eval_loop.py.save",
    ".claude/agents/evaluator_rubric.md",
)

FAILED_BRANCH_PREFIX = "eval-loop/failed/"


def _git(args: list[str], check: bool = True, cwd=None) -> subprocess.CompletedProcess:
    """Local git wrapper (mirrors eval_loop.git(), without importing it).

    `cwd` defaults to the project root; tests pass a throwaway repo instead.
    """
    return subprocess.run(
        ["git"] + list(args),
        capture_output=True,
        text=True,
        cwd=str(cwd if cwd is not None else PROJECT_ROOT),
        check=check,
    )


def find_latest_failed_branch(cwd=None):
    """Return the most recent eval-loop/failed/* branch name, or None.

    "Most recent" means highest committer date, via git for-each-ref.
    """
    result = _git(
        ["for-each-ref", "--sort=-committerdate",
         "--format=%(refname:short)", f"refs/heads/{FAILED_BRANCH_PREFIX}"],
        check=False, cwd=cwd,
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def changed_files(branch: str, cwd=None) -> list[tuple[str, str]]:
    """Return [(status, path)] the branch changed relative to its merge base.

    Uses the three-dot diff (main...branch) so only the branch's own changes
    are picked up, even after main has moved on. Statuses are collapsed to a
    single letter; renames become a delete of the old path plus an add of the
    new path. Infrastructure paths (RESCUE_EXCLUDE) are filtered out.
    """
    result = _git(["diff", "--name-status", f"main...{branch}"], cwd=cwd)
    out = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status[:1] in ("R", "C") and len(parts) == 3:
            # Rename/copy: old path, new path. A rename is a delete + an add;
            # a copy only adds the new path.
            if status[:1] == "R":
                out.append(("D", parts[1]))
            out.append(("A", parts[2]))
        else:
            out.append((status[:1], parts[1]))
    return [(s, p) for s, p in out if p not in RESCUE_EXCLUDE]


def mark_last_failed_done(tasks_file) -> bool:
    """Flip the last `- [~]` (failed) line in tasks.md to `- [x]` (done).

    Re-reads the file fresh and persists atomically via write_tasks().
    Returns True if a line was flipped, False (with a warning) if no failed
    line exists — which makes a second call a clean no-op.
    """
    lines = read_tasks(tasks_file)
    last = None
    for i, line in enumerate(lines):
        if line.startswith(FAILED):
            last = i
    if last is None:
        print("  [warning] no failed (- [~]) task in tasks.md — nothing to mark.")
        return False
    mark_task(lines, last, DONE, from_marker=FAILED)
    write_tasks(tasks_file, lines)
    return True


def _run_pytest(cwd) -> bool:
    """Run pytest in `cwd`, streaming output. Green iff exit code 0."""
    result = subprocess.run([sys.executable, "-m", "pytest"], cwd=str(cwd))
    return result.returncode == 0


def run_rescue(tasks_file, repo_dir=None, pytest_fn=None) -> int:
    """Cherry-pick the most recent eval-loop/failed/* branch onto main.

    Applies the branch's changed files onto main, runs pytest, and on green
    commits (locally — never pushed) and flips the last failed task in
    tasks.md to done. On red, main is rolled back and the failed branch is
    preserved for inspection. The failed branch is never deleted.

    `pytest_fn` (a zero-arg callable returning True for green) is injectable
    for tests; the default runs `python -m pytest` in the repo.

    Exit codes: 0 = rescued or idempotent no-op, 1 = no failed branch /
    dirty tree / pytest red / git error.
    """
    repo_dir = Path(repo_dir) if repo_dir is not None else PROJECT_ROOT
    if pytest_fn is None:
        pytest_fn = lambda: _run_pytest(repo_dir)  # noqa: E731

    branch = find_latest_failed_branch(cwd=repo_dir)
    if branch is None:
        print(f"No {FAILED_BRANCH_PREFIX}* branches found — nothing to rescue.")
        return 1

    if _git(["status", "--porcelain"], cwd=repo_dir).stdout.strip():
        print("Working tree is dirty — commit or stash your changes before "
              "running --rescue. Nothing was modified.")
        return 1

    original_branch = _git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir
    ).stdout.strip()

    def _restore_original():
        if original_branch and original_branch != "main":
            _git(["checkout", original_branch], check=False, cwd=repo_dir)

    print(f"Rescuing from branch: {branch}")

    try:
        _git(["checkout", "main"], cwd=repo_dir)
        files = changed_files(branch, cwd=repo_dir)

        added_paths = []  # paths that did not exist on main (for rollback)
        for status, path in files:
            if status == "D":
                _git(["rm", "-q", "--ignore-unmatch", "--", path],
                     check=False, cwd=repo_dir)
            else:
                if not (repo_dir / path).exists():
                    added_paths.append(path)
                _git(["checkout", branch, "--", path], cwd=repo_dir)

        if not _git(["status", "--porcelain"], cwd=repo_dir).stdout.strip():
            print("Nothing to apply — already rescued.")
            mark_last_failed_done(tasks_file)
            return 0

        print("Applied files:")
        for status, path in files:
            print(f"  {status}  {path}")

        print("Running pytest ...")
        if not pytest_fn():
            print("pytest is RED — rolling back main. The failed branch "
                  f"{branch} is preserved for inspection.")
            _git(["reset", "--hard", "HEAD"], cwd=repo_dir)
            if added_paths:
                _git(["clean", "-fd", "--"] + added_paths,
                     check=False, cwd=repo_dir)
            _restore_original()
            return 1

        print("pytest is GREEN — committing rescue to main (local only).")
        # Everything is already staged: `git checkout <branch> -- <path>`
        # updates the index as well as the working tree, and `git rm` stages
        # the deletion. No extra `git add` is needed (and `add -A -- <deleted
        # path>` would fail: the pathspec matches nothing).
        if _git(["diff", "--cached", "--quiet"],
                check=False, cwd=repo_dir).returncode != 0:
            _git(["commit", "-m", f"rescue: cherry-pick from {branch}"],
                 cwd=repo_dir)
        mark_last_failed_done(tasks_file)
        print(f"Rescued {branch} onto main. Review with `git diff main~1` — "
              "the commit is local; the failed branch was not deleted.")
        return 0

    except subprocess.CalledProcessError as e:
        print(f"[ERROR] git command failed during rescue: {e}\n"
              f"{(e.stderr or '').strip()}")
        _git(["reset", "--hard", "HEAD"], check=False, cwd=repo_dir)
        _restore_original()
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run queued tasks from tasks.md through the eval loop."
    )
    parser.add_argument(
        "--tasks",
        default=str(DEFAULT_TASKS_FILE),
        help="Path to the tasks file (default: tasks.md at the project root).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="List pending tasks without executing anything.",
    )
    mode.add_argument(
        "--rescue",
        action="store_true",
        help="Cherry-pick the most recent eval-loop/failed/* branch onto "
             "main, run pytest, and if green commit (locally) and mark the "
             "last failed task done. The failed branch is never deleted.",
    )
    args = parser.parse_args(argv)

    tasks_file = Path(args.tasks)
    _ensure_tasks_file(tasks_file)

    if args.rescue:
        return run_rescue(tasks_file)

    print(_alias_suggestion(tasks_file))

    if args.dry_run:
        lines = read_tasks(tasks_file)
        pending = pending_tasks(lines)
        if not pending:
            print("\nNo pending tasks.")
            return 0
        print(f"\n{len(pending)} pending task(s):")
        for n, problem in enumerate(pending, 1):
            print(f"  {n}. {problem}")
        return 0

    # Import here so --dry-run and --help work without a full eval_loop setup
    # (eval_loop.py reads CLAUDE.md / rubric at import time).
    import eval_loop

    return run_queue(tasks_file, eval_loop.run)


if __name__ == "__main__":
    sys.exit(main())
