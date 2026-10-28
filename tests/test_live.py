import threading
from datetime import timedelta

import pytest

from builders import INVENTORY, T0
from sharectl.play import Live, Run

MOMBASA_ONLY = Run(sites=frozenset({"mombasa"}))
pytestmark = pytest.mark.usefixtures("bounded")


def test_an_event_sent_lands_in_the_mocks_at_the_next_tick_and_is_taken_once() -> None:
    shown: list[str] = []
    live = Live(INVENTORY, run=MOMBASA_ONLY)
    live.send(
        {
            "kind": "paths-down",
            "site": "mombasa",
            "to": ["*"],
            "paths": ["uplink-a", "uplink-b", "oob"],
        },
    )

    live.listen(T0, shown.append, until=T0 + timedelta(seconds=30))

    assert [line[:31] for line in shown if " poll " in line] == ["03:00:01 site/mombasa poll mba-"]
    assert [line.split(" [")[0] for line in shown[-2:]] == [
        "03:00:31 site/mombasa site-isolation@v1",
        "03:00:31 site/mombasa wait@v1",
    ]


def test_a_reclaim_notice_is_told_to_the_controller_which_drains_at_once() -> None:
    shown: list[str] = []
    live = Live(INVENTORY, run=MOMBASA_ONLY)
    live.send({"kind": "reclaim-notice", "resources": ["mba-kilima-01"], "notice_minutes": 10})

    live.listen(T0, shown.append, until=T0)

    assert shown[-1] == (
        "03:00:01 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: "
        "cordon mba-kilima-01 and evict 4 of its 4 pods, each service at or above its min; "
        "fence at 03:05:01, release at 03:08:01 -> applied"
    )


def test_events_sent_within_one_tick_land_in_the_order_sent() -> None:
    shown: list[str] = []
    live = Live(INVENTORY, run=MOMBASA_ONLY)
    live.send({"kind": "paths-down", "site": "mombasa", "to": ["*"], "paths": ["oob"]})
    live.send({"kind": "paths-up", "site": "mombasa", "to": ["*"], "paths": ["oob"]})

    live.listen(T0, shown.append, until=T0 + timedelta(seconds=10))

    assert [line.split(";")[2] for line in shown if " poll " in line] == [
        " oob bfd 203.0.113.9 up, bgp 3/3 established"
    ]


def waited(live: Live, minutes: int, within: float = 5) -> list[str] | None:
    """What `live.wait(minutes)` gives, or None when it is still waiting after `within` seconds."""
    given: list[list[str]] = []
    waiting = threading.Thread(target=lambda: given.append(list(live.wait(minutes))), daemon=True)
    waiting.start()
    waiting.join(within)
    return given[0] if given else None


def test_wait_gives_every_line_not_shown_yet_once_the_clock_has_moved_on() -> None:
    shown: list[str] = []
    live = Live(INVENTORY, run=MOMBASA_ONLY)
    live.listen(T0, shown.append, until=T0 + timedelta(seconds=20))

    assert waited(live, 0) == shown
    again = waited(live, 0)
    assert again is not None, "the second wait is still waiting"
    assert not again


def test_wait_keeps_waiting_until_the_clock_has_moved_the_minutes_on() -> None:
    live = Live(INVENTORY, run=MOMBASA_ONLY)
    live.listen(T0, lambda _: None, until=T0 + timedelta(seconds=20))

    assert waited(live, 1, within=0.2) is None


def test_wait_waits_for_the_first_tick() -> None:
    shown: list[str] = []
    given: list[list[str]] = []
    live = Live(INVENTORY, run=MOMBASA_ONLY)
    waiting = threading.Thread(target=lambda: given.append(list(live.wait(0))), daemon=True)
    waiting.start()
    waiting.join(timeout=0.2)
    blocked = waiting.is_alive()

    live.listen(T0, shown.append, until=T0 + timedelta(seconds=20))
    waiting.join(timeout=10)

    assert blocked
    assert given
    assert given[0][0] == "controllers: site/mombasa"
    assert given[0] == shown[: len(given[0])]
