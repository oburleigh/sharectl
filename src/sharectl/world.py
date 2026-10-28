"""The simulated world: sites and the paths between them, the link, notices and the fakes."""

from datetime import datetime, timedelta
from string import Template
lazy from collections.abc import Callable, Iterable, Sequence

from sharectl.kube import Kube
from sharectl.model import Inventory, Layer, Role
from sharectl.stubs import (
    Batch,
    FakeAaa,
    FakeCloud,
    FakeConfigPush,
    FakeSdn,
    FakeSiteLog,
    FakeTelemetry,
    floor,
)
lazy from sharectl.log import Record

type Message = tuple[datetime, str, Batch]


class World:  # pylint: disable=too-many-instance-attributes
    """The fabric every controller observes and acts on.

    A real deployment swaps in probes, provider APIs and the network behind the same calls.
    """

    def __init__(self, inventory: Inventory, start: datetime) -> None:
        """Boot the estate at `start`: every resource active, every pod placed."""
        super().__init__()
        self.inventory = inventory
        self.now = start  # pragma: no mutate, advance sets the clock before anything reads it
        self._every = timedelta(minutes=inventory.telemetry.poll_minutes)
        boot = floor(start, self._every) - self._every
        node_site = {n.id: n.site for n in inventory.nodes}
        self.kubes = {s.id: Kube(inventory, s) for s in inventory.sites}
        configured = [h for h in inventory.handoffs if h.generation]
        self.sdn = FakeSdn(configured={h.id: boot for h in configured})
        self.config_push = FakeConfigPush(
            sdn=self.sdn, applied={h.id: h.generation for h in configured}
        )
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
        self._sessions_down: set[tuple[str, str, str]] = set()
        self._links_down: set[tuple[str, str]] = set()
        self._link: list[Message] = []
        self._cloud: dict[str, bool] = {}
        self._last = start - timedelta(seconds=inventory.params.tick_seconds)
        pops = [s.id for s in inventory.sites if s.role is Role.POP]
        self._settle = timedelta(seconds=inventory.params.rejoin_stable_seconds)
        self._stable = dict.fromkeys(pops, start - self._settle)
        self.site_logs = {
            s: FakeSiteLog(site=s, overlap=inventory.params.resend_overlap) for s in pops
        }
        self._handoffs = {s: [h for h, at in self.cloud.sites.items() if at == s] for s in pops}

    def cut(
        self,
        site: str,
        to: Sequence[str],
        paths: Sequence[str],
        *,
        down: bool,
        layer: Layer = Layer.LINK,
    ) -> None:
        """Take `paths` at `site` down or up, by `layer`.

        A link carries every session on it; a session cut touches only the sessions to the sites
        in `to` (`*`: every other).
        """
        if layer is Layer.LINK:
            links = {(site, path) for path in paths}
            self._links_down = self._links_down | links if down else self._links_down - links
            return
        others = [s.id for s in self.inventory.sites if s.id != site] if "*" in to else to
        keys = {_path(site, other, path) for other in others for path in paths}
        self._sessions_down = self._sessions_down | keys if down else self._sessions_down - keys

    def regions_reachable(self, site: str) -> tuple[int, bool]:
        """How many regions `site` reaches on any path, and whether one is on a data path.

        A region is reachable on a path when the path's link is up at both ends and the session
        between them is up.
        """
        network = self.inventory.network
        up = [
            (region, path)
            for region in self.inventory.regions
            for path in network.paths
            if self._reaches(site, region, path)
        ]
        return len({region for region, _ in up}), any(
            path not in network.management_only for _, path in up
        )

    def path_states(self, site: str) -> list[tuple[str, bool, int]]:
        """Each path at `site`, as its node reports it: the link up, and the regions it reaches."""
        return [
            (
                path,
                (site, path) not in self._links_down,
                sum(self._reaches(site, region, path) for region in self.inventory.regions),
            )
            for path in self.inventory.network.paths
        ]

    def _reaches(self, site: str, other: str, path: str) -> bool:
        """Whether `path`'s link is up at both ends and the session between them is up."""
        return (
            not {(site, path), (other, path)} & self._links_down
            and _path(site, other, path) not in self._sessions_down
        )

    def data_links_up(self, site: str) -> int:
        """How many of `site`'s data paths have their link up."""
        network = self.inventory.network
        data = [p for p in network.paths if p not in network.management_only]
        return sum((site, path) not in self._links_down for path in data)

    def send(self, site: str, batch: Batch) -> None:
        """Put a batch from `site` to the cloud on the link."""
        self._link.append((self.now, site, batch))

    def stable_since(self, site: str) -> datetime | None:
        """The tick a data path from `site` was first seen up, or None while none is up.

        A path up at boot counts as stable.
        """
        return self._stable.get(site)

    def advance(self, now: datetime, faults: Iterable[Callable[[], None]] = ()) -> None:
        """Move to `now` and let the faults land, then everything else that runs on its own.

        After the faults: the link, Kubernetes, and each PoP's collectors, AAA, configuration push
        and site log.
        """
        self.now, last, self._last = now, self._last, now
        for fault in faults:
            fault()
        self._deliver()
        for site, kube in self.kubes.items():
            for pod in kube.reconcile():
                self._line(
                    "kubernetes_recreated", site=site, service=pod.service, resource=pod.resource
                )
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
                if self.regions_reachable(site.id)[1]:
                    self._stable.setdefault(site.id, now)
                else:
                    self._stable.pop(site.id, None)
                self._poll(site.id, last)
                self._probe(site.id)
                self._push(site.id)
                self._ship(site.id)

    def _deliver(self) -> None:
        waiting: list[Message] = []
        for message in self._link:
            sent, site, batch = message
            if sent >= self.now or not self.regions_reachable(site)[1]:
                waiting.append(message)
            else:
                self.cloud.receive(batch, self.now)
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
                    self.site_logs[site].intervals.append(interval)

    def _push(self, site: str) -> None:
        """Configuration push takes the site's stream once a data path is stable, where it runs."""
        if not (self._settled(site) and self._running(site, "applies-desired")):
            return
        done = len(self.config_push.actions)
        self.config_push.pull(site, cloud=self.cloud, log=self.site_logs[site], at=self.now)
        for row in self.config_push.actions[done:]:
            self._line(f"config_push_{row['outcome']}", site=site, **row)

    def _ship(self, site: str) -> None:
        """Ship `site`'s log while a data path has been stable."""
        batch = self.site_logs[site].ship(stable=self._settled(site))
        if batch:
            self.send(site, batch)

    def _settled(self, site: str) -> bool:
        """Whether a data path has been up continuously for `rejoin_stable_seconds`."""
        since = self._stable.get(site)
        return since is not None and self.now - since >= self._settle

    def _probe(self, site: str) -> None:
        """When the site loses the cloud, note that its AAA still answers; nothing on return."""
        cloud = self.regions_reachable(site)[1]
        if self._cloud.get(site, True) and not cloud and self._running(site, "credential-cache"):
            self._line(f"aaa_{self.aaa.answer()}", site=site)
        self._cloud[site] = cloud

    def seal(self, resource: str) -> None:
        """Revoke `resource`'s identity and its disk key, and wipe its disks."""
        self.sealed.add(resource)

    def _line(self, name: str, **facts: object) -> None:
        """Note the inventory's screen line `name`, filled from `facts`."""
        self.note(self.text(name, **facts))

    def text(self, name: str, **facts: object) -> str:
        """The inventory's screen line `name`, filled from `facts`."""
        return Template(self.inventory.lines[name]).substitute(facts)

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
