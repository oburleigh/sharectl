from datetime import datetime, timedelta

from builders import T0
from sharectl.model import Interval
from sharectl.stubs import Batch, Coverage, FakeCloud

H = "mba-node-1/eth-1-12"
FIVE = timedelta(minutes=5)
FIRST = datetime(2026, 10, 3, 2, 55, tzinfo=T0.tzinfo)


def interval(n: int) -> Interval:
    return Interval(handoff=H, counter="c", start=FIRST + n * FIVE, bytes=1)


def cloud() -> FakeCloud:
    return FakeCloud(generations={H: 0}, every=FIVE, sites={H: "mombasa"}, configured={H: FIRST})


def test_rejoin_checks_coverage_dropping_resent_duplicates() -> None:
    store = cloud()
    store.receive(Batch(site="mombasa", intervals=(interval(0), interval(1), interval(2))), T0)
    held = tuple(interval(n) for n in range(6, 2, -1))
    resent = (interval(0), interval(1), interval(2))
    rejoin = Batch(site="mombasa", intervals=held, resent=resent, rejoin=True)
    store.receive(rejoin, FIRST + 7 * FIVE + timedelta(seconds=21))

    assert store.coverage == [
        Coverage(
            handoff=H, counter="c", held=4, resent=3, expected=7, unique=7, duplicates=3, missing=0
        )
    ]


def test_an_interval_never_shipped_is_missing_never_interpolated() -> None:
    store = cloud()
    rejoin = Batch(site="mombasa", intervals=(interval(0), interval(2)), rejoin=True)
    store.receive(rejoin, FIRST + 3 * FIVE)

    assert [(c.expected, c.unique, c.missing) for c in store.coverage] == [(3, 2, 1)]


def test_only_a_rejoin_runs_the_check_and_only_for_the_shipping_sites_handoffs() -> None:
    store = cloud()
    store.receive(Batch(site="mombasa", intervals=(interval(0),)), FIRST + FIVE)
    store.receive(Batch(site="barcelona", rejoin=True), FIRST + FIVE)

    assert not store.coverage


def test_a_confirmed_handoff_is_expected_from_the_first_whole_interval_after_it() -> None:
    store = cloud()
    confirmed = {
        "type": "circuit.config.confirmed",
        "time": "2026-10-03T03:02:11Z",
        "data": {"handoff": "new", "generation": 1},
    }
    store.receive(Batch(site="mombasa", events=(confirmed,)), T0)

    assert store.configured["new"] == datetime(2026, 10, 3, 3, 5, tzinfo=T0.tzinfo)


def test_each_write_to_a_handoff_raises_its_generation() -> None:
    store = cloud()

    first = store.write("a", 1, [H])
    second = store.write("b", 1, [H])

    assert [d.generation for d in first + second] == [1, 2]


def test_a_handoff_confirmed_on_a_boundary_is_expected_from_that_interval() -> None:
    store = cloud()
    confirmed = {
        "type": "circuit.config.confirmed",
        "time": "2026-10-03T03:05:00Z",
        "data": {"handoff": "new", "generation": 1},
    }
    store.receive(Batch(site="mombasa", events=(confirmed,)), T0)

    assert store.configured["new"] == datetime(2026, 10, 3, 3, 5, tzinfo=T0.tzinfo)


def test_each_rejoin_adds_its_own_coverage_rows() -> None:
    store = cloud()
    for n in (0, 1):
        store.receive(
            Batch(site="mombasa", intervals=(interval(n),), rejoin=True), FIRST + (n + 1) * FIVE
        )

    assert [(c.expected, c.unique) for c in store.coverage] == [(1, 1), (2, 2)]
