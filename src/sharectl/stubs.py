"""The systems outside the loop, each behind a protocol, with the in-memory fake version 1 uses."""

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Protocol

from sharectl.log import CONFIRMED, DECISION, Record, envelope, stamp
from sharectl.model import DesiredState, Interval, Outcome

RATE = 62_500_000
"""The fake counter's rate in bytes per second: a 500 Mbps circuit, full."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Batch:
    """What a site ships to the cloud in one message: records, then events, then intervals."""

    site: str
    records: tuple[Record, ...] = ()
    events: tuple[Record, ...] = ()
    intervals: tuple[Interval, ...] = ()
    resent: tuple[Interval, ...] = ()
    rejoin: bool = False


class Sdn(Protocol):
    """The SDN controller, each node's only configuration writer."""

    def apply(self, handoff: str, state: DesiredState, *, at: datetime) -> None:
        """Apply `state` to `handoff`; returning confirms it, and the same state twice is one."""
        ...


class ConfigPush(Protocol):
    """Hands each change on the site's stream to the SDN controller, once, by generation."""

    def pull(self, site: str, *, cloud: Cloud, log: SiteLog, at: datetime) -> None:
        """Take the changes on `site`'s stream after the last recorded; confirm each to `log`."""
        ...


class SiteLog(Protocol):
    """Holds a site's records and intervals and ships them to the cloud."""

    def write(self, record: Record) -> None:
        """Append `record` to the log."""
        ...


class Telemetry(Protocol):
    """Reads a counter over one interval."""

    def poll(self, handoff: str, counter: str, start: datetime) -> Interval | None:
        """The counter over the interval from `start`, or None if not measured then."""
        ...


class Aaa(Protocol):
    """Answers the credential-cache probe."""

    def answer(self) -> str:
        """Say where the site's AAA answers from: always its `local` credential copy."""
        ...


class Cloud(Protocol):
    """Desired state down, records up."""

    def write(self, agreement: str, version: int, handoffs: list[str], *, at: datetime) -> None:
        """Write the next generation for each handoff of a final agreement to its site's stream."""
        ...

    def resend(self, handoff: str, generation: int, at: datetime) -> None:
        """Put desired state already written on its site's stream again."""
        ...

    def changes(self, site: str, after: int, before: datetime) -> list[tuple[int, DesiredState]]:
        """Each change on `site`'s stream after sequence `after`, written before `before`."""
        ...

    def receive(self, batch: Batch, at: datetime) -> None:
        """Store a site's batch by key; on rejoin, run the coverage check."""
        ...


@dataclass
class FakeSdn:
    """Commits a handoff's state when it differs from the one running; returning confirms it."""

    configured: dict[str, datetime] = field(default_factory=dict[str, datetime])
    running: dict[str, DesiredState] = field(default_factory=dict[str, DesiredState])
    commits: list[tuple[datetime, str, int]] = field(
        default_factory=list[tuple[datetime, str, int]]
    )

    def apply(self, handoff: str, state: DesiredState, *, at: datetime) -> None:
        """Apply `state` to `handoff`; the same state twice changes nothing."""
        if self.running.get(handoff) != state:
            self.running[handoff] = state
            self.configured.setdefault(handoff, at)
            self.commits.append((at, handoff, state.generation))


