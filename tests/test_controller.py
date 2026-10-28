import base64
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from builders import ALL_PATHS, INVENTORY, KILIMA, MOMBASA, T0, at
from sharectl.controller import Controller
from sharectl.endpoints import Gnmi, Kubernetes, Leases, Message
from sharectl.log import line
from sharectl.model import Decision, Layer, Memory, Outcome, State
from sharectl.world import World

HANDOFF = "mba-node-1/eth-1-12"
KEY = "arn:aws:kms:eu-west-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"
POLL_UP = (
    "site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 up, bgp 3/3 established; "
    "uplink-b bfd 198.51.100.9 up, bgp 3/3 established; "
    "oob bfd 203.0.113.9 up, bgp 3/3 established; 3/3 regions reachable"
)
POLL_DOWN = (
    "site/mombasa poll mba-node-1: uplink-a bfd 192.0.2.9 down; uplink-b bfd 198.51.100.9 down; "
    "oob bfd 203.0.113.9 down; 0/3 regions reachable"
)


def run(world: World, controller: Controller, span: tuple[int, int]) -> None:
    for seconds in range(span[0], span[1] + 1, 10):
        world.advance(at(seconds))
        controller.step()


def mombasa(world: World) -> set[tuple[str, str, datetime]]:
    """The keys of the intervals the cloud holds from Mombasa's handoffs."""
    return {key for key in world.cloud.intervals if key[0].startswith("mba-")}


def polls(world: World) -> list[tuple[datetime, str]]:
    return [(t, text) for t, text in world.notes if " poll " in text]


def outcomes(controller: Controller) -> list[str]:
    return [
        line(r)
        for r in controller.log
        if r["type"] == "platform.decision" and "outcome" in str(r["id"])
    ]


def test_each_decision_is_logged_proposed_then_outcome_as_a_cloudevent() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    run(world, controller, (0, 30))

    proposed, outcome = controller.log[0], controller.log[1]
    assert (proposed["id"], proposed["sequence"], proposed["source"]) == (
        "mba-20261003T030001Z-0001/proposed",
        "1",
        "/controllers/site/mombasa",
    )
    assert outcome["id"] == "mba-20261003T030001Z-0001/outcome"
    assert outcomes(controller) == [
        "03:00:31 site/mombasa site-isolation@v1 [cloud.unreachable] enter-isolated-mode: "
        "0/3 regions reachable for 30 s on all paths -> applied",
        "03:00:31 site/mombasa wait@v1 [blocked] wait: site-bound services keep running on site "
        "resources; nothing moves, as a copy in the cloud could not reach the node -> applied",
    ]


def test_two_instances_for_one_site_started_apart_never_share_an_id() -> None:
    world = World(INVENTORY, T0)
    first = Controller(world, MOMBASA, T0)
    second = Controller(world, MOMBASA, T0 + timedelta(seconds=1))
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    run(world, first, (0, 30))
    run(world, second, (40, 70))

    assert [r["id"] for r in first.log] == [
        "mba-20261003T030001Z-0001/proposed",
        "mba-20261003T030001Z-0001/outcome",
        "mba-20261003T030001Z-0002/proposed",
        "mba-20261003T030001Z-0002/outcome",
    ]
    assert [r["id"] for r in second.log] == [
        "mba-20261003T030002Z-0001/proposed",
        "mba-20261003T030002Z-0001/outcome",
        "mba-20261003T030002Z-0002/proposed",
        "mba-20261003T030002Z-0002/outcome",
    ]


def test_an_isolated_site_holds_its_records_and_ships_them_first_on_rejoin_with_overlap() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    run(world, controller, (0, 590))
    shipped = mombasa(world)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    run(world, controller, (600, 1800))
    assert mombasa(world) == shipped
    assert not world.cloud.records

    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    run(world, controller, (1810, 2130))

    held = [line(r) for r in world.cloud.records.values() if "outcome" in str(r["id"])]
    assert [h.split(" [")[0] for h in held] == [
        "03:10:31 site/mombasa site-isolation@v1",
        "03:10:31 site/mombasa wait@v1",
        "03:35:11 site/mombasa site-rejoin@v1",
        "03:35:11 site/mombasa resume@v1",
    ]
    assert len(shipped) == 2
    assert len(mombasa(world)) == 8


