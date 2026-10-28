import json
import os
import signal as signals
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
lazy import argparse
lazy from collections.abc import Callable

import pytest

from sharectl import cli
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
        "--site",
        "mombasa",
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
    wrong.write_text(RECLAIM.read_text().replace("evict 4 of its 4", "evict 3 of its 4"))

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


def running(signal: Path) -> None:
    """Make `signal` look like a running controller's: its pid is this test's own process."""
    signal.mkdir(parents=True, exist_ok=True)
    (signal / "controller.pid").write_text(str(os.getpid()))


def never(seconds: float) -> None:
    """A sleep for a command that should have stopped before its first tick."""
    del seconds
    raise RanOnError


class RanOnError(AssertionError):
    """A command ran on to a tick when it should have stopped first."""


def wait_until(ready: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 60
    while not ready():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_demo_with_the_defaults_reads_the_inventory_and_scenarios_where_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("inventory", "scenarios"):
        (tmp_path / name).symlink_to(ROOT / name)
    monkeypatch.chdir(tmp_path)

    assert main(["demo", "--site", "mombasa"]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["fibre-cut", "partner-reclaim"]


@pytest.mark.script
@pytest.mark.parametrize(
    ("name", "decided"),
    [
        ("fibre-cut", ["site-isolation@v1", "wait@v1", "site-rejoin@v1", "resume@v1"]),
        ("partner-reclaim", ["drain-on-reclaim@v1", "fence@v1", "release@v1"]),
    ],
)
def test_each_script_drives_a_live_controller_that_acts_on_what_it_finds_or_is_told(
    tmp_path: Path, name: str, decided: list[str]
) -> None:
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

    lines = result.stdout.splitlines()
    assert result.returncode == 0, result.stderr
    assert lines[0].startswith("controller started (pid ")
    assert lines[1] == "controllers: site/mombasa"
    assert [w for line in lines for w in line.split() if w.endswith("@v1")] == decided
    assert lines[-1] == "controller stopped"
    assert not (tmp_path / "signal" / "controller.pid").exists()


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

    pid_when_handled: list[bool] = []

    def install(number: int, handler: object) -> None:
        del number, handler
        pid_when_handled.append(pid.exists())

    monkeypatch.setattr("signal.signal", install)
    replaced: list[Path] = []
    write = cli.replace_text

    def replace(path: Path, text: str) -> None:
        replaced.append(path)
        write(path, text)

    monkeypatch.setattr(cli, "replace_text", replace)

    assert (
        main(
            [
                "run",
                "--signal",
                str(signal),
                "--inventory",
                str(ROOT / "inventory"),
                "--site",
                "mombasa",
            ]
        )
        == 0
    )
    assert seen == [str(os.getpid())]
    assert pid in replaced, "the pid must be replaced in one step"
    assert not pid.exists()
    assert pid_when_handled == [False], "SIGTERM must be handled before the pid is published"


def test_play_exits_1_when_the_controller_reports_a_fail(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong.toml"
    wrong.write_text(RECLAIM.read_text().replace("evict 4 of its 4", "evict 3 of its 4"))

    assert main(["play", str(wrong), *flags(tmp_path)]) == 1


def test_help_describes_the_command_each_subcommand_and_the_pace(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in (["--help"], ["run", "--help"], ["wait", "--help"]):
        with pytest.raises(SystemExit) as exit_info:
            main(argv)
        assert exit_info.value.code == 0
    out = " ".join(capsys.readouterr().out.split())

    for text in (
        "Command-line entry point: play scenario files, or run the controller live on its mocks.",
        "demo play every scenario file at once",
        "scenario play one file at once",
        "run run the controller, live",
        "play play one file at a live pace",
        "emit hand one event to the running controller's mocks, as field=value pairs",
        "start start the controller in the background, unless one is running",
        "stop stop the running controller",
        "wait show the running controller's lines until its clock has moved on",
        "minutes simulated minutes",
        "--tick-ms TICK_MS wall-clock ms per 10 s tick",
        "--site SITE the PoPs whose controllers run, comma-separated; required",
    ):
        assert text in out


@pytest.mark.script
@pytest.mark.parametrize(
    ("target", "command"),
    [
        ("install", "uv sync"),
        ("fibre-cut", "./scripts/fibre-cut.sh --tick-ms 5"),
        ("partner-reclaim", "./scripts/partner-reclaim.sh --tick-ms 5"),
        ("run", "uv run sharectl run --tick-ms 5"),
        ("demo", "uv run sharectl demo --tick-ms 5"),
        ("test", "uv run pytest --tick-ms 5"),
    ],
)
def test_each_make_target_runs_its_command_with_the_args_given(target: str, command: str) -> None:
    result = subprocess.run(  # noqa: S603, make in dry-run mode prints and runs nothing
        ["make", "-n", target, "ARGS=--tick-ms 5"],  # noqa: S607, make from PATH
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == command


def test_demo_reads_its_directories_from_the_environment_and_a_flag_overrides_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SHARECTL_INVENTORY", str(ROOT / "inventory"))
    monkeypatch.setenv("SHARECTL_SCENARIOS", str(ROOT / "scenarios"))
    monkeypatch.setenv("SHARECTL_OUT", str(tmp_path / "from-env"))
    monkeypatch.setenv("SHARECTL_SITE", "mombasa")

    assert main(["demo"]) == 0
    assert main(["demo", "--out", str(tmp_path / "from-flag")]) == 0

    for out in ("from-env", "from-flag"):
        assert sorted(p.name for p in (tmp_path / out).iterdir()) == [
            "fibre-cut",
            "partner-reclaim",
        ]


def site_logs(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "out" / "partner-reclaim").glob("site-*.jsonl"))


def test_site_runs_only_the_named_pops_controllers(tmp_path: Path) -> None:
    assert main(["scenario", str(RECLAIM), *flags(tmp_path)]) == 0

    assert site_logs(tmp_path) == ["site-mba.jsonl"]


def test_no_site_stops_the_command_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unnamed = flags(tmp_path)[:-2]

    assert main(["scenario", str(RECLAIM), *unnamed]) == 2
    assert capsys.readouterr().err == (
        "sharectl: error: --site is required: the PoP this controller runs at\n"
    )


def test_the_environment_can_name_several_sites_and_the_first_line_says_which_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SHARECTL_SITE", "mombasa,barcelona")

    assert main(["scenario", str(RECLAIM), *flags(tmp_path)[:-2]]) == 0

    assert capsys.readouterr().out.splitlines()[0] == "controllers: site/barcelona, site/mombasa"
    assert site_logs(tmp_path) == ["site-bcn.jsonl", "site-mba.jsonl"]


@pytest.mark.parametrize("sites", ["mombasa,E1", "E2,E1"])
def test_a_site_that_is_not_a_pop_is_rejected_with_the_option_and_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], sites: str
) -> None:
    assert main(["scenario", str(RECLAIM), *flags(tmp_path), "--site", sites]) == 2

    assert capsys.readouterr().err == "sharectl: error: --site: E1 is not a PoP in the inventory\n"


def test_play_with_no_site_stops_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["play", str(RECLAIM), *flags(tmp_path)[:-2]]) == 2
    assert capsys.readouterr().err == (
        "sharectl: error: --site is required: the PoP this controller runs at\n"
    )


@pytest.mark.parametrize(
    ("given", "environment", "pause"),
    [([], "", 0.0), (["--tick-ms", "50"], "", 0.05), ([], "30", 0.03)],
)
def test_scenario_plays_at_once_unless_tick_ms_paces_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    given: list[str],
    environment: str,
    pause: float,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    if environment:
        monkeypatch.setenv("SHARECTL_TICK_MS", environment)
    settings = ["--inventory", str(ROOT / "inventory"), "--out", str(tmp_path), "--site", "mombasa"]

    assert main(["scenario", str(RECLAIM), *settings, *given]) == 0

    assert slept == [pause] * 61


def test_run_with_no_site_stops_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "sleep", never)

    assert main(["run", *flags(tmp_path)[:-2]]) == 2
    assert capsys.readouterr().err == (
        "sharectl: error: --site is required: the PoP this controller runs at\n"
    )


