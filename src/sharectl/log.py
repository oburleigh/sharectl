"""The decision log: decisions as `platform.decision` CloudEvents, and their screen lines."""

from typing import Any
lazy from datetime import datetime

type Record = dict[str, Any]

DECISION = "platform.decision"
CONFIRMED = "circuit.config.confirmed"


def stamp(at: datetime) -> str:
    """`at` as RFC 3339 UTC, to the second."""
    return f"{at:%Y-%m-%dT%H:%M:%SZ}"


def envelope(kind: str, *, source: str, key: str, at: datetime, data: Record) -> Record:
    """A CloudEvents 1.0 envelope; the caller sets `sequence`, which rises per source."""
    return {
        "specversion": "1.0",
        "type": kind,
        "source": source,
        "id": key,
        "time": stamp(at),
        "data": data,
    }


def line(record: Record) -> str:
    """One decision's outcome record as a screen line: time, controller, rule, trigger, reason."""
    data = record["data"]
    return (
        f"{data['at'][11:19]} {data['controller']} {data['rule']} [{data['trigger']}] "
        f"{data['action']}: {data['reason']} -> {data['outcome']}"
    )
