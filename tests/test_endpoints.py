import base64
import json
from datetime import timedelta

import pytest

from builders import INVENTORY, T0
from sharectl.endpoints import (
    BFD_PEERS,
    BGP_NEIGHBORS,
    Gnmi,
    Kms,
    Kubernetes,
    Leases,
    Message,
    Spire,
    Talos,
)
from sharectl.model import Layer
from sharectl.world import World

MOMBASA_NODE = next(n for n in INVENTORY.nodes if n.id == "mba-node-1")


def leaves(response: Message) -> dict[str, str]:
    """Each leaf as `name[key=value]/…/leaf`, decoded from its base64 JSON_IETF value."""
    found: dict[str, str] = {}
    for notification in response["notification"]:
        for update in notification["update"]:
            path = "/".join(
                e["name"] + "".join(f"[{k}={v}]" for k, v in e.get("key", {}).items())
                for e in update["path"]["elem"]
            )
            found[path] = json.loads(base64.b64decode(update["val"]["jsonIetfVal"]))
    return found


def test_get_answers_each_bfd_peer_as_openconfig_bfd_with_its_session_state() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ["uplink-a"], down=True)
    world.advance(T0)

    response = Gnmi(world, MOMBASA_NODE).get({"path": [BFD_PEERS], "encoding": "JSON_IETF"})

    peer = "bfd/interfaces/interface[id={}]/peers/peer[local-discriminator={}]/state/{}"
    assert leaves(response) == {
        peer.format("uplink-a", 1, "remote-address"): "192.0.2.9",
        peer.format("uplink-a", 1, "session-state"): "DOWN",
        peer.format("uplink-b", 2, "remote-address"): "198.51.100.9",
        peer.format("uplink-b", 2, "session-state"): "UP",
        peer.format("oob", 3, "remote-address"): "203.0.113.9",
        peer.format("oob", 3, "session-state"): "UP",
    }
    assert response["notification"][0]["timestamp"] == "1790996401000000000"


def test_get_answers_each_bgp_neighbour_with_its_session_state() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ["uplink-a"], down=True)
    world.cut("mombasa", ["E2"], ["oob"], down=True, layer=Layer.SESSION)
    world.advance(T0)

    response = Gnmi(world, MOMBASA_NODE).get({"path": [BGP_NEIGHBORS], "encoding": "JSON_IETF"})

    neighbor = (
        "network-instances/network-instance[name=DEFAULT]/protocols/"
        "protocol[identifier=BGP][name=DEFAULT]/bgp/neighbors/neighbor[neighbor-address={}]/"
        "state/session-state"
    )
    states = {
        "192.0.2.101": "IDLE",
        "192.0.2.102": "IDLE",
        "192.0.2.103": "IDLE",
        "198.51.100.101": "ESTABLISHED",
        "198.51.100.102": "ESTABLISHED",
        "198.51.100.103": "ESTABLISHED",
        "203.0.113.101": "ESTABLISHED",
        "203.0.113.102": "IDLE",
        "203.0.113.103": "ESTABLISHED",
    }
    assert leaves(response) == {neighbor.format(a): s for a, s in states.items()}


def test_get_answers_only_the_subtrees_asked_for() -> None:
    world = World(INVENTORY, T0)

    response = Gnmi(world, MOMBASA_NODE).get({"path": [], "encoding": "JSON_IETF"})

    assert response["notification"][0]["update"] == []


def test_a_leaf_is_encoded_as_base64_json_ietf_under_its_full_path() -> None:
    world = World(INVENTORY, T0)

    response = Gnmi(world, MOMBASA_NODE).get({"path": [BFD_PEERS], "encoding": "JSON_IETF"})

    assert response["notification"][0]["update"][1] == {
        "path": {
            "elem": [
                {"name": "bfd"},
                {"name": "interfaces"},
                {"name": "interface", "key": {"id": "uplink-a"}},
                {"name": "peers"},
                {"name": "peer", "key": {"local-discriminator": "1"}},
                {"name": "state"},
                {"name": "session-state"},
            ]
        },
        "val": {"jsonIetfVal": "IlVQIg=="},
    }


