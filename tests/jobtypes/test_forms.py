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
