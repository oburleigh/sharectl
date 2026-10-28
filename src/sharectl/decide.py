"""The rules, the guards and the limits: pure functions from a snapshot to decisions."""

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from math import inf
from string import Template

from sharectl.model import (
    Decision,
    Memory,
    Outcome,
    Pod,
    Reclaim,
    Rule,
    State,
)

type Effect = Callable[[Memory], Memory]
type Step = tuple[Decision, Effect]
type RuleCode = Callable[[State, Memory, Rule], Iterator[Step]]


def decide(state: State) -> tuple[list[Decision], Memory]:
    """Run every rule in order on a working copy of memory; return the decisions and new memory.

    Each allowed decision's effect is applied to the working copy as it stands, so a later step
    or rule sees it; a refused one leaves memory as it was.
    """
    memory = _track(state)
    decisions: list[Decision] = []
    for rule in state.inventory.rules:
        for step in RULES[rule.id](state, memory, rule):
            decision, memory = _guard(state, memory, step)
            if decision:
                decisions.append(decision)
    return decisions, memory


def _guard(state: State, memory: Memory, step: Step) -> tuple[Decision | None, Memory]:
    """Check a decision against the limits: refuse it, keeping memory as it was, or allow it.

    A refusal is logged and paged once.
    """
    decision, effect = step
    key = (decision.rule.id, decision.subject)
    refusal = _limit(state, decision)
    if refusal is None:
        return decision, effect(memory)
    reason = Template(state.inventory.refusals[refusal[0]]).substitute(refusal[1])
    refused = replace(decision, outcome=Outcome.REFUSED, reason=reason, page=True)
    return (None if key in memory.paged else refused), replace(memory, paged=memory.paged | {key})


def _limit(state: State, decision: Decision) -> tuple[str, dict[str, object]] | None:
    """The limit a decision breaks, with its facts, or None."""
    if not authorise(state, decision.resource):
        return "site", {"resource": decision.resource, "site": state.site.id}
    return None


def authorise(state: State, resource: str | None) -> bool:
    """Say whether this controller may act on `resource`: only on its own site's."""
    return resource is None or any(
        r.id == resource and r.site == state.site.id for r in state.resources
    )


def _track(state: State) -> Memory:
    """Note when the regions went silent."""
    memory = state.memory
    down = (memory.down_since or state.now) if state.reachable == 0 else None
    return replace(memory, down_since=down)


def _decision(rule: Rule, case: str, subject: str, **facts: object) -> Decision:
    reason = Template(rule.reasons[case]).substitute(facts)
    return Decision(rule=rule, subject=subject, reason=reason, observed=facts)


def _seconds(state: State, since: datetime | None) -> float:
    return -inf if since is None else (state.now - since).total_seconds()


def isolation(state: State, memory: Memory, rule: Rule) -> Iterator[Step]:
    """Enter isolated mode once no region has answered on any path for `confirm_seconds`."""
    seconds = _seconds(state, memory.down_since)
    if not memory.isolated and seconds >= state.inventory.params.confirm_seconds:
        facts = {
            "reachable": state.reachable,
            "regions": state.regions,
            "seconds": int(seconds),
            "data_links_up": state.data_links_up,
        }
        yield _decision(rule, "applied", state.site.id, **facts), partial(_isolate, isolated=True)


def rejoin(state: State, memory: Memory, rule: Rule) -> Iterator[Step]:
    """Leave isolated mode once a data path has been up for `rejoin_stable_seconds`."""
    seconds = _seconds(state, state.stable_since)
    if memory.isolated and seconds >= state.inventory.params.rejoin_stable_seconds:
        decision = _decision(rule, "applied", state.site.id, seconds=int(seconds))
        yield decision, partial(_isolate, isolated=False)


def drain(state: State, memory: Memory, rule: Rule) -> Iterator[Step]:
    """Cordon a leased resource its owner reclaims.

    Evict each pod whose service keeps at least its `min` copies running elsewhere; Kubernetes
    recreates them. Fence and release count back from the notice's deadline.
    """
    for resource in state.resources:
        deadline = state.notices.get(resource.id)
        if resource.lease is None or resource.id in memory.reclaims or deadline is None:
            continue
        reclaim = _schedule(state, deadline)
        on = [pod for pod in state.pods if pod.resource == resource.id]
        evict = _evictable(state, on)
        decision = _decision(
            rule,
            "applied",
            resource.id,
            resource=resource.id,
            evicted=len(evict),
            pods=len(on),
            fence=f"{reclaim.fence:%H:%M:%S}",
            release=f"{reclaim.release:%H:%M:%S}",
        )
        effect = partial(_reclaim, resource=resource.id, reclaim=reclaim)
        yield replace(decision, resource=resource.id, evict=evict), effect


