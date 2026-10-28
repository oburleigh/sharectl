from dataclasses import replace
from datetime import timedelta

from builders import INVENTORY, KILIMA, MOMBASA, T0, at
from sharectl.controller import Controller
from sharectl.log import line
from sharectl.world import World
lazy from sharectl.stubs import Batch

HANDOFF = "mba-node-1/eth-1-12"


def run(world: World, controller: Controller, span: tuple[int, int]) -> None:
    for seconds in range(span[0], span[1] + 1, 10):
        world.advance(at(seconds))
        controller.step()


def outcomes(controller: Controller) -> list[str]:
    return [
        line(r)
        for r in controller.log
        if r["type"] == "platform.decision" and "outcome" in str(r["id"])
    ]


def test_each_decision_is_logged_proposed_then_outcome_as_a_cloudevent() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.send("mombasa", world.cloud.write("atlas", 1, [HANDOFF])[0])
    run(world, controller, (0, 10))

    proposed, outcome = controller.log[0], controller.log[1]
    assert (proposed["id"], proposed["sequence"], proposed["source"]) == (
        "mba-20261003T030001Z-0001/proposed",
        "1",
        "/controllers/site/mombasa",
    )
    assert outcome["id"] == "mba-20261003T030001Z-0001/outcome"
    assert outcomes(controller) == [
        "03:00:11 site/mombasa desired-state@v1 [desired-state.received] apply-desired-state: "
        f"gen 8 for {HANDOFF} above applied 7; publish circuit.config.confirmed -> applied"
    ]


def test_two_instances_for_one_site_started_apart_never_share_an_id() -> None:
    world = World(INVENTORY, T0)
    first = Controller(world, MOMBASA, T0)
    second = Controller(world, MOMBASA, T0 + timedelta(seconds=1))
    world.cut("mombasa", ["*"], ["primary", "oob"], down=True)
    run(world, first, (0, 30))
    run(world, second, (40, 70))

    assert [r["id"] for r in first.log] == [
        "mba-20261003T030001Z-0001/proposed",
        "mba-20261003T030001Z-0001/outcome",
        "mba-20261003T030001Z-0002/proposed",
        "mba-20261003T030001Z-0002/outcome",
    ]
    assert [r["id"] for r in second.log] == [
        "mba-20261003T030002Z-0001/proposed",
        "mba-20261003T030002Z-0001/outcome",
        "mba-20261003T030002Z-0002/proposed",
        "mba-20261003T030002Z-0002/outcome",
    ]


def test_applied_desired_state_reaches_the_sdn_and_publishes_a_confirmation() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.send("mombasa", world.cloud.write("b", 1, ["mba-node-1/eth-1-13"])[0])
    run(world, controller, (0, 20))

    assert world.sdn.configured["mba-node-1/eth-1-13"] == at(10)
    assert world.sdn.applied == {"mba-node-1/eth-1-13": 1}
    assert [(e["source"], e["id"], e["time"]) for e in world.cloud.events] == [
        ("/controllers/site/mombasa", "mba-node-1/eth-1-13/1", "2026-10-03T03:00:11Z")
    ]
    assert [(e["type"], e["data"]) for e in world.cloud.events] == [
        (
            "circuit.config.confirmed",
            {"handoff": "mba-node-1/eth-1-13", "generation": 1, "agreement": "b", "version": 1},
        )
    ]


def test_an_isolated_site_holds_its_records_and_ships_them_first_on_rejoin_with_overlap() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    run(world, controller, (0, 590))
    shipped = set(world.cloud.intervals)
    world.cut("mombasa", ["*"], ["primary", "oob"], down=True)
    run(world, controller, (600, 1800))
    assert set(world.cloud.intervals) == shipped
    assert not world.cloud.records

    world.cut("mombasa", ["*"], ["primary", "oob"], down=False)
    run(world, controller, (1810, 2120))

    held = [line(r) for r in world.cloud.records.values() if "outcome" in str(r["id"])]
    assert [h.split(" [")[0] for h in held] == [
        "03:10:31 site/mombasa site-isolation@v1",
        "03:10:31 site/mombasa wait@v1",
        "03:35:11 site/mombasa site-rejoin@v1",
        "03:35:11 site/mombasa resume@v1",
    ]
    assert len(shipped) == 2
    assert len(world.cloud.intervals) == 8


def test_a_reclaim_drains_then_fences_then_releases_the_partner_vm() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.notices[KILIMA] = at(600)
    run(world, controller, (0, 10))
    kube = world.kubes["mombasa"]
    assert [p.resource for p in kube.pods if p.resource == KILIMA] == []

    run(world, controller, (20, 300))
    assert world.sealed == {KILIMA}
    assert KILIMA not in kube.nodes
    assert KILIMA not in world.released
    run(world, controller, (310, 480))

    assert world.released == {KILIMA}
    assert [line(r).split(" [")[0] for r in controller.log if r["id"].endswith("outcome")] == [
        "03:00:01 site/mombasa drain-on-reclaim@v1",
        "03:05:01 site/mombasa fence@v1",
        "03:08:01 site/mombasa release@v1",
    ]


