from datetime import timedelta

from builders import ALL_PATHS, DATA_PATHS, INVENTORY, T0, at
from sharectl.model import DesiredState
from sharectl.world import World
lazy from sharectl.stubs import Batch

RECORD = {"id": "x/proposed", "type": "platform.decision"}


def ticks(world: World, start: int, end: int) -> None:
    for seconds in range(start, end + 1, 10):
        world.advance(at(seconds))


def watch(world: World) -> list[Batch]:
    batches: list[Batch] = []
    receive = world.cloud.receive
    world.cloud.receive = lambda batch, at: (batches.append(batch), receive(batch, at))[1]
    return batches


def test_while_a_data_path_is_stable_a_record_ships_at_once() -> None:
    world = World(INVENTORY, T0)
    world.advance(at(0))
    world.site_logs["mombasa"].write(RECORD)
    ticks(world, 10, 20)

    assert world.cloud.records == {"x/proposed": RECORD}


def test_the_site_log_holds_until_a_data_path_is_stable_for_rejoin_stable_seconds() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    world.advance(at(0))
    world.site_logs["mombasa"].write(RECORD)
    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    ticks(world, 10, 310)
    assert not world.cloud.records

    world.advance(at(320))
    assert world.cloud.records == {"x/proposed": RECORD}


def test_a_management_path_alone_never_ships() -> None:
    world = World(INVENTORY, T0)
    world.cut("mombasa", ["*"], DATA_PATHS, down=True)
    world.advance(at(0))
    world.site_logs["mombasa"].write(RECORD)
    ticks(world, 10, 900)

    assert not world.cloud.records


def test_shipping_resumes_with_the_held_intervals_and_the_last_the_cloud_already_had() -> None:
    world = World(INVENTORY, T0)
    batches = watch(world)
    ticks(world, 0, 1190)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    ticks(world, 1200, 1490)
    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    ticks(world, 1500, 1810)

    mombasa = [b for b in batches if b.site == "mombasa"]
    resumed = [b for b in mombasa if b.rejoin]
    assert [f"{i.start:%H:%M}" for b in resumed for i in b.resent] == ["03:00", "03:05", "03:10"]
    assert [f"{i.start:%H:%M}" for b in resumed for i in b.intervals] == ["03:15", "03:20", "03:25"]
    assert not [b for b in mombasa if b.resent and not b.rejoin]


def test_the_resend_takes_the_last_intervals_of_each_handoff_and_counter() -> None:
    world = World(INVENTORY, T0)
    new = DesiredState(handoff="mba-node-1/eth-1-13", generation=1, agreement="b", version=1)
    confirmed = world.config_push.receive("mombasa", new, sequence=1, at=T0 - timedelta(minutes=10))
    world.site_logs["mombasa"].write(confirmed or {})
    batches = watch(world)
    ticks(world, 0, 1190)
    world.cut("mombasa", ["*"], ALL_PATHS, down=True)
    ticks(world, 1200, 1490)
    world.cut("mombasa", ["*"], ALL_PATHS, down=False)
    ticks(world, 1500, 1810)

    resent = [i for b in batches if b.site == "mombasa" and b.rejoin for i in b.resent]
    assert [(i.handoff[-2:], f"{i.start:%H:%M}") for i in resent] == [
        ("12", "03:00"),
        ("12", "03:05"),
        ("12", "03:10"),
        ("13", "03:00"),
        ("13", "03:05"),
        ("13", "03:10"),
    ]
