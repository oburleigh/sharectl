#!/usr/bin/env sh
# The fibre between Mombasa and the cloud is cut for six hours. Nothing tells the controller: its
# polls of the node find every path down. Agreements still go final, and one is resent on rejoin.
# Flags (for example --tick-ms 5) reach every command.
set -f # the cut's fields hold a literal *
cd "$(dirname "$0")/.." || exit 1
sharectl() { uv run sharectl "$@"; }
started=$(sharectl start --site mombasa "$@") || exit 1
echo "$started"
case "$started" in "controller started"*) trap 'sharectl stop "$@"' EXIT ;; esac
cut="site=mombasa to=* paths=uplink-a,uplink-b,oob"
sharectl wait 2 "$@"
sharectl emit agreement-final agreement=atlas-0001 version=1 \
  handoffs=bcn-node-1/eth-1-12,mrs-node-1/eth-1-12,mba-node-1/eth-1-12 "$@" > /dev/null
sharectl wait 10 "$@"
# shellcheck disable=SC2086 # the cut's fields are split on purpose
sharectl emit paths-down $cut "$@" > /dev/null
sharectl wait 108 "$@"
sharectl emit agreement-final agreement=second-buyer-0001 version=1 \
  handoffs=bcn-node-1/eth-1-13,mba-node-1/eth-1-13 "$@" > /dev/null
sharectl wait 252 "$@"
# shellcheck disable=SC2086 # the cut's fields are split on purpose
sharectl emit paths-up $cut "$@" > /dev/null
sharectl wait 5 "$@"
sharectl emit resend-desired handoff=mba-node-1/eth-1-13 generation=1 "$@" > /dev/null
sharectl wait 2 "$@"