KILIMA = "mba-kilima-01"
SHARE = "share"
PRESSURES = (
    ("MemoryPressure", "KubeletHasSufficientMemory", "kubelet has sufficient memory available"),
    ("DiskPressure", "KubeletHasNoDiskPressure", "kubelet has no disk pressure"),
    ("PIDPressure", "KubeletHasSufficientPID", "kubelet has sufficient PID available"),
)
"""Kubelet's usual healthy pressure conditions: from memory of its output, not verified."""
FAILURE: Message = {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure"}
NOT_FOUND: Message = {**FAILURE, "reason": "NotFound", "code": 404}


def test_the_pod_list_names_each_pod_by_its_service_with_its_node_and_phase() -> None:
    world = World(INVENTORY, T0)
    kube = world.kubes["mombasa"]
    kube.evict(tuple(p for p in kube.pods if p.resource == KILIMA and p.service == "aaa"))

    status, pods = Kubernetes(world, "mombasa").request("GET", "/api/v1/pods")

    assert status == 200
    assert (pods["kind"], pods["apiVersion"]) == ("PodList", "v1")
    aaa = [p for p in pods["items"] if p["metadata"]["labels"] == {"app": "aaa"}]
    assert [(p["metadata"]["name"], p["metadata"]["namespace"]) for p in aaa] == [
        ("aaa-0", SHARE),
        ("aaa-1", SHARE),
    ]
    assert aaa == [
        {
            "kind": "Pod",
            "apiVersion": "v1",
            "metadata": {"name": "aaa-0", "namespace": SHARE, "labels": {"app": "aaa"}},
            "spec": {"nodeName": "mba-srv-3"},
            "status": {"phase": "Running"},
        },
        {
            "kind": "Pod",
            "apiVersion": "v1",
            "metadata": {"name": "aaa-1", "namespace": SHARE, "labels": {"app": "aaa"}},
            "spec": {},
            "status": {"phase": "Pending"},
        },
    ]


def test_a_silent_node_reports_ready_unknown_since_it_went_silent() -> None:
    world = World(INVENTORY, T0)
    world.silent[KILIMA] = T0
    world.advance(T0 + timedelta(seconds=60))

    status, nodes = Kubernetes(world, "mombasa").request("GET", "/api/v1/nodes")

    assert (status, nodes["kind"], nodes["apiVersion"]) == (200, "NodeList", "v1")
    assert next(n for n in nodes["items"] if n["metadata"]["name"] == "mba-srv-1") == {
        "kind": "Node",
        "apiVersion": "v1",
        "metadata": {"name": "mba-srv-1"},
        "spec": {},
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "reason": "KubeletReady",
                    "message": "kubelet is posting ready status",
                    "lastHeartbeatTime": "2026-10-03T03:01:01Z",
                    "lastTransitionTime": "2026-10-03T03:00:01Z",
                },
                *(
                    {
                        "type": kind,
                        "status": "False",
                        "reason": reason,
                        "message": message,
                        "lastHeartbeatTime": "2026-10-03T03:01:01Z",
                        "lastTransitionTime": "2026-10-03T03:00:01Z",
                    }
                    for kind, reason, message in PRESSURES
                ),
            ]
        },
    }
    ready = {n["metadata"]["name"]: n["status"]["conditions"][0] for n in nodes["items"]}
    assert ready[KILIMA] == {
        "type": "Ready",
        "status": "Unknown",
        "reason": "NodeStatusUnknown",
        "message": "Kubelet stopped posting node status.",
        "lastHeartbeatTime": "2026-10-03T03:00:01Z",
        "lastTransitionTime": "2026-10-03T03:00:01Z",
    }
    assert (ready["mba-srv-1"]["status"], ready["mba-srv-1"]["lastHeartbeatTime"]) == (
        "True",
        "2026-10-03T03:01:01Z",
    )
    kilima = next(n for n in nodes["items"] if n["metadata"]["name"] == KILIMA)
    assert [(c["type"], c["status"], c["reason"]) for c in kilima["status"]["conditions"]] == [
        ("Ready", "Unknown", "NodeStatusUnknown"),
        ("MemoryPressure", "Unknown", "NodeStatusUnknown"),
        ("DiskPressure", "Unknown", "NodeStatusUnknown"),
        ("PIDPressure", "Unknown", "NodeStatusUnknown"),
    ]


