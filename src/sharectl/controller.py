"""A PoP's controller: observe the world, decide, act on its own site, remember, log."""

from datetime import datetime, timedelta

from sharectl.decide import decide
from sharectl.log import DECISION, Record, envelope, stamp
from sharectl.model import Decision, Memory, Outcome, Site, State
lazy from sharectl.world import World


class Controller:  # pylint: disable=too-many-instance-attributes
    """One instance per PoP. It acts only on its own site and never places a pod."""

    def __init__(self, world: World, site: Site, started: datetime) -> None:
        """Start for `site` at `started`, which every decision id carries."""
        super().__init__()
        self.world, self.site, self.started = world, site, started
        self.memory = Memory()
        self.log: list[Record] = []
        self.name = f"site/{site.id}"
        self._source = f"/controllers/{self.name}"
        self._decisions = 0
        self._every = timedelta(minutes=world.inventory.params.poll_report_minutes)
        self._polled: dict[str, tuple[str, datetime]] = {}

    def observe(self) -> State:
        """Take a frozen snapshot of the site, what arrived, and the memory."""
        world, site = self.world, self.site.id
        reachable, _ = world.regions_reachable(site)
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
            regions=len(world.inventory.regions),
            reachable=reachable,
            stable_since=world.stable_since(site),
            data_links_up=world.data_links_up(site),
            notices={r: t for r, t in world.notices.items() if r in ids},
            silent={r: t for r, t in world.silent.items() if r in ids},
            memory=self.memory,
        )

    def step(self) -> None:
        """One tick: observe, decide, log and act on each decision, remember."""
        state = self.observe()
        self._report(state)
        decisions, memory = decide(state)
        for decision in decisions:
            key = self._record(decision, state, phase="proposed")
            if decision.outcome is Outcome.APPLIED:
                self.act(decision)
            self._record(decision, state, phase="outcome", key=key)
        self.memory = memory

    def _report(self, state: State) -> None:
        """Show each node's poll: the first, any that differs, and one every few minutes."""
        world = self.world
        paths = world.path_states(self.site.id)
        for node in (n for n in world.inventory.nodes if n.site == self.site.id):
            links = "; ".join(
                world.text(
                    "poll_link_up" if up else "poll_link_down",
                    path=path,
                    peer=node.peers[path],
                    sessions=sessions,
                    regions=state.regions,
                )
                for path, up, sessions in paths
            )
            text = world.text(
                "poll",
                controller=self.name,
                node=node.id,
                paths=links,
                reachable=state.reachable,
                regions=state.regions,
            )
            last = self._polled.get(node.id)
            if last is None or text != last[0] or state.now - last[1] >= self._every:
                world.note(text)
                self._polled[node.id] = (text, state.now)

    def act(self, decision: Decision) -> None:
        """Carry out an allowed decision on this site."""
        world, kube = self.world, self.world.kubes[self.site.id]
        match decision.rule.id, decision.resource:
            case "drain-on-reclaim", resource if resource:
                kube.cordon(resource)
                kube.evict(decision.evict)
            case "fence", resource if resource:
                kube.delete(resource)
                world.seal(resource)
            case "release", resource if resource:
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
            "controller": self.name,
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
        self.world.site_logs[self.site.id].write(record)
        return key