@dataclass
class FakeConfigPush:
    """Hands each change on a site's stream to the SDN controller once, by generation.

    `sequence` holds, per site, the last change recorded: where the stream resumes after a gap.
    A change is recorded only after the SDN controller confirms it and its confirmation is in the
    site log, so one interrupted in between comes again; every change taken, a resend included,
    goes through the generation check.
    """

    sdn: FakeSdn
    applied: dict[str, int] = field(default_factory=dict[str, int])
    sequence: dict[str, int] = field(default_factory=dict[str, int])
    actions: list[Record] = field(default_factory=list[Record])

    def pull(self, site: str, *, cloud: Cloud, log: SiteLog, at: datetime) -> None:
        """Take the changes on `site`'s stream after the last recorded; confirm each to `log`."""
        for sequence, desired in cloud.changes(site, self.sequence.get(site, 0), at):
            confirmed = self.receive(site, desired, sequence=sequence, at=at)
            if confirmed:
                log.write(confirmed)
            self.record(site, sequence, desired if confirmed else None)

    def receive(
        self, site: str, desired: DesiredState, *, sequence: int, at: datetime
    ) -> Record | None:
        """Hand `desired` to the SDN controller if its generation is above the one applied.

        Returns its confirmation, keyed by site and sequence, or None when it is ignored.
        """
        handoff, generation = desired.handoff, desired.generation
        applied = self.applied.get(handoff, 0)
        higher = generation > applied
        self.actions.append(
            {
                "time": stamp(at),
                "sequence": sequence,
                "handoff": handoff,
                "generation": generation,
                "outcome": str(Outcome.APPLIED if higher else Outcome.IGNORED),
                "applied": applied,
            }
        )
        if not higher:
            return None
        self.sdn.apply(handoff, desired, at=at)
        data = {
            "handoff": handoff,
            "generation": generation,
            "agreement": desired.agreement,
            "version": desired.version,
        }
        key = f"{site}/{sequence}"
        return envelope(CONFIRMED, source=f"/config-push/site/{site}", key=key, at=at, data=data)

    def record(self, site: str, sequence: int, applied: DesiredState | None) -> None:
        """Record `sequence` as taken, and the generation `applied` to the SDN controller if any."""
        self.sequence[site] = sequence
        if applied:
            self.applied[applied.handoff] = applied.generation


@dataclass
class FakeSiteLog:
    """Holds what the site writes until it may ship, then ships it in one batch.

    When shipping resumes after a hold, the batch also resends the last `overlap` intervals of
    each counter the cloud already had.
    """

    site: str
    overlap: int
    records: list[Record] = field(default_factory=list[Record])
    intervals: list[Interval] = field(default_factory=list[Interval])
    holding: bool = False
    shipped: dict[tuple[str, str], list[Interval]] = field(
        default_factory=dict[tuple[str, str], list[Interval]]
    )

    def write(self, record: Record) -> None:
        """Append `record` to the log."""
        self.records.append(record)

    def ship(self, *, stable: bool) -> Batch | None:
        """What to send while a data path is `stable`; None while holding or with nothing held."""
        if not stable:
            self.holding = True
            return None
        rejoin, self.holding = self.holding, False
        if not (self.records or self.intervals):
            return None
        resent = (
            tuple(i for s in self.shipped.values() for i in s[-self.overlap :]) if rejoin else ()
        )
        batch = Batch(
            site=self.site,
            records=tuple(r for r in self.records if r["type"] == DECISION),
            events=tuple(r for r in self.records if r["type"] != DECISION),
            intervals=tuple(self.intervals),
            resent=resent,
            rejoin=rejoin,
        )
        for interval in self.intervals:
            self.shipped.setdefault((interval.handoff, interval.counter), []).append(interval)
        self.records, self.intervals = [], []
        return batch


@dataclass
class FakeTelemetry:
    """A counter rising at a fixed rate on every handoff the SDN controller has configured."""

    sdn: FakeSdn
    poll_minutes: int

    def poll(self, handoff: str, counter: str, start: datetime) -> Interval | None:
        """The interval from `start`, if its handoff was configured by then."""
        configured = self.sdn.configured.get(handoff)
        if configured is None or configured > start:
            return None
        seconds = int(timedelta(minutes=self.poll_minutes).total_seconds())
        return Interval(handoff=handoff, counter=counter, start=start, bytes=RATE * seconds)


class FakeAaa:
    """Answers from the site's credential copy; nothing in the cloud is in its request path."""

    def answer(self) -> str:
        """Say where the site's AAA answers from: always its `local` credential copy."""
        return "local"


@dataclass(frozen=True, slots=True, kw_only=True)
class Coverage:  # pylint: disable=too-many-instance-attributes
    """The coverage check for one counter on one handoff, run when a site's log arrives."""

    handoff: str
    counter: str
    held: int
    resent: int
    expected: int
    unique: int
    duplicates: int
    missing: int


