import os
import signal as signals
import subprocess
import sys
import time
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


@pytest.mark.parametrize(
    "pacing",
    [
        ([], "", 0.02, 0.02),
        (["--tick-ms", "5"], "", 0.005, 0.01),
        ([], "7", 0.007, 0.01),
        (["--tick-ms", "5"], "7", 0.005, 0.01),
    ],
)
def test_run_paces_each_tick_at_tick_ms_20_by_default_and_stops_on_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    capsys: pytest.CaptureFixture[str],
    pacing: tuple[list[str], str, float, float],
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

    if pacing[1]:
        monkeypatch.setenv("SHARECTL_TICK_MS", pacing[1])
    args, slept_per_tick, slept_idle = pacing[0], *pacing[2:]
    assert main(["run", "--site", "mombasa", *args]) == 0

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

    assert main(["demo", "--site", "mombasa"]) == 0
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
    assert result.stdout.splitlines()[0] == "controllers: site/mombasa"
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

    pid_when_handled: list[bool] = []

    def install(number: int, handler: object) -> None:
        del number, handler
        pid_when_handled.append(pid.exists())

    monkeypatch.setattr("signal.signal", install)

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
    assert not pid.exists()
    assert pid_when_handled == [False], "SIGTERM must be handled before the pid is published"


def test_play_exits_1_when_the_controller_reports_a_fail(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong.toml"
    wrong.write_text(RECLAIM.read_text().replace("evict 4 of its 4", "evict 3 of its 4"))

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
        "--site SITE the PoPs whose controllers run, comma-separated; required",
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


def test_run_runs_only_the_named_sites_controllers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal = tmp_path / "deep" / "signal"
    signal.mkdir(parents=True)
    (signal / "partner-reclaim.toml").write_bytes(RECLAIM.read_bytes())

    def sleep(seconds: float) -> None:
        del seconds
        if not [*signal.glob("*.toml"), *signal.glob("*.playing")]:
            raise KeyboardInterrupt

    def install(number: int, handler: object) -> None:
        del number, handler

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr("signal.signal", install)

    assert main(["run", *flags(tmp_path), "--site", "mombasa"]) == 0
    assert site_logs(tmp_path) == ["site-mba.jsonl"]


def test_play_with_no_site_and_no_controller_running_stops_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["play", str(RECLAIM), *flags(tmp_path)[:-2]]) == 2
    assert capsys.readouterr().err == (
        "sharectl: error: --site is required: the PoP this controller runs at\n"
    )


def test_play_checks_the_site_even_when_a_controller_is_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pid = tmp_path / "deep" / "signal" / "controller.pid"
    pid.parent.mkdir(parents=True)
    pid.write_text(str(os.getpid()))

    assert main(["play", str(RECLAIM), *flags(tmp_path)[:-2], "--site", "paris"]) == 2
    assert (
        capsys.readouterr().err == "sharectl: error: --site: paris is not a PoP in the inventory\n"
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


def test_play_hands_the_controller_it_starts_a_live_pace_of_20_ms_a_tick_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paces: list[int] = []

    def send(args: argparse.Namespace) -> int:
        paces.append(args.tick_ms)
        return 0

    monkeypatch.setattr(cli, "_send", send)

    assert main(["play", str(RECLAIM), "--site", "mombasa"]) == 0

    assert paces == [20]


def test_run_with_no_site_stops_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