def test_a_reclaim_drains_then_fences_then_releases_the_partner_vm() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    controller.told(KILIMA, at(600))
    run(world, controller, (0, 10))
    kube = world.kubes["mombasa"]
    assert [p.resource for p in kube.pods if p.resource == KILIMA] == []

    run(world, controller, (20, 300))
    endpoints = controller.endpoints
    assert (endpoints.spire.banned, endpoints.talos.wiped) == ({KILIMA}, {KILIMA})
    assert endpoints.kms.disabled == {KEY}
    assert KILIMA not in kube.nodes
    assert KILIMA not in world.released
    run(world, controller, (310, 480))

    assert world.released == {KILIMA}
    assert [line(r).split(" [")[0] for r in controller.log if r["id"].endswith("outcome")] == [
        "03:00:01 site/mombasa drain-on-reclaim@v1",
        "03:05:01 site/mombasa fence@v1",
        "03:08:01 site/mombasa release@v1",
    ]


def test_a_reclaim_during_the_cut_runs_at_the_isolated_site_and_reaches_the_cloud_on_rejoin() -> (
    None
):
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    run(world, controller, (0, 30))
    controller.told(KILIMA, at(640))
    run(world, controller, (40, 520))
    assert world.released == {KILIMA}
    assert not world.cloud.records

    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    run(world, controller, (530, 850))

    merged = [line(r) for r in world.cloud.records.values() if r["data"]["phase"] == "outcome"]
    assert merged == [
        "03:00:31 site/mombasa site-isolation@v1 [cloud.unreachable] enter-isolated-mode: "
        "0/3 regions reachable for 30 s on all paths -> applied",
        "03:00:31 site/mombasa wait@v1 [blocked] wait: site-bound services keep running on site "
        "resources; nothing moves, as a copy in the cloud could not reach the node -> applied",
        "03:00:41 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: "
        f"cordon {KILIMA} and evict 4 of its 4 pods, each service at or above its min; "
        "fence at 03:05:41, release at 03:08:41 -> applied",
        f"03:05:41 site/mombasa fence@v1 [reclaim.fence-time] fence: {KILIMA} holds 0 pods of "
        "Share's, deleted; identity revoked; node deleted; disk key revoked; disks wiped "
        "-> applied",
        f"03:08:41 site/mombasa release@v1 [reclaim.release-time] release-lease: release {KILIMA}: "
        "its owner reclaimed it -> applied",
        "03:13:51 site/mombasa site-rejoin@v1 [cloud.reachable] exit-isolated-mode: data path "
        "stable for 300 s -> applied",
        "03:13:51 site/mombasa resume@v1 [unblocked] resume: the site rejoined the cloud "
        "-> applied",
    ]
    drain = next(r for r in controller.log if r["data"]["rule"] == "drain-on-reclaim@v1")
    assert drain["data"]["observed"]["isolated"] is True
    assert drain["data"]["blast_radius"] == {"pods_moved": 4}


def test_the_isolation_record_has_every_field_of_the_platform_decision_event() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    run(world, controller, (0, 30))

    assert controller.log[0] == {
        "specversion": "1.0",
        "type": "platform.decision",
        "source": "/controllers/site/mombasa",
        "id": "mba-20261003T030001Z-0001/proposed",
        "time": "2026-10-03T03:00:31Z",
        "sequence": "1",
        "data": {
            "decision_id": "mba-20261003T030001Z-0001",
            "phase": "proposed",
            "at": "2026-10-03T03:00:31Z",
            "controller": "site/mombasa",
            "trigger": "cloud.unreachable",
            "observed": {
                "reachable": 0,
                "regions": 3,
                "seconds": 30,
                "data_links_up": 0,
                "isolated": False,
            },
            "rule": "site-isolation@v1",
            "action": "enter-isolated-mode",
            "subject": "mombasa",
            "reason": "0/3 regions reachable for 30 s on all paths",
            "blast_radius": {"pods_moved": 0},
            "rollback": "exit-isolated-mode",
            "page": False,
        },
    }
    assert [r["sequence"] for r in controller.log] == ["1", "2", "3", "4"]


