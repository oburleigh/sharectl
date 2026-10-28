from dataclasses import replace
from datetime import datetime, timedelta

from builders import ALL_PATHS, INVENTORY, KILIMA, MOMBASA, T0, at
from sharectl.controller import Controller
from sharectl.log import line
from sharectl.model import Layer
from sharectl.world import World

HANDOFF = "mba-node-1/eth-1-12"
POLL_UP = (
    "site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; "
    "uplink-b bfd 198.51.100.9 up, bgp 3/3 established; "
    "oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable"
)
POLL_DOWN = (
    "site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 down; uplink-b bfd 198.51.100.9 down; "
    "oob bfd 203.0.113.9 down; 0/3 regions reachable"
)


def run(world: World, controller: Controller, span: tuple[int, int]) -> None:
    for seconds in range(span[0], span[1] + 1, 10):
        world.advance(at(seconds))
        controller.step()


def mombasa(world: World) -> set[tuple[str, str, datetime]]:
    """The keys of the intervals the cloud holds from Mombasa's handoffs."""
    return {key for key in world.cloud.intervals if key[0].startswith("mba-")}


def polls(world: World) -> list[tuple[datetime, str]]:
    return [(t, text) for t, text in world.notes if " poll " in text]


def outcomes(controller: Controller) -> list[str]:
    return [
        line(r)
        for r in controller.log
        if r["type"] == "platform.decision" and "outcome" in str(r["id"])
    ]


def test_each_decision_is_logged_proposed_then_outcome_as_a_cloudevent() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    run(world, controller, (0, 30))

    proposed, outcome = controller.log[0], controller.log[1]
    assert (proposed["id"], proposed["sequence"], proposed["source"]) == (
        "mba-20261003T030001Z-0001/proposed",
        "1",
        "/controllers/site/mombasa",
    )
    assert outcome["id"] == "mba-20261003T030001Z-0001/outcome"
    assert outcomes(controller) == [
        "03:00:31 site/mombasa site-isolation@v1 [cloud.unreachable] enter-isolated-mode: "
        "0/3 regions reachable for 30 s on all paths -> applied",
        "03:00:31 site/mombasa wait@v1 [blocked] wait: site-bound services keep running on site "
        "resources; nothing moves, as a copy in the cloud could not reach the node -> applied",
    ]


def test_two_instances_for_one_site_started_apart_never_share_an_id() -> None:
    world = World(INVENTORY, T0)
    first = Controller(world, MOMBASA, T0)
    second = Controller(world, MOMBASA, T0 + timedelta(seconds=1))
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
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


def test_an_isolated_site_holds_its_records_and_ships_them_first_on_rejoin_with_overlap() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    run(world, controller, (0, 590))
    shipped = mombasa(world)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    run(world, controller, (600, 1800))
    assert mombasa(world) == shipped
    assert not world.cloud.records

    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    run(world, controller, (1810, 2130))

    held = [line(r) for r in world.cloud.records.values() if "outcome" in str(r["id"])]
    assert [h.split(" [")[0] for h in held] == [
        "03:10:31 site/mombasa site-isolation@v1",
        "03:10:31 site/mombasa wait@v1",
        "03:35:11 site/mombasa site-rejoin@v1",
        "03:35:11 site/mombasa resume@v1",
    ]
    assert len(shipped) == 2
    assert len(mombasa(world)) == 8


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
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    run(world, controller, (0, 30))
    world.notices[KILIMA] = at(640)
    run(world, controller, (40, 520))
    assert world.released == {KILIMA}
    assert not world.cloud.records

    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    run(world, controller, (530, 850))

    merged = [line(r) for r in world.cloud.records.values() if r["data"]["phase"] == "outcome"]
    assert merged == [
        "03:00:31 site/mombasa site-isolation@v1 [cloud.unreachable] enter-isolated-mode: "
        "0/3 regions reachable for 30 s on all paths -> applied",
        "03:00:31 site/mombasa wait@v1 [blocked] wait: site-bound services keep running on site "
        "resources; nothing moves, as a copy in the cloud could not reach the node -> applied",
        "03:00:41 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: "
        f"cordon {KILIMA} and evict 4 of its 4 pods, each service at or above its min; "
        "fence at 03:05:41, release at 03:08:41 -> applied",
        f"03:05:41 site/mombasa fence@v1 [reclaim.fence-time] fence: {KILIMA} holds 0 pods of "
        "Share's, deleted; identity revoked; node deleted; disk key revoked; disks wiped "
        "-> applied",
        f"03:08:41 site/mombasa release@v1 [reclaim.release-time] release-lease: release {KILIMA}: "
        "its owner reclaimed it -> applied",
        "03:13:51 site/mombasa site-rejoin@v1 [cloud.reachable] exit-isolated-mode: data path "
        "stable for 300 s -> applied",
        "03:13:51 site/mombasa resume@v1 [unblocked] resume: the site rejoined the cloud "
        "-> applied",
    ]
    drain = next(r for r in controller.log if r["data"]["rule"] == "drain-on-reclaim@v1")
    assert drain["data"]["observed"]["isolated"] is True
    assert drain["data"]["blast_radius"] == {"pods_moved": 4}


