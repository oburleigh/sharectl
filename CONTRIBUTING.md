# Contributing

## Set up

```sh
uv sync
uv run pre-commit install
```

`pre-commit install` registers the commit, commit-message and pre-push hooks listed in
`.pre-commit-config.yaml`.

## Gates

Every change passes these before it is pushed, and CI runs the same set:

```sh
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pylint src tests
uv run pytest --cov
```

## Commits

Messages follow [Conventional Commits](https://www.conventionalcommits.org/) with a required
scope, checked by commitlint on every commit and again in CI. The rules, including the allowed
types and scopes, are in `commitlint.config.mjs`.

## Tests

Tests assert behaviour, not implementation details. An expected result is
written from the rules, never copied from the program's own output.
