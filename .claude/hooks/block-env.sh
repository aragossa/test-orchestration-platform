#!/usr/bin/env bash
# PreToolUse guard for Bash: refuse commands that mention a .env file.
# .env.example is allowed. This catches accidental reads (cat, grep, source,
# python open); it is not a security boundary - a glob like `.e*` gets through.
cmd=$(jq -r '.tool_input.command // empty')
stripped=${cmd//.env.example/}
if printf '%s' "$stripped" | grep -Eq '\.env([^A-Za-z0-9_]|$)'; then
  jq -n '{hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: "Commands that touch .env files are blocked in this project (see CLAUDE.md, Production). Use .env.example."
  }}'
fi
exit 0
