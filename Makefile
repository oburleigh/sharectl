# Shortcuts for the commands in the README. Pass flags with ARGS, for example
# make fibre-cut ARGS="--tick-ms 5". Settings in .env, when it exists, reach every command.
# The scenario targets always run Mombasa's controller, as their scripts do.

-include .env
# run, demo and test use Mombasa's controller unless .env or the shell names another PoP.
SHARECTL_SITE ?= mombasa
export

.PHONY: install fibre-cut partner-reclaim run demo test

install:
	uv sync

fibre-cut:
	./scripts/fibre-cut.sh $(ARGS)

partner-reclaim:
	./scripts/partner-reclaim.sh $(ARGS)

run:
	uv run sharectl run $(ARGS)

demo:
	uv run sharectl demo $(ARGS)

test:
	uv run pytest $(ARGS)
