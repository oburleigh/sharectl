"""The simulated world: sites and the paths between them, the link, notices and the fakes."""

from collections import defaultdict
from datetime import datetime, timedelta

from sharectl.kube import Kube
from sharectl.model import DesiredState, Interval, Inventory, Role
from sharectl.stubs import Batch, FakeAaa, FakeCloud, FakeSdn, FakeTelemetry, Record, floor

type Message = tuple[datetime, str, DesiredState | Batch]


class World:  # pylint: disable=too-many-instance-attributes
    """The fabric every controller observes and acts on.

    A real deployment swaps in probes, provider APIs and the network behind the same calls.
    """

    def __init__(self, inventory: Inventory, start: datetime) -> None:
        """Boot the estate at `start`: every resource active, every pod placed."""
        super().__init__()
        self.inventory = inventory
        self.now = start
        self._every = timedelta(minutes=inventory.telemetry.poll_minutes)
        boot = floor(start, self._every) - self._every
        node_site = {n.id: n.site for n in inventory.nodes}
        self.kubes = {s.id: Kube(inventory, s) for s in inventory.sites}
        self.sdn = FakeSdn({h.id: boot for h in inventory.handoffs if h.generation})
        self.telemetry = FakeTelemetry(self.sdn, inventory.telemetry.poll_minutes)
        self.aaa = FakeAaa()
        self.cloud = FakeCloud(
            generations={h.id: h.generation for h in inventory.handoffs},
            every=self._every,
            sites={h.id: node_site[h.node] for h in inventory.handoffs},
            configured=dict(self.sdn.configured),
        )
        self.notices: dict[str, datetime] = {}
        self.silent: dict[str, datetime] = {}
        self.released: set[str] = set()
        self.sealed: set[str] = set()
        self.notes: list[tuple[datetime, str]] = []
        self.kubernetes: list[Record] = []
        self._down: set[tuple[str, str, str]] = set()
        self._link: list[Message] = []
        self._inbox: dict[str, list[DesiredState]] = defaultdict(list)
        self._polled: dict[str, list[Interval]] = defaultdict(list)
        self._aaa: dict[str, str] = {}
        self._last = start - timedelta(seconds=inventory.params.tick_seconds)
        self._handoffs: dict[str, list[str]] = defaultdict(list)
        for handoff in inventory.handoffs:
            self._handoffs[node_site[handoff.node]].append(handoff.id)

    def cut(self, site: str, to: list[str], paths: list[str], *, down: bool) -> None:
        """Take `paths` between `site` and the sites in `to` (`*`: every other) down or up."""
        others = [s.id for s in self.inventory.sites if s.id != site] if "*" in to else to
        keys = {_path(site, other, path) for other in others for path in paths}
        self._down = self._down | keys if down else self._down - keys

    def regions_reachable(self, site: str) -> tuple[int, bool]:
        """How many regions `site` reaches on any path, and whether one is on a data path."""
        network = self.inventory.network
        up = [
            (s.id, path)
            for s in self.inventory.sites
            for path in network.paths
            if s.role is Role.REGION and _path(site, s.id, path) not in self._down
        ]
        return len({region for region, _ in up}), any(
            path not in network.management_only for _, path in up
        )

    def send(self, site: str, payload: DesiredState | Batch) -> None:
        """Put a message between `site` and the cloud on the link."""
        self._link.append((self.now, site, payload))

    def take(self, site: str) -> tuple[DesiredState, ...]:
        """The desired state delivered to `site` since it last took it."""
        return tuple(self._inbox.pop(site, []))

    def polled(self, site: str) -> tuple[Interval, ...]:
        """The intervals `site`'s collectors closed since it last took them."""
        return tuple(self._polled.pop(site, []))

    def advance(self, now: datetime) -> None:
        """Move to `now`: deliver the link, let Kubernetes reconcile, poll, probe AAA."""
        self.now, last, self._last = now, self._last, now
        self._deliver()
        for site, kube in self.kubes.items():
            for pod in kube.reconcile():
                self.note(f"kubernetes recreated {pod.service} on {pod.resource}")
                self.kubernetes.append(
                    {
                        "time": f"{now:%Y-%m-%dT%H:%M:%SZ}",
                        "site": site,
                        "action": "recreate",
                        "service": pod.service,
                        "resource": pod.resource,
                    }
                )
        for site in self.inventory.sites:
            if site.role is Role.POP:
                self._poll(site.id, last)
                self._probe(site.id)

    def _deliver(self) -> None:
        waiting: list[Message] = []
        for message in self._link:
            sent, site, payload = message
            if sent >= self.now or not self.regions_reachable(site)[1]:
                waiting.append(message)
            elif isinstance(payload, Batch):
                self.cloud.receive(payload, self.now)
            else:
                self._inbox[site].append(payload)
        self._link = waiting

    def _poll(self, site: str, last: datetime) -> None:
        """At each poll boundary, measure the interval that has just closed on every handoff."""
        boundary = floor(self.now, self._every)
        if boundary <= last or not self._running(site, "collects"):
            return
        for handoff in self._handoffs[site]:
            for counter in self.inventory.telemetry.counters:
                interval = self.telemetry.poll(handoff, counter, boundary - self._every)
                if interval:
                    self._polled[site].append(interval)

    def _probe(self, site: str) -> None:
        """Note where the site's AAA answers from, when that changes."""
        if self._running(site, "credential-cache"):
            answer = self.aaa.answer(site, cloud=self.regions_reachable(site)[1])
            if answer != self._aaa.get(site, self.aaa.answer(site, cloud=True)):
                self.note(answer)
            self._aaa[site] = answer

    def seal(self, resource: str) -> None:
        """Revoke `resource`'s identity and wipe its disks."""
        self.sealed.add(resource)

    def note(self, text: str) -> None:
        """Add a line for the screen."""
        self.notes.append((self.now, text))

    def _running(self, site: str, role: str) -> bool:
        services = {s.id for s in self.inventory.services if role in s.roles}
        return any(p.service in services and p.resource for p in self.kubes[site].pods)


def _path(site: str, other: str, path: str) -> tuple[str, str, str]:
    """A path between two sites, whichever end names it."""
    first, second = sorted((site, other))
    return first, second, path
