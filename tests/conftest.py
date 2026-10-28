import os
import signal
import sys
from pathlib import Path
lazy from collections.abc import Iterator
lazy from types import FrameType

import pytest

sys.path.insert(0, str(Path(__file__).parent))


def _timed_out(number: int, frame: FrameType | None) -> None:
    del number, frame
    raise TimeoutError


@pytest.fixture(autouse=True)
def watchdog(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail a test that hangs, rather than hang the run; a script runs a whole scenario live."""
    previous = signal.signal(signal.SIGALRM, _timed_out)
    signal.alarm(120 if "script" in request.keywords else 20)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


@pytest.fixture(autouse=True)
def no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test on the built-in defaults, whatever `.env` or the shell exports."""
    for name in [n for n in os.environ if n.startswith("SHARECTL_")]:
        monkeypatch.delenv(name)
