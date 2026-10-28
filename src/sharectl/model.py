"""The domain types: the declared estate, and what the controller sees, decides and remembers."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal
lazy from collections.abc import Mapping
lazy from datetime import datetime
lazy from ipaddress import IPv4Address

from pydantic import Field


class Role(StrEnum):
    """What a site is: a PoP gets a controller; regions and the edge are probed."""

    POP = "pop"
    REGION = "region"
    EDGE = "edge"


class Trust(StrEnum):
    """How far a resource is trusted, lowest first."""

    UNTRUSTED = "untrusted"
    ATTESTED = "attested"
    TRUSTED = "trusted"

    def meets(self, needed: Trust) -> bool:
        """Say whether this trust is at least `needed`."""
        return list(Trust).index(self) >= list(Trust).index(needed)


class Outcome(StrEnum):
    """What became of a decision."""

    APPLIED = "applied"
    IGNORED = "ignored"
    REFUSED = "refused"


class Layer(StrEnum):
    """What a path reports: its link at a site, and over it a session to each other site."""

    LINK = "link"
    SESSION = "session"


class Placement(StrEnum):
    """Where a service may run: only at its PoP, or anywhere in the regions."""

    SITE_BOUND = "site-bound"
    ANYWHERE = "anywhere"


@dataclass(frozen=True, slots=True, kw_only=True)
class Network:
    """The paths joining every pair of sites, and those that carry no data."""

    paths: tuple[str, ...]
    management_only: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Cluster:
    """The namespace Share's pods run in at every site, and the trust domain its nodes join."""

    namespace: str
    trust_domain: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Telemetry:
    """The counters polled on every configured handoff, and how often."""

    counters: tuple[str, ...]
    poll_minutes: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Site:
    """A PoP, a cloud region or the edge; a region names its BGP neighbour on each path."""

    id: str
    code: Annotated[str, Field(pattern=r"^[a-z0-9-]+$")]
    role: Role
    peers: Mapping[str, IPv4Address] = frozendict()


@dataclass(frozen=True, slots=True, kw_only=True)
class Fence:
    """What a fence revokes on a resource: its SPIRE agent's SPIFFE ID path and its disks' key."""

    identity: str
    disk_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Lease:
    """The terms on which Share uses a resource another party owns, and what it revokes."""

    notice_minutes: int
    paid_by: str
    fence: Fence


@dataclass(frozen=True, slots=True, kw_only=True)
class Resource:
    """A Kubernetes node or region cluster that pods run on."""

    id: str
    site: str
    kind: str
    provider: str
    trust: Trust
    lease: Lease | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Node:
    """A network node: ports and counters at a site, and the far-end peer of each path."""

    id: str
    site: str
    peers: Mapping[str, IPv4Address]


@dataclass(frozen=True, slots=True, kw_only=True)
class Handoff:
    """A port on a network node, with the generation applied at boot (0: none)."""

    id: str
    node: str
    generation: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Service:  # pylint: disable=too-many-instance-attributes
    """A service and its placement rules."""

    id: str
    placement: Annotated[Placement, Field(alias="class")]
    active: int
    standby: int
    min: int
    min_trust: Trust
    tolerates_reclaimable: bool
    spread: Literal["provider"] | None = None
    roles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Params:  # pylint: disable=too-many-instance-attributes
    """The rules' thresholds and limits."""

    tick_seconds: int
    confirm_seconds: int
    rejoin_stable_seconds: int
    unreachable_grace_seconds: int
    resend_overlap: int
    fence_before_deadline_minutes: int
    release_before_deadline_minutes: int
    max_actions_per_hour: int
    poll_report_minutes: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Rule:
    """A rule as data: its trigger, condition and action are logged, never evaluated."""

    id: str
    version: int
    trigger: str
    condition: str
    action: str
    rollback: str
    reasons: Mapping[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class Inventory:  # pylint: disable=too-many-instance-attributes
    """The estate, the services, the rules, the refusal reasons and the stubs' screen lines."""

    network: Network
    cluster: Cluster
    telemetry: Telemetry
    sites: tuple[Site, ...] = Field(alias="site")
    resources: tuple[Resource, ...] = Field(alias="resource")
    nodes: tuple[Node, ...] = Field(alias="node")
    handoffs: tuple[Handoff, ...] = Field(alias="handoff")
    services: tuple[Service, ...] = Field(alias="service")
    params: Params
    rules: tuple[Rule, ...] = Field(alias="rule")
    refusals: Mapping[str, str]
    lines: Mapping[str, str]

    @property
    def regions(self) -> tuple[Site, ...]:
        """The sites of role `region`, in file order."""
        return tuple(s for s in self.sites if s.role is Role.REGION)

    @property
    def leases(self) -> dict[str, Lease]:
        """Each leased resource's id and its lease."""
        return {r.id: r.lease for r in self.resources if r.lease}


@dataclass(frozen=True, slots=True, kw_only=True)
class Pod:
    """A copy of a service, on a resource or pending (`resource` is None)."""

    service: str
    resource: str | None
    standby: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class DesiredState:
    """A handoff's configuration; the generation orders it, the rest is carried."""

    handoff: str
    generation: int
    agreement: str
    version: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Interval:
    """One counter's bytes over one poll interval, keyed by the interval it was scheduled for."""

    handoff: str
    counter: str
    start: datetime
    bytes: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Reclaim:
    """A leased resource under reclaim: when it is fenced and released."""

    fence: datetime
    release: datetime
    notice: bool
    fenced: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Memory:  # pylint: disable=too-many-instance-attributes
    """What a controller carries from one tick to the next; replaced, never changed."""

    isolated: bool = False
    down_since: datetime | None = None
    waits: frozenset[tuple[str, str]] = frozenset()
    reclaims: Mapping[str, Reclaim] = frozendict()
    actions: tuple[datetime, ...] = ()
    paged: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True, slots=True, kw_only=True)
class State:  # pylint: disable=too-many-instance-attributes
    """The frozen snapshot a controller decides on."""

    inventory: Inventory
    now: datetime
    site: Site
    resources: tuple[Resource, ...]
    pods: tuple[Pod, ...]
    regions: int
    reachable: int
    stable_since: datetime | None
    data_links_up: int
    notices: Mapping[str, datetime] = frozendict()
    silent: Mapping[str, datetime] = frozendict()
    memory: Memory = Memory()


@dataclass(frozen=True, slots=True, kw_only=True)
class Decision:  # pylint: disable=too-many-instance-attributes
    """What a rule decided, and why."""

    rule: Rule
    subject: str
    reason: str
    observed: Mapping[str, object]
    outcome: Outcome = Outcome.APPLIED
    resource: str | None = None
    limited: bool = False
    page: bool = False
    evict: tuple[Pod, ...] = ()
