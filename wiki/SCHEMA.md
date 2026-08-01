# Laserbox Wiki Schema

## Purpose
This wiki is the living knowledge base for laserbox — a Python options
premium trading system. The LLM writes and maintains it. Len reads it.
This replaces the monolithic CLAUDE.md as the primary context source
for Claude Code sessions and lb-loop planner tasks.

## Directories
- wiki/raw/     — immutable source documents. Never edit these.
- wiki/architecture/ — system design, data flows, module relationships
- wiki/decisions/    — why things are built the way they are (ADRs)
- wiki/modules/      — one page per major Python module

## Page format
Every wiki page starts with this frontmatter:
---
title: Page Title
type: architecture | decision | module | query
updated: YYYY-MM-DD
sources: [raw/CLAUDE.md]
related: [[other-page]]
---

Then the content. Keep pages under 300 lines.
Cross-link with [[page-name]] syntax.
One concept per page.

## Operations

### Ingest (when a new document lands in wiki/raw/)
1. Read wiki/index.md to understand existing pages
2. Extract key facts and integrate into existing pages
3. Create new pages for concepts not yet covered
4. Update wiki/index.md with any new pages
5. Append to wiki/log.md: ## YYYY-MM-DD | ingest | FILENAME

### Query (when answering a question about the system)
1. Read wiki/index.md first to find relevant pages
2. Read only the relevant pages (not all of them)
3. Synthesize answer with page citations
4. If the answer reveals a gap, create a new wiki page

### Lint (run periodically)
Check for: contradictions, stale facts, orphan pages (not in index),
missing cross-references, concepts that need their own page.

### File decision (after a significant architecture or code decision)
Create wiki/decisions/SLUG.md documenting:
- Context: what problem were we solving?
- Decision: what did we choose?
- Rationale: why this over alternatives?
- Consequences: what does this enable or constrain?

## Decision log (architecture decision records)
Every non-trivial design choice gets filed here so future sessions
understand WHY things are built the way they are, not just HOW.

## loop.config.json schema (agent-loop configuration)
Optional file at the project root, loaded by scripts/eval_loop_config.py.
Overrides the built-in DEFAULTS; a missing file means pure defaults.
Copy loop.config.example.json to loop.config.json to start (the real file
is gitignored so per-user overrides are never committed).

Keys (all optional):
- max_iterations — int >= 1. Loop iteration cap (default 10). Booleans and
  numeric strings are rejected.
- models — object keyed by role: planner | generator | evaluator. Values
  are strings passed to `claude -p --model`. Empty string "" means "use the
  CLI's default model".
- allowed_tools — object keyed by the same three roles. Each value is
  either a comma-separated string (passed verbatim to `--allowedTools`) or
  a list of strings (joined with commas). List elements must all be
  strings. Empty string "" means no --allowedTools flag for that role.

Error behavior:
- Malformed JSON or any invalid value → ValueError; eval_loop.py exits
  non-zero at startup, before any agent runs. It never silently falls back
  to defaults.
- Unknown top-level keys and unknown roles inside models/allowed_tools →
  warning on stderr, value ignored (never an error).
