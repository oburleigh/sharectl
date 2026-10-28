"""The generic scenario player: inject a file's faults on the clock, run every controller, check."""

import json
import time
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import zip_longest
from typing import Annotated
lazy from collections.abc import Callable, Mapping
lazy from pathlib import Path

from pydantic import Field, TypeAdapter

from sharectl.controller import Controller
from sharectl.log import CONFIRMED, line
from sharectl.model import Inventory, Outcome, Role
from sharectl.world import World
lazy from sharectl.stubs import Coverage, Record


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
class Scenario:  # pylint: disable=too-many-instance-attributes
    """A scenario file: faults on a timeline and what the controllers should decide."""

    name: Annotated[str, Field(pattern=r"^[a-z0-9-]+$")]
    start: datetime
    end: datetime
    phases: tuple[Phase, ...] = ()
    events: tuple[Event, ...] = Field(default=(), alias="event")
    expect: tuple[Expect, ...] = ()
    expect_coverage: tuple[Coverage, ...] = ()
    expect_summary: Mapping[str, int] = Field(default_factory=dict[str, int])


def load_scenario(path: Path) -> Scenario:
    """Read and validate a scenario file."""
    return TypeAdapter(Scenario).validate_python(tomllib.loads(path.read_text()))


def play(
    scenario: Scenario,
    inventory: Inventory,
    out: Path,
    *,
    show: Callable[[str], None],
    pace: float = 0.0,
) -> list[str]:
    """Play `scenario`, showing each line as it happens; write the files; return the failures.

    `pace` is the wall-clock seconds each tick takes; 0 plays at once. Every line also goes to
    `out/<scenario>/screen.txt` as it is shown.
    """
    directory = out / scenario.name
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "screen.txt").open("w") as screen:

        def emit(text: str) -> None:
            screen.write(f"{text}\n")
            screen.flush()
            show(text)

        player = Player(scenario, inventory, emit)
        player.run(pace)
        failures = player.report()
        player.write(directory)
        emit(f"{scenario.name}: {'FAIL' if failures else 'pass'}")
    return failures


class Player:
    """Plays one scenario on a fresh world: the clock, the faults, every controller, the lines."""

    def __init__(
        self, scenario: Scenario, inventory: Inventory, emit: Callable[[str], None]
    ) -> None:
        """Boot the world at the scenario's start, with one controller per PoP in site-id order."""
        super().__init__()
        self.scenario, self.emit = scenario, emit
        self.world = World(inventory, scenario.start)
        pops = sorted((s for s in inventory.sites if s.role is Role.POP), key=lambda s: s.id)
        self.controllers = [Controller(self.world, site, scenario.start) for site in pops]
        self.decided: list[Record] = []
        self._tick = timedelta(seconds=inventory.params.tick_seconds)
        self._shown = 0, [0] * len(self.controllers)  # pragma: no mutate, nothing to skip yet

    def run(self, pace: float) -> None:
        """Show the boot placement, then run the clock from start to end."""
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
        """One tick: the world advances, the due faults land, each controller steps."""
        self.world.advance(now)
        for phase in (p for p in self.scenario.phases if now - self._tick < p.at <= now):
            self.emit(f"{phase.at:%H:%M:%S} [phase] {phase.title}")
        for event in (e for e in self.scenario.events if now - self._tick < e.at <= now):
            self._inject(event)
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
                handoffs = list(event.handoffs)
                for desired in world.cloud.write(event.agreement, event.version, handoffs):
                    world.send(world.cloud.sites[desired.handoff], desired)
            case Kind.PATHS_DOWN | Kind.PATHS_UP:
                down = event.kind is Kind.PATHS_DOWN
                world.cut(event.site, list(event.to), list(event.paths), down=down)
            case Kind.RECLAIM_NOTICE:
                for resource in event.resources:
                    world.notices[resource] = event.at + timedelta(minutes=event.notice_minutes)
            case Kind.RESOURCE_SILENT:
                world.silent[event.resource] = event.at
            case Kind.RESEND_DESIRED:
                desired = world.cloud.resend(event.handoff, event.generation)
                world.send(world.cloud.sites[event.handoff], desired)

    def summary(self) -> dict[str, int]:
        """The counts the scenario's `expect_summary` can name."""
        records = self.world.cloud.records.values()
        pods = [p for kube in self.world.kubes.values() for p in kube.pods]
        return {
            "generations_applied": sum(e["type"] == CONFIRMED for e in self.world.cloud.events),
            "replays_ignored": sum(r["data"].get("outcome") == Outcome.IGNORED for r in records),
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
                f"coverage {row.handoff} {row.counter}: {row.held} held, {row.resent} resent, "
                f"{row.expected} expected, {row.unique} unique, {row.duplicates} duplicates "
                f"dropped, {row.missing} missing"
            )
        self.emit("summary: " + ", ".join(f"{k} {v}" for k, v in summary.items()))
        failures = [*self._decisions(), *self._coverage()] + [
            f"summary {key}: {summary.get(key)}, expected {wanted}"
            for key, wanted in self.scenario.expect_summary.items()
            if summary.get(key) != wanted
        ]
        for failure in failures:
            self.emit(failure)
        return failures

    def _decisions(self) -> list[str]:
        failures: list[str] = []
        for expect, record in zip_longest(self.scenario.expect, self.decided):
            data: Record = record["data"] if record else {}
            head = " ".join(
                [
                    str(data.get("at"))[11:19],
                    *(str(data.get(k)) for k in ("controller", "rule", "action", "outcome")),
                ]
            )
            if expect is None:
                failures.append(f"unexpected: {head}")
            elif record is None:
                failures.append(f"missing: {expect.head()}")
            elif head != expect.head() or expect.reason_contains not in data["reason"]:
                got = data["reason"] if head == expect.head() else head
                failures.append(
                    f"expected: {expect.head()}, reason with '{expect.reason_contains}'; got: {got}"
                )
        return failures

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
            "events.jsonl": self.world.cloud.events,
            "kubernetes.jsonl": self.world.kubernetes,
        }
        for name, records in files.items():
            (directory / name).write_text("".join(f"{json.dumps(r)}\n" for r in records))
        rows = [asdict(row) for row in self.world.cloud.coverage]
        text = json.dumps(rows, indent=2)  # pragma: no mutate, layout only
        (directory / "coverage.json").write_text(f"{text}\n")
