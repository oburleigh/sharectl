"""The dummy endpoints the controller talks to, each answering from the simulated estate."""

import base64
import json
import re
from dataclasses import dataclass
from typing import Any
lazy from collections.abc import Iterator, Mapping

from sharectl.log import stamp
lazy from sharectl.model import Cluster, Node
lazy from sharectl.world import World

type Message = dict[str, Any]

BFD_PEERS: Message = {
    "elem": [{"name": n} for n in ("bfd", "interfaces", "interface", "peers", "peer", "state")]
}
"""Every BFD peer's state on every interface (openconfig-bfd)."""

BGP_NEIGHBORS: Message = {
    "elem": [
        {"name": "network-instances"},
        {"name": "network-instance", "key": {"name": "DEFAULT"}},
        {"name": "protocols"},
        {"name": "protocol", "key": {"identifier": "BGP", "name": "DEFAULT"}},
        *({"name": n} for n in ("bgp", "neighbors", "neighbor", "state")),
    ]
}
"""Every BGP neighbour's state in the default instance (openconfig-bgp)."""


class Gnmi:
    """A network node's gNMI server: Get answers BFD and BGP state as JSON_IETF leaves."""

    def __init__(self, world: World, node: Node) -> None:
        """Answer for `node`, from what `world` says of its site's paths."""
        super().__init__()
        self.world, self.node = world, node

    def get(self, request: Message) -> Message:
        """A GetResponse with every leaf under the paths `request` asks for."""
        wanted = request["path"]
        update = [leaf for leaf in self._leaves() if any(_under(leaf, p) for p in wanted)]
        nanoseconds = int(self.world.now.timestamp()) * 1_000_000_000
        return {"notification": [{"timestamp": str(nanoseconds), "update": update}]}

    def _leaves(self) -> Iterator[Message]:
        world, node = self.world, self.node
        for number, (path, up) in enumerate(world.links_up(node.site), start=1):
            keys = {"interface": {"id": path}, "peer": {"local-discriminator": str(number)}}
            yield _leaf(BFD_PEERS, keys=keys, name="remote-address", value=str(node.peers[path]))
            yield _leaf(BFD_PEERS, keys=keys, name="session-state", value="UP" if up else "DOWN")
        for region in world.inventory.regions:
            for path, address in region.peers.items():
                keys = {"neighbor": {"neighbor-address": str(address)}}
                state = "ESTABLISHED" if world.reaches(node.site, region.id, path) else "IDLE"
                yield _leaf(BGP_NEIGHBORS, keys=keys, name="session-state", value=state)


PRESSURES = (
    ("MemoryPressure", "KubeletHasSufficientMemory", "kubelet has sufficient memory available"),
    ("DiskPressure", "KubeletHasNoDiskPressure", "kubelet has no disk pressure"),
    ("PIDPressure", "KubeletHasSufficientPID", "kubelet has sufficient PID available"),
)
"""A node's pressure conditions, each with kubelet's healthy reason and message."""