def _schedule(state: State, deadline: datetime) -> Reclaim:
    """Fence and release count back from the owner's deadline."""
    params = state.inventory.params
    return Reclaim(
        fence=deadline - timedelta(minutes=params.fence_before_deadline_minutes),
        release=deadline - timedelta(minutes=params.release_before_deadline_minutes),
    )


def _evictable(state: State, on: Sequence[Pod]) -> tuple[Pod, ...]:
    """The pods in `on` whose services keep at least their `min` running pods without them."""
    running = Counter(pod.service for pod in state.pods if pod.resource is not None)
    floor = {service.id: service.min for service in state.inventory.services}
    evict: list[Pod] = []
    for pod in on:
        if running[pod.service] - 1 >= floor[pod.service]:
            running[pod.service] -= 1  # pragma: no mutate, one copy per node
            evict.append(pod)
    return tuple(evict)


def fence(state: State, memory: Memory, rule: Rule) -> Iterator[Step]:
    """At its fence time, delete what is left of Share's on a reclaimed resource and seal it."""
    for resource, reclaim in memory.reclaims.items():
        if not reclaim.fenced and state.now >= reclaim.fence:
            left = sum(pod.resource == resource for pod in state.pods)
            decision = _decision(rule, "applied", resource, resource=resource, pods=left)
            effect = partial(_reclaim, resource=resource, reclaim=replace(reclaim, fenced=True))
            yield replace(decision, resource=resource), effect


def release(state: State, memory: Memory, rule: Rule) -> Iterator[Step]:
    """At its release time, give a fenced resource back to its owner."""
    for resource, reclaim in memory.reclaims.items():
        if reclaim.fenced and state.now >= reclaim.release:
            decision = _decision(rule, "applied", resource, resource=resource)
            effect = partial(_reclaim, resource=resource, reclaim=None)
            yield replace(decision, resource=resource), effect


def _blocked(state: State, memory: Memory) -> dict[tuple[str, str], dict[str, object]]:
    """Each blocked situation, keyed by its case and subject, with its facts."""
    pending = Counter(pod.service for pod in state.pods if pod.resource is None)
    blocked: dict[tuple[str, str], dict[str, object]] = {
        ("pending", service): {"service": service, "pending": count}
        for service, count in pending.items()
    }
    if memory.isolated:
        blocked["isolated", state.site.id] = {}
    return blocked


def wait(state: State, memory: Memory, rule: Rule) -> Iterator[Step]:
    """Log each blocked situation once, when it starts; page when pods cannot be placed."""
    for (case, subject), facts in sorted(_blocked(state, memory).items()):
        if (case, subject) not in memory.waits:
            decision = _decision(rule, case, subject, **facts)
            yield replace(decision, page=case == "pending"), partial(_wait, key=(case, subject))


def resume(state: State, memory: Memory, rule: Rule) -> Iterator[Step]:
    """Log each blocked situation once, when it ends."""
    for case, subject in sorted(memory.waits - _blocked(state, memory).keys()):
        effect = partial(_wait, key=(case, subject), blocked=False)  # pragma: no mutate, falsy
        yield _decision(rule, case, subject, service=subject), effect


def _isolate(memory: Memory, *, isolated: bool) -> Memory:
    return replace(memory, isolated=isolated)


def _reclaim(memory: Memory, *, resource: str, reclaim: Reclaim | None) -> Memory:
    """Record a resource's reclaim, or drop it once released."""
    reclaims = {r: c for r, c in memory.reclaims.items() if r != resource}
    return replace(memory, reclaims=reclaims | ({resource: reclaim} if reclaim else {}))


def _wait(memory: Memory, *, key: tuple[str, str], blocked: bool = True) -> Memory:
    return replace(memory, waits=memory.waits | {key} if blocked else memory.waits - {key})


RULES: dict[str, RuleCode] = {
    "site-isolation": isolation,
    "site-rejoin": rejoin,
    "drain-on-reclaim": drain,
    "fence": fence,
    "release": release,
    "wait": wait,
    "resume": resume,
}
"""Each rule's code, keyed by the rule's id in `rules.toml`."""