def test_a_cordon_patch_and_an_eviction_drain_a_node_as_the_api_server_would() -> None:
    world = World(INVENTORY, T0)
    api = Kubernetes(world, "mombasa")

    patched = api.request("PATCH", f"/api/v1/nodes/{KILIMA}", {"spec": {"unschedulable": True}})
    eviction = {
        "apiVersion": "policy/v1",
        "kind": "Eviction",
        "metadata": {"name": "aaa-1", "namespace": SHARE},
    }
    evicted = api.request("POST", f"/api/v1/namespaces/{SHARE}/pods/aaa-1/eviction", eviction)
    world.advance(T0 + timedelta(seconds=10))

    assert (patched[0], patched[1]["spec"]) == (200, {"unschedulable": True})
    assert evicted == (
        201,
        {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Success", "code": 201},
    )
    assert [p.resource for p in world.kubes["mombasa"].pods if p.service == "aaa"] == [
        "mba-srv-3",
        "mba-srv-1",
    ]


def test_deleting_a_pod_at_once_then_its_node_leaves_nothing_on_it() -> None:
    world = World(INVENTORY, T0)
    api = Kubernetes(world, "mombasa")

    pod = api.request("DELETE", f"/api/v1/namespaces/{SHARE}/pods/aaa-1?gracePeriodSeconds=0")
    node = api.request("DELETE", f"/api/v1/nodes/{KILIMA}")

    assert (pod[0], pod[1]["kind"], pod[1]["metadata"]["name"]) == (200, "Pod", "aaa-1")
    assert node == (
        200,
        {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Success", "code": 200},
    )
    assert KILIMA not in world.kubes["mombasa"].nodes
    assert all(p.resource != KILIMA for p in world.kubes["mombasa"].pods)


def test_a_call_the_api_server_has_no_route_for_is_not_found() -> None:
    world = World(INVENTORY, T0)

    status, body = Kubernetes(world, "mombasa").request("DELETE", "/api/v1/nodes/no-such-node")

    assert (status, body) == (404, NOT_FOUND)


def test_the_lease_api_reports_a_reclaim_then_releases_the_lease() -> None:
    world = World(INVENTORY, T0)
    leases = Leases(world)
    active = leases.request("GET", f"/v1/leases/{KILIMA}")
    world.notices[KILIMA] = T0 + timedelta(minutes=10)
    reclaiming = leases.request("GET", f"/v1/leases/{KILIMA}")
    released = leases.request("POST", f"/v1/leases/{KILIMA}/release")

    assert active == (200, {"id": KILIMA, "state": "active", "reclaimAt": None})
    assert reclaiming == (
        200,
        {"id": KILIMA, "state": "reclaiming", "reclaimAt": "2026-10-03T03:10:01Z"},
    )
    assert released == (204, {})
    assert leases.request("GET", f"/v1/leases/{KILIMA}")[1]["state"] == "released"
    assert world.released == {KILIMA}


def test_spire_bans_an_agent_by_its_spiffe_id_and_not_one_it_does_not_know() -> None:
    world = World(INVENTORY, T0)
    spire = Spire(world)
    agent = {"trustDomain": "share.example", "path": "/spire/agent/k8s_psat/mombasa/mba-kilima-01"}

    banned = spire.call("spire.api.server.agent.v1.Agent/BanAgent", {"id": agent})
    unknown = spire.call(
        "spire.api.server.agent.v1.Agent/BanAgent", {"id": {**agent, "path": "/spire/agent/x"}}
    )
    elsewhere = spire.call(
        "spire.api.server.agent.v1.Agent/BanAgent",
        {"id": {**agent, "trustDomain": "other.example"}},
    )

    assert banned == ("OK", {})
    assert unknown == elsewhere == ("NOT_FOUND", {"message": "agent not found"})
    assert spire.banned == {KILIMA}


def test_kms_disables_a_key_and_says_so_for_one_it_does_not_hold() -> None:
    world = World(INVENTORY, T0)
    kms = Kms(world)
    key = "arn:aws:kms:eu-west-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"

    disabled = kms.call("TrentService.DisableKey", {"KeyId": key})
    unknown = kms.call("TrentService.DisableKey", {"KeyId": "no-such-key"})

    assert disabled == (200, {})
    assert unknown == (
        400,
        {"__type": "NotFoundException", "message": "Key 'no-such-key' does not exist"},
    )
    assert kms.disabled == {key}


def test_talos_resets_the_node_it_is_sent_to_and_no_node_it_does_not_have() -> None:
    talos = Talos(World(INVENTORY, T0))
    request = {"graceful": False, "reboot": False, "mode": "ALL"}
    reset = talos.call("machine.MachineService/Reset", request, node=KILIMA)
    unknown = talos.call("machine.MachineService/Reset", request, node="no-such-node")

    assert reset == ("OK", {"messages": [{"metadata": {"hostname": KILIMA}}]})
    assert unknown == ("NOT_FOUND", {"message": "node not found"})
    assert talos.wiped == {KILIMA}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/v1/nodes/no-such-node"),
        ("POST", f"/api/v1/namespaces/{SHARE}/pods/no-such-pod/eviction"),
        ("DELETE", f"/api/v1/namespaces/{SHARE}/pods/no-such-pod?gracePeriodSeconds=0"),
        ("POST", "/api/v1/namespaces/elsewhere/pods/aaa-1/eviction"),
    ],
)
def test_a_call_on_a_node_or_pod_the_cluster_does_not_have_is_not_found(
    method: str, path: str
) -> None:
    world = World(INVENTORY, T0)
    before = list(world.kubes["mombasa"].pods)

    status, body = Kubernetes(world, "mombasa").request(method, path, {"spec": {}})

    assert (status, body) == (404, NOT_FOUND)
    assert world.kubes["mombasa"].pods == before


