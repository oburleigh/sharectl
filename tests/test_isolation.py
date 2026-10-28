from builders import Memory, at, lines, state
from sharectl.decide import decide


def test_isolates_after_no_region_answers_for_confirm_seconds() -> None:
    _, memory = decide(state(0, reachable=0, stable_since=None))
    decisions, memory = decide(state(20, memory, reachable=0, stable_since=None))
    assert not decisions

    decisions, memory = decide(state(30, memory, reachable=0, stable_since=None))

    assert lines(decisions)[0] == (
        "site-isolation mombasa applied: 0/3 regions reachable for 30 s on all paths"
    )
    assert memory.isolated is True
    assert decisions[0].page is False
    assert decisions[0].limited is False


def test_a_region_answering_on_any_path_resets_the_confirmation() -> None:
    _, memory = decide(state(0, reachable=0, stable_since=None))
    _, memory = decide(state(20, memory, reachable=1, stable_since=None))
    decisions, _ = decide(state(30, memory, reachable=0, stable_since=None))

    assert not decisions


def test_rejoins_after_a_data_path_is_stable_for_rejoin_stable_seconds() -> None:
    isolated = Memory(isolated=True, waits=frozenset({("isolated", "mombasa")}))
    back = {"stable_since": at(0)}
    decisions, memory = decide(state(290, isolated, **back))
    assert [d.rule.id for d in decisions] == []

    decisions, memory = decide(state(300, memory, **back))

    assert lines(decisions) == [
        "site-rejoin mombasa applied: data path stable for 300 s",
        "resume mombasa applied: the site rejoined the cloud",
    ]
    assert memory.isolated is False


def test_no_data_path_up_never_rejoins() -> None:
    isolated = Memory(isolated=True, waits=frozenset({("isolated", "mombasa")}))
    decisions, _ = decide(state(300, isolated, stable_since=None))

    assert not decisions


def test_isolation_records_how_many_data_links_were_up() -> None:
    _, memory = decide(state(0, reachable=0, stable_since=None, data_links_up=2))
    decisions, _ = decide(state(30, memory, reachable=0, stable_since=None, data_links_up=2))

    assert decisions[0].observed == {
        "reachable": 0,
        "regions": 3,
        "seconds": 30,
        "data_links_up": 2,
    }