class Kubernetes:
    """A site's Kubernetes API server: the core/v1 and policy/v1 calls a drain and a fence make.

    Pods are named by their service and their copy's place among that service's copies.
    """

    def __init__(self, world: World, site: str) -> None:
        """Answer for `site`'s cluster."""
        super().__init__()
        self.world, self.kube = world, world.kubes[site]
        self._namespace = world.inventory.cluster.namespace
        pods = f"/api/v1/namespaces/{re.escape(self._namespace)}/pods/(?P<pod>[^/?]+)"
        self._routes = (
            ("GET", re.compile(r"/api/v1/pods"), self._pods),
            ("PATCH", re.compile(r"/api/v1/nodes/(?P<node>[^/]+)"), self._patch),
            ("DELETE", re.compile(r"/api/v1/nodes/(?P<node>[^/]+)"), self._delete_node),
            ("POST", re.compile(f"{pods}/eviction"), self._evict),
            ("DELETE", re.compile(rf"{pods}\?gracePeriodSeconds=0"), self._delete_pod),
        )

    def request(self, method: str, path: str, body: Message | None = None) -> tuple[int, Message]:
        """Answer one request as the API server would: its HTTP status and its body."""
        for verb, route, answer in self._routes:
            if verb == method and (match := route.fullmatch(path)):
                return answer(body or {}, **match.groupdict())
        return NOT_FOUND

    def _named(self) -> dict[str, int]:
        """Each pod's name and its place in the cluster's pod list."""
        names: dict[str, int] = {}
        for index, pod in enumerate(self.kube.pods):
            copy = sum(p.service == pod.service for p in self.kube.pods[:index])
            names[f"{pod.service}-{copy}"] = index
        return names

    def _pod(self, name: str, index: int) -> Message:
        pod = self.kube.pods[index]
        return {
            "kind": "Pod",
            "apiVersion": "v1",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": {"app": pod.service},
            },
            "spec": {"nodeName": pod.resource} if pod.resource else {},
            "status": {"phase": "Running" if pod.resource else "Pending"},
        }

    def _pods(self, _: Message) -> tuple[int, Message]:
        items = [self._pod(name, index) for name, index in self._named().items()]
        return 200, {"kind": "PodList", "apiVersion": "v1", "items": items}

    def _node(self, name: str) -> Message:
        world = self.world
        ready = {
            "status": "True",
            "reason": "KubeletReady",
            "message": "kubelet is posting ready status",
            "lastHeartbeatTime": stamp(world.now),
            "lastTransitionTime": stamp(world.started),
        }
        pressures = [
            {"type": kind, **ready, "status": "False", "reason": reason, "message": message}
            for kind, reason, message in PRESSURES
        ]
        return {
            "kind": "Node",
            "apiVersion": "v1",
            "metadata": {"name": name},
            "spec": {"unschedulable": True} if name in self.kube.cordoned else {},
            "status": {"conditions": [{"type": "Ready", **ready}, *pressures]},
        }

    def _patch(self, body: Message, node: str) -> tuple[int, Message]:
        if node not in self.kube.nodes:
            return NOT_FOUND
        if body.get("spec", {}).get("unschedulable"):
            self.kube.cordon(node)
        return 200, self._node(node)

    def _delete_node(self, _: Message, node: str) -> tuple[int, Message]:
        if node not in self.kube.nodes:
            return NOT_FOUND
        self.kube.delete(node)
        return 200, _status(200)

    def _evict(self, _: Message, pod: str) -> tuple[int, Message]:
        if (index := self._named().get(pod)) is None:
            return NOT_FOUND
        self.kube.evict((self.kube.pods[index],))
        return 201, _status(201)

    def _delete_pod(self, _: Message, pod: str) -> tuple[int, Message]:
        if (index := self._named().get(pod)) is None:
            return NOT_FOUND
        deleted = self._pod(pod, index)
        self.kube.evict((self.kube.pods[index],))
        return 200, deleted


class Leases:
    """Kilima's lease API: the release of a lease."""

    def __init__(self, world: World) -> None:
        """Answer for every lease Share holds."""
        super().__init__()
        self.world = world

    def request(self, method: str, path: str) -> tuple[int, Message]:
        """`POST /v1/leases/{id}/release` to end a lease; Kilima tells, so nothing else is read."""
        match = re.fullmatch(r"/v1/leases/(?P<lease>[^/]+)/release", path)
        if not match or match["lease"] not in self.world.inventory.leases:
            return 404, {"message": "no such lease"}
        if method != "POST":
            return 405, {"message": "method not allowed"}
        self.world.released.add(match["lease"])
        return 204, {}


class Spire:
    """The SPIRE server's agent.v1 Agent service."""

    def __init__(self, world: World) -> None:
        """Answer for the trust domain the inventory names."""
        super().__init__()
        self.world = world
        self.banned: set[str] = set()
        """The resources whose agents are banned."""

    def call(self, rpc: str, request: Message) -> tuple[str, Message]:
        """BanAgent: the agent's next call is refused and it cannot attest again."""
        del rpc
        inventory = self.world.inventory
        found = [
            resource
            for resource, lease in inventory.leases.items()
            if request == agent(inventory.cluster, lease.fence.identity)
        ]
        if not found:
            return "NOT_FOUND", {"message": "agent not found"}
        self.banned.update(found)
        return "OK", {}


