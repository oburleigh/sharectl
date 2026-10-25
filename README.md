# sharectl

[![Verify](https://github.com/oburleigh/sharectl/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/oburleigh/sharectl/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/github/oburleigh/sharectl/graph/badge.svg)](https://app.codecov.io/github/oburleigh/sharectl)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue)](LICENSE)
![Python 3.15](https://img.shields.io/badge/python-3.15-blue)

A self-healing controller for services that run across PoPs, cloud regions and partner machines.
It reads a declared inventory, watches health, re-homes services when a site is cut or a machine
is reclaimed, reconciles when the site returns, and logs every decision with its reason.

## Requirements

- [uv](https://docs.astral.sh/uv/) 0.13.0 or later, the first release that installs CPython
  3.15.0. On an older uv, run `uv self update`.
- Network access on the first run, to download Python 3.15 and the locked dependencies.

## Run it

```sh
uv run sharectl --version
```

## Test it

```sh
uv run pytest
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports go through [SECURITY.md](SECURITY.md).

## Licence

[MIT](LICENSE)
