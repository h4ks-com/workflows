from dataclasses import replace

import pytest
from pydantic import JsonValue
from pydantic import ValidationError

from workflows.jobtypes.forms import FieldSpec
from workflows.jobtypes.forms import Form
from workflows.jobtypes.forms import condition_holds
from workflows.jobtypes.forms import describe_condition
from workflows.jobtypes.forms import form_from_schema

FORM = Form(
    "Echo",
    (
        FieldSpec("prompt", "Prompt", "", "textarea", True, min_length=1, max_length=5),
        FieldSpec("mode", "Mode", "", "select", False, enum=("a", "b"), default="a"),
        FieldSpec(
            "count",
            "Count",
            "",
            "number",
            False,
            value_type="integer",
            minimum=1,
            maximum=3,
            default=1,
        ),
        FieldSpec(
            "link",
            "Link",
            "",
            "text",
            False,
            value_type="url",
            accept="image/*",
            show_when={"mode": "b"},
        ),
        FieldSpec("loud", "Loud", "", "checkbox", False, value_type="boolean", default=False),
    ),
)


def test_validate_fills_defaults_and_keeps_json() -> None:
    assert FORM.validate({"prompt": "hi"}) == {
        "prompt": "hi",
        "mode": "a",
        "count": 1,
        "link": None,
        "loud": False,
    }


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"prompt": ""},
        {"prompt": "too long"},
        {"prompt": "hi", "mode": "c"},
        {"prompt": "hi", "count": 4},
        {"prompt": "hi", "extra": 1},
        {"prompt": "hi", "mode": "b", "link": "not a url"},
        {"prompt": "hi", "link": "https://x/cat.png"},
    ],
)
def test_validate_is_strict(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FORM.validate(raw)  # type: ignore[arg-type]


def test_a_shown_field_is_accepted_when_its_condition_holds() -> None:
    params = FORM.validate({"prompt": "hi", "mode": "b", "link": "https://x/cat.png"})
    assert params["link"] == "https://x/cat.png"


def test_the_schema_round_trips_through_the_json_schema_driver() -> None:
    assert form_from_schema(FORM.json_schema()).fields == FORM.fields


MOVES = FieldSpec(
    "moves",
    "Moves",
    "",
    "checklist",
    False,
    value_type="list",
    enum=("Idle", "Walk", "Run"),
    max_length=2,
    default=[],
)
CHECKLIST = Form(
    "Rig",
    (
        FieldSpec("mode", "Mode", "", "select", False, enum=("a", "b"), default="a"),
        MOVES,
        FieldSpec(
            "extras",
            "Extras",
            "",
            "checklist",
            True,
            value_type="list",
            enum=("Dance", "Roll"),
            min_length=1,
            default=[],
            show_when={"mode": "b"},
        ),
    ),
)


def test_a_checklist_keeps_its_ticked_options() -> None:
    assert CHECKLIST.validate({"moves": ["Run", "Idle"]}) == {
        "mode": "a",
        "moves": ["Run", "Idle"],
        "extras": None,
    }
    assert CHECKLIST.validate({})["moves"] == []


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ({"moves": ["Fly"]}, "choose only listed options, drop Fly"),
        ({"moves": ["Idle", "Idle"]}, "choose each option once"),
        ({"moves": ["Idle", "Walk", "Run"]}, "choose at most 2"),
        ({"moves": "Idle"}, "list"),
        ({"mode": "b"}, "fill in extras"),
        ({"mode": "b", "extras": []}, "fill in extras"),
        ({"extras": ["Roll"]}, "extras needs mode b"),
    ],
)
def test_a_checklist_validates_its_ticks(raw: dict[str, JsonValue], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        CHECKLIST.validate(raw)


def test_a_required_checklist_needs_its_minimum() -> None:
    required = Form("Rig", (replace(MOVES, required=True, min_length=2),))

    with pytest.raises(ValidationError, match="choose at least 2"):
        required.validate({"moves": ["Idle"]})
    with pytest.raises(ValidationError, match="Field required"):
        required.validate({})


def test_a_shown_checklist_accepts_its_ticks() -> None:
    assert CHECKLIST.validate({"mode": "b", "extras": ["Roll"]})["extras"] == ["Roll"]


def test_a_checklist_schema_lists_its_options_and_limit() -> None:
    moves = CHECKLIST.json_schema()["properties"]
    assert isinstance(moves, dict)
    assert moves["moves"] == {
        "default": [],
        "description": "",
        "items": {"type": "string", "enum": ["Idle", "Walk", "Run"]},
        "maxItems": 2,
        "title": "Moves",
        "type": "array",
        "uniqueItems": True,
    }
    _, moves_again, extras_again = form_from_schema(CHECKLIST.json_schema()).fields
    assert moves_again == MOVES
    assert (extras_again.kind, extras_again.enum) == ("checklist", ("Dance", "Roll"))


@pytest.mark.parametrize(
    ("condition", "value", "holds", "words"),
    [
        ("a", "a", True, "mode a"),
        ({"not": "a"}, "b", True, "mode other than a"),
        ({"one_of": ["a", "b"]}, "c", False, "mode a or b"),
        ({"not_one_of": ["a", "b"]}, "c", True, "mode other than a or b"),
        ({"empty": True}, "", True, "mode empty"),
        ({"empty": False}, None, False, "mode filled in"),
    ],
)
def test_show_conditions(condition: JsonValue, value: str | None, holds: bool, words: str) -> None:
    assert condition_holds(condition, value) is holds
    assert describe_condition("mode", condition) == words