class Kms:
    """Share's KMS: DisableKey under awsJson1_1. The not-found message is this dummy's own."""

    def __init__(self, world: World) -> None:
        """Answer for the disk keys the inventory names."""
        super().__init__()
        self.world = world
        self.disabled: set[str] = set()
        """The keys disabled."""

    def call(self, target: str, body: Message) -> tuple[int, Message]:
        """`TrentService.DisableKey`: 200 with an empty body, or the key is not found."""
        del target
        key = body["KeyId"]
        if key not in {lease.fence.disk_key for lease in self.world.inventory.leases.values()}:
            return 400, {"__type": "NotFoundException", "message": f"Key '{key}' does not exist"}
        self.disabled.add(key)
        return 200, {}


class Talos:
    """Each node's Talos machine API."""

    def __init__(self, world: World) -> None:
        """Answer for every node the inventory holds."""
        super().__init__()
        self.world = world
        self.wiped: set[str] = set()
        """The nodes reset."""

    def call(self, rpc: str, request: Message, *, node: str) -> tuple[str, Message]:
        """`machine.MachineService/Reset` on `node`: its disks are wiped."""
        del rpc, request
        if node not in {r.id for r in self.world.inventory.resources}:
            return "NOT_FOUND", {"message": "node not found"}
        self.wiped.add(node)
        return "OK", {"messages": [{"metadata": {"hostname": node}}]}


@dataclass(frozen=True, slots=True)
class Endpoints:
    """Every endpoint a site's controller talks to."""

    nodes: list[Gnmi]
    kubernetes: Kubernetes
    leases: Leases
    spire: Spire
    kms: Kms
    talos: Talos

    @classmethod
    def of(cls, world: World, site: str) -> Endpoints:
        """The dummies for `site`, answering from `world`."""
        return cls(
            nodes=[Gnmi(world, n) for n in world.inventory.nodes if n.site == site],
            kubernetes=Kubernetes(world, site),
            leases=Leases(world),
            spire=Spire(world),
            kms=Kms(world),
            talos=Talos(world),
        )


def agent(cluster: Cluster, path: str) -> Message:
    """The SPIRE agent.v1 request naming the agent at `path` in `cluster`'s trust domain."""
    return {"id": {"trustDomain": cluster.trust_domain, "path": path}}


def _status(code: int) -> Message:
    """A metav1 Status for a call that succeeded with `code`."""
    return {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Success", "code": code}


def _leaf(
    base: Message, *, keys: Mapping[str, Mapping[str, str]], name: str, value: str
) -> Message:
    """One Update: the leaf `name` under `base`, its lists keyed by `keys`, `value` as JSON_IETF."""
    elems: list[Message] = []
    for elem in base["elem"]:
        key = {**elem.get("key", {}), **keys.get(elem["name"], {})}
        elems.append({"name": elem["name"], **({"key": key} if key else {})})
    encoded = base64.b64encode(json.dumps(value).encode()).decode()
    return {"path": {"elem": [*elems, {"name": name}]}, "val": {"jsonIetfVal": encoded}}


def _under(leaf: Message, prefix: Message) -> bool:
    """Whether `leaf` lies under `prefix`, matching each name and any key the prefix gives."""
    path = leaf["path"]["elem"]
    asked = prefix["elem"]
    return len(path) >= len(asked) and all(
        a["name"] == path[i]["name"] and a.get("key", {}).items() <= path[i].get("key", {}).items()
        for i, a in enumerate(asked)
    )


NOT_FOUND: tuple[int, Message] = (
    404,
    {
        "kind": "Status",
        "apiVersion": "v1",
        "metadata": {},
        "status": "Failure",
        "reason": "NotFound",
        "code": 404,
    },
)
"""The API server's answer for a node or pod it does not have."""
