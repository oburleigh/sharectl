import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from builders import ALL_PATHS, INVENTORY, RECREATED, reached, recreated_rows
from sharectl.model import Layer, Outcome
from sharectl.play import Event, Kind, Player, load_scenario, play
from sharectl.stubs import Coverage

SCENARIOS = Path(__file__).parent.parent / "scenarios"
FILES = sorted(SCENARIOS.glob("*.toml"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_each_scenario_plays_its_expected_decisions_and_passes(path: Path, tmp_path: Path) -> None:
    shown: list[str] = []

    failures = play(load_scenario(path), INVENTORY, tmp_path, show=shown.append)

    assert failures == []
    assert shown[-1] == f"{path.stem}: pass"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_every_output_file_is_identical_across_two_runs(path: Path, tmp_path: Path) -> None:
    for run in ("one", "two"):
        play(load_scenario(path), INVENTORY, tmp_path / run, show=lambda _: None)

    one = {p.relative_to(tmp_path / "one"): p.read_bytes() for p in (tmp_path / "one").rglob("*.*")}
    two = {p.relative_to(tmp_path / "two"): p.read_bytes() for p in (tmp_path / "two").rglob("*.*")}
    assert sorted(str(p) for p in one) == [
        f"{path.stem}/{name}"
        for name in (
            "coverage.json",
            "decisions.jsonl",
            "events.jsonl",
            "kubernetes.jsonl",
            "screen.txt",
            "site-bcn.jsonl",
            "site-mba.jsonl",
            "site-mrs.jsonl",
        )
    ]
    assert one == two


def test_a_decision_the_file_does_not_expect_fails_the_scenario(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / "partner-reclaim.toml")
    shown: list[str] = []

    failures = play(
        replace(scenario, expect=scenario.expect[:2]), INVENTORY, tmp_path, show=shown.append
    )

    assert failures == ["unexpected: 10:08:01 site/mombasa release@v1 release-lease applied"]
    assert shown[-1] == "partner-reclaim: FAIL"


def test_a_reason_that_differs_fails_the_scenario(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / "partner-reclaim.toml")
    wrong = replace(scenario.expect[2], reason_contains="its lease ran out")

    failures = play(
        replace(scenario, expect=(*scenario.expect[:2], wrong)),
        INVENTORY,
        tmp_path,
        show=lambda _: None,
    )

    assert failures == [
        "expected: 10:08:01 site/mombasa release@v1 release-lease applied, reason with "
        "'its lease ran out'; got: release mba-kilima-01: its owner reclaimed it"
    ]


def test_coverage_and_summary_that_differ_fail_the_scenario(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / "fibre-cut.toml")
    coverage = replace(scenario.expect_coverage[0], missing=1)
    summary = {**scenario.expect_summary, "replays_ignored": 2}

    failures = play(
        replace(scenario, expect_coverage=(coverage,), expect_summary=summary),
        INVENTORY,
        tmp_path,
        show=lambda _: None,
    )

    assert failures == [
        "coverage mba-node-1/eth-1-12 ifHCInOctets: missing 0, expected 1",
        "summary replays_ignored: 1, expected 2",
    ]


def test_the_partner_vm_pods_are_recreated_on_share_servers_at_the_next_tick(
    tmp_path: Path,
) -> None:
    shown: list[str] = []
    play(load_scenario(SCENARIOS / "partner-reclaim.toml"), INVENTORY, tmp_path, show=shown.append)

    assert [line for line in shown if "kubernetes" in line] == [
        f"10:00:11 kubernetes at mombasa recreated {s} on {r}" for s, r in RECREATED
    ]
    rows = (tmp_path / "partner-reclaim" / "kubernetes.jsonl").read_text().splitlines()
    assert [json.loads(row) for row in rows] == recreated_rows("2026-10-10T10:00:11Z")


def test_the_fibre_cut_shows_and_writes_the_reconciliation(tmp_path: Path) -> None:
    shown: list[str] = []
    play(load_scenario(SCENARIOS / "fibre-cut.toml"), INVENTORY, tmp_path, show=shown.append)
    out = tmp_path / "fibre-cut"

    assert "boot mombasa: mba-kilima-01 4, mba-srv-1 6, mba-srv-2 6, mba-srv-3 6" in shown
    assert [line for line in shown if " aaa at " in line] == [
        "03:12:11 aaa at mombasa still answers from the local credential copy",
    ]
    assert [line for line in shown if " site-log " in line] == [
        "09:17:11 site-log at mombasa ships what it held, its own check having seen the link "
        "stable for 300 s: decision records 4, events 1, intervals 73, resent 3"
    ]
    assert [line for line in shown if " config-push " in line] == [
        "03:02:11 config-push at barcelona applied gen 8 to bcn-node-1/eth-1-12 (above applied 7)",
        "03:02:11 config-push at marseille applied gen 8 to mrs-node-1/eth-1-12 (above applied 7)",
        "03:02:11 config-push at mombasa applied gen 8 to mba-node-1/eth-1-12 (above applied 7)",
        "05:00:11 config-push at barcelona applied gen 1 to bcn-node-1/eth-1-13 (above applied 0)",
        "09:17:11 config-push at mombasa applied gen 1 to mba-node-1/eth-1-13 (above applied 0)",
        "09:17:31 config-push at mombasa ignored gen 1 for mba-node-1/eth-1-13: 1 <= applied 1",
    ]
    assert shown[-3:-1] == [
        "cloud coverage mba-node-1/eth-1-12 ifHCInOctets: 73 held, 3 resent, 76 expected, "
        "76 unique, 3 duplicates dropped, 0 missing",
        "summary: generations_applied 5, replays_ignored 1, decisions_merged 4, pods_recreated 0, "
        "pods_pending 0, pods_left_on_released 0",
    ]
    assert json.loads((out / "coverage.json").read_text()) == [
        {
            "handoff": "mba-node-1/eth-1-12",
            "counter": "ifHCInOctets",
            "held": 73,
            "resent": 3,
            "expected": 76,
            "unique": 76,
            "duplicates": 3,
            "missing": 0,
        }
    ]
    merged = [json.loads(r)["data"] for r in (out / "decisions.jsonl").read_text().splitlines()]
    keys = [(d["at"], d["decision_id"], d["phase"]) for d in merged]
    assert keys == sorted(keys)
    assert len(merged) == 8
    assert sorted(Counter(d["decision_id"] for d in merged).values()) == [2] * 4
    events = [json.loads(r)["data"] for r in (out / "events.jsonl").read_text().splitlines()]
    assert [(e["handoff"], e["generation"], e["agreement"], e["version"]) for e in events] == [
        ("bcn-node-1/eth-1-12", 8, "atlas-0001", 1),
        ("mrs-node-1/eth-1-12", 8, "atlas-0001", 1),
        ("mba-node-1/eth-1-12", 8, "atlas-0001", 1),
        ("bcn-node-1/eth-1-13", 1, "second-buyer-0001", 1),
        ("mba-node-1/eth-1-13", 1, "second-buyer-0001", 1),
    ]
    site = (out / "site-mba.jsonl").read_text().splitlines()
    assert [json.loads(r)["sequence"] for r in site] == [str(n) for n in range(1, 9)]


def test_a_config_push_action_the_file_does_not_expect_fails_the_scenario(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / "fibre-cut.toml")

    failures = play(
        replace(scenario, expect_config_push=scenario.expect_config_push[:5]),
        INVENTORY,
        tmp_path,
        show=lambda _: None,
    )

    assert failures == ["unexpected: 09:17:31 config-push mba-node-1/eth-1-13 gen 1 ignored"]


def test_an_expected_config_push_action_that_never_comes_is_missing(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / "fibre-cut.toml")
    extra = replace(scenario.expect_config_push[-1], generation=2)

    failures = play(
        replace(scenario, expect_config_push=(*scenario.expect_config_push, extra)),
        INVENTORY,
        tmp_path,
        show=lambda _: None,
    )

    assert failures == ["missing: 09:17:31 config-push mba-node-1/eth-1-13 gen 2 ignored"]


def test_a_config_push_action_that_differs_shows_what_came_instead(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / "fibre-cut.toml")
    wrong = replace(scenario.expect_config_push[-1], outcome=Outcome.APPLIED)

    failures = play(
        replace(scenario, expect_config_push=(*scenario.expect_config_push[:5], wrong)),
        INVENTORY,
        tmp_path,
        show=lambda _: None,
    )

    assert failures == [
        "expected: 09:17:31 config-push mba-node-1/eth-1-13 gen 1 applied; "
        "got: 09:17:31 config-push mba-node-1/eth-1-13 gen 1 ignored"
    ]


def test_a_scenario_name_that_is_not_a_safe_file_name_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(
        (SCENARIOS / "partner-reclaim.toml")
        .read_text()
        .replace('name = "partner-reclaim"', 'name = "../etc"')
    )

    with pytest.raises(
        ValidationError, match=r"name\n  String should match pattern '\^\[a-z0-9-\]\+\$'"
    ):
        load_scenario(bad)


def test_each_phase_heads_the_lines_from_its_time(tmp_path: Path) -> None:
    shown: list[str] = []
    play(load_scenario(SCENARIOS / "partner-reclaim.toml"), INVENTORY, tmp_path, show=shown.append)

    heading = shown.index("10:00:01 [phase] Kilima gives ten minutes' notice")
    assert [line.split(" poll ")[0] for line in shown[heading + 1 : heading + 4]] == [
        "10:00:01 site/barcelona",
        "10:00:01 site/marseille",
        "10:00:01 site/mombasa",
    ]
    assert shown[heading + 4] == (
        "10:00:01 site/mombasa PATCH /api/v1/nodes/mba-kilima-01 "
        '{"spec": {"unschedulable": true}} -> 200'
    )
    assert [line.split("/pods/")[1].split("/")[0] for line in shown[heading + 5 : heading + 9]] == [
        "sdn-controller-1",
        "aaa-1",
        "config-push-1",
        "telemetry-collector-1",
    ]
    assert shown[heading + 9].startswith("10:00:01 site/mombasa drain-on-reclaim@v1")


def test_a_second_play_writes_over_the_first(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / "partner-reclaim.toml")
    play(scenario, INVENTORY, tmp_path, show=lambda _: None)

    assert play(scenario, INVENTORY, tmp_path, show=lambda _: None) == []


def test_each_phase_heading_is_shown_once(tmp_path: Path) -> None:
    shown: list[str] = []
    play(load_scenario(SCENARIOS / "partner-reclaim.toml"), INVENTORY, tmp_path, show=shown.append)

    assert [line for line in shown if "[phase]" in line] == [
        "10:00:01 [phase] Kilima gives ten minutes' notice",
        "10:05:01 [phase] the fence time",
        "10:08:01 [phase] the release time",
    ]


def test_a_silent_resource_event_is_shown_with_the_failure_it_causes(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / "partner-reclaim.toml")
    silent = Event(at=scenario.start, kind=Kind.RESOURCE_SILENT, resource="mba-kilima-01")
    shown: list[str] = []

    failures = play(
        replace(scenario, events=(silent,), expect=(), expect_summary={}),
        INVENTORY,
        tmp_path,
        show=shown.append,
    )

    unexpected = "unexpected: 10:00:31 site/mombasa drain-on-reclaim@v1 cordon-and-drain applied"
    assert failures == [unexpected]
    assert shown[-2:] == [unexpected, "partner-reclaim: FAIL"]


def test_an_expected_decision_that_never_comes_is_missing(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / "partner-reclaim.toml")

    failures = play(
        replace(scenario, events=(), expect_summary={}), INVENTORY, tmp_path, show=lambda _: None
    )

    assert failures == [
        "missing: 10:00:01 site/mombasa drain-on-reclaim@v1 cordon-and-drain applied",
        "missing: 10:05:01 site/mombasa fence@v1 fence applied",
        "missing: 10:08:01 site/mombasa release@v1 release-lease applied",
    ]


def test_a_decision_that_differs_shows_what_came_instead(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / "partner-reclaim.toml")
    wrong = replace(scenario.expect[2], action="release")

    failures = play(
        replace(scenario, expect=(*scenario.expect[:2], wrong)),
        INVENTORY,
        tmp_path,
        show=lambda _: None,
    )

    assert failures == [
        "expected: 10:08:01 site/mombasa release@v1 release applied, reason with "
        "'release mba-kilima-01: its owner reclaimed it'; "
        "got: 10:08:01 site/mombasa release@v1 release-lease applied"
    ]


def test_coverage_the_scenario_never_checked_fails_every_field(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIOS / "partner-reclaim.toml")
    rows = tuple(
        Coverage(
            handoff=h,
            counter="ifHCInOctets",
            held=1,
            resent=0,
            expected=1,
            unique=1,
            duplicates=0,
            missing=0,
        )
        for h in ("bcn-node-1/eth-1-12", "mrs-node-1/eth-1-12")
    )

    failures = play(
        replace(scenario, expect_coverage=rows), INVENTORY, tmp_path, show=lambda _: None
    )

    assert failures == [
        f"coverage {h} ifHCInOctets: {key} None, expected {value}"
        for h in ("bcn-node-1/eth-1-12", "mrs-node-1/eth-1-12")
        for key, value in (
            ("handoff", h),
            ("counter", "ifHCInOctets"),
            ("held", 1),
            ("resent", 0),
            ("expected", 1),
            ("unique", 1),
            ("duplicates", 0),
            ("missing", 0),
        )
    ]


def test_a_paths_event_cuts_only_sessions_when_its_layer_says_so() -> None:
    scenario = load_scenario(SCENARIOS / "partner-reclaim.toml")
    regions = ("E1", "E2", "E3")
    cut = Event(
        at=scenario.start,
        kind=Kind.PATHS_DOWN,
        site="mombasa",
        to=regions,
        paths=ALL_PATHS,
        layer=Layer.SESSION,
    )
    player = Player(replace(scenario, events=(cut,)), INVENTORY, lambda _: None)

    player.step(scenario.start)

    assert (reached(player.world, "mombasa"), player.world.data_path_up("mombasa")) == (0, False)
    assert [up for _, up in player.world.links_up("mombasa")] == [True, True, True]


def test_a_paths_event_with_a_layer_that_is_not_link_or_session_is_rejected(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(
        (SCENARIOS / "fibre-cut.toml")
        .read_text()
        .replace('kind = "paths-down"\n', 'kind = "paths-down"\nlayer = "fibre"\n')
    )

    with pytest.raises(ValidationError, match=r"layer\n  Input should be 'link' or 'session'"):
        load_scenario(bad)


def test_a_paths_event_with_no_layer_takes_the_links_down_whatever_its_sites() -> None:
    scenario = load_scenario(SCENARIOS / "partner-reclaim.toml")
    cut = Event(
        at=scenario.start, kind=Kind.PATHS_DOWN, site="mombasa", to=("E1",), paths=ALL_PATHS
    )
    player = Player(replace(scenario, events=(cut,)), INVENTORY, lambda _: None)

    player.step(scenario.start)

    assert (reached(player.world, "mombasa"), player.world.data_path_up("mombasa")) == (0, False)
    assert [up for _, up in player.world.links_up("mombasa")] == [False, False, False]