@pytest.mark.script
@pytest.mark.parametrize(
    ("dotenv", "site"), [(None, "mombasa"), ("SHARECTL_SITE=barcelona\n", "barcelona")]
)
def test_make_reads_dotenv_and_falls_back_to_mombasa(
    tmp_path: Path, dotenv: str | None, site: str
) -> None:
    (tmp_path / "Makefile").write_bytes((ROOT / "Makefile").read_bytes())
    if dotenv:
        (tmp_path / ".env").write_text(dotenv)
    shim = tmp_path / "bin" / "uv"
    shim.parent.mkdir()
    shim.write_text('#!/usr/bin/env sh\necho "$SHARECTL_SITE"\n')
    shim.chmod(0o755)
    path = f"{shim.parent}{os.pathsep}{os.environ['PATH']}"

    result = subprocess.run(
        ["make", "-s", "run"],  # noqa: S607, make from PATH
        cwd=tmp_path,
        env={**os.environ, "PATH": path},
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout == f"{site}\n"


@pytest.mark.script
def test_env_example_names_mombasa_and_git_ignores_env() -> None:
    lines = (ROOT / ".env.example").read_text().splitlines()
    settings = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"],  # noqa: S607, git from PATH
        cwd=ROOT,
        check=False,
    )

    assert settings["SHARECTL_SITE"] == "mombasa"
    assert ignored.returncode == 0


