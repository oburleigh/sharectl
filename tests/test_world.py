from dataclasses import replace
from datetime import timedelta

from builders import (
    ALL_PATHS,
    DATA_PATHS,
    INVENTORY,
    KILIMA,
    RECREATED,
    T0,
    at,
    reached,
    recreated_rows,
)
from sharectl.model import DesiredState, Interval, Layer
from sharectl.stubs import Batch
from sharectl.world import World

BATCH = Batch(site="mombasa", records=({"id": "x/proposed"},))


def holding(*sites: str) -> World:
    """A world whose `sites` have no data path, so their site logs hold what they measure."""
    world = World(INVENTORY, T0)
    for site in sites:
        world.cut(site, ["*"], DATA_PATHS, down=True)
    return world


def held(world: World, site: str) -> list[Interval]:
    """Take the intervals `site`'s log holds."""
    log = world.site_logs[site]
    taken, log.intervals = log.intervals, []
    return taken


def ticks(world: World, start: int, end: int) -> None:
    for seconds in range(start, end + 1, 10):
        world.advance(at(seconds))


def test_cutting_every_path_from_a_site_leaves_the_others_reaching_the_regions() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)

    assert (reached(world, "mombasa"), world.data_path_up("mombasa")) == (0, False)
    assert (reached(world, "barcelona"), world.data_path_up("barcelona")) == (3, True)


def test_a_management_path_reaches_the_regions_but_carries_no_data() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], DATA_PATHS, down=True)

    assert (reached(world, "mombasa"), world.data_path_up("mombasa")) == (3, False)


def test_a_message_waits_for_a_data_path_then_arrives_at_the_next_tick() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], DATA_PATHS, down=True)
    world.advance(at(0))
    world.send("mombasa", BATCH)
    world.advance(at(10))
    assert not world.cloud.records

    world.cut("mombasa", ["*"], DATA_PATHS, down=False)
    world.advance(at(20))
    assert world.cloud.records == {"x/proposed": {"id": "x/proposed"}}


def test_a_message_sent_this_tick_is_not_delivered_until_the_next() -> None:
    world = World(INVENTORY, T0)
    world.advance(at(0))
    world.send("mombasa", BATCH)
    world.advance(at(0))

    assert not world.cloud.records


def test_a_batch_from_a_site_reaches_the_cloud_store() -> None:
    world = World(INVENTORY, T0)
    world.advance(at(0))
    world.send("mombasa", Batch(site="mombasa", records=({"id": "x/proposed"},)))
    world.advance(at(10))

    assert world.cloud.records == {"x/proposed": {"id": "x/proposed"}}


def test_collectors_poll_each_configured_handoff_into_the_interval_before_the_boundary() -> None:
    world = holding("mombasa")
    world.advance(T0)
    late = held(world, "mombasa")
    world.advance(T0 + timedelta(minutes=5, seconds=9))  # a late tick
    later = held(world, "mombasa")

    start = T0 - timedelta(minutes=5, seconds=1)
    assert [(i.handoff, i.start) for i in late] == [("mba-node-1/eth-1-12", start)]
    assert [i.start for i in later] == [start + timedelta(minutes=5)]
    assert not held(world, "mombasa")


def test_a_handoff_configured_later_is_first_measured_over_the_next_whole_interval() -> None:
    world = holding("mombasa")
    new = DesiredState(handoff="mba-node-1/eth-1-13", generation=1, agreement="a", version=1)
    world.sdn.apply("mba-node-1/eth-1-13", new, at=at(130))
    ticks(world, 0, 600)

    starts = [i.start for i in held(world, "mombasa") if i.handoff.endswith("13")]
    assert starts == [at(299)]


def test_kubernetes_recreates_evicted_pods_at_the_next_tick_and_says_so() -> None:
    world = World(INVENTORY, T0)
    kube = world.kubes["mombasa"]
    kube.cordon(KILIMA)
    kube.evict(tuple(p for p in kube.pods if p.resource == KILIMA))
    world.advance(at(10))

    assert world.notes == [
        (at(10), f"kubernetes at mombasa recreated {s} on {r}") for s, r in RECREATED
    ]
    assert world.kubernetes == recreated_rows("2026-10-03T03:00:11Z")


def test_aaa_answers_from_the_local_copy_while_the_site_cannot_reach_the_cloud() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    ticks(world, 0, 20)

    assert [n for n in world.notes if "aaa" in n[1]] == [
        (at(0), "aaa at mombasa still answers from the local credential copy")
    ]