def test_a_refused_decision_is_logged_and_not_acted_on(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    rule = next(r for r in INVENTORY.rules if r.id == "drain-on-reclaim")
    refused = Decision(
        rule=rule,
        subject=KILIMA,
        reason="not here",
        observed={},
        outcome=Outcome.REFUSED,
        resource=KILIMA,
        page=True,
    )

    def refuse(state: State) -> tuple[list[Decision], Memory]:
        return [refused], state.memory

    monkeypatch.setattr("sharectl.controller.decide", refuse)
    run(world, controller, (0, 0))

    assert outcomes(controller) == [
        "03:00:01 site/mombasa drain-on-reclaim@v1 [resource.reclaimed] cordon-and-drain: "
        "not here -> refused"
    ]
    assert controller.log[1]["data"]["page"] is True
    assert not [text for _, text in world.notes if " -> " in text]


def test_one_uplinks_link_down_with_the_other_up_is_not_isolation() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ["uplink-a", "oob"], down=True)
    run(world, controller, (0, 120))

    assert not controller.log


def fail_the_cells(world: World) -> None:
    """Every service in every cloud region down: no node schedulable, every pod evicted."""
    for region in ("E1", "E2", "E3"):
        cell = world.kubes[region]
        for node in list(cell.nodes):
            cell.cordon(node)
        cell.evict(tuple(cell.pods))


def test_every_region_session_down_with_the_links_up_isolates_and_says_so() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    fail_the_cells(world)
    world.cut("mombasa", ["E1", "E2", "E3"], ALL_PATHS, down=True, layer=Layer.SESSION)
    run(world, controller, (0, 30))

    assert outcomes(controller)[0] == (
        "03:00:31 site/mombasa site-isolation@v1 [cloud.unreachable] enter-isolated-mode: "
        "0/3 regions reachable for 30 s on all paths -> applied"
    )
    assert controller.log[0]["data"]["observed"]["data_links_up"] == 2


def test_a_cloud_service_failing_changes_nothing() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    fail_the_cells(world)
    run(world, controller, (0, 120))

    assert not controller.log


def test_the_controller_reports_its_first_poll_then_every_5_minutes_while_nothing_changes() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    run(world, controller, (0, 600))

    assert polls(world) == [(at(0), POLL_UP), (at(300), POLL_UP), (at(600), POLL_UP)]
    assert not controller.log


def test_a_poll_that_differs_is_reported_at_once_and_the_5_minutes_count_from_it() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    run(world, controller, (0, 20))
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    run(world, controller, (30, 330))

    assert polls(world) == [(at(0), POLL_UP), (at(30), POLL_DOWN), (at(330), POLL_DOWN)]


def test_a_session_down_on_a_link_that_is_up_shows_in_the_poll() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["E1"], ["uplink-b"], down=True, layer=Layer.SESSION)
    run(world, controller, (0, 0))

    assert polls(world) == [
        (at(0), POLL_UP.replace("198.51.100.9 up, bgp 3/3", "198.51.100.9 up, bgp 2/3"))
    ]


def test_the_poll_texts_and_how_often_it_repeats_are_the_inventory_s() -> None:
    texts = {
        **INVENTORY.lines,
        "poll": "P $controller $node $reachable $regions [$paths]",
        "poll_link_up": "U $path $peer $sessions $regions",
        "poll_link_down": "D $path $peer",
    }
    params = replace(INVENTORY.params, poll_report_minutes=1)
    world = World(replace(INVENTORY, lines=texts, params=params), T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ["uplink-a"], down=True)
    run(world, controller, (0, 60))

    assert [(t, text) for t, text in world.notes if text.startswith("P ")] == [
        (
            at(s),
            "P site/mombasa mba-node-1 3 3 "
            "[D uplink-a 192.0.2.9; U uplink-b 198.51.100.9 3 3; U oob 203.0.113.9 3 3]",
        )
        for s in (0, 60)
    ]


