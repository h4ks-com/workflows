from dataclasses import replace
from datetime import datetime
from datetime import timedelta

import pytest

from workflows.api.views import JobEventView
from workflows.api.views import JobView
from workflows.api.views import ProgressView
from workflows.db import JobStatus
from workflows.db import utcnow
from workflows.jobtypes.catalog import Step
from workflows.jobtypes.catalog import retired_type
from workflows.web.render import relative_time
from workflows.web.render import short_time
from workflows.web.render import step_rows

NOW = datetime(2026, 9, 25, 12, 0)


def test_each_step_shows_how_long_it_took() -> None:
    steps = (Step("fetch", 5), Step("transcribe", 90), Step("store", 5))
    midi = replace(retired_type("midi"), steps=steps)
    job = JobView(
        id=1,
        type="midi",
        status=JobStatus.SUCCEEDED,
        owner=None,
        quote=1,
        params={},
        progress=ProgressView(step="store", done=None, total=None, fraction=1.0),
        result=None,
        removed_at=None,
        error=None,
        created_at=NOW,
        queued_at=NOW,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=92),
    )
    events = [
        JobEventView(kind="step", data={"step": name}, created_at=NOW + timedelta(seconds=at))
        for name, at in [("fetch", 0), ("transcribe", 1), ("transcribe", 60), ("store", 91)]
    ]

    assert [row.when for row in step_rows(job, midi, events)] == ["0:01", "1:30", "0:01"]


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
