"""Command-line entry point: play scenario files, or run the controller live on its mocks."""

import argparse
import contextlib
import dataclasses
import json
import os
import signal
import sys
import threading
from datetime import UTC, datetime
from functools import partial
from importlib.metadata import version
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
lazy from collections.abc import Iterator, Sequence

from pydantic import TypeAdapter, ValidationError

from sharectl.inventory import load
from sharectl.model import Role
from sharectl.play import (
    Event,
    Kind,
    Live,
    Run,
    load_scenario,
    play,
)
lazy from sharectl.model import Inventory

_show = partial(print, flush=True)
"""Print a screen line at once, so a reader of a pipe sees it as it happens."""

SOCKET = "controller.sock"
"""The live controller's socket in its signal directory, where `emit` and `wait` reach it."""

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
    if args.tick_ms is None:
        live = "20" if args.command in {"run", "play"} else "0"
        args.tick_ms = int(os.getenv("SHARECTL_TICK_MS", live))
    command = {"demo": _demo, "scenario": _demo, "run": _serve, "play": _demo, "emit": _emit}
    command |= {"wait": _wait}
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
        for name in ("demo", "scenario", "run", "play", "emit", "wait")
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

    def __init__(self) -> None:
        """Say what is running."""
        super().__init__("run: a controller is already running")


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


def _serve(args: argparse.Namespace) -> int:
    """Run the controller live from now, taking each event sent to its mocks, until stopped."""
    inventory = load(args.inventory)
    run = Run(pace=args.tick_ms / 1000, sites=_sites(args, inventory))
    args.signal.mkdir(parents=True, exist_ok=True)
    socket = args.signal / SOCKET
    try:
        Client(str(socket)).close()
    except FileNotFoundError, ConnectionRefusedError:  # refused: a killed controller's socket
        socket.unlink(missing_ok=True)
    else:
        raise RunningError
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    live = Live(inventory, run=run)
    with Listener(str(socket)) as listener:
        threading.Thread(target=answer, args=(listener, live), daemon=True).start()
        _show(f"controller watching {args.signal}")
        now = datetime.now(UTC)
        start = now.replace(microsecond=0)  # pragma: no mutate, stamps are whole seconds
        with contextlib.suppress(KeyboardInterrupt):
            live.listen(start, _show)
    return 0


def answer(listener: Listener, live: Live) -> None:
    """Answer each `emit` and `wait` sent to the controller, each in its own thread, for ever.

    Run on a daemon thread: each reply thread inherits that, so a wait still open never holds the
    controller open.
    """
    while True:
        threading.Thread(target=reply, args=(listener.accept(), live)).start()


def reply(connection: Connection, live: Live) -> None:
    """Hand an event to the mocks, or send each line a wait shows; `null` ends the reply."""
    with connection, contextlib.suppress(EOFError, OSError):  # the asker has gone
        request = json.loads(connection.recv_bytes())
        if "emit" in request:
            live.send(request["emit"])
        else:
            for line in live.wait(request["wait"]):
                connection.send_bytes(json.dumps(line).encode())
        connection.send_bytes(b"null")


def _ask(args: argparse.Namespace, request: dict[str, object]) -> Iterator[str]:
    """Send `request` to the running controller and give each line of its reply."""
    try:
        connection = Client(str(args.signal / SOCKET))
    except (FileNotFoundError, ConnectionRefusedError) as error:
        raise NotRunningError(args.command) from error
    with connection:
        connection.send_bytes(json.dumps(request).encode())
        try:
            while (line := json.loads(connection.recv_bytes())) is not None:
                yield line
        except EOFError as error:
            raise NotRunningError(args.command) from error


def _emit(args: argparse.Namespace) -> int:
    """Hand one event to the running controller's mocks.

    The event is checked as a scenario event is, and lands at the controller's next tick.
    """
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
    list(_ask(args, {"emit": record}))  # an emit's reply is only its end
    _show(f"emitted {json.dumps(record)}")
    return 0


def _wait(args: argparse.Namespace) -> int:
    """Show the controller's lines not shown yet until its clock has moved `minutes` on.

    Stops with an error if the controller is not running, or stops while this waits.
    """
    for line in _ask(args, {"wait": args.minutes}):
        _show(line)
    return 0