def test_aaa_says_so_each_time_the_site_loses_the_cloud_and_nothing_when_it_returns() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    world.advance(at(0))
    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    world.advance(at(10))
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    world.advance(at(20))

    assert [n for n in world.notes if "aaa" in n[1]] == [
        (at(0), "aaa at mombasa still answers from the local credential copy"),
        (at(20), "aaa at mombasa still answers from the local credential copy"),
    ]


def test_aaa_says_nothing_where_no_credential_cache_runs() -> None:
    services = tuple(replace(s, roles=()) if s.id == "aaa" else s for s in INVENTORY.services)
    world = World(replace(INVENTORY, services=services), T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    world.advance(at(0))

    assert [n for n in world.notes if "aaa" in n[1]] == []


def test_each_path_reports_its_link_and_reaches_a_region_only_with_its_session() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ["uplink-a"], down=True)
    world.cut("mombasa", ["E1"], ["uplink-b"], down=True, layer=Layer.SESSION)
    world.cut("E2", ["*"], ["oob"], down=True)

    assert world.links_up("mombasa") == [("uplink-a", False), ("uplink-b", True), ("oob", True)]
    assert [
        [r.id for r in world.inventory.regions if world.reaches("mombasa", r.id, path)]
        for path in ALL_PATHS
    ] == [[], ["E2", "E3"], ["E1", "E3"]]


def test_sessions_cut_to_named_sites_leave_the_others_reachable() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["E1"], ALL_PATHS, down=True, layer=Layer.SESSION)

    assert (reached(world, "mombasa"), world.data_path_up("mombasa")) == (2, True)


def test_one_uplinks_link_down_leaves_every_region_reachable_on_the_other() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ["uplink-a"], down=True)

    assert (reached(world, "mombasa"), world.data_path_up("mombasa")) == (3, True)
    assert [up for _, up in world.links_up("mombasa")] == [False, True, True]


def test_a_link_down_takes_every_session_on_it_down() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["E1"], DATA_PATHS, down=True)

    assert (reached(world, "mombasa"), world.data_path_up("mombasa")) == (3, False)
    assert [up for _, up in world.links_up("mombasa")] == [False, False, True]
    assert (reached(world, "barcelona"), world.data_path_up("barcelona")) == (3, True)


def test_every_region_session_down_leaves_no_region_reachable_with_the_links_up() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["E1", "E2", "E3"], ALL_PATHS, down=True, layer=Layer.SESSION)

    assert (reached(world, "mombasa"), world.data_path_up("mombasa")) == (0, False)
    assert [up for _, up in world.links_up("mombasa")] == [True, True, True]


def test_a_session_comes_back_only_with_its_link() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    world.cut("mombasa", ["*"], ALL_PATHS, down=False, layer=Layer.SESSION)
    assert (reached(world, "mombasa"), world.data_path_up("mombasa")) == (0, False)

    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    assert (reached(world, "mombasa"), world.data_path_up("mombasa")) == (3, True)


def test_a_poll_boundary_is_measured_once_however_often_the_clock_lands_on_it() -> None:
    world = holding("mombasa")
    boundary = T0 + timedelta(minutes=4, seconds=59)
    world.advance(boundary)
    world.advance(boundary)

    assert [(i.start, i.bytes) for i in held(world, "mombasa")] == [
        (boundary - timedelta(minutes=5), 62_500_000 * 300)
    ]


def test_a_site_with_no_collector_running_measures_nothing() -> None:
    world = holding("mombasa", "barcelona")
    kube = world.kubes["mombasa"]
    for resource in list(kube.nodes):
        kube.cordon(resource)
    kube.evict(tuple(p for p in kube.pods if p.service == "telemetry-collector"))
    world.advance(T0)

    assert not held(world, "mombasa")
    assert len(held(world, "barcelona")) == 1


def test_the_kubernetes_and_aaa_lines_are_the_texts_in_the_inventory() -> None:
    texts = {
        **INVENTORY.lines,
        "kubernetes_recreated": "K $site $service $resource",
        "aaa_local": "L $site",
    }
    world = World(replace(INVENTORY, lines=texts), T0)
    kube = world.kubes["mombasa"]
    kube.cordon(KILIMA)
    kube.evict(tuple(p for p in kube.pods if p.resource == KILIMA))
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    world.advance(at(0))
    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    world.advance(at(10))

    assert [text for _, text in world.notes] == [
        *(f"K mombasa {s} {r}" for s, r in RECREATED),
        "L mombasa",
    ]


def test_a_session_cut_to_every_other_site_leaves_the_links_up_and_no_region() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True, layer=Layer.SESSION)

    assert (reached(world, "mombasa"), world.data_path_up("mombasa")) == (0, False)
    assert [up for _, up in world.links_up("mombasa")] == [True, True, True]
    assert (reached(world, "barcelona"), world.data_path_up("barcelona")) == (3, True)
