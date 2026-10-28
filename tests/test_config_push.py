from dataclasses import replace

import pytest

from builders import ALL_PATHS, INVENTORY, MOMBASA, T0, at, flap
from sharectl.controller import Controller
from sharectl.model import DesiredState
from sharectl.stubs import FakeConfigPush, FakeSdn
from sharectl.world import World

HANDOFF = "mba-node-1/eth-1-12"
NEW = "mba-node-1/eth-1-13"


def desired(
    generation: int, handoff: str = HANDOFF, *, agreement: str = "b", version: int = 1
) -> DesiredState:
    return DesiredState(
        handoff=handoff, generation=generation, agreement=agreement, version=version
    )


def row(generation: int, *, outcome: str, applied: int) -> dict[str, object]:
    """Configuration push's action at 03:00:11 on Atlas's Mombasa handoff."""
    return {
        "time": "2026-10-03T03:00:11Z",
        "sequence": 4,
        "handoff": HANDOFF,
        "generation": generation,
        "outcome": outcome,
        "applied": applied,
    }


def ticks(world: World, start: int, end: int) -> None:
    for seconds in range(start, end + 1, 10):
        world.advance(at(seconds))


def pushed(world: World) -> list[str]:
    return [text for _, text in world.notes if text.startswith("config-push")]


def test_a_higher_generation_goes_to_the_sdn_and_is_confirmed_by_site_and_sequence() -> None:
    push = FakeConfigPush(sdn=FakeSdn(), applied={HANDOFF: 7})

    confirmed = push.receive("mombasa", desired(8, version=1), sequence=4, at=at(10))

    assert push.sdn.running == {HANDOFF: desired(8, version=1)}
    assert confirmed == {
        "specversion": "1.0",
        "type": "circuit.config.confirmed",
        "source": "/config-push/site/mombasa",
        "id": "mombasa/4",
        "time": "2026-10-03T03:00:11Z",
        "data": {"handoff": HANDOFF, "generation": 8, "agreement": "b", "version": 1},
    }
    assert push.actions == [row(8, outcome="applied", applied=7)]


def test_a_change_counts_as_applied_only_once_it_is_recorded() -> None:
    push = FakeConfigPush(sdn=FakeSdn(), applied={HANDOFF: 7})
    push.receive("mombasa", desired(8), sequence=4, at=at(10))
    assert (push.applied[HANDOFF], push.sequence) == (7, {})

    push.record("mombasa", 4, desired(8))

    assert (push.applied[HANDOFF], push.sequence) == (8, {"mombasa": 4})


def test_a_reused_handoff_applies_a_higher_generation_carrying_a_lower_version() -> None:
    push = FakeConfigPush(sdn=FakeSdn())
    push.receive("mombasa", desired(7, agreement="a", version=2), sequence=1, at=at(0))
    push.record("mombasa", 1, desired(7, agreement="a", version=2))

    confirmed = push.receive("mombasa", desired(8, agreement="b", version=1), sequence=2, at=at(10))

    assert push.sdn.running[HANDOFF] == desired(8, agreement="b", version=1)
    assert confirmed is not None
    assert confirmed["data"] == {
        "handoff": HANDOFF,
        "generation": 8,
        "agreement": "b",
        "version": 1,
    }


@pytest.mark.parametrize("generation", [7, 6])
def test_a_generation_at_or_below_the_applied_one_is_ignored_and_confirms_nothing(
    generation: int,
) -> None:
    push = FakeConfigPush(sdn=FakeSdn(), applied={HANDOFF: 7})

    assert push.receive("mombasa", desired(generation, version=9), sequence=4, at=at(10)) is None
    assert not push.sdn.running
    assert push.actions == [row(generation, outcome="ignored", applied=7)]


def test_the_sdn_commits_a_state_once_however_often_it_is_applied() -> None:
    sdn = FakeSdn()

    sdn.apply(NEW, desired(1, NEW), at=at(130))
    sdn.apply(NEW, desired(1, NEW), at=at(140))

    assert sdn.commits == [(at(130), NEW, 1)]
    assert sdn.configured == {NEW: at(130)}