def test_a_reclaim_during_the_cut_runs_at_the_isolated_site_and_reaches_the_cloud_on_rejoin() -> (
    None
):
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ["primary", "oob"], down=True)
    run(world, controller, (0, 30))
    world.notices[KILIMA] = at(640)
    run(world, controller, (40, 520))
    assert world.released == {KILIMA}
    assert not world.cloud.records

    world.cut("mombasa", ["*"], ["primary", "oob"], down=False)
    run(world, controller, (530, 840))

    merged = [line(r) for r in world.cloud.records.values() if r["data"]["phase"] == "outcome"]
    assert merged == [
        "03:00:31 site/mombasa site-isolation@v1 [cloud.unreachable] enter-isolated-mode: "
        "0/3 regions reachable for 30 s on all paths -> applied",
        "03:00:31 site/mombasa wait@v1 [blocked] wait: site-bound services keep running on site "
        "resources; nothing moves, as a copy in the cloud could not reach the node -> applied",
        "03:00:41 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: "
        f"cordon {KILIMA} and evict 5 of its 5 pods, each service at or above its min; "
        "fence at 03:05:41, release at 03:08:41 -> applied",
        f"03:05:41 site/mombasa fence@v1 [reclaim.fence-time] fence: {KILIMA} holds 0 pods of "
        "Share's, deleted; identity revoked; node deleted; disks wiped -> applied",
        f"03:08:41 site/mombasa release@v1 [reclaim.release-time] release-lease: release {KILIMA}: "
        "its owner reclaimed it -> applied",
        "03:13:51 site/mombasa site-rejoin@v1 [cloud.reachable] exit-isolated-mode: data path "
        "stable for 300 s; ship 10 records, then 2 intervals -> applied",
        "03:13:51 site/mombasa resume@v1 [unblocked] resume: the site rejoined the cloud "
        "-> applied",
    ]
    drain = next(r for r in controller.log if r["data"]["rule"] == "drain-on-reclaim@v1")
    assert drain["data"]["observed"]["isolated"] is True
    assert drain["data"]["blast_radius"] == {"pods_moved": 5}


def test_the_isolation_record_has_every_field_of_the_platform_decision_event() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ["primary", "oob"], down=True)
    run(world, controller, (0, 30))

    assert controller.log[0] == {
        "specversion": "1.0",
        "type": "platform.decision",
        "source": "/controllers/site/mombasa",
        "id": "mba-20261003T030001Z-0001/proposed",
        "time": "2026-10-03T03:00:31Z",
        "sequence": "1",
        "data": {
            "decision_id": "mba-20261003T030001Z-0001",
            "phase": "proposed",
            "at": "2026-10-03T03:00:31Z",
            "controller": "site/mombasa",
            "trigger": "cloud.unreachable",
            "observed": {"reachable": 0, "regions": 3, "seconds": 30, "isolated": False},
            "rule": "site-isolation@v1",
            "action": "enter-isolated-mode",
            "subject": "mombasa",
            "reason": "0/3 regions reachable for 30 s on all paths",
            "blast_radius": {"pods_moved": 0},
            "rollback": "exit-isolated-mode",
            "page": False,
        },
    }
    assert [r["sequence"] for r in controller.log] == ["1", "2", "3", "4"]


def test_rejoin_resends_the_last_intervals_the_cloud_already_had() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    batches: list[Batch] = []
    receive = world.cloud.receive
    world.cloud.receive = lambda batch, at: (batches.append(batch), receive(batch, at))[1]
    run(world, controller, (0, 1190))
    world.cut("mombasa", ["*"], ["primary", "oob"], down=True)
    run(world, controller, (1200, 1490))
    world.cut("mombasa", ["*"], ["primary", "oob"], down=False)
    run(world, controller, (1500, 1810))

    rejoin = [b for b in batches if b.rejoin]
    assert [f"{i.start:%H:%M}" for b in rejoin for i in b.resent] == ["03:05", "03:10", "03:15"]
    assert [f"{i.start:%H:%M}" for b in rejoin for i in b.intervals] == ["03:20", "03:25"]
    assert not [b for b in batches if b.resent and not b.rejoin]


def test_a_silent_leased_resource_is_drained_fenced_after_grace_and_released() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.silent[KILIMA] = at(0)
    run(world, controller, (0, 650))

    assert outcomes(controller) == [
        "03:00:31 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: "
        f"cordon {KILIMA} and evict 5 of its 5 pods, each service at or above its min; "
        "fence at 03:10:31, release at 03:10:41 -> applied",
        f"03:10:31 site/mombasa fence@v1 [reclaim.fence-time] fence: {KILIMA} holds 0 pods of "
        "Share's, deleted; identity revoked; node deleted; disks wiped -> applied",
        f"03:10:41 site/mombasa release@v1 [reclaim.release-time] release-lease: release {KILIMA}: "
        "its owner reclaimed it -> applied",
    ]


def test_a_refused_decision_is_logged_and_not_acted_on() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    controller.memory = replace(controller.memory, actions=(at(0),) * 10)
    world.silent[KILIMA] = at(0)
    run(world, controller, (0, 40))

    assert outcomes(controller) == [
        "03:00:31 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: "
        "10 unforced actions in the last hour, the cap is 10: frozen, a human decides -> refused"
    ]
    assert controller.log[1]["data"]["page"] is True
    assert len([p for p in world.kubes["mombasa"].pods if p.resource == KILIMA]) == 5
