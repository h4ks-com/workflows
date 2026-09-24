import pytest

from workflows.pricing import PriceError, PriceRule
from workflows.probe import Probe, ProbeError


@pytest.mark.parametrize(
    ("expression", "params", "credits"),
    [
        ("40", {}, 40),
        ("80 if size == 'large' else 40", {"size": "large"}, 80),
        ("80 if size == 'large' else 40", {"size": "normal"}, 40),
        (
            "(0.3 if model == 'ace-step' else 1.8) * seconds + 60",
            {"model": "ace-step", "seconds": 150},
            105,
        ),
        ("90 * minutes + 120", {"minutes": 6}, 660),
        ("10 if minutes >= 5 else 5", {"minutes": 5}, 10),
        ("-minutes + 20 / 4", {"minutes": 1}, 4),
        ("30 if style != 'lofi' else 10", {"style": "lofi"}, 10),
    ],
)
def test_prices_evaluate_the_expression(
    expression: str, params: dict[str, object], credits: int
) -> None:
    assert PriceRule(expression).evaluate(params, None) == credits  # type: ignore[arg-type]


def test_duration_reads_the_probed_media() -> None:
    rule = PriceRule("1.2 * duration(url) + 120")

    assert rule.media_field == "url"
    assert rule.evaluate({"url": "https://x"}, Probe(100.0, "Song")) == 240
    with pytest.raises(ProbeError):
        rule.evaluate({"url": "https://x"}, None)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('true')",
        "open('x')",
        "duration(url, voice)",
        "duration(url) + duration(voice)",
        "seconds ** 2",
        "[1, 2]",
        "minutes.real",
        "1 if",
        "a < b < c",
    ],
)
def test_prices_reject_anything_but_arithmetic(expression: str) -> None:
    with pytest.raises(PriceError):
        PriceRule(expression)


def test_prices_reject_unknown_fields_and_text_arithmetic() -> None:
    with pytest.raises(PriceError, match="minutes"):
        PriceRule("minutes * 2").evaluate({}, None)
    with pytest.raises(PriceError, match="not a number"):
        PriceRule("style * 2").evaluate({"style": "lofi"}, None)
