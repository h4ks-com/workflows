from datetime import datetime, timedelta

import pytest

from workflows.db import utcnow
from workflows.web.render import relative_time, short_time

NOW = datetime(2026, 9, 25, 12, 0)


def test_short_time_formats_a_timestamp() -> None:
    assert short_time(None) == "never"
    assert short_time(utcnow().replace(2026, 1, 2, 3, 4)) == "Jan 02 03:04"


@pytest.mark.parametrize(
    ("age", "text"),
    [
        (timedelta(seconds=20), "just now"),
        (timedelta(minutes=4), "4 min ago"),
        (timedelta(hours=1), "1 hour ago"),
        (timedelta(hours=3), "3 hours ago"),
        (timedelta(hours=30), "yesterday"),
        (timedelta(days=3), "3 days ago"),
        (timedelta(days=8), "1 week ago"),
        (timedelta(days=20), "2 weeks ago"),
        (timedelta(days=60), "Jul 27"),
        (timedelta(days=300), "Nov 29, 2025"),
    ],
)
def test_relative_time_reads_like_a_person_would_say_it(age: timedelta, text: str) -> None:
    assert relative_time(NOW - age, NOW) == text


def test_relative_time_of_nothing_is_never() -> None:
    assert relative_time(None) == "never"
