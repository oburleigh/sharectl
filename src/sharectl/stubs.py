"""The systems outside the loop, each behind a protocol, with the in-memory fake version 1 uses."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Any, Protocol

from sharectl.model import DesiredState, Interval

type Record = dict[str, Any]

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
    """Applies a handoff's desired state.

    Real: configuration push over NETCONF, which takes the lock on the node's candidate
    datastore and commits by confirmed commit (RFC 6241 §7.5, §8.3).
    """

    def apply(self, handoff: str, generation: int, at: datetime) -> None:
        """Apply `generation` to `handoff`."""
        ...


class Telemetry(Protocol):
    """Reads a counter over one interval. Real: SNMP or gNMI polling by the collectors."""

    def poll(self, handoff: str, counter: str, start: datetime) -> Interval | None:
        """The counter over the interval from `start`, or None if not measured then."""
        ...


class Aaa(Protocol):
    """Answers the credential-cache probe. Real: the RADIUS servers at the PoP."""

    def answer(self, site: str, *, cloud: bool) -> str:
        """Say where `site`'s AAA answers from."""
        ...


class Cloud(Protocol):
    """Desired state down, records up.

    Real: the cells' circuit workflow and desired-state stream, ingestion into the record,
    and the reconciler.
    """

    def write(self, agreement: str, version: int, handoffs: list[str]) -> list[DesiredState]:
        """Write the next generation for each handoff of a final agreement."""
        ...

    def resend(self, handoff: str, generation: int) -> DesiredState:
        """Resend desired state already sent."""
        ...

    def receive(self, batch: Batch, at: datetime) -> None:
        """Store a site's batch by key; on rejoin, run the coverage check."""
        ...


@dataclass
class FakeSdn:
    """Records the generation applied to each handoff and when it was configured."""

    configured: dict[str, datetime] = field(default_factory=dict[str, datetime])
    applied: dict[str, int] = field(default_factory=dict[str, int])

    def apply(self, handoff: str, generation: int, at: datetime) -> None:
        """Apply `generation` to `handoff`; it carries configuration from its first apply."""
        self.applied[handoff] = generation
        self.configured.setdefault(handoff, at)


@dataclass
class FakeTelemetry:
    """A counter rising at a fixed rate on every handoff the SDN has configured."""

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
    """Answers from the cloud, or from the site's local credential copy when it is cut off."""

    def answer(self, site: str, *, cloud: bool) -> str:
        """Say where `site`'s AAA answers from."""
        return f"aaa at {site} answers from {'the cloud' if cloud else 'the local credential copy'}"


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
    records: dict[str, Record] = field(default_factory=dict[str, Record])
    events: list[Record] = field(default_factory=list[Record])
    intervals: dict[tuple[str, str, datetime], Interval] = field(
        default_factory=dict[tuple[str, str, datetime], Interval]
    )

    def write(self, agreement: str, version: int, handoffs: list[str]) -> list[DesiredState]:
        """Write the next generation for each handoff, whatever agreement it carried before."""
        written: list[DesiredState] = []
        for handoff in handoffs:
            generation = self.generations[handoff] + 1
            self.generations[handoff] = generation
            desired = DesiredState(
                handoff=handoff, generation=generation, agreement=agreement, version=version
            )
            self.sent[handoff, generation] = desired
            written.append(desired)
        return written

    def resend(self, handoff: str, generation: int) -> DesiredState:
        """The desired state already sent for `handoff` at `generation`, as a stale cell would."""
        return self.sent[handoff, generation]

    coverage: list[Coverage] = field(default_factory=list[Coverage])

    def receive(self, batch: Batch, at: datetime) -> None:
        """Store a batch by key; when a site rejoins, check the coverage of its handoffs."""
        self.records |= {str(r["id"]): r for r in batch.records}
        self.events += batch.events
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