def test_the_controller_sees_its_site_only_through_its_node_s_gnmi_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    get = Gnmi.get

    def silent(self: Gnmi, request: Message) -> Message:
        response = get(self, request)
        for update in response["notification"][0]["update"]:
            if update["path"]["elem"][-1]["name"] == "session-state":
                update["val"]["jsonIetfVal"] = base64.b64encode(b'"DOWN"').decode()
        return response

    monkeypatch.setattr(Gnmi, "get", silent)
    run(world, controller, (0, 30))

    assert next(line(r) for r in controller.log if "outcome" in str(r["id"])).startswith(
        "03:00:31 site/mombasa site-isolation@v1 [cloud.unreachable] enter-isolated-mode: "
        "0/3 regions reachable for 30 s on all paths"
    )
    assert polls(world)[0] == (at(0), POLL_DOWN)


def eviction(pod: str) -> str:
    return (
        f"site/mombasa POST /api/v1/namespaces/share/pods/{pod}/eviction "
        f'{{"apiVersion": "policy/v1", "kind": "Eviction", '
        f'"metadata": {{"name": "{pod}", "namespace": "share"}}}} -> 201'
    )


def test_a_reclaim_makes_the_calls_a_drain_a_fence_and_a_release_make_and_shows_each() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    controller.told(KILIMA, at(600))
    run(world, controller, (0, 480))

    assert [(t, text) for t, text in world.notes if " -> " in text] == [
        (
            at(0),
            "site/mombasa PATCH /api/v1/nodes/mba-kilima-01 "
            '{"spec": {"unschedulable": true}} -> 200',
        ),
        *(
            (at(0), eviction(f"{s}-1"))
            for s in ("sdn-controller", "aaa", "config-push", "telemetry-collector")
        ),
        (
            at(300),
            "site/mombasa spire.api.server.agent.v1.Agent/BanAgent "
            '{"id": {"trustDomain": "share.example", '
            '"path": "/spire/agent/k8s_psat/mombasa/mba-kilima-01"}} -> OK',
        ),
        (at(300), "site/mombasa DELETE /api/v1/nodes/mba-kilima-01 -> 200"),
        (at(300), f'site/mombasa TrentService.DisableKey {{"KeyId": "{KEY}"}} -> 200'),
        (
            at(300),
            "site/mombasa machine.MachineService/Reset on mba-kilima-01 "
            '{"graceful": false, "reboot": false, "mode": "ALL"} -> OK',
        ),
        (at(480), "site/mombasa POST /v1/leases/mba-kilima-01/release -> 204"),
    ]


def test_a_pod_the_drain_could_not_evict_is_deleted_at_once_at_the_fence() -> None:
    services = tuple(replace(s, min=2) if s.id == "aaa" else s for s in INVENTORY.services)
    world = World(replace(INVENTORY, services=services), T0)
    controller = Controller(world, MOMBASA, T0)
    controller.told(KILIMA, at(600))
    run(world, controller, (0, 300))

    calls = [(t, text) for t, text in world.notes if " -> " in text]
    assert eviction("aaa-1") not in [text for _, text in calls]
    assert (
        at(300),
        "site/mombasa DELETE /api/v1/namespaces/share/pods/aaa-1?gracePeriodSeconds=0 -> 200",
    ) in calls
    assert next(text for t, text in calls if t == at(300)).startswith(
        "site/mombasa DELETE /api/v1/namespaces/"
    )


