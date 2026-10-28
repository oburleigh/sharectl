"""Command-line entry point: play scenario files, or run the controller and hand it files."""

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path
lazy from collections.abc import Sequence

from pydantic import ValidationError

from sharectl.inventory import load
from sharectl.model import Role
from sharectl.play import Run, load_scenario, play
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
    "play": "hand a file to the controller",
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
    if args.tick_ms is None:  # demo and scenario play at once unless paced; run and play are live
        live = "20" if args.command in {"run", "play"} else "0"
        args.tick_ms = int(os.getenv("SHARECTL_TICK_MS", live))
    command = {"demo": _demo, "scenario": _demo, "run": _serve, "play": _send}[args.command]
    try:
        return command(args)
    except (ValidationError, MissingSiteError, UnknownSiteError) as error:
        sys.stderr.write(f"{parser.prog}: error: {error}\n")
        return 2


def _demo(args: argparse.Namespace) -> int:
    """Play every scenario file, or the one named, at once; 0 only if all pass."""
    demo = args.command == "demo"
    files: list[Path] = sorted(args.scenarios.glob("*.toml")) if demo else [args.file]
    inventory = load(args.inventory)
    sites = _sites(args, inventory)
    pace = args.tick_ms / 1000
    passed = [_play(f, inventory, args, sites=sites, pace=pace) for f in files]
    return 0 if all(passed) else 1


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
        for name in ("demo", "scenario", "run", "play")
    }
    one, hand = added["scenario"], added["play"]
    for command in (one, hand):
        command.add_argument("file", type=Path)
    return parser


class MissingSiteError(ValueError):
    """No `--site` and no `SHARECTL_SITE`: a controller is always told where it runs."""

    def __init__(self) -> None:
        """Name the option and the reason."""
        super().__init__("--site is required: the PoP this controller runs at")


class UnknownSiteError(ValueError):
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


def _play(
    path: Path,
    inventory: Inventory,
    args: argparse.Namespace,
    *,
    sites: frozenset[str],
    pace: float,
) -> bool:
    """Play one file, showing each line as it happens; say whether it passed."""
    run = Run(pace=pace, sites=sites)
    return not play(load_scenario(path), inventory, args.out, show=_show, run=run)


def _serve(args: argparse.Namespace) -> int:
    """Run the controller: play each scenario file handed to the signal directory, in name order."""
    pause = args.tick_ms / 1000
    inventory = load(args.inventory)
    sites = _sites(args, inventory)
    args.signal.mkdir(parents=True, exist_ok=True)
    pid = args.signal / "controller.pid"
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    pid.write_text(str(os.getpid()))
    _show(f"controller watching {args.signal}")
    try:
        while True:
            for path in sorted(args.signal.glob("*.toml")):
                playing = path.rename(path.with_suffix(".playing"))
                _play(playing, inventory, args, sites=sites, pace=pause)
                playing.unlink()
            time.sleep(max(pause, 0.01))
    except KeyboardInterrupt:
        return 0
    finally:
        pid.unlink(missing_ok=True)


def _send(args: argparse.Namespace) -> int:
    """Hand a file to the running controller and show its lines until the verdict.

    A controller is started for the file when none is running, and stopped after it.
    """
    name = load_scenario(args.file).name
    pid = args.signal / "controller.pid"
    _sites(args, load(args.inventory))
    running = _alive(pid)
    flags = [
        w
        for k in (*SETTINGS, "tick_ms", "site")
        for w in (f"--{k.replace('_', '-')}", str(getattr(args, k)))
    ]
    with (
        contextlib.nullcontext()
        if running
        else subprocess.Popen(  # noqa: S603, this interpreter and the parsed settings
            [sys.executable, "-m", "sharectl", "run", *flags], stdout=subprocess.DEVNULL
        )
    ) as started:
        try:
            shown = _follow(args, name, pid)
        finally:
            if started is not None:
                started.terminate()
    return 0 if shown[-1] == f"{name}: pass" else 1


def _follow(args: argparse.Namespace, name: str, pid: Path) -> list[str]:
    """Hand the file to the controller once it runs; show its lines until the verdict."""
    while not _alive(pid):
        time.sleep(0.01)  # pragma: no mutate, poll interval
    screen: Path = args.out / name / "screen.txt"
    screen.unlink(missing_ok=True)
    part = args.signal / f"{name}.part"
    part.write_bytes(args.file.read_bytes())
    part.rename(args.signal / f"{name}.toml")
    verdicts = {f"{name}: pass", f"{name}: FAIL"}
    shown: list[str] = []
    while not shown or shown[-1] not in verdicts:
        lines = screen.read_text().split("\n")[:-1] if screen.exists() else []
        for line in lines[len(shown) :]:
            _show(line)
        shown = lines
        time.sleep(0.01)  # pragma: no mutate, poll interval
    return shown


def _alive(pid: Path) -> bool:
    """Say whether the process whose id `pid` holds is running."""
    try:
        os.kill(int(pid.read_text()), 0)
    except FileNotFoundError, ProcessLookupError, ValueError:
        return False
    return True