def test_a_patch_that_does_not_set_unschedulable_leaves_the_node_schedulable() -> None:
    world = World(INVENTORY, T0)

    status, node = Kubernetes(world, "mombasa").request(
        "PATCH", f"/api/v1/nodes/{KILIMA}", {"spec": {"unschedulable": False}}
    )

    assert (status, node["spec"]) == (200, {})
    assert world.kubes["mombasa"].cordoned == set()


@pytest.mark.parametrize("path", ["/v1/leases/mba-srv-1", "/v1/elsewhere/mba-kilima-01"])
def test_the_lease_api_knows_only_share_s_leases(path: str) -> None:
    world = World(INVENTORY, T0)

    assert Leases(world).request("POST", path) == (404, {"message": "no such lease"})
    assert world.released == set()


def test_a_patch_with_no_spec_changes_nothing() -> None:
    world = World(INVENTORY, T0)

    status, node = Kubernetes(world, "mombasa").request("PATCH", f"/api/v1/nodes/{KILIMA}", {})

    assert (status, node["spec"]) == (200, {})
    assert world.kubes["mombasa"].cordoned == set()


def test_get_answers_nothing_under_another_network_instance() -> None:
    world = World(INVENTORY, T0)
    other = {
        "elem": [
            {"name": "network-instances"},
            {"name": "network-instance", "key": {"name": "OTHER"}},
        ]
    }

    response = Gnmi(world, MOMBASA_NODE).get({"path": [other], "encoding": "JSON_IETF"})

    assert response["notification"][0]["update"] == []


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", f"/v1/leases/{KILIMA}/release"), ("POST", f"/v1/leases/{KILIMA}")],
)
def test_the_lease_api_answers_405_to_a_method_a_path_does_not_take(method: str, path: str) -> None:
    world = World(INVENTORY, T0)

    assert Leases(world).request(method, path) == (405, {"message": "method not allowed"})
    assert world.released == set()


def test_get_answers_a_leaf_asked_for_by_its_whole_path() -> None:
    world = World(INVENTORY, T0)
    whole = {
        "elem": [
            {"name": "bfd"},
            {"name": "interfaces"},
            {"name": "interface", "key": {"id": "oob"}},
            {"name": "peers"},
            {"name": "peer"},
            {"name": "state"},
            {"name": "session-state"},
        ]
    }

    response = Gnmi(world, MOMBASA_NODE).get({"path": [whole], "encoding": "JSON_IETF"})

    leaf = "bfd/interfaces/interface[id=oob]/peers/peer[local-discriminator=3]/state/session-state"
    assert leaves(response) == {leaf: "UP"}


def test_get_answers_nothing_for_a_path_deeper_than_any_leaf() -> None:
    world = World(INVENTORY, T0)
    deeper = {"elem": [*BFD_PEERS["elem"], {"name": "session-state"}, {"name": "x"}]}

    response = Gnmi(world, MOMBASA_NODE).get({"path": [deeper], "encoding": "JSON_IETF"})

    assert response["notification"][0]["update"] == []