def test_emit_hands_one_event_to_the_running_controller_s_mocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    given = ["emit", "reclaim-notice", "resources=mba-kilima-01,mba-srv-1", "notice_minutes=10"]
    signal = tmp_path / "deep" / "signal"

    running(signal)
    assert main([*given, "--signal", str(signal)]) == 0
    assert main([*given, "--signal", str(signal)]) == 0

    events = sorted((signal / "events").glob("*"))
    assert [path.suffix for path in events] == [".json", ".json"]
    record = {
        "kind": "reclaim-notice",
        "resources": ["mba-kilima-01", "mba-srv-1"],
        "notice_minutes": 10,
    }
    assert json.loads(events[0].read_text()) == record
    assert capsys.readouterr().out == f"emitted {json.dumps(record)}\n" * 2


def test_emit_keeps_an_equals_sign_inside_a_value(tmp_path: Path) -> None:
    running(tmp_path)
    assert main(["emit", "agreement-final", "agreement=a=1", "--signal", str(tmp_path)]) == 0

    event = next((tmp_path / "events").glob("*.json"))
    assert json.loads(event.read_text()) == {"kind": "agreement-final", "agreement": "a=1"}


def test_emit_rejects_a_kind_no_event_has(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["emit", "meteor", "--signal", str(tmp_path)])

    assert stopped.value.code == 2
    assert "argument kind: invalid choice: 'meteor'" in capsys.readouterr().err


def test_emit_rejects_a_value_naming_the_field_and_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    given = ["emit", "reclaim-notice", "resources=mba-kilima-01", "notice_minutes=ten"]

    running(tmp_path)
    assert main([*given, "--signal", str(tmp_path)]) == 2

    error = capsys.readouterr().err
    assert "notice_minutes" in error
    assert "Input should be a valid integer" in error
    assert not (tmp_path / "events").exists()


def test_emit_rejects_a_field_no_event_has(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    running(tmp_path)
    assert main(["emit", "paths-down", "pathz=oob", "--signal", str(tmp_path)]) == 2

    assert capsys.readouterr().err == "sharectl: error: emit: pathz is not a field of an event\n"


def test_emit_rejects_a_field_with_no_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["emit", "paths-down", "oob", "--signal", str(tmp_path)])

    assert stopped.value.code == 2
    assert capsys.readouterr().err.endswith("argument field=value: oob: not field=value\n")


def test_run_is_the_live_controller_acting_on_an_event_sent_to_its_mocks(tmp_path: Path) -> None:
    signal = tmp_path / "deep" / "signal"
    notice = ["emit", "reclaim-notice", "resources=mba-kilima-01", "notice_minutes=10"]
    command = [sys.executable, "-m", "sharectl", "run", *flags(tmp_path)]
    elsewhere = {**os.environ, "TZ": "Asia/Kolkata"}
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=elsewhere) as run:  # noqa: S603
        try:
            wait_until((signal / "clock").exists)
            started = datetime.fromisoformat((signal / "clock").read_text())
            assert abs((datetime.now(UTC) - started).total_seconds()) < 60
            assert main([*notice, "--signal", str(signal)]) == 0
            wait_until(lambda: " release@v1 " in (signal / "screen.txt").read_text())
        finally:
            run.terminate()
        out, _ = run.communicate()
    assert run.returncode == 0
    assert out.splitlines()[:2] == [f"controller watching {signal}", "controllers: site/mombasa"]
    assert [
        line.split(" ")[2]
        for line in out.splitlines()
        if " site/mombasa " in line and "@v1" in line
    ] == [
        "drain-on-reclaim@v1",
        "fence@v1",
        "release@v1",
    ]
    assert not (signal / "controller.pid").exists()


