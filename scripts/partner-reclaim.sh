#!/usr/bin/env sh
# Kilima takes back its VM at Mombasa. The controller is told through Kilima's lease API, then
# drains the VM, fences it and releases it. Flags (for example --tick-ms 5) reach every command.
cd "$(dirname "$0")/.." || exit 1
sharectl() { uv run sharectl "$@"; }
started=$(sharectl start --site mombasa "$@") || exit 1
echo "$started"
case "$started" in "controller started"*) trap 'sharectl stop "$@"' EXIT ;; esac
sharectl wait 3 "$@"
sharectl emit reclaim-notice resources=mba-kilima-01 notice_minutes=10 "$@" > /dev/null
sharectl wait 10 "$@"
