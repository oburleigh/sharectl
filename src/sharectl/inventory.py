"""Load the inventory files and validate them once, at start."""

import tomllib
lazy from pathlib import Path

from pydantic import TypeAdapter

from sharectl.model import Inventory


def load(directory: Path) -> Inventory:
    """Read `sites.toml`, `services.toml` and `rules.toml` from `directory` as one inventory.

    Raises:
        pydantic.ValidationError: A file is invalid; the message names the field and the reason.
    """
    data: dict[str, object] = {}
    for name in ("sites", "services", "rules"):
        data |= tomllib.loads((directory / f"{name}.toml").read_text())
    return TypeAdapter(Inventory).validate_python(data)
