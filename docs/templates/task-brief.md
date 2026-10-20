# Task <id>: <title>

Implementer: <Sonnet 5.5 | Gemini 3.8 Flash> — <one sentence why>
Reviewer: <the other model>
Branch: task/<id>-<slug> (worktree ../logai2-task-<id>, created by the tech lead)
PR file: .team/prs/task-<id>-<slug>.md (from docs/templates/pr.md)

Read docs/TEAM_WORKFLOW.md "Rules for every agent" first. If anything below
is unclear, stop and reply `QUESTION: ...` instead of guessing.

## Why
<one paragraph: where this fits, what the user gets>

## Read first
- Spec: <path + sections>
- Code: <files and functions to read>

## Files
- Create: <paths>
- Modify: <paths>
- Test: <paths>
Touching any other file needs a QUESTION first.

## Interfaces
- Consumes: <exact signatures from existing code>
- Produces: <exact signatures later tasks rely on>

## Requirements
<numbered, testable; exact values copied from the spec>

## Tests (write first, see them fail)
<test names and the assertions that must hold>

## Done when
- [ ] new tests fail before the code and pass after
- [ ] `.venv/bin/pytest -q` passes, output clean
- [ ] commits on the branch, explicit-path staging, no attribution lines
- [ ] PR file filled in
- [ ] reply: `DONE <last sha>` (or `QUESTION: ...`)
