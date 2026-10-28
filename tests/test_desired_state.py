import pytest

from builders import Memory, lines, state
from sharectl.decide import decide
from sharectl.model import DesiredState, Outcome

HANDOFF = "mba-node-1/eth-1-12"
NEW = "mba-node-1/eth-1-13"


def desired(generation: int, handoff: str = HANDOFF, version: int = 1) -> DesiredState:
    return DesiredState(handoff=handoff, generation=generation, agreement="a", version=version)


def test_applies_a_generation_above_the_applied_one_whatever_version_it_carries() -> None:
    applied_v2 = Memory(applied={HANDOFF: 7})
    decisions, memory = decide(state(0, applied_v2, inbound=(desired(8, version=1),)))

    assert lines(decisions) == [
        f"desired-state {HANDOFF} applied: gen 8 for {HANDOFF} above applied 7; "
        "publish circuit.config.confirmed"
    ]
    assert decisions[0].desired == desired(8)
    assert memory.applied[HANDOFF] == 8


def test_a_handoff_never_configured_counts_as_generation_0() -> None:
    decisions, memory = decide(state(0, inbound=(desired(1, NEW),)))

    assert lines(decisions) == [
        f"desired-state {NEW} applied: gen 1 for {NEW} above applied 0; "
        "publish circuit.config.confirmed"
    ]
    assert memory.applied[NEW] == 1


@pytest.mark.parametrize("generation", [7, 6])
def test_ignores_a_generation_at_or_below_the_applied_one(generation: int) -> None:
    decisions, memory = decide(state(0, inbound=(desired(generation, version=9),)))

    assert lines(decisions) == [
        f"desired-state {HANDOFF} ignored: gen {generation} for {HANDOFF} again: "
        f"{generation} <= applied 7"
    ]
    assert decisions[0].outcome is Outcome.IGNORED
    assert memory.applied[HANDOFF] == 7


def test_while_isolated_queues_and_decides_only_the_newest_generation_on_rejoin() -> None:
    isolated = Memory(isolated=True, applied={HANDOFF: 7})
    first, memory = decide(state(0, isolated, reachable=0, data_up=False, inbound=(desired(9),)))
    second, memory = decide(state(10, memory, inbound=(desired(8), desired(1, NEW))))
    rejoined, memory = decide(state(310, memory))

    assert [d.rule.id for d in first + second] == ["wait"]
    assert lines(rejoined) == [
        "site-rejoin mombasa applied: data path stable for 300 s; ship 0 records, then 0 intervals",
        f"desired-state {HANDOFF} applied: gen 9 for {HANDOFF} above applied 7; "
        "publish circuit.config.confirmed",
        f"desired-state {NEW} applied: gen 1 for {NEW} above applied 0; "
        "publish circuit.config.confirmed",
        "resume mombasa applied: the site rejoined the cloud",
    ]
    assert memory.queued == {}


def test_an_ignored_generation_does_not_stop_the_next_handoff_being_applied() -> None:
    decisions, _ = decide(state(0, inbound=(desired(7), desired(1, NEW))))

    assert [(d.subject, d.outcome) for d in decisions] == [
        (HANDOFF, Outcome.IGNORED),
        (NEW, Outcome.APPLIED),
    ]


def test_the_same_generation_twice_in_one_tick_is_applied_once() -> None:
    decisions, _ = decide(state(0, inbound=(desired(8), desired(8))))

    assert [d.outcome for d in decisions] == [Outcome.APPLIED, Outcome.IGNORED]
    assert decisions[1].reason == f"gen 8 for {HANDOFF} again: 8 <= applied 8"


def test_a_stale_queued_generation_is_ignored_and_dropped_on_rejoin() -> None:
    isolated = Memory(isolated=True, applied={HANDOFF: 7})
    _, memory = decide(state(0, isolated, reachable=0, data_up=False, inbound=(desired(7),)))
    _, memory = decide(state(10, memory))
    rejoined, memory = decide(state(310, memory))

    assert [d.outcome for d in rejoined if d.rule.id == "desired-state"] == [Outcome.IGNORED]
    assert memory.queued == {}