@pytest.mark.parametrize(
    "pacing",
    [
        ([], "", 0.02),
        (["--tick-ms", "5"], "", 0.005),
        ([], "7", 0.007),
        (["--tick-ms", "5"], "7", 0.005),
    ],
)
def test_run_paces_each_tick_at_tick_ms_20_by_default_and_stops_on_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    capsys: pytest.CaptureFixture[str],
    pacing: tuple[list[str], str, float],
) -> None:
    given, environment, pause = pacing
    (tmp_path / "inventory").symlink_to(ROOT / "inventory")
    (tmp_path / "signal").mkdir()
    (tmp_path / "signal" / "shown").write_text("99")
    monkeypatch.chdir(tmp_path)
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            raise KeyboardInterrupt

    handlers: list[tuple[int, object]] = []

    def install(number: int, handler: object) -> None:
        handlers.append((number, handler))

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr("signal.signal", install)
    if environment:
        monkeypatch.setenv("SHARECTL_TICK_MS", environment)

    assert main(["run", "--site", "mombasa", *given]) == 0

    assert slept == [pause] * 3
    assert handlers == [(signals.SIGTERM, signals.default_int_handler)]
    assert capsys.readouterr().out.splitlines()[:2] == [
        "controller watching signal",
        "controllers: site/mombasa",
    ]
    assert sorted(p.name for p in (tmp_path / "signal").iterdir()) == ["clock", "screen.txt"]


def test_play_plays_the_file_at_a_live_pace_of_20_ms_a_tick_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    settings = ["--inventory", str(ROOT / "inventory"), "--out", str(tmp_path), "--site", "mombasa"]

    assert main(["play", str(RECLAIM), *settings]) == 0

    assert slept == [0.02] * 61
    assert capsys.readouterr().out.splitlines()[-1] == "partner-reclaim: pass"


def lifecycle(tmp_path: Path) -> list[str]:
    return [*flags(tmp_path)[:6], "--tick-ms", "1", "--site", "mombasa"]


def stop(tmp_path: Path) -> int:
    """Stop the controller `start` began in this process, and reap it so no zombie is left."""
    started = int((tmp_path / "deep" / "signal" / "controller.pid").read_text())
    code = main(["stop", *lifecycle(tmp_path)])
    os.waitpid(started, 0)
    return code


def test_start_runs_a_controller_in_its_own_session_quietly_and_stop_stops_it(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    pid = tmp_path / "deep" / "signal" / "controller.pid"

    assert main(["start", *lifecycle(tmp_path)]) == 0
    started = pid.read_text()
    assert os.getsid(int(started)) == int(started)
    time.sleep(0.2)
    assert stop(tmp_path) == 0

    assert capfd.readouterr().out.splitlines() == [
        f"controller started (pid {started})",
        "controller stopped",
    ]
    assert not pid.exists()


def test_start_leaves_a_running_controller_alone_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = tmp_path / "deep" / "signal" / "controller.pid"
    pid.parent.mkdir(parents=True)
    pid.write_text(str(os.getpid()))
    monkeypatch.setattr(subprocess, "Popen", None)

    assert main(["start", *lifecycle(tmp_path)]) == 0
    assert capsys.readouterr().out == f"controller already running (pid {os.getpid()})\n"


def test_stop_with_no_controller_running_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["stop", *lifecycle(tmp_path)]) == 0
    assert capsys.readouterr().out == "no controller running\n"


def test_wait_shows_the_controller_s_new_lines_until_its_clock_moves_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["start", *lifecycle(tmp_path)]) == 0
    capsys.readouterr()
    try:
        assert main(["wait", "1", *lifecycle(tmp_path)]) == 0
        first = capsys.readouterr().out.splitlines()
        assert main(["wait", "5", *lifecycle(tmp_path)]) == 0
        second = capsys.readouterr().out.splitlines()
    finally:
        stop(tmp_path)

    assert first[0] == "controllers: site/mombasa"
    assert sum(" site/mombasa poll " in line for line in first) == 1
    assert sum(" site/mombasa poll " in line for line in second) == 1
    assert "controllers: site/mombasa" not in second


def test_wait_with_no_controller_running_stops_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["wait", "1", *lifecycle(tmp_path)]) == 2
    assert capsys.readouterr().err == "sharectl: error: wait: no controller is running\n"


def test_wait_stops_when_the_controller_dies_rather_than_waiting_for_ever(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pid = tmp_path / "deep" / "signal" / "controller.pid"
    assert main(["start", *lifecycle(tmp_path)]) == 0
    os.kill(int(pid.read_text()), signals.SIGKILL)
    os.waitpid(int(pid.read_text()), 0)

    assert main(["wait", "600", *lifecycle(tmp_path)]) == 2
    assert capsys.readouterr().err == "sharectl: error: wait: no controller is running\n"


def test_run_refuses_a_second_controller_on_the_same_signal_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "sleep", never)
    pid = tmp_path / "deep" / "signal" / "controller.pid"
    pid.parent.mkdir(parents=True)
    pid.write_text(str(os.getpid()))

    assert main(["run", *lifecycle(tmp_path)]) == 2
    assert capsys.readouterr().err == (
        f"sharectl: error: run: a controller is already running (pid {os.getpid()})\n"
    )
    assert pid.read_text() == str(os.getpid())