@dataclass
class FakeCloud:  # pylint: disable=too-many-instance-attributes
    """Writes desired state from final agreements and stores what the sites ship, by key.

    `configured` holds, per handoff, the first interval the cloud expects to be measured.
    """

    generations: dict[str, int]
    every: timedelta
    sites: dict[str, str]
    configured: dict[str, datetime]
    sent: dict[tuple[str, int], DesiredState] = field(
        default_factory=dict[tuple[str, int], DesiredState]
    )
    streams: dict[str, list[tuple[datetime, DesiredState]]] = field(
        default_factory=dict[str, list[tuple[datetime, DesiredState]]]
    )
    records: dict[str, Record] = field(default_factory=dict[str, Record])
    events: dict[str, Record] = field(default_factory=dict[str, Record])
    intervals: dict[tuple[str, str, datetime], Interval] = field(
        default_factory=dict[tuple[str, str, datetime], Interval]
    )

    def write(self, agreement: str, version: int, handoffs: list[str], *, at: datetime) -> None:
        """Write the next generation for each handoff, whatever agreement it carried before."""
        for handoff in handoffs:
            generation = self.generations[handoff] + 1
            self.generations[handoff] = generation
            desired = DesiredState(
                handoff=handoff, generation=generation, agreement=agreement, version=version
            )
            self.sent[handoff, generation] = desired
            self.streams.setdefault(self.sites[handoff], []).append((at, desired))

    def resend(self, handoff: str, generation: int, at: datetime) -> None:
        """Put desired state already written on its site's stream again, as a stale cell would."""
        self.streams[self.sites[handoff]].append((at, self.sent[handoff, generation]))

    def changes(self, site: str, after: int, before: datetime) -> list[tuple[int, DesiredState]]:
        """Each change on `site`'s stream after sequence `after`, written before `before`.

        A site's changes are numbered from 1 in the order written, with no gaps.
        """
        stream = enumerate(self.streams.get(site, [])[after:], start=after + 1)
        due = itertools.takewhile(lambda c: c[1][0] < before, stream)
        return [(sequence, desired) for sequence, (_, desired) in due]

    coverage: list[Coverage] = field(default_factory=list[Coverage])

    def receive(self, batch: Batch, at: datetime) -> None:
        """Store a batch by key; when a site rejoins, check the coverage of its handoffs."""
        self.records |= {str(r["id"]): r for r in batch.records}
        self.events |= {str(e["id"]): e for e in batch.events}
        for event in batch.events:
            configured = datetime.fromisoformat(event["time"]) - timedelta.resolution
            first = floor(configured, self.every) + self.every
            self.configured.setdefault(event["data"]["handoff"], first)
        stored = set(self.intervals)
        arrived = (*batch.intervals, *batch.resent)
        self.intervals |= {(i.handoff, i.counter, i.start): i for i in arrived}
        if batch.rejoin:
            self.coverage += self._check(batch, stored, floor(at, self.every) - self.every)

    def _check(
        self, batch: Batch, stored: set[tuple[str, str, datetime]], last: datetime
    ) -> list[Coverage]:
        """Check each counter the site shipped, on each of its handoffs, up to `last`."""
        arrived = (*batch.intervals, *batch.resent)
        keys = {(i.handoff, i.counter) for i in arrived if self.sites[i.handoff] == batch.site}
        return [self._row(batch, key, stored=stored, last=last) for key in sorted(keys)]

    def _row(
        self,
        batch: Batch,
        key: tuple[str, str],
        *,
        stored: set[tuple[str, str, datetime]],
        last: datetime,
    ) -> Coverage:
        """Every interval from the handoff's first to `last`, counted once; none interpolated."""
        first = self.configured[key[0]]
        held = [i for i in batch.intervals if (i.handoff, i.counter) == key]
        again = [i for i in batch.resent if (i.handoff, i.counter) == key]
        expected = (last - first) // self.every + 1
        unique = sum((h, c) == key and first <= at <= last for h, c, at in self.intervals)
        return Coverage(
            handoff=key[0],
            counter=key[1],
            held=len(held),
            resent=len(again),
            expected=expected,
            unique=unique,
            duplicates=sum((i.handoff, i.counter, i.start) in stored for i in held + again),
            missing=expected - unique,
        )


def floor(at: datetime, step: timedelta) -> datetime:
    """The last multiple of `step` at or before `at`, counted from midnight."""
    midnight = datetime.combine(at.date(), dt_time(), at.tzinfo)
    return midnight + (at - midnight) // step * step
