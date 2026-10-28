from builders import Memory, lines, state
from sharectl.decide import decide
from sharectl.model import Pod

ISOLATED = Memory(isolated=True)
WAIT = (
    "wait mombasa applied: site-bound services keep running on site resources; "
    "nothing moves, as a copy in the cloud could not reach the node"
)


def test_waits_once_when_the_site_is_isolated_and_resumes_once_on_rejoin() -> None:
    first, memory = decide(state(0, ISOLATED, reachable=0, data_up=False))
    again, memory = decide(state(10, memory, reachable=0, data_up=False))
    _, memory = decide(state(20, memory))
    back, _ = decide(state(320, memory))

    assert lines(first) == [WAIT]
    assert not again
    assert lines(back) == [
        "site-rejoin mombasa applied: data path stable for 300 s; ship 0 records, then 0 intervals",
        "resume mombasa applied: the site rejoined the cloud",
    ]


def test_wait_runs_after_isolation_and_sees_it() -> None:
    _, memory = decide(state(0, reachable=0, data_up=False))
    decisions, _ = decide(state(30, memory, reachable=0, data_up=False))

    assert [d.rule.id for d in decisions] == ["site-isolation", "wait"]


def test_pending_pods_wait_and_page_once_then_resume() -> None:
    pending = (
        Pod(service="site-postgres", resource=None),
        Pod(service="aaa", resource="mba-srv-1"),
    )
    first, memory = decide(state(0, pods=pending))
    again, memory = decide(state(10, memory, pods=pending))
    placed, _ = decide(state(20, memory, pods=()))

    assert lines(first) == [
        "wait site-postgres applied: site-postgres has 1 pods pending with no allowed resource"
    ]
    assert first[0].page is True
    assert not again
    assert lines(placed) == ["resume site-postgres applied: site-postgres has no pod pending"]
