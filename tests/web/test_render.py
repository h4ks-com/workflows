from datetime import timedelta

from workflows.db import utcnow
from workflows.web.render import relative_time, short_time


def test_short_time_formats_a_timestamp() -> None:
    assert short_time(None) == "never"
    assert short_time(utcnow().replace(2026, 1, 2, 3, 4)) == "Jan 02 03:04"


def test_relative_time_buckets_by_age() -> None:
    now = utcnow()
    assert relative_time(None) == "never"
    assert relative_time(now) == "just now"
    assert relative_time(now - timedelta(minutes=5)) == "5m ago"
    assert relative_time(now - timedelta(hours=3)) == "3h ago"
    assert relative_time(now - timedelta(days=3)) == short_time(now - timedelta(days=3))
