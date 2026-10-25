"""Command-line entry point."""

import argparse
from importlib.metadata import version
lazy from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run the requested command.

    Args:
        argv: Arguments without the program name; `None` reads `sys.argv`.

    Returns:
        The process exit status.
    """
    parser = argparse.ArgumentParser(prog="sharectl", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('sharectl')}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
