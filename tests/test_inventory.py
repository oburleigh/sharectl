from ipaddress import IPv4Address
from pathlib import Path

import pytest
from pydantic import ValidationError

from sharectl.inventory import load
from sharectl.model import Cluster, Fence, Lease, Role, Trust

INVENTORY = Path(__file__).parent.parent / "inventory"


def test_loads_the_declared_estate() -> None:
    inventory = load(INVENTORY)

    assert [s.code for s in inventory.sites if s.role is Role.POP] == ["bcn", "mrs", "mba"]
    assert [s.id for s in inventory.sites if s.role is Role.REGION] == ["E1", "E2", "E3"]
    assert [s.id for s in inventory.sites if s.role is Role.EDGE] == ["cloudflare"]
    leased = [(r.site, r.trust, r.lease) for r in inventory.resources if r.lease]
    fence = Fence(
        identity="/spire/agent/k8s_psat/mombasa/mba-kilima-01",
        disk_key="arn:aws:kms:eu-west-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab",
    )
    assert leased == [
        ("mombasa", Trust.ATTESTED, Lease(notice_minutes=10, paid_by="share", fence=fence))
    ]
    assert len(inventory.services) == 15
    assert inventory.params.confirm_seconds == 30
    assert inventory.cluster == Cluster(namespace="share", trust_domain="share.example")
    assert {n.id: n.peers for n in inventory.nodes}["mba-node-1"] == {
        "uplink-a": IPv4Address("192.0.2.9"),
        "uplink-b": IPv4Address("198.51.100.9"),
        "oob": IPv4Address("203.0.113.9"),
    }
    assert {s.id: s.peers for s in inventory.sites if s.peers} == {
        region: {
            "uplink-a": IPv4Address(f"192.0.2.{host}"),
            "uplink-b": IPv4Address(f"198.51.100.{host}"),
            "oob": IPv4Address(f"203.0.113.{host}"),
        }
        for region, host in (("E1", 101), ("E2", 102), ("E3", 103))
    }


def test_rejects_a_peer_that_is_not_an_address(tmp_path: Path) -> None:
    for name in ("sites", "services", "rules"):
        text = (INVENTORY / f"{name}.toml").read_text()
        (tmp_path / f"{name}.toml").write_text(text.replace('"192.0.2.9"', '"192.0.2.309"'))

    with pytest.raises(ValidationError) as error:
        load(tmp_path)

    assert [(e["loc"], e["msg"]) for e in error.value.errors()] == [
        (("node", 2, "peers", "uplink-a"), "Input is not a valid IPv4 address")
    ]


def test_rejects_a_bad_file_naming_the_field_and_the_reason(tmp_path: Path) -> None:
    for name in ("sites", "services", "rules"):
        text = (INVENTORY / f"{name}.toml").read_text()
        (tmp_path / f"{name}.toml").write_text(text.replace('code = "mba"', 'code = "MBA/.."'))

    with pytest.raises(ValidationError) as error:
        load(tmp_path)

    assert [(e["loc"], e["msg"]) for e in error.value.errors()] == [
        (("site", 2, "code"), "String should match pattern '^[a-z0-9-]+$'")
    ]


def test_rejects_a_lease_with_nothing_for_the_fence_to_revoke(tmp_path: Path) -> None:
    for name in ("sites", "services", "rules"):
        text = (INVENTORY / f"{name}.toml").read_text()
        head, _, tail = text.partition("[resource.lease.fence]")
        if tail:
            text = head + tail.split("\n\n", 1)[1]
        (tmp_path / f"{name}.toml").write_text(text)

    with pytest.raises(ValidationError) as error:
        load(tmp_path)

    assert [(e["loc"], e["msg"]) for e in error.value.errors()] == [
        (("resource", 9, "lease", "fence"), "Field required")
    ]
