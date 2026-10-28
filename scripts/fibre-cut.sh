#!/usr/bin/env sh
cd "$(dirname "$0")/.." && exec uv run sharectl play --site mombasa "scenarios/fibre-cut.toml" "$@"