def test_start_says_so_when_the_controller_exits_as_it_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", "/bin/false")

    assert main(["start", *lifecycle(tmp_path)]) == 2
    assert (
        capsys.readouterr().err == "sharectl: error: start: the controller exited as it started\n"
    )


def test_a_reader_never_sees_the_clock_or_the_pid_half_written(tmp_path: Path) -> None:
    signal = tmp_path / "deep" / "signal"
    clock, pid = signal / "clock", signal / "controller.pid"
    command = [sys.executable, "-m", "sharectl", "run", *flags(tmp_path)]
    seen: set[str] = set()
    with subprocess.Popen(command, stdout=subprocess.DEVNULL) as run:  # noqa: S603, fixed arguments
        try:
            wait_until(clock.exists)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                seen |= {clock.read_text(), pid.read_text()}
        finally:
            run.terminate()

    assert "" not in seen


@pytest.mark.parametrize(
    ("command", "function", "pace"),
    [
        (["run"], "_serve", 20),
        (["start"], "_start", 20),
        (["play", "x.toml"], "_demo", 20),
        (["scenario", "x.toml"], "_demo", 0),
        (["demo"], "_demo", 0),
    ],
)
def test_the_commands_that_run_the_controller_live_pace_it_at_20_ms_a_tick_by_default(
    monkeypatch: pytest.MonkeyPatch, *, command: list[str], function: str, pace: int
) -> None:
    paces: list[int] = []

    def record(args: argparse.Namespace) -> int:
        paces.append(args.tick_ms)
        return 0

    monkeypatch.setattr(cli, function, record)

    assert main([*command, "--site", "mombasa"]) == 0
    assert paces == [pace]


def test_wait_shows_a_restarted_controller_s_lines_from_its_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for _ in range(2):
        assert main(["start", *lifecycle(tmp_path)]) == 0
        capsys.readouterr()
        try:
            assert main(["wait", "1", *lifecycle(tmp_path)]) == 0
        finally:
            stop(tmp_path)
        assert capsys.readouterr().out.splitlines()[0] == "controllers: site/mombasa"


def test_wait_keeps_waiting_until_the_screen_and_clock_appear_and_stops_on_the_minute(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    signal = tmp_path / "deep" / "signal"
    signal.mkdir(parents=True)
    (signal / "controller.pid").write_text(str(os.getpid()))
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        assert len(naps) < 3, "wait did not stop on the minute it was given"
        (signal / "screen.txt").write_text("controllers: site/mombasa\n")
        (signal / "clock").write_text("2026-10-03T03:00:01Z")

    monkeypatch.setattr(time, "sleep", nap)

    assert main(["wait", "0", *lifecycle(tmp_path)]) == 0
    assert capsys.readouterr().out == "controllers: site/mombasa\n"
    assert len(naps) == 1


def test_run_starts_its_clock_at_the_time_now_in_utc_wherever_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()

    def install(number: int, handler: object) -> None:
        del number, handler

    monkeypatch.setattr("signal.signal", install)
    signal = tmp_path / "deep" / "signal"

    def interrupt(seconds: float) -> None:
        del seconds
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", interrupt)
    try:
        assert main(["run", *flags(tmp_path)]) == 0
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()

    started = datetime.fromisoformat((signal / "clock").read_text())
    assert abs((datetime.now(UTC) - started).total_seconds()) < 60


def test_emit_with_no_controller_running_stops_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    given = ["emit", "reclaim-notice", "resources=mba-kilima-01", "notice_minutes=10"]

    assert main([*given, "--signal", str(tmp_path)]) == 2
    assert capsys.readouterr().err == "sharectl: error: emit: no controller is running\n"
    assert not (tmp_path / "events").exists()


def test_run_drops_events_left_from_before_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal = tmp_path / "deep" / "signal"
    (signal / "events").mkdir(parents=True)
    stale = {"kind": "reclaim-notice", "resources": ["mba-kilima-01"], "notice_minutes": 10}
    (signal / "events" / "0-reclaim-notice.json").write_text(json.dumps(stale))
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            raise KeyboardInterrupt

    def install(number: int, handler: object) -> None:
        del number, handler

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr("signal.signal", install)

    assert main(["run", *flags(tmp_path)]) == 0
    assert "drain-on-reclaim@v1" not in (signal / "screen.txt").read_text()
    assert not list((signal / "events").glob("*"))
