import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail a run that does not stop within 20 ticks, rather than hang on it."""
    ticks: list[float] = []

    def tick(seconds: float) -> None:
        ticks.append(seconds)
        assert len(ticks) < 20, "the estate ran on past its last tick"

    monkeypatch.setattr(time, "sleep", tick)


@pytest.fixture(autouse=True)
def no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test on the built-in defaults, whatever `.env` or the shell exports."""
    for name in [n for n in os.environ if n.startswith("SHARECTL_")]:
        monkeypatch.delenv(name)
