#!/usr/bin/env sh
cd "$(dirname "$0")/.." && exec uv run sharectl play "scenarios/fibre-cut.toml" "$@"
