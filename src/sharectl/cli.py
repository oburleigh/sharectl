"""Command-line entry point: play scenario files, or run the controller live on its mocks."""

import argparse
import contextlib
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
lazy from collections.abc import Sequence

from pydantic import TypeAdapter, ValidationError

from sharectl.inventory import load
from sharectl.model import Role
from sharectl.play import (
    CLOCK,
    EVENTS,
    PID,
    SCREEN,
    SHOWN,
    Event,
    Kind,
    Live,
    Run,
    load_scenario,
    play,
    replace_text,
)
lazy from sharectl.model import Inventory

SETTINGS = {"inventory": "inventory", "scenarios": "scenarios", "out": "out", "signal": "signal"}
"""Each directory setting's default, relative to where the command runs.

Every setting's environment variable, `SHARECTL_<SETTING>`, replaces its default.
"""

HELP = {
    "sharectl": __doc__,
    "tick-ms": "wall-clock ms per 10 s tick",
    "site": "the PoPs whose controllers run, comma-separated; required",
    "demo": "play every scenario file at once",
    "scenario": "play one file at once",
    "run": "run the controller, live",
    "play": "play one file at a live pace",
    "emit": "hand one event to the running controller's mocks, as field=value pairs",
    "start": "start the controller in the background, unless one is running",
    "stop": "stop the running controller",
    "wait": "show the running controller's lines until its clock has moved on",
}
"""The help text of the command and each of its parts."""


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run the requested command.

    Args:
        argv: Arguments without the program name; `None` reads `sys.argv`.

    Returns:
        The process exit status: 0 when every scenario played passes, 2 for an invalid file.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.tick_ms is None:  # demo and scenario play at once unless paced; the rest run live
        live = "20" if args.command in {"run", "play", "start"} else "0"
        args.tick_ms = int(os.getenv("SHARECTL_TICK_MS", live))
    command = {"demo": _demo, "scenario": _demo, "run": _serve, "play": _demo, "emit": _emit}
    command |= {"start": _start, "stop": _stop, "wait": _wait}
    try:
        return command[args.command](args)
    except (ValidationError, CommandError) as error:
        sys.stderr.write(f"{parser.prog}: error: {error}\n")
        return 2


def _demo(args: argparse.Namespace) -> int:
    """Play every scenario file, or the one named, at `--tick-ms`; 0 only if all pass."""
    demo = args.command == "demo"
    files: list[Path] = sorted(args.scenarios.glob("*.toml")) if demo else [args.file]
    inventory = load(args.inventory)
    run = Run(pace=args.tick_ms / 1000, sites=_sites(args, inventory))
    failed = [play(load_scenario(f), inventory, args.out, show=_show, run=run) for f in files]
    return 1 if any(failed) else 0


def _parser() -> argparse.ArgumentParser:
    settings = argparse.ArgumentParser(add_help=False)  # pragma: no mutate, None is falsy too
    for name, default in SETTINGS.items():
        variable = f"SHARECTL_{name.upper()}"
        settings.add_argument(f"--{name}", type=Path, default=Path(os.getenv(variable, default)))
    settings.add_argument("--tick-ms", type=int, help=HELP["tick-ms"])
    settings.add_argument("--site", default=os.getenv("SHARECTL_SITE", ""), help=HELP["site"])
    parser = argparse.ArgumentParser(prog="sharectl", description=HELP["sharectl"])
    release = version("sharectl")  # pragma: no mutate, package names are normalised
    parser.add_argument("--version", action="version", version=f"%(prog)s {release}")
    commands = parser.add_subparsers(dest="command")
    added = {
        name: commands.add_parser(name, parents=[settings], help=HELP[name])
        for name in ("demo", "scenario", "run", "play", "emit", "start", "stop", "wait")
    }
    added["wait"].add_argument("minutes", type=int, help="simulated minutes")
    one, hand, emit = added["scenario"], added["play"], added["emit"]
    for command in (one, hand):
        command.add_argument("file", type=Path)
    emit.add_argument("kind", choices=list(Kind))
    emit.add_argument("fields", nargs="*", type=_field, metavar="field=value")
    return parser


def _field(text: str) -> tuple[str, str]:
    """One `field=value` pair."""
    name, equals, value = text.partition("=")
    if not equals:
        raise PairError(text)
    return name, value


class PairError(argparse.ArgumentTypeError):
    """An `emit` argument that is not `field=value`."""

    def __init__(self, text: str) -> None:
        """Name the argument and the reason."""
        super().__init__(f"{text}: not field=value")


class CommandError(ValueError):
    """A command that cannot go on; the message names the option or the state, and the reason."""


class RunningError(CommandError):
    """`run` found a controller already running on its signal directory."""

    def __init__(self, pid: str) -> None:
        """Name the running controller."""
        super().__init__(f"run: a controller is already running (pid {pid})")


class StartError(CommandError):
    """The controller `start` launched exited before it was running."""

    def __init__(self) -> None:
        """Say what happened."""
        super().__init__("start: the controller exited as it started")


class NotRunningError(CommandError):
    """A command that needs a running controller found none, or it stopped."""

    def __init__(self, command: str) -> None:
        """Name the command and what is missing."""
        super().__init__(f"{command}: no controller is running")


class UnknownFieldError(CommandError):
    """An `emit` field that no event has."""

    def __init__(self, name: str) -> None:
        """Name the field and the reason."""
        super().__init__(f"emit: {name} is not a field of an event")


class MissingSiteError(CommandError):
    """No `--site` and no `SHARECTL_SITE`: a controller is always told where it runs."""

    def __init__(self) -> None:
        """Name the option and the reason."""
        super().__init__("--site is required: the PoP this controller runs at")


