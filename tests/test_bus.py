from workflows.bus import QUEUE_TOPIC, BusEvent, EventBus, job_topic


async def test_subscribers_receive_events_for_their_topic_only() -> None:
    bus = EventBus()
    event = BusEvent("status", {"job_id": 1, "status": "queued"})

    with bus.subscribe(QUEUE_TOPIC) as queue, bus.subscribe(job_topic(2)) as other:
        bus.publish(QUEUE_TOPIC, event)
        assert await queue.get() == event
        assert other.empty()

    bus.publish(QUEUE_TOPIC, event)
    assert queue.empty()
