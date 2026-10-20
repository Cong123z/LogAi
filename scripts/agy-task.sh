#!/usr/bin/env bash
# Run a Gemini (Antigravity CLI) implementer on a task brief, or continue its
# conversation with an answer / review findings. See docs/TEAM_WORKFLOW.md.
#
#   scripts/agy-task.sh start    <worktree> <brief.md> <run-name>
#   scripts/agy-task.sh continue <worktree> <conversation-id> <message-file> <run-name>
#
# Writes the JSON result to <worktree>/.team/runs/<run-name>.json and prints
# the conversation id, status and response.
set -euo pipefail

MODEL="${AGY_MODEL:-gemini-3.8-flash-high}"
TIMEOUT="${AGY_TIMEOUT:-1800s}"

cmd="${1:?start|continue}"; worktree="${2:?worktree}"
mkdir -p "$worktree/.team/runs"
cd "$worktree"

case "$cmd" in
  start)
    brief="${3:?brief}"; run="${4:?run-name}"
    prompt="$(cat "$brief")"
    extra=()
    ;;
  continue)
    conv="${3:?conversation id}"; msg="${4:?message file}"; run="${5:?run-name}"
    prompt="$(cat "$msg")"
    extra=(--conversation "$conv")
    ;;
  *) echo "usage: $0 start|continue ..." >&2; exit 2 ;;
esac

out=".team/runs/$run.json"
agy -p "$prompt" "${extra[@]}" --model "$MODEL" --mode accept-edits \
    --print-timeout "$TIMEOUT" --output-format json > "$out"

python3 - "$out" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
print("conversation:", r.get("conversation_id"))
print("status:", r.get("status"))
print("response:", (r.get("response") or "").strip())
PY
