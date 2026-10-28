from dataclasses import replace

from builders import BOOT, INVENTORY, KILIMA, at, lines, state
from sharectl.decide import decide
from sharectl.model import Pod

NOTICE = {KILIMA: at(600)}


def test_a_notice_drains_every_pod_a_service_can_spare_and_sets_fence_and_release() -> None:
    decisions, memory = decide(state(0, pods=BOOT, notices=NOTICE))

    assert lines(decisions) == [
        f"drain-on-reclaim {KILIMA} applied: cordon {KILIMA} and evict 4 of its 4 pods, "
        "each service at or above its min; fence at 03:05:01, release at 03:08:01"
    ]
    assert decisions[0].evict == BOOT[:4]
    assert (memory.reclaims[KILIMA].fence, memory.reclaims[KILIMA].release) == (at(300), at(480))


def test_a_drain_never_takes_a_service_below_its_min() -> None:
    pods = (
        Pod(service="sdn-controller", resource=KILIMA),
        Pod(service="sdn-controller", resource="mba-srv-1"),
    )

    decisions, _ = decide(state(0, pods=pods, notices=NOTICE))

    assert decisions[0].evict == ()
    assert lines(decisions) == [
        f"drain-on-reclaim {KILIMA} applied: cordon {KILIMA} and evict 0 of its 1 pods, each "
        "service at or above its min; fence at 03:05:01, release at 03:08:01"
    ]


def test_fences_at_the_fence_time_deleting_what_is_left_then_releases() -> None:
    _, memory = decide(state(0, pods=BOOT, notices=NOTICE))
    early, memory = decide(state(290, memory, pods=BOOT[:1], notices=NOTICE))
    fenced, memory = decide(state(300, memory, pods=BOOT[:1], notices=NOTICE))
    waiting, memory = decide(state(470, memory, notices=NOTICE))
    released, memory = decide(state(480, memory, notices=NOTICE))

    assert not early
    assert not waiting
    assert lines(fenced) == [
        f"fence {KILIMA} applied: {KILIMA} holds 1 pods of Share's, deleted; identity revoked; "
        "node deleted; disk key revoked; disks wiped"
    ]
    assert lines(released) == [
        f"release {KILIMA} applied: release {KILIMA}: its owner reclaimed it"
    ]
    assert memory.reclaims == {}


TWO = replace(
    INVENTORY,
    resources=(
        *INVENTORY.resources,
        replace(next(r for r in INVENTORY.resources if r.id == KILIMA), id="mba-kilima-02"),
    ),
)


def test_a_notice_naming_two_leased_resources_drains_fences_and_releases_each_once() -> None:
    notice = {KILIMA: at(600), "mba-kilima-02": at(600)}
    resources = tuple(r for r in TWO.resources if r.site == "mombasa")
    seen: list[str] = []
    memory = None
    for seconds in (0, 10, 300, 310, 480):
        decisions, memory = decide(
            state(seconds, memory, inventory=TWO, resources=resources, notices=notice)
        )
        seen += [f"{d.rule.id} {d.subject}" for d in decisions]

    assert seen == [
        f"drain-on-reclaim {KILIMA}",
        "drain-on-reclaim mba-kilima-02",
        f"fence {KILIMA}",
        "fence mba-kilima-02",
        f"release {KILIMA}",
        "release mba-kilima-02",
    ]
    assert memory is not None
    assert memory.reclaims == {}
