#!/usr/bin/env python3
"""
wiki_ingest.py — ingest a document into the wiki.

Copies the given file into wiki/raw/ (idempotently), then runs a
`claude -p` call that follows the Ingest operation defined in
wiki/SCHEMA.md: integrate key facts into existing pages, create pages
for uncovered concepts, update wiki/index.md, and append an ingest line
to wiki/log.md.

Usage:
    python3 scripts/wiki_ingest.py path/to/doc.md
    python3 scripts/wiki_ingest.py path/to/doc.md --dry-run
    python3 scripts/wiki_ingest.py path/to/doc.md --no-claude
    python3 scripts/wiki_ingest.py path/to/doc.md --model sonnet
    python3 scripts/wiki_ingest.py path/to/doc.md --timeout 900

Exit codes:
    0 — success
    1 — bad source path (missing / not a regular file / unreadable)
    2 — claude call failed (claude missing from PATH, launch failure,
        timeout, non-zero exit, or an error result such as
        subtype "error_max_turns" / is_error in the JSON output)
    3 — post-run verification failed (no ingest line in wiki/log.md)

(argparse itself exits 2 on invalid flag values, e.g. --max-turns 0,
before any filesystem or subprocess work.)

On exit codes 2 and 3 the raw copy is preserved, so a re-run is a pure
retry (the copy stage is idempotent: identical content is never
re-copied, changed content overwrites in place — no "file (1).md"
duplicates, ever).

Lineage tracking: wiki/raw/.sources.json maps each raw basename to the
absolute path it was last ingested from. When an incoming file would
overwrite an existing raw copy with different content AND a different
(or unknown) source path, a loud WARNING is printed to stderr before
overwriting — basename collisions never clobber silently. A missing or
corrupt manifest is treated as empty and is never fatal.

Stdlib-only. Portable across macOS and Linux.
"""

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

# The ingest agent reads and writes wiki pages; it has no reason to run
# shell commands, so Bash is deliberately excluded.
ALLOWED_TOOLS = "Read,Grep,Glob,Write,Edit"
DEFAULT_MAX_TURNS = 25
DEFAULT_TIMEOUT = 600  # seconds; wiki ingests are bounded, not open-ended

# Sidecar provenance manifest inside wiki/raw/: basename -> absolute source path.
MANIFEST_NAME = ".sources.json"

EXIT_OK = 0
EXIT_BAD_SOURCE = 1
EXIT_CLAUDE_FAILED = 2
EXIT_VERIFY_FAILED = 3

# Human-readable note per copy status (also used in the final summary).
STATUS_NOTES = {
    "new": "new document ingested",
    "unchanged": "already ingested, refreshing wiki",
    "updated": "re-ingest of an updated source",
}


# -- copy stage ----------------------------------------------------------------

def validate_source(src: Path) -> str | None:
    """Return an error message if src is not an ingestable file, else None."""
    if not src.exists():
        return f"source does not exist: {src}"
    if not src.is_file():
        return f"source is not a regular file: {src}"
    if not os.access(src, os.R_OK):
        return f"source is not readable: {src}"
    return None


