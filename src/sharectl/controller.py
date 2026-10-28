"""A PoP's controller: observe the world, decide, act on its own site, remember, ship its log."""

from collections import defaultdict
lazy from datetime import datetime

from sharectl.decide import decide
from sharectl.log import CONFIRMED, DECISION, envelope, stamp
from sharectl.model import Decision, Interval, Memory, Outcome, Role, Site, State
from sharectl.stubs import Batch, Record
lazy from sharectl.world import World


class Controller:  # pylint: disable=too-many-instance-attributes
    """One instance per PoP. It acts only on its own site and never places a pod."""

    def __init__(self, world: World, site: Site, started: datetime) -> None:
        """Start for `site` at `started`, which every decision id carries."""
        super().__init__()
        self.world, self.site, self.started = world, site, started
        nodes = {n.id for n in world.inventory.nodes if n.site == site.id}
        own = [h for h in world.inventory.handoffs if h.node in nodes]
        self.memory = Memory(applied={h.id: h.generation for h in own})
        self.log: list[Record] = []
        self._source = f"/controllers/site/{site.id}"
        self._decisions = 0
        self._outbox: list[Record] = []
        self._intervals: list[Interval] = []
        self._shipped: dict[tuple[str, str], list[Interval]] = defaultdict(list)

    def observe(self) -> State:
        """Take a frozen snapshot of the site, what arrived, and the memory."""
        world, site = self.world, self.site.id
        self._intervals += world.polled(site)
        reachable, data_up = world.regions_reachable(site)
        resources = tuple(
            r for r in world.inventory.resources if r.site == site and r.id not in world.released
        )
        ids = {r.id for r in resources}
        return State(
            inventory=world.inventory,
            now=world.now,
            site=self.site,
            resources=resources,
            pods=tuple(world.kubes[site].pods),
            regions=sum(s.role is Role.REGION for s in world.inventory.sites),
            reachable=reachable,
            data_up=data_up,
            notices={r: t for r, t in world.notices.items() if r in ids},
            silent={r: t for r, t in world.silent.items() if r in ids},
            inbound=world.take(site),
            held_records=len(self._outbox),
            held_intervals=len(self._intervals),
            memory=self.memory,
        )

    def step(self) -> None:
        """One tick: observe, decide, log and act on each decision, remember, ship."""
        state = self.observe()
        decisions, memory = decide(state)
        for decision in decisions:
            key = self._record(decision, state, phase="proposed")
            if decision.outcome is Outcome.APPLIED:
                self.act(decision)
            self._record(decision, state, phase="outcome", key=key)
        rejoined = self.memory.isolated and not memory.isolated
        self.memory = memory
        if not memory.isolated:
            self._ship(rejoined=rejoined)

    def act(self, decision: Decision) -> None:
        """Carry out an allowed decision on this site."""
        world, kube = self.world, self.world.kubes[self.site.id]
        match decision.rule.id, decision.resource, decision.desired:
            case "desired-state", _, desired if desired:
                world.sdn.apply(desired.handoff, desired.generation, world.now)
                data = {
                    "handoff": desired.handoff,
                    "generation": desired.generation,
                    "agreement": desired.agreement,
                    "version": desired.version,
                }
                self._outbox.append(
                    envelope(
                        CONFIRMED,
                        source=self._source,
                        key=f"{desired.handoff}/{desired.generation}",
                        at=world.now,
                        data=data,
                    )
                )
            case "drain-on-reclaim", resource, _ if resource:
                kube.cordon(resource)
                kube.evict(decision.evict)
            case "fence", resource, _ if resource:
                kube.delete(resource)
                world.seal(resource)
            case "release", resource, _ if resource:
                world.released.add(resource)
            case _:  # pragma: no mutate, a match with no arm already does nothing
                pass

    def _record(self, decision: Decision, state: State, *, phase: str, key: str = "") -> str:
        if not key:
            self._decisions += 1
            key = f"{self.site.code}-{self.started:%Y%m%dT%H%M%SZ}-{self._decisions:04d}"
        rule = decision.rule
        data: Record = {
            "decision_id": key,
            "phase": phase,
            "at": stamp(state.now),
            "controller": f"site/{self.site.id}",
            "trigger": rule.trigger,
            "observed": {**decision.observed, "isolated": state.memory.isolated},
            "rule": f"{rule.id}@v{rule.version}",
            "action": rule.action,
            "subject": decision.subject,
            "reason": decision.reason,
            "blast_radius": {"pods_moved": len(decision.evict)},
            "rollback": rule.rollback,
            "page": decision.page,
        } | ({"outcome": str(decision.outcome)} if phase == "outcome" else {})
        record = envelope(
            DECISION, source=self._source, key=f"{key}/{phase}", at=state.now, data=data
        )
        record["sequence"] = str(len(self.log) + 1)
        self.log.append(record)
        self._outbox.append(record)
        return key

    def _ship(self, *, rejoined: bool) -> None:
        """Send what the site holds; on rejoin, resend the last intervals the cloud already had."""
        overlap = self.world.inventory.params.resend_overlap
        resent = tuple(i for s in self._shipped.values() for i in s[-overlap:]) if rejoined else ()
        if not (self._outbox or self._intervals):
            return
        records = tuple(r for r in self._outbox if r["type"] == DECISION)
        events = tuple(r for r in self._outbox if r["type"] != DECISION)
        self.world.send(
            self.site.id,
            Batch(
                site=self.site.id,
                records=records,
                events=events,
                intervals=tuple(self._intervals),
                resent=resent,
                rejoin=rejoined,
            ),
        )
        for interval in self._intervals:
            self._shipped[interval.handoff, interval.counter].append(interval)
        self._outbox, self._intervals = [], []
