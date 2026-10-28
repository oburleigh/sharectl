import os
import signal as signals
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path
lazy from collections.abc import Callable

import pytest

from sharectl.cli import main

ROOT = Path(__file__).parent.parent
RECLAIM = ROOT / "scenarios" / "partner-reclaim.toml"


def flags(tmp: Path) -> list[str]:
    return [
        "--inventory",
        str(ROOT / "inventory"),
        "--out",
        str(tmp / "out"),
        "--signal",
        str(tmp / "deep" / "signal"),
        "--tick-ms",
        "0",
    ]


def test_version_reports_the_installed_package(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"sharectl {version('sharectl')}\n"


def test_no_command_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out.startswith("usage: sharectl")


def test_demo_plays_every_scenario_file_and_succeeds_when_all_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["demo", "--scenarios", str(ROOT / "scenarios"), *flags(tmp_path)])

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert [line for line in out if line.endswith((": pass", ": FAIL"))] == [
        "fibre-cut: pass",
        "partner-reclaim: pass",
    ]


def test_scenario_fails_when_a_file_expects_something_else(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong.toml"
    wrong.write_text(RECLAIM.read_text().replace("evict 5 of its 5", "evict 4 of its 5"))

    assert main(["scenario", str(wrong), *flags(tmp_path)]) == 1


def test_scenario_rejects_an_invalid_file_with_the_field_and_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(RECLAIM.read_text().replace('kind = "reclaim-notice"', 'kind = "meteor"'))

    assert main(["scenario", str(bad), *flags(tmp_path)]) == 2
    assert (
        "event.0.kind\n  Input should be 'agreement-final', 'paths-down', 'paths-up', "
        "'reclaim-notice', 'resource-silent' or 'resend-desired'"
    ) in capsys.readouterr().err


def wait_until(ready: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 60
    while not ready():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@pytest.mark.parametrize("stale", ["999999999", "not-a-pid"])
def test_play_starts_a_controller_when_none_runs_shows_its_lines_and_stops_it(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], stale: str
) -> None:
    screen = tmp_path / "out" / "partner-reclaim" / "screen.txt"
    screen.parent.mkdir(parents=True)
    screen.write_text("partner-reclaim: FAIL\n")
    signal = tmp_path / "deep" / "signal"
    signal.mkdir(parents=True)
    (signal / "controller.pid").write_text(stale)

    code = main(["play", str(RECLAIM), *flags(tmp_path)])

    assert code == 0
    assert capfd.readouterr().out.splitlines() == screen.read_text().splitlines()
    assert screen.read_text().splitlines()[-1] == "partner-reclaim: pass"
    assert not (signal / "controller.pid").exists()


def test_play_hands_the_file_to_a_running_controller_and_leaves_it_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    signal = tmp_path / "deep" / "signal"
    signal.mkdir(parents=True)
    pid = signal / "controller.pid"
    command = [sys.executable, "-m", "sharectl", "run", *flags(tmp_path)]
    with subprocess.Popen(command, stdout=subprocess.DEVNULL) as run:  # noqa: S603, fixed arguments
        try:
            wait_until(lambda: pid.exists() and pid.read_text() == str(run.pid))
            with monkeypatch.context() as patch:
                patch.setattr(subprocess, "Popen", None)
                assert main(["play", str(RECLAIM), *flags(tmp_path)]) == 0
            assert run.poll() is None
            assert pid.read_text() == str(run.pid)
        finally:
            run.terminate()
    assert capsys.readouterr().out.splitlines()[-1] == "partner-reclaim: pass"
    assert not pid.exists()


def test_run_plays_the_files_handed_to_it_in_name_order(tmp_path: Path) -> None:
    signal = tmp_path / "deep" / "signal"
    signal.mkdir(parents=True)
    for name in ("b-second", "a-first"):
        (signal / f"{name}.toml").write_text(
            RECLAIM.read_text().replace('name = "partner-reclaim"', f'name = "{name}"')
        )
    command = [sys.executable, "-m", "sharectl", "run", *flags(tmp_path)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as run:  # noqa: S603
        try:
            wait_until(
                lambda: not list(signal.glob("*.toml")) and not list(signal.glob("*.playing"))
            )
        finally:
            run.terminate()
        out, _ = run.communicate()
    assert run.returncode == 0
    assert [line for line in out.splitlines() if line.endswith(": pass")] == [
        "a-first: pass",
        "b-second: pass",
    ]


@pytest.mark.parametrize("pacing", [([], 0.02, 0.02), (["--tick-ms", "5"], 0.005, 0.01)])
def test_run_paces_each_tick_at_tick_ms_20_by_default_and_stops_on_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    capsys: pytest.CaptureFixture[str],
    pacing: tuple[list[str], float, float],
) -> None:
    (tmp_path / "inventory").symlink_to(ROOT / "inventory")
    (tmp_path / "signal").mkdir()
    (tmp_path / "signal" / "partner-reclaim.toml").write_bytes(RECLAIM.read_bytes())
    monkeypatch.chdir(tmp_path)
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        assert len(slept) <= 62, "the file handed to the controller was never played"
        if not [*(tmp_path / "signal").glob("*.toml"), *(tmp_path / "signal").glob("*.playing")]:
            raise KeyboardInterrupt

    handlers: list[tuple[int, object]] = []

    def install(number: int, handler: object) -> None:
        handlers.append((number, handler))

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr("signal.signal", install)

    args, slept_per_tick, slept_idle = pacing
    assert main(["run", *args]) == 0

    assert slept == [slept_per_tick] * 61 + [slept_idle]
    assert handlers == [(signals.SIGTERM, signals.default_int_handler)]
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "controller watching signal"
    assert out[-1] == "partner-reclaim: pass"
    assert (tmp_path / "out" / "partner-reclaim" / "screen.txt").exists()
    assert sorted(p.name for p in (tmp_path / "signal").iterdir()) == []


def test_demo_with_the_defaults_reads_the_inventory_and_scenarios_where_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("inventory", "scenarios"):
        (tmp_path / name).symlink_to(ROOT / name)
    monkeypatch.chdir(tmp_path)

    assert main(["demo"]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["fibre-cut", "partner-reclaim"]


@pytest.mark.script
@pytest.mark.parametrize("name", ["fibre-cut", "partner-reclaim"])
def test_each_script_plays_its_scenario_on_the_controller(tmp_path: Path, name: str) -> None:
    result = subprocess.run(  # noqa: S603, the repository's own script
        [
            ROOT / "scripts" / f"{name}.sh",
            "--tick-ms",
            "0",
            "--out",
            str(tmp_path / "out"),
            "--signal",
            str(tmp_path / "signal"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[-1] == f"{name}: pass"


def test_run_creates_its_signal_directory_and_holds_its_pid_there_while_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal = tmp_path / "a" / "b"
    pid = signal / "controller.pid"
    seen: list[str] = []

    def sleep(seconds: float) -> None:
        del seconds
        seen.append(pid.read_text())
        pid.unlink()
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", sleep)

    def install(number: int, handler: object) -> None:
        del number, handler

    monkeypatch.setattr("signal.signal", install)

    assert main(["run", "--signal", str(signal), "--inventory", str(ROOT / "inventory")]) == 0
    assert seen == [str(os.getpid())]
    assert not pid.exists()


def test_play_exits_1_when_the_controller_reports_a_fail(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong.toml"
    wrong.write_text(RECLAIM.read_text().replace("evict 5 of its 5", "evict 4 of its 5"))

    assert main(["play", str(wrong), *flags(tmp_path)]) == 1


def test_help_describes_the_command_each_subcommand_and_the_pace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in (["--help"], ["run", "--help"]):
        with pytest.raises(SystemExit) as exit_info:
            main(argv)
        assert exit_info.value.code == 0
    out = " ".join(capsys.readouterr().out.split())

    for text in (
        "Command-line entry point: play scenario files, or run the controller and hand it files.",
        "demo play every scenario file at once",
        "scenario play one file at once",
        "run run the controller, live",
        "play hand a file to the controller",
        "--tick-ms TICK_MS wall-clock ms per 10 s tick",
    ):
        assert text in out


def test_play_interrupted_while_waiting_stops_the_controller_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = tmp_path / "deep" / "signal" / "controller.pid"
    sleep = time.sleep

    def interrupt(seconds: float) -> None:
        if pid.exists():
            raise RuntimeError("interrupted")
        sleep(seconds)

    monkeypatch.setattr(time, "sleep", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        main(["play", str(RECLAIM), *flags(tmp_path)])
    monkeypatch.setattr(time, "sleep", sleep)

    wait_until(lambda: not pid.exists())
