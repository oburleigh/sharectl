"""The generic scenario player: inject a file's faults on the clock, run every controller, check."""

import json
import time
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from itertools import zip_longest
from typing import Annotated
lazy from collections.abc import Callable, Mapping
lazy from pathlib import Path

from pydantic import Field, TypeAdapter

from sharectl.controller import Controller
from sharectl.log import CONFIRMED, Record, line
from sharectl.model import Inventory, Layer, Outcome, Role
from sharectl.world import World
lazy from sharectl.stubs import Coverage


class Kind(StrEnum):
    """What a scenario event does to the world."""

    AGREEMENT_FINAL = "agreement-final"
    PATHS_DOWN = "paths-down"
    PATHS_UP = "paths-up"
    RECLAIM_NOTICE = "reclaim-notice"
    RESOURCE_SILENT = "resource-silent"
    RESEND_DESIRED = "resend-desired"


@dataclass(frozen=True, slots=True, kw_only=True)
class Phase:
    """A heading on the screen from `at`."""

    at: datetime
    title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:  # pylint: disable=too-many-instance-attributes
    """A fault or an outside event at `at`; each kind reads only its own fields."""

    at: datetime
    kind: Kind
    agreement: str = ""
    version: int = 0
    handoffs: tuple[str, ...] = ()
    site: str = ""
    to: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    layer: Layer = Layer.LINK
    resources: tuple[str, ...] = ()
    notice_minutes: int = 0
    resource: str = ""
    handoff: str = ""
    generation: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Expect:
    """A decision the scenario expects, with a fragment of its reason."""

    at: datetime
    controller: str
    rule: str
    action: str
    outcome: Outcome
    reason_contains: str

    def head(self) -> str:
        """The decision, without its reason."""
        return f"{self.at:%H:%M:%S} {self.controller} {self.rule} {self.action} {self.outcome}"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectPush:
    """An action the scenario expects of configuration push."""

    at: datetime
    handoff: str
    generation: int
    outcome: Outcome

    def head(self) -> str:
        """The action as the player compares it."""
        return f"{self.at:%H:%M:%S} config-push {self.handoff} gen {self.generation} {self.outcome}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Scenario:  # pylint: disable=too-many-instance-attributes
    """A scenario file: faults on a timeline and what the controllers should decide."""

    name: Annotated[str, Field(pattern=r"^[a-z0-9-]+$")]
    start: datetime
    end: datetime
    phases: tuple[Phase, ...] = ()
    events: tuple[Event, ...] = Field(default=(), alias="event")
    expect: tuple[Expect, ...] = ()
    expect_config_push: tuple[ExpectPush, ...] = ()
    expect_coverage: tuple[Coverage, ...] = ()
    expect_summary: Mapping[str, int] = Field(default_factory=dict[str, int])


def load_scenario(path: Path) -> Scenario:
    """Read and validate a scenario file."""
    return TypeAdapter(Scenario).validate_python(tomllib.loads(path.read_text()))


@dataclass(frozen=True, slots=True, kw_only=True)
class Run:
    """How a play runs.

    `pace` is the wall-clock seconds each tick takes, 0 for at once; `sites` names the PoPs whose
    controllers run, every PoP when empty.
    """

    pace: float = 0.0
    sites: frozenset[str] = frozenset()


AT_ONCE = Run()
"""Every PoP's controller, with no pause between ticks."""


def play(
    scenario: Scenario,
    inventory: Inventory,
    out: Path,
    *,
    show: Callable[[str], None],
    run: Run = AT_ONCE,
) -> list[str]:
    """Play `scenario`, showing each line as it happens; write the files; return the failures.

    Every line also goes to `out/<scenario>/screen.txt` as it is shown.
    """
    directory = out / scenario.name
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "screen.txt").open("w") as screen:

        def emit(text: str) -> None:
            screen.write(f"{text}\n")
            screen.flush()
            show(text)

        player = Player(scenario, inventory, emit, sites=run.sites)
        player.run(run.pace)
        failures = player.report()
        player.write(directory)
        emit(f"{scenario.name}: {'FAIL' if failures else 'pass'}")
    return failures


