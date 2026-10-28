import ast
from pathlib import Path

from builders import INVENTORY

SOURCE = Path(__file__).parent.parent / "src" / "sharectl"
NAMES = {
    *(s.id for s in INVENTORY.sites),
    *(s.code for s in INVENTORY.sites),
    *(r.id for r in INVENTORY.resources),
    *(r.kind for r in INVENTORY.resources),
    *(r.provider for r in INVENTORY.resources),
    *(n.id for n in INVENTORY.nodes),
    *(h.id for h in INVENTORY.handoffs),
    *(s.id for s in INVENTORY.services),
    *INVENTORY.network.paths,
    *INVENTORY.telemetry.counters,
}


def test_no_module_names_anything_from_the_inventory() -> None:
    found = [
        f"{path.name}:{node.lineno} {node.value!r}"
        for path in sorted(SOURCE.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant) and node.value in NAMES
    ]

    assert found == []
