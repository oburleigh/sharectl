#!/usr/bin/env sh
cd "$(dirname "$0")/.." || exit 1
sharectl() { uv run sharectl "$@"; }
sharectl wait 3 "$@" || { echo "no controller is running: start it first with make run" >&2; exit 1; }
sharectl emit reclaim-notice resources=mba-kilima-01 notice_minutes=10 "$@" > /dev/null
sharectl wait 10 "$@"
