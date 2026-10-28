from datetime import timedelta

from builders import INVENTORY, KILIMA, RECREATED, T0, at, recreated_rows
from sharectl.model import DesiredState
from sharectl.stubs import Batch
from sharectl.world import World

GEN = DesiredState(handoff="mba-node-1/eth-1-12", generation=8, agreement="a", version=1)


def ticks(world: World, start: int, end: int) -> None:
    for seconds in range(start, end + 1, 10):
        world.advance(at(seconds))


def test_cutting_every_path_from_a_site_leaves_the_others_reaching_the_regions() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ["primary", "oob"], down=True)

    assert world.regions_reachable("mombasa") == (0, False)
    assert world.regions_reachable("barcelona") == (3, True)


def test_a_management_path_reaches_the_regions_but_carries_no_data() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ["primary"], down=True)

    assert world.regions_reachable("mombasa") == (3, False)


def test_a_message_waits_for_a_data_path_then_arrives_at_the_next_tick() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ["primary"], down=True)
    world.advance(at(0))
    world.send("mombasa", GEN)
    world.advance(at(10))
    assert not world.take("mombasa")

    world.cut("mombasa", ["*"], ["primary"], down=False)
    world.advance(at(20))
    assert world.take("mombasa") == (GEN,)
    assert not world.take("mombasa")


def test_a_message_sent_this_tick_is_not_delivered_until_the_next() -> None:
    world = World(INVENTORY, T0)
    world.advance(at(0))
    world.send("mombasa", GEN)
    world.advance(at(0))

    assert not world.take("mombasa")


def test_a_batch_from_a_site_reaches_the_cloud_store() -> None:
    world = World(INVENTORY, T0)
    world.advance(at(0))
    world.send("mombasa", Batch(site="mombasa", records=({"id": "x/proposed"},)))
    world.advance(at(10))

    assert world.cloud.records == {"x/proposed": {"id": "x/proposed"}}


def test_collectors_poll_each_configured_handoff_into_the_interval_before_the_boundary() -> None:
    world = World(INVENTORY, T0)
    world.advance(T0)
    late = world.polled("mombasa")
    world.advance(T0 + timedelta(minutes=5, seconds=9))  # a late tick
    later = world.polled("mombasa")

    start = T0 - timedelta(minutes=5, seconds=1)
    assert [(i.handoff, i.start) for i in late] == [("mba-node-1/eth-1-12", start)]
    assert [i.start for i in later] == [start + timedelta(minutes=5)]
    assert not world.polled("mombasa")


def test_a_handoff_configured_later_is_first_measured_over_the_next_whole_interval() -> None:
    world = World(INVENTORY, T0)
    world.sdn.apply("mba-node-1/eth-1-13", 1, at(130))
    ticks(world, 0, 600)

    starts = [i.start for i in world.polled("mombasa") if i.handoff.endswith("13")]
    assert starts == [at(299)]


def test_kubernetes_recreates_evicted_pods_at_the_next_tick_and_says_so() -> None:
    world = World(INVENTORY, T0)
    kube = world.kubes["mombasa"]
    kube.cordon(KILIMA)
    kube.evict(tuple(p for p in kube.pods if p.resource == KILIMA))
    world.advance(at(10))

    assert world.notes == [(at(10), f"kubernetes recreated {s} on {r}") for s, r in RECREATED]
    assert world.kubernetes == recreated_rows("2026-10-03T03:00:11Z")


def test_aaa_answers_from_the_local_copy_while_the_site_cannot_reach_the_cloud() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ["primary", "oob"], down=True)
    ticks(world, 0, 20)

    assert [n for n in world.notes if "aaa" in n[1]] == [
        (at(0), "aaa at mombasa answers from the local credential copy")
    ]


def test_a_cut_to_named_sites_leaves_the_others_reachable() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["E1"], ["primary", "oob"], down=True)

    assert world.regions_reachable("mombasa") == (2, True)


def test_a_poll_boundary_is_measured_once_however_often_the_clock_lands_on_it() -> None:
    world = World(INVENTORY, T0)
    boundary = T0 + timedelta(minutes=4, seconds=59)
    world.advance(boundary)
    world.advance(boundary)

    assert [(i.start, i.bytes) for i in world.polled("mombasa")] == [
        (boundary - timedelta(minutes=5), 62_500_000 * 300)
    ]


def test_a_site_with_no_collector_running_measures_nothing() -> None:
    world = World(INVENTORY, T0)
    kube = world.kubes["mombasa"]
    for resource in list(kube.nodes):
        kube.cordon(resource)
    kube.evict(tuple(p for p in kube.pods if p.service == "telemetry-collector"))
    world.advance(T0)

    assert not world.polled("mombasa")
    assert len(world.polled("barcelona")) == 1
