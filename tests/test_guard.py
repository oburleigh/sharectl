from dataclasses import replace

from builders import BOOT, KILIMA, Memory, at, lines, state
from sharectl.decide import decide
from sharectl.model import Outcome

SILENT = {KILIMA: at(0)}
FULL = Memory(actions=tuple(at(100 - 3600 + 60 * n) for n in range(10)))


def test_refuses_to_act_on_another_sites_resource() -> None:
    elsewhere = tuple(
        replace(r, site="barcelona") if r.id == KILIMA else r for r in state().resources
    )

    decisions, memory = decide(state(30, resources=elsewhere, pods=BOOT, silent=SILENT))

    assert lines(decisions) == [
        f"drain-on-reclaim {KILIMA} refused: {KILIMA} is not on mombasa: "
        "a controller acts only on its own site"
    ]
    assert memory.reclaims == {}


def test_past_the_hourly_cap_refuses_unforced_actions_once_and_pages() -> None:
    decisions, memory = decide(state(30, FULL, pods=BOOT, silent=SILENT))
    again, _ = decide(state(40, memory, pods=BOOT, silent=SILENT))

    assert decisions[0].outcome is Outcome.REFUSED
    assert decisions[0].page
    assert decisions[0].reason == (
        "10 unforced actions in the last hour, the cap is 10: frozen, a human decides"
    )
    assert memory.reclaims == {}
    assert not again


def test_actions_older_than_an_hour_leave_the_cap() -> None:
    decisions, memory = decide(state(100, FULL, pods=BOOT, silent=SILENT))

    assert decisions[0].outcome is Outcome.APPLIED
    assert memory.actions[-1] == at(100)
    assert len(memory.actions) == 10


def test_an_owners_notice_is_forced_and_passes_the_cap() -> None:
    notice = {KILIMA: at(600)}
    decisions, memory = decide(state(30, FULL, pods=BOOT, notices=notice))

    assert [(d.rule.id, d.outcome) for d in decisions] == [("drain-on-reclaim", Outcome.APPLIED)]
    assert memory.actions == FULL.actions


def test_a_refusal_stays_paged_when_a_later_step_of_the_same_rule_is_allowed() -> None:
    second = replace(next(r for r in state().resources if r.id == KILIMA), id="mba-kilima-02")
    resources = (*state().resources, second)
    signals = {"silent": SILENT, "notices": {"mba-kilima-02": at(600)}}
    first, memory = decide(state(30, FULL, resources=resources, pods=BOOT, **signals))
    again, _ = decide(state(40, memory, resources=resources, pods=BOOT, **signals))

    assert [(d.subject, d.outcome) for d in first] == [
        (KILIMA, Outcome.REFUSED),
        ("mba-kilima-02", Outcome.APPLIED),
    ]
    assert not again


def test_a_fence_and_release_under_notice_are_forced_past_the_cap() -> None:
    notice = {KILIMA: at(600)}
    _, memory = decide(state(0, FULL, pods=BOOT, notices=notice))
    fenced, memory = decide(state(300, memory, notices=notice))
    released, memory = decide(state(480, memory, notices=notice))

    assert [(d.rule.id, d.outcome) for d in fenced + released] == [
        ("fence", Outcome.APPLIED),
        ("release", Outcome.APPLIED),
    ]
    assert memory.actions == FULL.actions


def test_a_fence_with_no_notice_counts_against_the_cap() -> None:
    nine = Memory(actions=(at(0),) * 9)
    drained, memory = decide(state(30, nine, pods=BOOT, silent=SILENT))
    fenced, _ = decide(state(630, memory, silent=SILENT))

    assert [d.outcome for d in drained] == [Outcome.APPLIED]
    assert lines(fenced) == [
        f"fence {KILIMA} refused: 10 unforced actions in the last hour, the cap is 10: "
        "frozen, a human decides"
    ]


def test_isolation_is_not_limited_by_the_cap() -> None:
    _, memory = decide(state(0, FULL, reachable=0, data_up=False))
    decisions, _ = decide(state(30, memory, reachable=0, data_up=False))

    assert [(d.rule.id, d.outcome) for d in decisions] == [
        ("site-isolation", Outcome.APPLIED),
        ("wait", Outcome.APPLIED),
    ]


def test_each_refused_subject_is_logged_once_in_its_own_right() -> None:
    second = replace(next(r for r in state().resources if r.id == KILIMA), id="mba-kilima-02")
    resources = (*state().resources, second)
    silent = {KILIMA: at(0), "mba-kilima-02": at(0)}
    decisions, _ = decide(state(30, FULL, resources=resources, pods=BOOT, silent=silent))

    assert [(d.subject, d.outcome) for d in decisions] == [
        (KILIMA, Outcome.REFUSED),
        ("mba-kilima-02", Outcome.REFUSED),
    ]
