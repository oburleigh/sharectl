import json
import os
import signal as signals
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from multiprocessing.connection import Client, Connection, Listener, Pipe
from pathlib import Path
lazy import argparse
lazy from collections.abc import Callable, Generator

import pytest
from xprocess import (  # pyright: ignore[reportMissingTypeStubs], it ships no types
    ProcessStarter,
    XProcess,
    XProcessInfo,
)

from builders import INVENTORY, T0
from sharectl import cli
from sharectl.cli import main
from sharectl.play import Live, Run

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
        "'reclaim-notice' or 'resend-desired'"
    ) in capsys.readouterr().err


def never(seconds: float) -> None:
    """A sleep for a command that should have stopped before its first tick."""
    del seconds
    raise RanOnError


class RanOnError(AssertionError):
    """A command ran on to a tick when it should have stopped first."""


@pytest.fixture(name="controller")
def fixture_controller(
    xprocess: XProcess, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> Generator[Callable[..., XProcessInfo]]:
    """Start `sharectl run` with the settings given, from its first line; stop each at the end.

    What xprocess prints as it starts one is dropped, so a test sees only its own output.
    """
    started: list[XProcessInfo] = []

    def start(*settings: str) -> XProcessInfo:
        args = [sys.executable, "-m", "sharectl", "run", *settings]
        starter = type("Run", (ProcessStarter,), {"pattern": "controller watching", "args": args})
        name = f"{os.getpid()}-{tmp_path.name}-{len(started)}"  # concurrent runs share one cache
        xprocess.ensure(name, starter, restart=True, persist_logs=False)  # pyright: ignore[reportUnknownMemberType]
        capsys.readouterr()
        started.append(xprocess.getinfo(name))  # pyright: ignore[reportUnknownMemberType]
        return started[-1]

    yield start
    for info in started:
        info.terminate()


def kill(info: XProcessInfo) -> None:
    """Kill the controller `info` names with no chance to clean up, then reap it."""
    assert info.pid is not None
    os.kill(info.pid, signals.SIGKILL)
    info.terminate()


def printed(info: XProcessInfo) -> list[str]:
    """What the controller `info` names has printed so far."""
    log = Path(str(info.logpath))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return log.read_text().splitlines()


def test_demo_with_the_defaults_reads_the_inventory_and_scenarios_where_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("inventory", "scenarios"):
        (tmp_path / name).symlink_to(ROOT / name)
    monkeypatch.chdir(tmp_path)

    assert main(["demo", "--site", "mombasa"]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["fibre-cut", "partner-reclaim"]


def script(name: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run a scenario script against the signal directory under `tmp_path`."""
    return subprocess.run(  # noqa: S603, the repository's own script
        [ROOT / "scripts" / f"{name}.sh", "--tick-ms", "5", "--signal", str(tmp_path / "signal")],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


@pytest.mark.script
@pytest.mark.timeout(120)  # the fibre cut plays six simulated hours live
@pytest.mark.parametrize(
    ("name", "decided"),
    [
        ("fibre-cut", ["site-isolation@v1", "wait@v1", "site-rejoin@v1", "resume@v1"]),
        ("partner-reclaim", ["drain-on-reclaim@v1", "fence@v1", "release@v1"]),
    ],
)
def test_each_script_sends_its_events_to_the_running_controller_and_leaves_it_running(
    tmp_path: Path, controller: Callable[..., XProcessInfo], *, name: str, decided: list[str]
) -> None:
    signal = ["--signal", str(tmp_path / "signal"), "--site", "mombasa", "--tick-ms", "5"]
    run = controller(*flags(tmp_path)[:4], *signal)
    result = script(name, tmp_path)

    assert result.returncode == 0, result.stderr
    assert run.isrunning(), "the script stopped a controller it did not start"
    assert [w for line in printed(run) for w in line.split() if w.endswith("@v1")] == decided


@pytest.mark.script
@pytest.mark.parametrize("name", ["fibre-cut", "partner-reclaim"])
def test_a_script_with_no_controller_running_does_nothing_and_says_so(
    tmp_path: Path, name: str
) -> None:
    result = script(name, tmp_path)

    assert result.returncode == 1
    assert result.stderr.splitlines()[-1] == (
        "no controller is running: start it first with make run"
    )
    assert not list((tmp_path / "signal" / "events").glob("*"))


def test_run_answers_on_a_socket_in_its_signal_directory_while_it_runs_and_removes_it_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = tmp_path / "a" / "b" / "controller.sock"
    seen: list[bool] = []

    def sleep(seconds: float) -> None:
        del seconds
        seen.append(socket.is_socket())
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", sleep)
    handled: list[bool] = []

    def install(number: int, handler: object) -> None:
        del number, handler
        handled.append(socket.exists())

    monkeypatch.setattr("signal.signal", install)
    settings = ["--signal", str(socket.parent), "--inventory", str(ROOT / "inventory")]

    assert main(["run", *settings, "--site", "mombasa"]) == 0
    assert seen == [True]
    assert not socket.exists()
    assert handled == [False], "SIGTERM must be handled before the socket is opened"


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
    tmp_path: Path, controller: Callable[..., XProcessInfo], capsys: pytest.CaptureFixture[str]
) -> None:
    given = ["emit", "reclaim-notice", "resources=mba-kilima-01,mba-srv-1", "notice_minutes=10"]
    controller(*lifecycle(tmp_path))

    assert main([*given, *lifecycle(tmp_path)]) == 0
    assert main([*given, *lifecycle(tmp_path)]) == 0

    record = {
        "kind": "reclaim-notice",
        "resources": ["mba-kilima-01", "mba-srv-1"],
        "notice_minutes": 10,
    }
    assert capsys.readouterr().out == f"emitted {json.dumps(record)}\n" * 2


def test_emit_keeps_an_equals_sign_inside_a_value(
    tmp_path: Path, controller: Callable[..., XProcessInfo], capsys: pytest.CaptureFixture[str]
) -> None:
    controller(*lifecycle(tmp_path))

    assert main(["emit", "agreement-final", "agreement=a=1", *lifecycle(tmp_path)]) == 0
    assert capsys.readouterr().out == ('emitted {"kind": "agreement-final", "agreement": "a=1"}\n')


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

    assert main([*given, "--signal", str(tmp_path)]) == 2

    error = capsys.readouterr().err
    assert "notice_minutes" in error
    assert "Input should be a valid integer" in error


def test_emit_rejects_a_field_no_event_has(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["emit", "paths-down", "pathz=oob", "--signal", str(tmp_path)]) == 2

    assert capsys.readouterr().err == "sharectl: error: emit: pathz is not a field of an event\n"


def test_emit_rejects_a_field_with_no_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["emit", "paths-down", "oob", "--signal", str(tmp_path)])

    assert stopped.value.code == 2
    assert capsys.readouterr().err.endswith("argument field=value: oob: not field=value\n")


def test_run_is_the_live_controller_acting_on_an_event_sent_to_its_mocks(
    tmp_path: Path, controller: Callable[..., XProcessInfo], capsys: pytest.CaptureFixture[str]
) -> None:
    signal = tmp_path / "deep" / "signal"
    notice = ["emit", "reclaim-notice", "resources=mba-kilima-01", "notice_minutes=10"]
    run = controller(*lifecycle(tmp_path))

    assert main([*notice, *lifecycle(tmp_path)]) == 0
    assert main(["wait", "10", *lifecycle(tmp_path)]) == 0

    assert printed(run)[:2] == [f"controller watching {signal}", "controllers: site/mombasa"]
    assert [
        line.split(" ")[2]
        for line in capsys.readouterr().out.splitlines()
        if " site/mombasa " in line and "@v1" in line
    ] == [
        "drain-on-reclaim@v1",
        "fence@v1",
        "release@v1",
    ]


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
    assert not list((tmp_path / "signal").iterdir())


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


def test_wait_shows_the_controller_s_new_lines_until_its_clock_moves_on(
    tmp_path: Path, controller: Callable[..., XProcessInfo], capsys: pytest.CaptureFixture[str]
) -> None:
    # xprocess looks for the ready line every 0.1 s: 5 simulated s at 1 ms a tick, 50 s at 20 ms
    controller(*flags(tmp_path)[:6], "--tick-ms", "20", "--site", "mombasa")

    assert main(["wait", "1", *lifecycle(tmp_path)]) == 0
    first = capsys.readouterr().out.splitlines()
    assert main(["wait", "5", *lifecycle(tmp_path)]) == 0
    second = capsys.readouterr().out.splitlines()

    assert first[0] == "controllers: site/mombasa"
    assert sum(" site/mombasa poll " in line for line in first) == 1
    assert sum(" site/mombasa poll " in line for line in second) == 1
    assert "controllers: site/mombasa" not in second


def test_wait_with_no_controller_running_stops_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["wait", "1", *lifecycle(tmp_path)]) == 2
    assert capsys.readouterr().err == "sharectl: error: wait: no controller is running\n"


def test_wait_and_emit_find_no_controller_on_the_socket_a_killed_one_left(
    tmp_path: Path, controller: Callable[..., XProcessInfo], capsys: pytest.CaptureFixture[str]
) -> None:
    kill(controller(*lifecycle(tmp_path)))

    assert (tmp_path / "deep" / "signal" / "controller.sock").is_socket()
    assert main(["wait", "1", *lifecycle(tmp_path)]) == 2
    assert main(["emit", "paths-down", "paths=oob", *lifecycle(tmp_path)]) == 2
    assert capsys.readouterr().err == (
        "sharectl: error: wait: no controller is running\n"
        "sharectl: error: emit: no controller is running\n"
    )


def test_wait_stops_when_the_controller_dies_while_it_waits(
    tmp_path: Path, controller: Callable[..., XProcessInfo], capsys: pytest.CaptureFixture[str]
) -> None:
    run = controller(*lifecycle(tmp_path))
    codes: list[int] = []
    waiting = threading.Thread(
        target=lambda: codes.append(main(["wait", "600000", *lifecycle(tmp_path)]))
    )
    waiting.start()
    waiting.join(timeout=1)
    run.terminate()
    waiting.join(timeout=10)

    assert codes == [2]
    assert capsys.readouterr().err == "sharectl: error: wait: no controller is running\n"


def test_run_starts_on_the_socket_a_killed_controller_left(
    tmp_path: Path,
    controller: Callable[..., XProcessInfo],
    capsys: pytest.CaptureFixture[str],
) -> None:
    kill(controller(*lifecycle(tmp_path)))

    controller(*lifecycle(tmp_path))
    assert main(["wait", "0", *lifecycle(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "controllers: site/mombasa"


def test_run_refuses_a_second_controller_on_the_same_signal_directory(
    tmp_path: Path,
    controller: Callable[..., XProcessInfo],
    *,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller(*lifecycle(tmp_path))
    monkeypatch.setattr(time, "sleep", never)

    assert main(["run", *lifecycle(tmp_path)]) == 2
    assert capsys.readouterr().err == "sharectl: error: run: a controller is already running\n"
    assert main(["wait", "0", *lifecycle(tmp_path)]) == 0


@pytest.mark.parametrize(
    ("command", "function", "pace"),
    [
        (["run"], "_serve", 20),
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


@pytest.mark.parametrize("command", ["start", "stop"])
def test_the_controller_is_never_started_or_stopped_in_the_background(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([command, *lifecycle(tmp_path)])

    assert exit_info.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == (
        f"sharectl: error: argument command: invalid choice: '{command}' "
        "(choose from 'demo', 'scenario', 'run', 'play', 'emit', 'wait')"
    )


def test_wait_shows_a_restarted_controller_s_lines_from_its_first(
    tmp_path: Path, controller: Callable[..., XProcessInfo], capsys: pytest.CaptureFixture[str]
) -> None:
    for _ in range(2):
        run = controller(*lifecycle(tmp_path))
        assert main(["wait", "1", *lifecycle(tmp_path)]) == 0
        run.terminate()
        assert capsys.readouterr().out.splitlines()[0] == "controllers: site/mombasa"


def test_run_starts_its_clock_at_the_time_now_in_utc_wherever_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    starts: list[datetime] = []

    def listen(live: Live, start: datetime, show: Callable[[str], None]) -> None:
        del live, show
        starts.append(start)

    def install(number: int, handler: object) -> None:
        del number, handler

    monkeypatch.setattr(Live, "listen", listen)
    monkeypatch.setattr("signal.signal", install)
    try:
        assert main(["run", *flags(tmp_path)]) == 0
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()

    assert starts[0].tzinfo is UTC
    assert starts[0].microsecond == 0
    assert abs((datetime.now(UTC) - starts[0]).total_seconds()) < 60


def test_emit_with_no_controller_running_stops_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    given = ["emit", "reclaim-notice", "resources=mba-kilima-01", "notice_minutes=10"]

    assert main([*given, "--signal", str(tmp_path)]) == 2
    assert capsys.readouterr().err == "sharectl: error: emit: no controller is running\n"
    assert not (tmp_path / "events").exists()


def test_run_answers_on_its_socket_from_a_daemon_thread_while_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = tmp_path / "controller.sock"
    replies: list[list[bytes]] = []
    daemons: list[bool] = []
    before = set(threading.enumerate())

    def sleep(seconds: float) -> None:
        del seconds
        with Client(str(socket)) as asker:
            asker.send_bytes(b'{"wait": 0}')
            replies.append(answered(asker))
        daemons.extend(t.daemon for t in threading.enumerate() if t not in before)
        raise KeyboardInterrupt

    def install(number: int, handler: object) -> None:
        del number, handler

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr("signal.signal", install)
    settings = ["--signal", str(tmp_path), "--inventory", str(ROOT / "inventory")]

    assert main(["run", *settings, "--site", "mombasa"]) == 0
    assert replies[0][0] == json.dumps("controllers: site/mombasa").encode()
    assert daemons
    assert all(daemons)


@pytest.mark.usefixtures("bounded")
def test_a_reply_hands_an_emitted_event_to_the_mocks_then_ends() -> None:
    live = Live(INVENTORY, run=Run(sites=frozenset({"mombasa"})))
    asker, answerer = Pipe()
    notice = {"kind": "reclaim-notice", "resources": ["mba-kilima-01"], "notice_minutes": 10}
    asker.send_bytes(json.dumps({"emit": notice}).encode())

    cli.reply(answerer, live)

    assert asker.recv_bytes() == b"null"
    shown: list[str] = []
    live.listen(T0, shown.append, until=T0)
    assert " drain-on-reclaim@v1 " in shown[-1]


@pytest.mark.usefixtures("bounded")
def test_a_reply_to_a_wait_sends_each_line_then_ends() -> None:
    live = Live(INVENTORY, run=Run(sites=frozenset({"mombasa"})))
    shown: list[str] = []
    live.listen(T0, shown.append, until=T0)
    asker, answerer = Pipe()
    asker.send_bytes(b'{"wait": 0}')

    cli.reply(answerer, live)

    assert [json.loads(asker.recv_bytes()) for _ in range(len(shown) + 1)] == [*shown, None]


def test_a_reply_to_an_asker_that_has_gone_ends_quietly() -> None:
    asker, answerer = Pipe()
    asker.close()

    cli.reply(answerer, Live(INVENTORY, run=Run()))

    assert answerer.closed


def answered(asker: Connection) -> list[bytes]:
    """Each message `asker` gets before `null`, failing rather than waiting over 5 s for one."""
    messages: list[bytes] = []
    while True:
        assert asker.poll(5), "the controller did not answer"
        if (message := asker.recv_bytes()) == b"null":
            return messages
        messages.append(message)


@pytest.mark.usefixtures("bounded")
def test_a_reply_to_an_asker_that_left_while_it_waited_ends_quietly() -> None:
    live = Live(INVENTORY, run=Run(sites=frozenset({"mombasa"})))
    live.listen(T0, lambda _: None, until=T0)
    asker, answerer = Pipe()
    asker.send_bytes(b'{"wait": 0}')
    asker.close()

    cli.reply(answerer, live)

    assert answerer.closed


@pytest.mark.usefixtures("bounded")
def test_a_wait_still_open_never_holds_the_controller_open(tmp_path: Path) -> None:
    live = Live(INVENTORY, run=Run(sites=frozenset({"mombasa"})))
    live.listen(T0, lambda _: None, until=T0)
    address = str(tmp_path / "controller.sock")
    before = set(threading.enumerate())
    with Listener(address) as listener:
        threading.Thread(target=cli.answer, args=(listener, live), daemon=True).start()
        with Client(address) as asker:
            asker.send_bytes(b'{"wait": 1}')
            assert asker.poll(5), "the controller did not answer"
            others = [t for t in threading.enumerate() if t not in before]
            live.listen(T0, lambda _: None, until=T0 + timedelta(seconds=70))  # ends the wait

    assert len(others) == 2
    assert all(t.daemon for t in others)


@pytest.mark.usefixtures("bounded")
def test_the_controller_answers_each_asker_in_turn(tmp_path: Path) -> None:
    live = Live(INVENTORY, run=Run(sites=frozenset({"mombasa"})))
    shown: list[str] = []
    live.listen(T0, shown.append, until=T0)
    address = str(tmp_path / "controller.sock")
    replies: list[list[bytes]] = []
    with Listener(address) as listener:
        threading.Thread(target=cli.answer, args=(listener, live), daemon=True).start()
        for _ in range(2):
            with Client(address) as asker:
                asker.send_bytes(b'{"wait": 0}')
                replies.append(answered(asker))

    assert replies == [[json.dumps(line).encode() for line in shown], []]
