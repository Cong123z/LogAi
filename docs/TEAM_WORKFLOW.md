# Team workflow: tech lead + coding agents

Status: **planned, not yet in use** (2026-10-21). The user decides when to
switch to it. Until then, work as before.

This describes how logai2 work is split between one tech lead and two kinds
of coding agents, all on this machine, with local branches and local
"pull requests" (logai2 has no GitHub remote, by choice).

## Roles

| Role | Who | Does | Never does |
|---|---|---|---|
| Tech lead | Claude Opus 5.5 (the main Claude Code session) | Clarifies the request with the user, writes the spec/plan, splits it into tasks, writes each task brief, answers agents' questions, assigns a reviewer, does the final review, merges | Writes feature code itself (only tiny fixes during review, and says so) |
| Implementer A | Claude Sonnet 5.5 (Claude Code subagent, `model: sonnet`) | Tasks that need judgment: exactly-once path, reader/owner/engine, SQL, detection math, anything touching several modules | Merges; reviews its own branch |
| Implementer B | Gemini 3.8 Flash (High) via the Antigravity CLI `agy` | Bounded tasks with a clear interface: web views and routes, scripts, docs, pure functions, extra tests | Merges; touches files outside its brief |
| Reviewer / tester | The *other* implementer model | Reviews a branch against its brief (spec + quality), runs focused tests, writes extra adversarial tests on request | Rewrites the feature |

Cross-model review is the default: a Sonnet branch is reviewed or tested by
Gemini, a Gemini branch by Sonnet. The tech lead's final review is always
Opus and always happens before a merge.

Model choice per task is the tech lead's call and is written in the brief
(`Implementer:` line), with one sentence why.

## Life of a task

```
user request
  → tech lead clarifies (questions to the user, one at a time)
  → spec + plan (approved by the user)
  → task brief docs/tasks/<id>-<slug>.md         (tech lead)
  → branch task/<id>-<slug> off master, in its own worktree
  → implementer: tests first, code, full suite, commits, PR file   (asks QUESTION: when unsure)
  → reviewer (other model): spec + quality verdict on the PR file
  → fix rounds (implementer), max 3, then tech lead decides
  → tech lead final review (Opus) → merge --no-ff into master, or send back
  → worktree and branch removed
```

### Branches and worktrees

- One branch per task: `task/<id>-<slug>`, created from the current `master`.
- Each branch lives in its own worktree:
  `git worktree add ../logai2-task-<id> -b task/<id>-<slug> master`.
  Agents work only inside their worktree.
- Two tasks run in parallel only when their briefs list disjoint files.
- `master` is changed only by the tech lead's merge. Nobody commits to it
  directly.

### The local pull request

There is no GitHub. A "PR" is:

1. The branch itself (its commits).
2. A PR file written by the implementer at `.team/prs/task-<id>-<slug>.md`
   (git-ignored), from `docs/templates/pr.md`: summary, files changed, how
   it was tested (commands + output), TDD evidence, deviations from the
   brief, open questions.
3. Review notes appended to the same file by the reviewer and the tech lead.
4. The merge: `git merge --no-ff task/<id>-<slug>` with the PR summary as the
   merge commit message, so the history keeps one merge commit per PR.

### Questions go up, never sideways

An agent that is not sure **stops and asks**; it never guesses. It ends its
reply with a line starting `QUESTION:`. The tech lead answers in the same
conversation:

- Sonnet subagent: answer with `SendMessage` to the same agent.
- Gemini: answer with `agy -p "<answer>" --conversation <id> ...` (same id).

The answer and any decision it implies go into the ledger (below) as a
`Ruling:` line with what it costs if wrong.

### Review gates

1. **Reviewer (other model):** reads the brief, the PR file and the diff;
   returns `Spec: ✅/❌`, issues by severity (Critical / Important / Minor),
   `Approved | Needs fixes`. Runs focused tests only for a specific doubt.
2. **Fix rounds:** the same implementer fixes Critical/Important findings and
   appends a fix report. At most 3 rounds; after that the tech lead rules on
   each open finding.
3. **Tech lead final review (Opus), before every merge:** checks the seams
   with the rest of the system (contracts, units, transactions, security),
   that the full suite passes on the branch, and that docs
   (ARCHITECTURE.md, docs/WEB_UI.md, README) are updated when behavior
   changed. Verdict: merge, or send back with findings.

Minor findings never block a merge; they go into the ledger as deferred.

### Ledger

`.team/ledger.md` (git-ignored) is the tech lead's record across sessions:
one line per dispatch, question, ruling, review verdict and merge. After a
context reset, trust the ledger and `git log` over memory.

## Rules for every agent

- Read the brief first; it is the requirements. Read only the spec sections
  it names.
- Work only in your worktree and only on the files the brief lists. Need
  another file? Ask.
- Tests first (see the test fail), then code, then the full suite:
  `.venv/bin/pytest -q` against the test Postgres on `127.0.0.1:5433`.
- Stage files by explicit path. Never `git add -A`.
- Commit messages: plain English, no `Co-Authored-By` or AI attribution.
- Never merge, rebase `master`, push, or delete branches.
- Never start, stop or rebuild containers unless the brief says so; never
  touch logai-engine (its code, git, containers).
- Never dispatch other agents. Review is scheduled by the tech lead.
- Not sure → `QUESTION:` and stop.

## How the tech lead runs each implementer

### Sonnet 5.5 (Claude subagent)

`Agent` tool, `model: sonnet`, prompt = one line of context + the brief path
+ the PR file path + "reply with Status / commits / tests / QUESTION or
concerns". Resume with `SendMessage` for answers and fix rounds.

### Gemini 3.8 Flash (Antigravity CLI)

Proven on 2026-10-21 in a throwaway repo: `agy` created a branch, edited
code, wrote and ran a pytest test, committed, and accepted a follow-up in
the same conversation.

```bash
cd ../logai2-task-<id>
agy -p "$(cat docs/tasks/<id>-<slug>.md)" \
    --model gemini-3.8-flash-high \
    --mode accept-edits \
    --print-timeout 1800s \
    --output-format json > .team/runs/<id>-1.json
# → {"conversation_id": ..., "status": "SUCCESS", "response": "DONE <sha>" | "QUESTION: ..."}

# answer a question or send review findings, same conversation:
agy -p "<answer or findings>" --conversation <conversation_id> \
    --model gemini-3.8-flash-high --mode accept-edits \
    --print-timeout 1800s --output-format json > .team/runs/<id>-2.json
```

`scripts/agy-task.sh` wraps these two calls.

Notes:
- Run long Gemini tasks in the background and wait on the run, not by
  polling with short sleeps.
- Antigravity's own settings (`~/.gemini/antigravity-cli/settings.json`)
  currently auto-approve tools and allow access outside the workspace, so
  the brief's file list and the worktree are the real fence. Check
  `git -C <worktree> status` and the diff after every run.
- Gemini commits under the user's git identity; check that its commit
  messages have no attribution lines.

## Templates

- Task brief: [`docs/templates/task-brief.md`](templates/task-brief.md)
- PR file: [`docs/templates/pr.md`](templates/pr.md)