def _read_manifest(raw_dir: Path) -> dict:
    """Read wiki/raw/.sources.json; missing or corrupt -> {} (never fatal)."""
    path = raw_dir / MANIFEST_NAME
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(raw_dir: Path, manifest: dict) -> None:
    """Write the provenance manifest. Best-effort — never fatal."""
    try:
        (raw_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
    except OSError:
        pass


def copy_to_raw(src: Path, root: Path) -> tuple[Path, str, str | None]:
    """Copy src into root/wiki/raw/ idempotently.

    Returns (dest, status, warning) where status is one of:
      "new"       — dest did not exist; file copied
      "unchanged" — dest exists with identical content (or src IS dest);
                    nothing copied
      "updated"   — dest existed with different content; overwritten

    warning is a basename-collision message (or None): it is set when an
    overwrite replaces content whose recorded source path differs from —
    or is unknown to — the wiki/raw/.sources.json manifest.
    """
    raw_dir = root / "wiki" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_dir / src.name
    src_resolved = str(src.resolve())
    manifest = _read_manifest(raw_dir)
    recorded = manifest.get(src.name)
    warning = None

    if dest.exists():
        if src.resolve() == dest.resolve():
            status = "unchanged"
        elif filecmp.cmp(str(src), str(dest), shallow=False):
            status = "unchanged"
        else:
            if recorded is not None and recorded != src_resolved:
                warning = (
                    f"wiki/raw/{src.name} previously came from {recorded}; "
                    f"now overwriting with {src_resolved} — different "
                    "document lineage"
                )
            elif recorded is None:
                warning = (
                    f"wiki/raw/{src.name} has unknown lineage (no manifest "
                    f"entry) and different content; overwriting with "
                    f"{src_resolved}"
                )
            shutil.copy2(str(src), str(dest))
            status = "updated"
    else:
        shutil.copy2(str(src), str(dest))
        status = "new"

    # Record lineage idempotently: only rewrite when the entry changes.
    if manifest.get(src.name) != src_resolved:
        manifest[src.name] = src_resolved
        _write_manifest(raw_dir, manifest)

    return dest, status, warning


# -- prompt construction ---------------------------------------------------------

def build_prompts(dest: Path, root: Path, today: str) -> tuple[str, str]:
    """Build (system, prompt) for the ingest claude call.

    The system prompt embeds wiki/SCHEMA.md read at runtime, so schema
    changes propagate without editing this script. The user prompt spells
    out the Ingest operation with today's date passed in explicitly so
    the model never guesses it.
    """
    schema_path = root / "wiki" / "SCHEMA.md"
    schema = schema_path.read_text() if schema_path.exists() else ""
    system = (
        "You are the wiki maintainer for this project. You keep the wiki "
        "accurate, cross-linked, and conforming to its schema. "
        "Here is the full wiki schema (wiki/SCHEMA.md):\n\n"
        f"{schema}"
    )
    name = dest.name
    prompt = f"""A new document has landed at wiki/raw/{name}.
Today's date is {today}.

Follow the Ingest operation from the schema:
1. Read wiki/index.md to understand existing pages.
2. Read wiki/raw/{name} and extract its key facts.
3. Integrate those facts into existing wiki pages.
4. Create new pages for concepts not yet covered — respect the
   frontmatter format, the allowed `type` values, the 300-line page
   limit, and [[cross-link]] syntax.
5. Update wiki/index.md with any new or changed pages.
6. Append this exact line to wiki/log.md: ## {today} | ingest | {name}

Never edit files under wiki/raw/ — they are immutable sources.
Only modify files inside the wiki/ directory."""
    return system, prompt


# -- claude -p invocation ---------------------------------------------------------

def run_claude(
    prompt: str,
    system: str,
    cwd: Path,
    allowed_tools: str = "",
    max_turns: int = DEFAULT_MAX_TURNS,
    model: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, str, str]:
    """Run claude -p headless. Returns (returncode, stdout, stderr).

    Never raises on launch/timeout problems — they come back as
    synthetic non-zero return codes (127 missing binary, 126 launch
    failure, 124 timeout) with the explanation in the stderr slot, so
    callers handle every failure through one path.

    Self-contained on purpose: importing eval_loop.py would trigger its
    module-level side effects (config load that can sys.exit, DB init).
    """
    cmd = [
        "claude", "-p", prompt,
        "--system-prompt", system,
        "--max-turns", str(max_turns),
        "--output-format", "json",
    ]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if model:
        cmd += ["--model", model]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout,
        )
    except FileNotFoundError:
        return (
            127, "",
            "claude CLI not found on PATH — install it or check your "
            "shell environment",
        )
    except subprocess.TimeoutExpired as e:
        msg = f"claude call timed out after {timeout}s"
        partial = e.stdout or e.stderr
        if partial:
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            msg += f" (partial output: {partial[:300]})"
        return 124, "", msg
    except OSError as e:
        return 126, "", f"failed to launch claude: {e}"

    return result.returncode, result.stdout.strip(), result.stderr


def interpret_claude_output(rc: int, out: str) -> tuple[bool, str]:
    """Interpret a run_claude result: (ok, text).

    On ok=True, text is the model's result text to display. On ok=False,
    text explains the failure. Failure is structural — a non-zero return
    code, unparseable stdout, is_error, or a non-"success" subtype (the
    CLI reports max-turns exhaustion as subtype "error_max_turns") —
    never a substring sniff of model-authored prose, so a result that
    merely *mentions* "Reached max turns" is still a success.
    """
    if rc != 0:
        return False, f"claude exited with rc={rc}"
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return False, f"claude stdout is not valid JSON: {out[:300]!r}"
    if not isinstance(data, dict):
        return False, f"claude stdout is not a JSON object: {out[:300]!r}"
    if data.get("is_error"):
        detail = data.get("result", out)
        return False, f"claude reported an error: {str(detail)[:300]}"
    subtype = data.get("subtype")
    if subtype is not None and subtype != "success":
        return False, f"claude ended with subtype {subtype!r}"
    result = data.get("result")
    return True, result if isinstance(result, str) else out


# -- post-run verification --------------------------------------------------------

def verify_ingest(root: Path, name: str, today: str) -> bool:
    """True iff wiki/log.md has the exact ingest line for this file today.

    The expected line is `## {today} | ingest | {name}`. The only
    tolerance is whitespace around the `|` separators — every field must
    match exactly, so `my-notes.md` never satisfies `notes.md` and
    `reingested` never satisfies `ingest`.
    """
    log = root / "wiki" / "log.md"
    if not log.exists():
        return False
    for line in log.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("##"):
            continue
        fields = [f.strip() for f in stripped.split("|")]
        if len(fields) != 3:
            continue
        head, op, doc = fields
        if not head.startswith("##"):
            continue
        if head[2:].strip() != today:
            continue
        if op == "ingest" and doc == name:
            return True
    return False


