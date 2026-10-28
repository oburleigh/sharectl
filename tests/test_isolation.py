from builders import Memory, lines, state
from sharectl.decide import decide


def test_isolates_after_no_region_answers_for_confirm_seconds() -> None:
    _, memory = decide(state(0, reachable=0, data_up=False))
    decisions, memory = decide(state(20, memory, reachable=0, data_up=False))
    assert not decisions

    decisions, memory = decide(state(30, memory, reachable=0, data_up=False))

    assert lines(decisions)[0] == (
        "site-isolation mombasa applied: 0/3 regions reachable for 30 s on all paths"
    )
    assert memory.isolated is True
    assert decisions[0].page is False
    assert decisions[0].limited is False


def test_a_region_answering_on_any_path_resets_the_confirmation() -> None:
    _, memory = decide(state(0, reachable=0, data_up=False))
    _, memory = decide(state(20, memory, reachable=1, data_up=False))
    decisions, _ = decide(state(30, memory, reachable=0, data_up=False))

    assert not decisions


def test_rejoins_after_a_data_path_is_stable_for_rejoin_stable_seconds() -> None:
    isolated = Memory(isolated=True)
    _, memory = decide(state(0, isolated, reachable=3, held_records=4, held_intervals=73))
    decisions, memory = decide(state(290, memory, held_records=4, held_intervals=73))
    assert [d.rule.id for d in decisions] == []

    decisions, memory = decide(state(300, memory, held_records=4, held_intervals=73))

    assert lines(decisions)[0] == (
        "site-rejoin mombasa applied: data path stable for 300 s; ship 4 records, then 73 intervals"
    )
    assert memory.isolated is False


def test_a_flapping_data_path_restarts_the_hysteresis() -> None:
    isolated = Memory(isolated=True)
    _, memory = decide(state(0, isolated))
    _, memory = decide(state(100, memory, reachable=1, data_up=False))
    decisions, memory = decide(state(300, memory))
    assert not decisions

    decisions, _ = decide(state(599, memory))
    assert not decisions

    decisions, _ = decide(state(600, memory))
    assert [d.rule.id for d in decisions] == ["site-rejoin", "resume"]


def test_only_a_management_path_does_not_start_the_hysteresis() -> None:
    _, memory = decide(state(0, Memory(isolated=True), reachable=3, data_up=False))
    decisions, _ = decide(state(300, memory, reachable=3, data_up=False))

    assert not decisions