def test_the_isolation_record_has_every_field_of_the_platform_decision_event() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
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
            "observed": {
                "reachable": 0,
                "regions": 3,
                "seconds": 30,
                "data_links_up": 0,
                "isolated": False,
            },
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


def test_a_silent_leased_resource_is_drained_fenced_after_grace_and_released() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.silent[KILIMA] = at(0)
    run(world, controller, (0, 650))

    assert outcomes(controller) == [
        "03:00:31 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: "
        f"cordon {KILIMA} and evict 4 of its 4 pods, each service at or above its min; "
        "fence at 03:10:31, release at 03:10:41 -> applied",
        f"03:10:31 site/mombasa fence@v1 [reclaim.fence-time] fence: {KILIMA} holds 0 pods of "
        "Share's, deleted; identity revoked; node deleted; disk key revoked; disks wiped "
        "-> applied",
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
    assert len([p for p in world.kubes["mombasa"].pods if p.resource == KILIMA]) == 4


def test_one_uplinks_link_down_with_the_other_up_is_not_isolation() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ["uplink-a", "oob"], down=True)
    run(world, controller, (0, 120))

    assert not controller.log


def test_every_region_session_down_with_the_links_up_isolates_and_says_so() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["E1", "E2", "E3"], ALL_PATHS, down=True, layer=Layer.SESSION)
    run(world, controller, (0, 30))

    assert outcomes(controller)[0] == (
        "03:00:31 site/mombasa site-isolation@v1 [cloud.unreachable] enter-isolated-mode: "
        "0/3 regions reachable for 30 s on all paths -> applied"
    )
    assert controller.log[0]["data"]["observed"]["data_links_up"] == 2


def test_a_cloud_service_failing_changes_nothing() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    cells = [world.kubes[region] for region in ("E1", "E2", "E3")]
    for cell in cells:
        for node in list(cell.nodes):
            cell.cordon(node)
        cell.evict(tuple(cell.pods))
    run(world, controller, (0, 120))

    assert all(cell.pods for cell in cells)
    assert all(p.resource is None for cell in cells for p in cell.pods)
    assert not controller.log


def test_the_controller_reports_its_first_poll_then_every_5_minutes_while_nothing_changes() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    run(world, controller, (0, 600))

    assert polls(world) == [(at(0), POLL_UP), (at(300), POLL_UP), (at(600), POLL_UP)]


def test_a_poll_that_differs_is_reported_at_once_and_the_5_minutes_count_from_it() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    run(world, controller, (0, 20))
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    run(world, controller, (30, 330))

    assert polls(world) == [(at(0), POLL_UP), (at(30), POLL_DOWN), (at(330), POLL_DOWN)]


def test_a_session_down_on_a_link_that_is_up_shows_in_the_poll() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["E1"], ["uplink-b"], down=True, layer=Layer.SESSION)
    run(world, controller, (0, 0))

    assert polls(world) == [
        (at(0), POLL_UP.replace("198.51.100.9 up, bgp 3/3", "198.51.100.9 up, bgp 2/3"))
    ]


def test_the_poll_texts_and_how_often_it_repeats_are_the_inventory_s() -> None:
    texts = {
        **INVENTORY.lines,
        "poll": "P $controller $node $reachable $regions [$paths]",
        "poll_link_up": "U $path $peer $sessions $regions",
        "poll_link_down": "D $path $peer",
    }
    params = replace(INVENTORY.params, poll_report_minutes=1)
    world = World(replace(INVENTORY, lines=texts, params=params), T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ["uplink-a"], down=True)
    run(world, controller, (0, 60))

    assert [(t, text) for t, text in world.notes if text.startswith("P ")] == [
        (
            at(s),
            "P site/mombasa mba-node-1 3 3 "
            "[D uplink-a 192.0.2.9; U uplink-b 198.51.100.9 3 3; U oob 203.0.113.9 3 3]",
        )
        for s in (0, 60)
    ]
