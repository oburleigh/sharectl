-include .env
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