class Player:
    """Plays one scenario on a fresh world: the clock, the faults, every controller, the lines."""

    def __init__(
        self,
        scenario: Scenario,
        inventory: Inventory,
        emit: Callable[[str], None],
        *,
        sites: frozenset[str] = frozenset(),
    ) -> None:
        """Boot the world at the scenario's start, with a controller for each PoP in `sites`.

        Controllers run in site-id order, one for every PoP when `sites` is empty.
        """
        super().__init__()
        self.scenario, self.emit = scenario, emit
        self.world = World(inventory, scenario.start)
        pops = sorted(
            (s for s in inventory.sites if s.role is Role.POP and (not sites or s.id in sites)),
            key=lambda s: s.id,
        )
        self.controllers = [Controller(self.world, site, scenario.start) for site in pops]
        self.decided: list[Record] = []
        self._tick = timedelta(seconds=inventory.params.tick_seconds)
        self._shown = 0, [0] * len(self.controllers)  # pragma: no mutate, nothing to skip yet

    def run(self, pace: float) -> None:
        """Show the controllers running and the boot placement, then run the clock to the end."""
        self.emit("controllers: " + ", ".join(c.name for c in self.controllers))
        for site, kube in self.world.kubes.items():
            on = sorted(str(p.resource) for p in kube.pods)
            if on:
                self.emit(
                    f"boot {site}: " + ", ".join(f"{r} {on.count(r)}" for r in dict.fromkeys(on))
                )
        now = self.scenario.start
        while now <= self.scenario.end:
            self.step(now)
            time.sleep(pace)
            now += self._tick

    def step(self, now: datetime) -> None:
        """One tick: the due faults land as the world advances, then each controller steps."""
        due = [e for e in self.scenario.events if now - self._tick < e.at <= now]
        self.world.advance(now, faults=[partial(self._inject, e) for e in due])
        for phase in (p for p in self.scenario.phases if now - self._tick < p.at <= now):
            self.emit(f"{phase.at:%H:%M:%S} [phase] {phase.title}")
        for controller in self.controllers:
            controller.step()
        notes, logged = self._shown
        for at, text in self.world.notes[notes:]:
            self.emit(f"{at:%H:%M:%S} {text}")
        for index, controller in enumerate(self.controllers):
            for record in controller.log[logged[index] :]:
                if record["data"]["phase"] == "outcome":
                    self.decided.append(record)
                    self.emit(line(record))
        self._shown = len(self.world.notes), [len(c.log) for c in self.controllers]

    def _inject(self, event: Event) -> None:
        world = self.world
        match event.kind:
            case Kind.AGREEMENT_FINAL:
                world.cloud.write(
                    event.agreement, event.version, list(event.handoffs), at=world.now
                )
            case Kind.PATHS_DOWN | Kind.PATHS_UP:
                down = event.kind is Kind.PATHS_DOWN
                world.cut(event.site, event.to, event.paths, down=down, layer=event.layer)
            case Kind.RECLAIM_NOTICE:
                for resource in event.resources:
                    world.notices[resource] = event.at + timedelta(minutes=event.notice_minutes)
            case Kind.RESOURCE_SILENT:
                world.silent[event.resource] = event.at
            case Kind.RESEND_DESIRED:
                world.cloud.resend(event.handoff, event.generation, world.now)

    def summary(self) -> dict[str, int]:
        """The counts the scenario's `expect_summary` can name."""
        records = self.world.cloud.records.values()
        pods = [p for kube in self.world.kubes.values() for p in kube.pods]
        return {
            "generations_applied": sum(
                e["type"] == CONFIRMED for e in self.world.cloud.events.values()
            ),
            "replays_ignored": sum(
                a["outcome"] == Outcome.IGNORED for a in self.world.config_push.actions
            ),
            "decisions_merged": len({r["data"]["decision_id"] for r in records}),
            "pods_recreated": len(self.world.kubernetes),
            "pods_pending": sum(p.resource is None for p in pods),
            "pods_left_on_released": sum(p.resource in self.world.released for p in pods),
        }

    def report(self) -> list[str]:
        """Show the coverage and the summary, then every difference from the file; return them."""
        summary = self.summary()
        for row in self.world.cloud.coverage:
            self.emit(
                f"cloud coverage {row.handoff} {row.counter}: {row.held} held, "
                f"{row.resent} resent, {row.expected} expected, {row.unique} unique, "
                f"{row.duplicates} duplicates dropped, {row.missing} missing"
            )
        self.emit("summary: " + ", ".join(f"{k} {v}" for k, v in summary.items()))
        failures = [*self._decisions(), *self._pushes(), *self._coverage()] + [
            f"summary {key}: {summary.get(key)}, expected {wanted}"
            for key, wanted in self.scenario.expect_summary.items()
            if summary.get(key) != wanted
        ]
        for failure in failures:
            self.emit(failure)
        return failures

    def _decisions(self) -> list[str]:
        fields = ("controller", "rule", "action", "outcome")
        actual = [
            (
                " ".join([r["data"]["at"][11:19], *(str(r["data"][k]) for k in fields)]),
                r["data"]["reason"],
            )
            for r in self.decided
        ]
        return _compare([(e.head(), e.reason_contains) for e in self.scenario.expect], actual)

    def _pushes(self) -> list[str]:
        """Configuration push's actions against the file's, by head alone: a push has no reason."""
        done = (
            ExpectPush(
                at=datetime.fromisoformat(a["time"]),
                handoff=a["handoff"],
                generation=a["generation"],
                outcome=Outcome(a["outcome"]),
            )
            for a in self.world.config_push.actions
        )
        actual = [(push.head(), push.head()) for push in done]
        return _compare([(e.head(), "") for e in self.scenario.expect_config_push], actual)

    def _coverage(self) -> list[str]:
        actual = {(c.handoff, c.counter): asdict(c) for c in self.world.cloud.coverage}
        failures: list[str] = []
        for expect in self.scenario.expect_coverage:
            row, name = (
                actual.get((expect.handoff, expect.counter), {}),
                f"{expect.handoff} {expect.counter}",
            )
            failures += [
                f"coverage {name}: {key} {row.get(key)}, expected {value}"
                for key, value in asdict(expect).items()
                if row.get(key) != value
            ]
        return failures

    def write(self, directory: Path) -> None:
        """Write each site's log, the merged store, events, Kubernetes' actions and coverage."""
        merged = sorted(
            self.world.cloud.records.values(),
            key=lambda r: (r["data"]["at"], r["data"]["decision_id"], r["data"]["phase"]),
        )
        files = {f"site-{c.site.code}.jsonl": c.log for c in self.controllers} | {
            "decisions.jsonl": merged,
            "events.jsonl": list(self.world.cloud.events.values()),
            "kubernetes.jsonl": self.world.kubernetes,
        }
        for name, records in files.items():
            (directory / name).write_text("".join(f"{json.dumps(r)}\n" for r in records))
        rows = [asdict(row) for row in self.world.cloud.coverage]
        text = json.dumps(rows, indent=2)  # pragma: no mutate, layout only
        (directory / "coverage.json").write_text(f"{text}\n")


def _compare(expected: list[tuple[str, str]], actual: list[tuple[str, str]]) -> list[str]:
    """Compare (head, reason) pairs in order; an expected reason is a fragment, "" for none."""
    failures: list[str] = []
    for want, got in zip_longest(expected, actual):
        if want is None:
            failures.append(f"unexpected: {got[0]}")
        elif got is None:
            failures.append(f"missing: {want[0]}")
        elif got[0] != want[0] or want[1] not in got[1]:
            wanted = f"{want[0]}, reason with '{want[1]}'" if want[1] else want[0]
            failures.append(f"expected: {wanted}; got: {got[1] if got[0] == want[0] else got[0]}")
    return failures
