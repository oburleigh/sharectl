import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from builders import INVENTORY, RECREATED, recreated_rows
from sharectl.play import Event, Kind, load_scenario, play
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


def test_the_partner_vm_pods_are_recreated_on_share_servers_at_the_next_tick(
    tmp_path: Path,
) -> None:
    shown: list[str] = []
    play(load_scenario(SCENARIOS / "partner-reclaim.toml"), INVENTORY, tmp_path, show=shown.append)

    assert [line for line in shown if "kubernetes" in line] == [
        f"10:00:11 kubernetes recreated {s} on {r}" for s, r in RECREATED
    ]
    rows = (tmp_path / "partner-reclaim" / "kubernetes.jsonl").read_text().splitlines()
    assert [json.loads(row) for row in rows] == recreated_rows("2026-10-10T10:00:11Z")


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
    assert shown[heading + 1].startswith("10:00:01 site/mombasa drain-on-reclaim@v1")


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