class UnknownSiteError(CommandError):
    """A `--site` name that is not a PoP in the inventory."""

    def __init__(self, name: str) -> None:
        """Name the option, the site and the reason."""
        super().__init__(f"--site: {name} is not a PoP in the inventory")


def _sites(args: argparse.Namespace, inventory: Inventory) -> frozenset[str]:
    """The PoPs `--site` names: at least one, and every one a PoP in the inventory."""
    pops = {s.id for s in inventory.sites if s.role is Role.POP}
    named = frozenset(name for name in args.site.split(",") if name)
    if not named:
        raise MissingSiteError
    if unknown := sorted(named - pops):
        raise UnknownSiteError(unknown[0])
    return named


def _show(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def _serve(args: argparse.Namespace) -> int:
    """Run the controller live from now, taking each event sent to its mocks, until stopped."""
    inventory = load(args.inventory)
    run = Run(pace=args.tick_ms / 1000, sites=_sites(args, inventory))
    args.signal.mkdir(parents=True, exist_ok=True)
    pid = args.signal / PID
    if _alive(pid):
        raise RunningError(pid.read_text())
    (args.signal / SHOWN).unlink(missing_ok=True)
    shutil.rmtree(args.signal / EVENTS, ignore_errors=True)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    replace_text(pid, str(os.getpid()))
    _show(f"controller watching {args.signal}")
    now = datetime.now(UTC)
    start = now.replace(microsecond=0)  # pragma: no mutate, stamps are whole seconds
    try:
        with contextlib.suppress(KeyboardInterrupt):
            Live(inventory, args.signal, run=run).listen(start, _show)
    finally:
        pid.unlink(missing_ok=True)
    return 0


def _emit(args: argparse.Namespace) -> int:
    """Hand one event to the running controller's mocks, through the signal directory.

    The event is checked as a scenario event is, and lands at the controller's next tick.
    """
    if not _alive(args.signal / PID):
        raise NotRunningError(args.command)
    fields = {f.name: f.default for f in dataclasses.fields(Event)}
    given: dict[str, object] = {}
    for name, value in args.fields:
        if name not in fields:
            raise UnknownFieldError(name)
        given[name] = value.split(",") if isinstance(fields[name], tuple) else value
    adapter = TypeAdapter(Event)
    at = "1970-01-01T00:00:00Z"  # pragma: no mutate, a placeholder: it lands at the next tick
    values = adapter.dump_python(adapter.validate_python({**given, "kind": args.kind, "at": at}))
    record = {"kind": args.kind, **{name: values[name] for name in given}}
    events = args.signal / EVENTS
    events.mkdir(exist_ok=True)
    replace_text(events / f"{time.time_ns()}-{args.kind}.json", json.dumps(record))
    _show(f"emitted {json.dumps(record)}")
    return 0


def _start(args: argparse.Namespace) -> int:
    """Start the controller in the background, unless one is running; say which."""
    pid = args.signal / PID
    if _alive(pid):
        _show(f"controller already running (pid {pid.read_text()})")
        return 0
    _sites(args, load(args.inventory))
    flags = [
        w
        for k in (*SETTINGS, "tick_ms", "site")
        for w in (f"--{k.replace('_', '-')}", str(getattr(args, k)))
    ]
    # It outlives this command, so it is not opened in a `with`.
    started = subprocess.Popen(  # noqa: S603  # pylint: disable=consider-using-with
        [sys.executable, "-m", "sharectl", "run", *flags],
        stdout=subprocess.DEVNULL,
        start_new_session=True,
    )
    while not (_alive(pid) and pid.read_text() == str(started.pid)):
        if started.poll() is not None:
            raise StartError
        time.sleep(0.01)  # pragma: no mutate, poll interval
    _show(f"controller started (pid {started.pid})")
    return 0


def _stop(args: argparse.Namespace) -> int:
    """Stop the running controller, and wait until it has."""
    pid = args.signal / PID
    if not _alive(pid):
        _show("no controller running")
        return 0
    os.kill(int(pid.read_text()), signal.SIGTERM)
    while _alive(pid):
        time.sleep(0.01)  # pragma: no mutate, poll interval
    _show("controller stopped")
    return 0


def _wait(args: argparse.Namespace) -> int:
    """Show the controller's lines not shown yet until its clock has moved `minutes` on.

    Stops with an error if the controller is not running, or stops while this waits.
    """
    pid, clock, screen, shown = (args.signal / name for name in (PID, CLOCK, SCREEN, SHOWN))
    until: datetime | None = None  # pragma: no mutate, falsy until the clock is first read
    while True:
        if not _alive(pid):
            raise NotRunningError(args.command)
        now = datetime.fromisoformat(clock.read_text()) if clock.exists() else None
        if now and not until:
            until = now + timedelta(minutes=args.minutes)
        lines: list[str] = screen.read_text().split("\n")[:-1] if screen.exists() else []
        done = int(shown.read_text()) if shown.exists() else 0
        for line in lines[done:]:
            _show(line)
        shown.write_text(str(len(lines)))
        if now and until and now >= until:
            return 0
        time.sleep(0.01)  # pragma: no mutate, poll interval


def _alive(pid: Path) -> bool:
    """Say whether the process whose id `pid` holds is running."""
    try:
        os.kill(int(pid.read_text()), 0)  # pragma: no mutate, signal 0 only asks if it runs
    except FileNotFoundError, ProcessLookupError, ValueError:
        return False
    return True