def test_a_change_applied_but_not_recorded_comes_again_and_changes_nothing() -> None:
    world = World(INVENTORY, T0)
    record = world.config_push.record
    stops: list[int] = []

    def stop_once(site: str, sequence: int, applied: DesiredState | None) -> None:
        if not stops:
            stops.append(sequence)
            raise RuntimeError(stops)
        record(site, sequence, applied)

    world.config_push.record = stop_once
    world.advance(at(0))
    world.cloud.write("b", 1, [NEW], at=at(0))
    with pytest.raises(RuntimeError):
        world.advance(at(10))
    ticks(world, 20, 40)

    assert world.sdn.commits == [(at(10), NEW, 1)]
    assert [(a["sequence"], a["outcome"]) for a in world.config_push.actions] == [
        (1, "applied"),
        (1, "applied"),
    ]
    assert [e["id"] for e in world.cloud.events.values()] == ["mombasa/1"]


def test_desired_state_written_in_a_cut_is_applied_once_a_data_path_is_stable_for_300_s() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    world.advance(at(0))
    world.cloud.write("b", 1, [NEW], at=at(0))
    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    ticks(world, 10, 300)
    assert not pushed(world)

    world.advance(at(310))

    assert pushed(world) == [f"config-push at mombasa applied gen 1 to {NEW} (above applied 0)"]
    assert world.notes[-1][0] == at(310)


def test_a_flap_inside_the_stable_time_applies_nothing_until_the_path_is_stable_again() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    world.advance(at(0))
    world.cloud.write("b", 1, [NEW], at=at(0))
    flap(world)
    ticks(world, 220, 510)
    assert not pushed(world)

    world.advance(at(520))

    assert [n for n in world.notes if n[1].startswith("config-push")] == [
        (at(520), f"config-push at mombasa applied gen 1 to {NEW} (above applied 0)")
    ]


def test_its_confirmation_ships_once_the_path_is_stable_with_no_controller_running() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    world.advance(at(0))
    world.cloud.write("b", 1, [NEW], at=at(0))
    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    ticks(world, 10, 300)
    assert not world.cloud.events

    world.advance(at(310))
    world.advance(at(320))
    assert [(e["source"], e["data"]["handoff"]) for e in world.cloud.events.values()] == [
        ("/config-push/site/mombasa", NEW)
    ]


def test_the_stream_resumes_after_the_last_change_applied_and_a_resend_is_ignored() -> None:
    world = World(INVENTORY, T0)
    world.advance(at(0))
    world.cloud.write("b", 1, [NEW], at=at(0))
    ticks(world, 10, 20)
    world.cloud.resend(NEW, 1, at(20))
    ticks(world, 30, 40)

    assert pushed(world) == [
        f"config-push at mombasa applied gen 1 to {NEW} (above applied 0)",
        f"config-push at mombasa ignored gen 1 for {NEW}: 1 <= applied 1",
    ]


def test_the_controller_never_applies_desired_state() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.advance(at(0))
    world.cloud.write("b", 1, [NEW], at=at(0))
    for seconds in (10, 20):
        world.advance(at(seconds))
        controller.step()

    assert world.config_push.applied[NEW] == 1
    assert not controller.log


def test_its_lines_are_the_reason_texts_in_the_inventory() -> None:
    texts = {
        **INVENTORY.lines,
        "config_push_applied": "A $site $handoff $generation $applied",
        "config_push_ignored": "I $site $handoff $generation",
    }
    world = World(replace(INVENTORY, lines=texts), T0)
    world.advance(at(0))
    world.cloud.write("b", 1, [NEW], at=at(0))
    world.cloud.resend(NEW, 1, at(0))
    world.advance(at(10))

    assert [text for _, text in world.notes] == [f"A mombasa {NEW} 1 0", f"I mombasa {NEW} 1"]


def test_an_ignored_older_generation_leaves_the_applied_one_as_it_was() -> None:
    world = World(INVENTORY, T0)
    world.advance(at(0))
    world.cloud.write("b", 1, [NEW], at=at(0))
    world.cloud.write("c", 1, [NEW], at=at(0))
    world.cloud.resend(NEW, 1, at(0))
    ticks(world, 10, 20)

    assert world.config_push.applied[NEW] == 2


def test_no_change_is_taken_while_no_config_push_pod_runs_at_the_site() -> None:
    world = World(INVENTORY, T0)
    kube = world.kubes["mombasa"]
    for node in list(kube.nodes):
        kube.cordon(node)
    kube.evict(tuple(p for p in kube.pods if p.service == "config-push"))
    world.advance(at(0))
    world.cloud.write("b", 1, [NEW], at=at(0))
    ticks(world, 10, 60)

    assert not pushed(world)
    assert NEW not in world.config_push.applied
