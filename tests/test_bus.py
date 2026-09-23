import pytest

from workflows.bus import QUEUE_TOPIC, SUBSCRIBER_QUEUE_SIZE, BusEvent, EventBus, job_topic
from workflows.db import JsonObject


async def test_subscribers_receive_events_for_their_topic_only() -> None:
    bus = EventBus()
    event = BusEvent("status", {"job_id": 1, "status": "queued"})

    with bus.subscribe(QUEUE_TOPIC) as queue, bus.subscribe(job_topic(2)) as other:
        bus.publish(QUEUE_TOPIC, event)
        assert await queue.get() == event
        assert other.empty()

    bus.publish(QUEUE_TOPIC, event)
    assert queue.empty()


async def test_a_full_subscriber_drops_events_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus()
    event = BusEvent("status", {"job_id": 1, "status": "queued"})

    with bus.subscribe(QUEUE_TOPIC) as queue:
        for _ in range(SUBSCRIBER_QUEUE_SIZE + 3):
            bus.publish(QUEUE_TOPIC, event)

        assert queue.qsize() == SUBSCRIBER_QUEUE_SIZE
    assert len(caplog.records) == 1


def test_snapshot_is_built_once_per_publish() -> None:
    bus = EventBus()
    builds: list[int] = []

    def build() -> JsonObject:
        builds.append(1)
        return {"builds": len(builds)}

    first = bus.snapshot(QUEUE_TOPIC, build)
    assert bus.snapshot(QUEUE_TOPIC, build) == first
    bus.publish(QUEUE_TOPIC, BusEvent("status", {}))

    assert bus.snapshot(QUEUE_TOPIC, build) == {"builds": 2}
    assert len(builds) == 2
