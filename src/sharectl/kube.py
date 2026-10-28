"""The Kubernetes fake: one cluster per site, placing pods by the inventory's placement rules.

In a real deployment these rules are the manifests Argo CD applies: a required node affinity
on trust, a toleration for the reclaimable taint, a topology spread on the provider, a pod
anti-affinity for one copy per node, and a PodDisruptionBudget for each service's `min`.
"""

from dataclasses import replace

from sharectl.model import Inventory, Placement, Pod, Resource, Role, Site

WHAT_RUNS = {Role.POP: Placement.SITE_BOUND, Role.REGION: Placement.ANYWHERE}


class Kube:
    """A site's cluster: places, recreates and deletes pods; never decides to move one."""

    def __init__(self, inventory: Inventory, site: Site) -> None:
        """Place every service that runs at `site`, active copies before standby."""
        super().__init__()
        self._services = {
            s.id: s for s in inventory.services if s.placement is WHAT_RUNS.get(site.role)
        }
        self.nodes = {r.id: r for r in inventory.resources if r.site == site.id}
        self._shared = site.role is Role.REGION
        self.cordoned: set[str] = set()
        self.pods = [
            Pod(service=s.id, resource=None, standby=copy >= s.active)
            for s in self._services.values()
            for copy in range(s.active + s.standby)
        ]
        self.reconcile()

    def reconcile(self) -> list[Pod]:
        """Place every pending pod that has an allowed node; return the pods placed."""
        placed: list[Pod] = []
        for index, pod in enumerate(self.pods):
            if pod.resource is None and (resource := self._place(pod)):
                self.pods[index] = replace(pod, resource=resource.id)
                placed.append(self.pods[index])
        return placed

    def cordon(self, resource: str) -> None:
        """Take no new pod on `resource`."""
        self.cordoned.add(resource)

    def evict(self, pods: tuple[Pod, ...]) -> None:
        """Evict `pods`; they wait pending for the next reconcile."""
        for pod in pods:
            self.pods[self.pods.index(pod)] = replace(pod, resource=None)

    def delete(self, resource: str) -> None:
        """Delete the node `resource`; any pod left on it is recreated elsewhere."""
        del self.nodes[resource]
        self.pods = [replace(p, resource=None) if p.resource == resource else p for p in self.pods]

    def _place(self, pod: Pod) -> Resource | None:
        service = self._services[pod.service]
        copies = [
            self.nodes[p.resource] for p in self.pods if p.service == pod.service and p.resource
        ]
        allowed = [
            r
            for r in self.nodes.values()
            if r.id not in self.cordoned
            and r.trust.meets(service.min_trust)
            and (r.lease is None or service.tolerates_reclaimable)
            and (self._shared or r not in copies)
        ]

        def rank(r: Resource) -> tuple[int, bool, int, str]:
            spread = sum(c.provider == r.provider for c in copies if service.spread)
            return spread, r.lease is not None, sum(p.resource == r.id for p in self.pods), r.id

        return min(allowed, key=rank, default=None)