def _print_wiki_git_status(root: Path) -> None:
    """Best-effort `git status --short wiki/` listing for review. Never fatal."""
    try:
        result = subprocess.run(
            ["git", "status", "--short", "wiki/"],
            capture_output=True,
            text=True,
            cwd=str(root),
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            print("[wiki-ingest] changed wiki files (review before committing):")
            print(result.stdout.rstrip())
    except OSError:
        pass


# -- main -----------------------------------------------------------------------

def positive_int(value: str) -> int:
    """argparse type: integer >= 1. Rejection exits 2 before any work."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {n}")
    return n


def main(argv=None, root: Path | None = None, today: str | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest a document into the wiki: copy it to wiki/raw/ "
        "and run claude -p to integrate it into the wiki pages.",
    )
    parser.add_argument("file_path", help="path to the document to ingest")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="copy nothing, call nothing; print what would happen",
    )
    parser.add_argument(
        "--no-claude", action="store_true",
        help="copy into wiki/raw/ only, skip the claude -p wiki update",
    )
    parser.add_argument(
        "--model", default="",
        help="model for claude -p (default: the CLI's default model)",
    )
    parser.add_argument(
        "--max-turns", type=positive_int, default=DEFAULT_MAX_TURNS,
        help=f"max claude turns for the ingest (default {DEFAULT_MAX_TURNS})",
    )
    parser.add_argument(
        "--timeout", type=positive_int, default=DEFAULT_TIMEOUT,
        help="seconds before the claude call is killed "
        f"(default {DEFAULT_TIMEOUT})",
    )
    args = parser.parse_args(argv)

    root = (root or PROJECT_ROOT).resolve()
    today = today or datetime.now().strftime("%Y-%m-%d")
    src = Path(args.file_path).expanduser()

    err = validate_source(src)
    if err:
        print(f"[wiki-ingest] ERROR: {err}", file=sys.stderr)
        return EXIT_BAD_SOURCE

    dest = root / "wiki" / "raw" / src.name

    if args.dry_run:
        print("[wiki-ingest] dry run — nothing copied, nothing called")
        print(f"  would copy: {src} -> {dest}")
        model_note = f", model: {args.model}" if args.model else ""
        print(
            f"  would run:  claude -p (allowed tools: {ALLOWED_TOOLS}, "
            f"max turns: {args.max_turns}, timeout: {args.timeout}s"
            f"{model_note})"
        )
        return EXIT_OK

    dest, status, warning = copy_to_raw(src, root)
    print(f"[wiki-ingest] raw: {dest} ({STATUS_NOTES[status]})")
    if warning:
        print(f"[wiki-ingest] WARNING: {warning}", file=sys.stderr)

    if args.no_claude:
        print("[wiki-ingest] --no-claude: raw copy only, wiki update skipped")
        return EXIT_OK

    # Snapshot index.md so we can report whether the ingest touched it.
    index_path = root / "wiki" / "index.md"
    index_before = index_path.read_text() if index_path.exists() else None

    system, prompt = build_prompts(dest, root, today)
    print("[wiki-ingest] updating wiki via claude -p ...")
    rc, out, errout = run_claude(
        prompt=prompt,
        system=system,
        cwd=root,
        allowed_tools=ALLOWED_TOOLS,
        max_turns=args.max_turns,
        model=args.model,
        timeout=args.timeout,
    )

    ok, text = interpret_claude_output(rc, out)
    if not ok:
        print(f"[wiki-ingest] claude error: {text}", file=sys.stderr)
        if errout:
            print(f"  stderr: {errout[:300]}", file=sys.stderr)
        print(
            f"[wiki-ingest] raw copy preserved at {dest} — re-running this "
            "command is a safe retry.",
            file=sys.stderr,
        )
        return EXIT_CLAUDE_FAILED

    if text:
        print(text)

    if not verify_ingest(root, dest.name, today):
        print(
            f"[wiki-ingest] WARNING: wiki/log.md has no "
            f"'## {today} | ingest | {dest.name}' line — the ingest may be "
            "incomplete. Raw copy preserved; re-run to retry.",
            file=sys.stderr,
        )
        return EXIT_VERIFY_FAILED

    index_after = index_path.read_text() if index_path.exists() else None
    if index_before == index_after:
        print(
            "[wiki-ingest] note: wiki/index.md unchanged — existing pages "
            "may already cover this document.",
            file=sys.stderr,
        )

    print(f"[wiki-ingest] done — {dest.name} ingested ({STATUS_NOTES[status]}).")
    _print_wiki_git_status(root)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
