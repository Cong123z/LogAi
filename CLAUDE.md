# logai2 — instructions for Claude sessions

Docs: README.md (run), ARCHITECTURE.md (how it works, as implemented),
docs/WEB_UI.md (pages + HTTP API), docs/specs/ and docs/plans/ (designs).
The code and tests are the source of truth; fix a doc that disagrees.

## Project rules
- logai2 is a separate local project: no GitHub remote. Never push, and
  never touch ~/Documents/logai-engine (code, git, containers).
- Tests: `.venv/bin/pytest -q` against the test Postgres on 127.0.0.1:5433
  (`docker compose up -d postgres`).
- Stage by explicit path; no `Co-Authored-By` / AI attribution in commits.
- `agents.md` is the local, git-ignored work log; add an entry per task.

## Team workflow (when the user says to use it)
Planned in docs/TEAM_WORKFLOW.md — read it before starting. In short:
- This session (Opus) is the tech lead: clarify with the user, plan, write
  briefs (docs/templates/task-brief.md), answer `QUESTION:` replies, review,
  merge. It does not write feature code itself.
- Implementers: Claude Sonnet 5.5 subagents (`model: sonnet`) and Gemini 3.8
  Flash via `scripts/agy-task.sh` (Antigravity CLI `agy`). One branch +
  worktree per task, local PR file in `.team/prs/`, cross-model review,
  then the tech lead's final review and `git merge --no-ff`.
- Keep `.team/ledger.md` up to date: dispatches, questions, rulings,
  verdicts, merges.
