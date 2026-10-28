import json
import time
from datetime import timedelta
lazy from pathlib import Path

import pytest

from builders import INVENTORY, T0
from sharectl.play import AT_ONCE, Live, Run

MOMBASA_ONLY = Run(sites=frozenset({"mombasa"}))


@pytest.fixture(autouse=True)
def bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail a run that does not stop, rather than hang on it."""
    ticks: list[float] = []

    def tick(seconds: float) -> None:
        ticks.append(seconds)
        assert len(ticks) < 100, "the estate ran on past its last tick"

    monkeypatch.setattr(time, "sleep", tick)


def send(signal: Path, name: str, event: dict[str, object]) -> None:
    (signal / "events").mkdir(parents=True, exist_ok=True)
    (signal / "events" / f"{name}.json").write_text(json.dumps(event))


def test_an_event_waiting_lands_in_the_mocks_at_the_next_tick_and_is_taken(tmp_path: Path) -> None:
    shown: list[str] = []
    send(
        tmp_path,
        "1-cut",
        {
            "kind": "paths-down",
            "site": "mombasa",
            "to": ["*"],
            "paths": ["uplink-a", "uplink-b", "oob"],
        },
    )

    Live(INVENTORY, tmp_path, run=MOMBASA_ONLY).listen(
        T0, shown.append, until=T0 + timedelta(seconds=30)
    )

    assert [line[:31] for line in shown if " poll " in line] == ["03:00:01 site/mombasa poll mba-"]
    assert [line.split(" [")[0] for line in shown[-2:]] == [
        "03:00:31 site/mombasa site-isolation@v1",
        "03:00:31 site/mombasa wait@v1",
    ]
    assert not list((tmp_path / "events").glob("*"))


def test_a_reclaim_notice_is_told_to_the_controller_which_drains_at_once(tmp_path: Path) -> None:
    shown: list[str] = []
    send(
        tmp_path,
        "1-notice",
        {"kind": "reclaim-notice", "resources": ["mba-kilima-01"], "notice_minutes": 10},
    )

    Live(INVENTORY, tmp_path, run=MOMBASA_ONLY).listen(T0, shown.append, until=T0)

    assert shown[-1] == (
        "03:00:01 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: "
        "cordon mba-kilima-01 and evict 4 of its 4 pods, each service at or above its min; "
        "fence at 03:05:01, release at 03:08:01 -> applied"
    )


def test_events_waiting_for_one_tick_land_in_name_order(tmp_path: Path) -> None:
    shown: list[str] = []
    send(tmp_path, "2-up", {"kind": "paths-up", "site": "mombasa", "to": ["*"], "paths": ["oob"]})
    send(
        tmp_path, "1-down", {"kind": "paths-down", "site": "mombasa", "to": ["*"], "paths": ["oob"]}
    )

    Live(INVENTORY, tmp_path, run=MOMBASA_ONLY).listen(
        T0, shown.append, until=T0 + timedelta(seconds=10)
    )

    assert [line.split(";")[2] for line in shown if " poll " in line] == [
        " oob bfd 203.0.113.9 up, bgp 3/3 established"
    ]


def test_the_clock_and_the_screen_are_written_each_tick(tmp_path: Path) -> None:
    shown: list[str] = []

    Live(INVENTORY, tmp_path, run=AT_ONCE).listen(
        T0, shown.append, until=T0 + timedelta(seconds=20)
    )

    assert (tmp_path / "clock").read_text() == "2026-10-03T03:00:21Z"
    assert (tmp_path / "screen.txt").read_text().splitlines() == shown
