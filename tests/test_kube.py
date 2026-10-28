from collections import Counter
from dataclasses import replace

from builders import INVENTORY, KILIMA
from sharectl.kube import Kube
from sharectl.model import Pod, Service, Trust

TOLERANT = {"sdn-controller", "aaa", "config-push", "telemetry-collector", "local-log"}


def cluster(site: str = "mombasa", services: tuple[Service, ...] = INVENTORY.services) -> Kube:
    inventory = replace(INVENTORY, services=services)
    return Kube(inventory, next(s for s in inventory.sites if s.id == site))


def test_boot_places_25_pods_at_mombasa_five_of_them_on_the_partner_vm() -> None:
    kube = cluster()

    placed: dict[str, list[str | None]] = {}
    for pod in kube.pods:
        placed.setdefault(pod.service, []).append(pod.resource)
    srv1, srv2, srv3 = "mba-srv-1", "mba-srv-2", "mba-srv-3"
    assert placed == {
        "sdn-controller": [srv1, KILIMA, srv2],
        "aaa": [srv3, KILIMA],
        "config-push": [srv1, KILIMA],
        "telemetry-collector": [srv2, KILIMA],
        "local-log": [srv3, KILIMA, srv1],
        "verifier-probe": [srv2, srv3],
        "site-controller": [srv1, srv2],
        "site-postgres": [srv3, srv1, srv2],
        "spire-server": [srv3, srv1, srv2],
        "argocd-core": [srv3],
        "git-mirror": [srv1],
        "image-mirror": [srv2],
    }
    assert [p.service for p in kube.pods if p.standby] == [
        "config-push",
        "verifier-probe",
        "site-controller",
    ]


def test_no_node_holds_two_copies_of_a_service_except_a_region_cluster() -> None:
    pop, region = cluster(), cluster("E1")

    assert max(Counter((p.service, p.resource) for p in pop.pods).values()) == 1
    assert Counter(p.service for p in region.pods if p.resource == "e1-cluster")["api"] == 2


def test_spread_keeps_a_majority_on_share_and_prefers_an_unleased_node() -> None:
    kube = cluster()

    sdn = [p.resource for p in kube.pods if p.service == "sdn-controller"]
    assert sdn.count(KILIMA) == 1
    assert sdn[0] != KILIMA
    standby = next(p for p in kube.pods if p.service == "config-push" and p.standby)
    assert standby.resource == KILIMA


def only_the_partner_vm(kube: Kube) -> None:
    for server in ("mba-srv-1", "mba-srv-2", "mba-srv-3"):
        kube.cordon(server)
    kube.evict(tuple(p for p in kube.pods if p.resource))
    kube.reconcile()


def test_a_leased_node_takes_only_services_that_tolerate_reclaimable_capacity() -> None:
    attested = tuple(replace(s, min_trust=Trust.ATTESTED) for s in INVENTORY.services)
    kube = cluster(services=attested)
    only_the_partner_vm(kube)

    assert {p.service for p in kube.pods if p.resource == KILIMA} == TOLERANT
    assert all(p.resource is None for p in kube.pods if p.service not in TOLERANT)


def test_a_node_takes_no_pod_whose_min_trust_it_does_not_meet() -> None:
    trusted = tuple(
        replace(s, min_trust=Trust.TRUSTED) if s.id == "aaa" else s for s in INVENTORY.services
    )
    kube = cluster(services=trusted)
    only_the_partner_vm(kube)

    assert {p.service for p in kube.pods if p.resource == KILIMA} == TOLERANT - {"aaa"}
    assert [p.resource for p in kube.pods if p.service == "aaa"] == [None, None]


def test_an_evicted_pod_is_recreated_at_the_next_reconcile_on_a_node_that_lacks_it() -> None:
    kube = cluster()
    on_kilima = tuple(p for p in kube.pods if p.resource == KILIMA)
    kube.cordon(KILIMA)
    kube.evict(on_kilima)

    assert sum(p.resource is None for p in kube.pods) == 5
    recreated = kube.reconcile()

    assert sorted(p.service for p in recreated) == sorted(TOLERANT)
    assert all(p.resource not in {None, KILIMA} for p in kube.pods)
    assert max(Counter((p.service, p.resource) for p in kube.pods).values()) == 1


def test_a_pod_with_no_allowed_node_stays_pending() -> None:
    kube = cluster()
    for resource in ("mba-srv-1", "mba-srv-2", "mba-srv-3", KILIMA):
        kube.cordon(resource)
    kube.evict(tuple(p for p in kube.pods if p.service == "site-postgres"))

    assert not kube.reconcile()
    assert [p for p in kube.pods if p.resource is None] == [
        Pod(service="site-postgres", resource=None)
    ] * 3


def test_deleting_a_node_deletes_its_pods_and_they_are_recreated_elsewhere() -> None:
    kube = cluster()
    kube.delete(KILIMA)

    assert sum(p.resource is None for p in kube.pods) == 5
    assert len(kube.reconcile()) == 5
    assert not [p for p in kube.pods if p.resource in {None, KILIMA}]


def test_a_region_runs_every_anywhere_service_on_its_cluster() -> None:
    placed = Counter(p.service for p in cluster("E1").pods if p.resource == "e1-cluster")

    assert placed == {"api": 2, "workflow-workers": 2, "chain-indexer": 1, "portal": 2}
