"""Small builders for decide's tests: Mombasa's controller on the declared inventory."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sharectl.inventory import load
from sharectl.model import Decision, Memory, Pod, State

INVENTORY = load(Path(__file__).parent.parent / "inventory")
T0 = datetime(2026, 10, 3, 3, 0, 1, tzinfo=UTC)
MOMBASA = next(s for s in INVENTORY.sites if s.id == "mombasa")
ALL_PATHS = ("uplink-a", "uplink-b", "oob")
DATA_PATHS = ("uplink-a", "uplink-b")
"""Every path in the inventory, and those that carry data (`oob` is management only)."""


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def state(seconds: int = 0, memory: Memory | None = None, **changes: object) -> State:
    base = State(
        inventory=INVENTORY,
        now=at(seconds),
        site=MOMBASA,
        resources=tuple(r for r in INVENTORY.resources if r.site == MOMBASA.id),
        pods=(),
        regions=3,
        reachable=3,
        stable_since=at(-300),
        data_links_up=2,
        memory=memory or Memory(),
    )
    return replace(base, **changes)


def lines(decisions: list[Decision]) -> list[str]:
    return [f"{d.rule.id} {d.subject} {d.outcome}: {d.reason}" for d in decisions]


KILIMA = "mba-kilima-01"
_ON_KILIMA = ("sdn-controller", "aaa", "config-push", "telemetry-collector")
BOOT = (
    *(Pod(service=s, resource=KILIMA) for s in _ON_KILIMA),
    *(Pod(service=s, resource="mba-srv-1") for s in _ON_KILIMA),
    Pod(service="sdn-controller", resource="mba-srv-2"),
)


RECREATED = (
    ("sdn-controller", "mba-srv-3"),
    ("aaa", "mba-srv-1"),
    ("config-push", "mba-srv-2"),
    ("telemetry-collector", "mba-srv-1"),
)
"""Where Kubernetes recreates the partner VM's four pods at Mombasa, derived from SPEC §4."""


def recreated_rows(time: str) -> list[dict[str, object]]:
    return [
        {"time": time, "site": "mombasa", "action": "recreate", "service": s, "resource": r}
        for s, r in RECREATED
    ]
