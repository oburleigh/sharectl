"""A PoP's controller: observe the world, decide, act on its own site, remember, log."""

import base64
import json
from datetime import datetime, timedelta
from functools import partial
lazy from collections.abc import Callable, Iterable, Sequence

from sharectl.decide import decide
from sharectl.endpoints import BFD_PEERS, BGP_NEIGHBORS, Endpoints, Message, agent
from sharectl.log import DECISION, Record, envelope, stamp
from sharectl.model import Decision, Memory, Node, Outcome, Pod, Resource, Site, State
lazy from sharectl.world import World

POLL: Message = {"path": [BFD_PEERS, BGP_NEIGHBORS], "encoding": "JSON_IETF"}
"""The gNMI Get the controller sends each node every tick."""


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
        self.endpoints = Endpoints.of(world, site.id)
        self._names: dict[tuple[str, str | None], str]
        self._stable: datetime | None = None  # pragma: no mutate, falsy until a poll sets it
        self._paths: dict[str, list[tuple[str, bool, tuple[str, ...]]]] = {}

    def observe(self) -> State:
        """Take a frozen snapshot of the site, what arrived, and the memory."""
        world, site = self.world, self.site.id
        reachable, data_links_up = self._poll()
        leases = self._leases(r for r in world.inventory.resources if r.site == site)
        return State(
            inventory=world.inventory,
            now=world.now,
            site=self.site,
            resources=tuple(r for r in world.inventory.resources if r.site == site),
            pods=self._pods(),
            regions=len(world.inventory.regions),
            reachable=reachable,
            stable_since=self._stable,
            data_links_up=data_links_up,
            notices={
                r: datetime.fromisoformat(lease["reclaimAt"])
                for r, lease in leases.items()
                if lease["state"] == "reclaiming"
            },
            silent=self._silent(),
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

    def _poll(self) -> tuple[int, int]:
        """Ask each node for its BFD peers and BGP neighbours; count regions reached, links up.

        Each node's paths are kept for the poll line, and the data path's "stable since" moves.
        """
        inventory = self.world.inventory
        regions = inventory.regions
        reached: set[str] = set()
        data, links_up = False, 0
        for gnmi in self.endpoints.nodes:
            paths = self._paths[gnmi.node.id] = _paths(gnmi.node, gnmi.get(POLL), regions)
            for path, up, here in paths:
                carries = path not in inventory.network.management_only
                reached |= set(here)
                data |= bool(here) and carries
                links_up += up and carries
        self._stable = (self._stable or self.world.now) if data else None
        return len(reached), links_up

    def _pods(self) -> tuple[Pod, ...]:
        """Every pod in the site's cluster, from the API server; their names are kept to act on."""
        _, pods = self.endpoints.kubernetes.request("GET", "/api/v1/pods")
        self._names = {
            (p["metadata"]["labels"]["app"], p["spec"].get("nodeName")): p["metadata"]["name"]
            for p in pods["items"]
        }
        return tuple(Pod(service=s, resource=r) for s, r in self._names)

    def _silent(self) -> dict[str, datetime]:
        """Each node whose Ready condition is Unknown, and since when."""
        _, nodes = self.endpoints.kubernetes.request("GET", "/api/v1/nodes")
        return {
            n["metadata"]["name"]: datetime.fromisoformat(c["lastTransitionTime"])
            for n in nodes["items"]
            for c in n["status"]["conditions"]
            if (c["type"], c["status"]) == ("Ready", "Unknown")
        }

    def _leases(self, resources: Iterable[Resource]) -> dict[str, Message]:
        """The state of the lease on each leased resource, from its owner's API."""
        leased = self.world.inventory.leases
        return {
            r.id: self.endpoints.leases.request("GET", f"/v1/leases/{r.id}")[1]
            for r in resources
            if r.id in leased
        }

    def _report(self, state: State) -> None:
        """Show each node's poll: the first, any that differs, and one every few minutes."""
        world = self.world
        for node in (gnmi.node for gnmi in self.endpoints.nodes):
            links = "; ".join(
                world.text(
                    "poll_link_up" if up else "poll_link_down",
                    path=path,
                    peer=node.peers[path],
                    sessions=len(sessions),
                    regions=state.regions,
                )
                for path, up, sessions in self._paths[node.id]
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
        """Carry out an allowed decision on this site, through the endpoints."""
        match decision.rule.id, decision.resource:
            case "drain-on-reclaim", resource if resource:
                self._drain(resource, decision.evict)
            case "fence", resource if resource:
                self._fence(resource)
            case "release", resource if resource:
                self._call(self.endpoints.leases.request, "POST", f"/v1/leases/{resource}/release")
            case _:  # pragma: no mutate, a match with no arm already does nothing
                pass

    def _drain(self, resource: str, evict: Sequence[Pod]) -> None:
        """Cordon `resource`, then evict each pod in `evict` through the Eviction API."""
        kubernetes, namespace = self.endpoints.kubernetes, self.world.inventory.cluster.namespace
        node = {"spec": {"unschedulable": True}}
        self._call(kubernetes.request, "PATCH", f"/api/v1/nodes/{resource}", node)
        for pod in evict:
            name = self._names[pod.service, pod.resource]
            eviction = {
                "apiVersion": "policy/v1",
                "kind": "Eviction",
                "metadata": {"name": name, "namespace": namespace},
            }
            path = f"/api/v1/namespaces/{namespace}/pods/{name}/eviction"
            self._call(kubernetes.request, "POST", path, eviction)

    def _fence(self, resource: str) -> None:
        """Delete what is left on `resource`, ban its agent, delete it, disable its key, wipe it."""
        endpoints, cluster = self.endpoints, self.world.inventory.cluster
        for (_, on), name in self._names.items():
            if on == resource:
                path = f"/api/v1/namespaces/{cluster.namespace}/pods/{name}?gracePeriodSeconds=0"
                self._call(endpoints.kubernetes.request, "DELETE", path)
        fence = self.world.inventory.leases[resource].fence
        ban = agent(cluster, fence.identity)
        self._call(endpoints.spire.call, "spire.api.server.agent.v1.Agent/BanAgent", ban)
        self._call(endpoints.kubernetes.request, "DELETE", f"/api/v1/nodes/{resource}")
        self._call(endpoints.kms.call, "TrentService.DisableKey", {"KeyId": fence.disk_key})
        reset = {"graceful": False, "reboot": False, "mode": "ALL"}
        talos = partial(endpoints.talos.call, node=resource)
        target = self.world.text("call_node", node=resource)
        self._call(talos, "machine.MachineService/Reset", reset, shown=target)

    def _call(
        self,
        send: Callable[..., tuple[object, Message]],
        *request: object,
        shown: str = "",
    ) -> None:
        """Send one request to an endpoint and show it, with what came back."""
        status, _ = send(*request)
        words = [str(r) if isinstance(r, str) else json.dumps(r) for r in request]
        if shown:
            words.insert(1, shown)
        self.world.note(
            self.world.text("call", controller=self.name, call=" ".join(words), status=status)
        )

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


def _paths(
    node: Node, response: Message, regions: Sequence[Site]
) -> list[tuple[str, bool, tuple[str, ...]]]:
    """Each of `node`'s paths as its gNMI answer says: its link up, and the regions it reaches.

    A path reaches a region only when its link and its session to that region are both up.
    """
    links, sessions = _states(response)
    paths: list[tuple[str, bool, tuple[str, ...]]] = []
    for path, peer in node.peers.items():
        up = links.get(str(peer)) == "UP"
        established = (r.id for r in regions if sessions.get(str(r.peers[path])) == "ESTABLISHED")
        paths.append((path, up, tuple(established) if up else ()))
    return paths


def _states(response: Message) -> tuple[dict[str, str], dict[str, str]]:
    """Each BFD peer's and each BGP neighbour's address and session state, from a GetResponse."""
    peers: dict[str, dict[str, str]] = {}
    sessions: dict[str, str] = {}
    for notification in response["notification"]:
        for update in notification["update"]:
            elems = update["path"]["elem"]
            value = json.loads(base64.b64decode(update["val"]["jsonIetfVal"]))
            if elems[0]["name"] == BFD_PEERS["elem"][0]["name"]:
                peer = f"{elems[2]['key']['id']}/{elems[4]['key']['local-discriminator']}"
                peers.setdefault(peer, {})[elems[-1]["name"]] = value
            else:
                sessions[elems[-3]["key"]["neighbor-address"]] = value
    links = {p["remote-address"]: p["session-state"] for p in peers.values()}
    return links, sessions
