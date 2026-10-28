from dataclasses import replace

from builders import BOOT, KILIMA, at, lines, state
from sharectl.decide import decide
from sharectl.model import Outcome

NOTICE = {KILIMA: at(600)}


def test_refuses_to_act_on_another_sites_resource() -> None:
    elsewhere = tuple(
        replace(r, site="barcelona") if r.id == KILIMA else r for r in state().resources
    )

    decisions, memory = decide(state(0, resources=elsewhere, pods=BOOT, notices=NOTICE))
    again, _ = decide(state(10, memory, resources=elsewhere, pods=BOOT, notices=NOTICE))

    assert lines(decisions) == [
        f"drain-on-reclaim {KILIMA} refused: {KILIMA} is not on mombasa: "
        "a controller acts only on its own site"
    ]
    assert decisions[0].page
    assert memory.reclaims == {}
    assert not again


def test_each_refused_subject_is_logged_once_in_its_own_right() -> None:
    second = replace(next(r for r in state().resources if r.id == KILIMA), id="mba-kilima-02")
    resources = tuple(
        replace(r, site="barcelona") if r.id in {KILIMA, "mba-kilima-02"} else r
        for r in (*state().resources, second)
    )
    notices = {KILIMA: at(600), "mba-kilima-02": at(600)}

    decisions, _ = decide(state(0, resources=resources, pods=BOOT, notices=notices))

    assert [(d.subject, d.outcome) for d in decisions] == [
        (KILIMA, Outcome.REFUSED),
        ("mba-kilima-02", Outcome.REFUSED),
    ]