def test_a_link_its_node_reports_down_reaches_no_region_whatever_bgp_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    get = Gnmi.get

    def bfd_down(self: Gnmi, request: Message) -> Message:
        response = get(self, request)
        for update in response["notification"][0]["update"]:
            if update["path"]["elem"][0]["name"] == "bfd" and (
                update["path"]["elem"][-1]["name"] == "session-state"
            ):
                update["val"]["jsonIetfVal"] = base64.b64encode(b'"DOWN"').decode()
        return response

    monkeypatch.setattr(Gnmi, "get", bfd_down)
    run(world, controller, (0, 30))

    assert polls(world)[0] == (at(0), POLL_DOWN)
    assert next(line(r) for r in controller.log if "outcome" in str(r["id"])).startswith(
        "03:00:31 site/mombasa site-isolation@v1"
    )


def test_a_management_path_alone_coming_back_does_not_rejoin_the_site() -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    run(world, controller, (0, 30))
    world.cut("mombasa", ["*"], ["oob"], down=False)
    run(world, controller, (40, 400))

    assert [line(r).split(" [")[0] for r in controller.log if r["id"].endswith("outcome")] == [
        "03:00:31 site/mombasa site-isolation@v1",
        "03:00:31 site/mombasa wait@v1",
    ]


def test_pods_are_read_from_the_pod_list_not_the_world(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    controller.told(KILIMA, at(600))

    real = Kubernetes.request

    def fewer(self: Kubernetes, method: str, path: str, *sent: Message) -> tuple[int, Message]:
        status, body = real(self, method, path, *sent)
        if path == "/api/v1/pods":
            items = [p for p in body["items"] if p["metadata"]["name"] != "aaa-1"]
            return status, {**body, "items": items}
        return status, body

    monkeypatch.setattr(Kubernetes, "request", fewer)
    run(world, controller, (0, 0))

    assert "evict 3 of its 3 pods" in outcomes(controller)[0]


def test_a_reclaim_notice_told_to_the_controller_is_acted_on_with_no_lease_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    sent: list[tuple[str, str]] = []
    real = Leases.request

    def spy(self: Leases, method: str, path: str) -> tuple[int, Message]:
        sent.append((method, path))
        return real(self, method, path)

    monkeypatch.setattr(Leases, "request", spy)
    controller.told(KILIMA, at(600))
    run(world, controller, (0, 480))

    assert [o.split(" [")[0] for o in outcomes(controller)] == [
        "03:00:01 site/mombasa drain-on-reclaim@v1",
        "03:05:01 site/mombasa fence@v1",
        "03:08:01 site/mombasa release@v1",
    ]
    assert sent == [("POST", "/v1/leases/mba-kilima-01/release")]


def test_kubernetes_is_read_only_from_the_owner_s_notice_to_the_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    reads: list[datetime] = []
    real = Kubernetes.request

    def spy(self: Kubernetes, method: str, path: str, *sent: Message) -> tuple[int, Message]:
        if method == "GET":
            reads.append(world.now)
        return real(self, method, path, *sent)

    monkeypatch.setattr(Kubernetes, "request", spy)
    run(world, controller, (0, 60))
    quiet = list(reads)
    controller.told(KILIMA, at(670))
    run(world, controller, (70, 700))

    assert not quiet
    assert reads == [at(s) for s in range(70, 551, 10)]


@pytest.mark.parametrize(
    ("wait", "read"), [(("pending", "aaa"), True), (("isolated", "mombasa"), False)]
)
def test_pods_waiting_for_room_keep_kubernetes_read_and_isolation_does_not(
    monkeypatch: pytest.MonkeyPatch, *, wait: tuple[str, str], read: bool
) -> None:
    world = World(INVENTORY, T0)
    controller = Controller(world, MOMBASA, T0)
    controller.memory = replace(controller.memory, waits=frozenset({wait}))
    reads: list[str] = []
    real = Kubernetes.request

    def spy(self: Kubernetes, method: str, path: str, *sent: Message) -> tuple[int, Message]:
        reads.append(path)
        return real(self, method, path, *sent)

    monkeypatch.setattr(Kubernetes, "request", spy)
    run(world, controller, (0, 0))

    assert reads == (["/api/v1/pods"] if read else [])
