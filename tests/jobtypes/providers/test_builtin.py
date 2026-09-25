import httpx
import pytest
from pydantic import ValidationError

from conftest import SONG_URL, FakeProber
from workflows.jobtypes.catalog import HttpExecutor, JobType
from workflows.jobtypes.forms import form_from_schema
from workflows.jobtypes.probe import Probe
from workflows.jobtypes.providers.builtin import BUILTINS, Builtin, builtin_job_types

PROBE = Probe(100.0, "Some Song")


def job_types(n8n_url: str = "https://n8n.test") -> dict[str, JobType]:
    return {
        job_type.name: job_type
        for job_type in builtin_job_types(httpx.AsyncClient(), n8n_url, "key")
    }


@pytest.mark.parametrize("builtin", BUILTINS, ids=lambda builtin: builtin.name)
def test_forms_reproduce_the_params_schema(builtin: Builtin) -> None:
    schema = builtin.params.model_json_schema()
    assert form_from_schema(schema).json_schema() == schema


def test_prices_follow_the_type_formulas() -> None:
    types = job_types()
    parody = types["parody"].validate({"url": SONG_URL})
    voice = types["voice"].validate({"url": SONG_URL, "voice_url": SONG_URL})
    assert types["parody"].price.evaluate(parody, PROBE) == 240
    assert types["voice"].price.evaluate(voice, PROBE) == 290
    assert types["song"].price.evaluate(types["song"].validate({"prompt": "cats"}), None) == 105
    minimax = types["song"].validate({"prompt": "cats", "model": "minimax", "seconds": 100})
    assert types["song"].price.evaluate(minimax, None) == 240
    assert types["podcast"].price.evaluate(types["podcast"].validate({"prompt": "c"}), None) == 660
    assert types["image"].price.evaluate(types["image"].validate({"prompt": "a"}), None) == 40
    large = types["image"].validate({"prompt": "a", "size": "large"})
    assert types["image"].price.evaluate(large, None) == 80


async def test_quote_probes_only_types_with_media() -> None:
    types = job_types()
    prober = FakeProber()

    parody = await types["parody"].quote(types["parody"].validate({"url": SONG_URL}), prober)
    song = await types["song"].quote(types["song"].validate({"prompt": "cats"}), prober)

    assert (parody.credits, parody.estimate_seconds, parody.probe) == (240, 240, PROBE)
    assert song.probe is None


def test_types_without_n8n_are_unavailable() -> None:
    types = job_types("")

    assert list(types) == ["parody", "song", "voice", "podcast", "image"]
    assert not types["parody"].available
    assert types["podcast"].step_names() == ["research", "cast", "bed", "write", "speak", "mix"]


def test_executors_point_at_the_n8n_webhooks() -> None:
    executor = job_types()["parody"].executor

    assert isinstance(executor, HttpExecutor)
    assert executor.url == "https://n8n.test/webhook/workflows-parody"


def test_image_reference_is_optional_and_uploadable() -> None:
    image = job_types()["image"]
    field = next(spec for spec in image.form.fields if spec.name == "reference_url")

    assert (field.accept, field.show_when, field.required) == (
        "image/*",
        {"model": "flux-klein"},
        False,
    )
    assert image.validate({"prompt": "a cat"})["reference_url"] is None


def test_image_reference_needs_flux_klein() -> None:
    image = job_types()["image"]

    with pytest.raises(ValidationError, match="flux-klein"):
        image.validate({"prompt": "a cat", "reference_url": "https://x/cat.png"})
    params = image.validate(
        {"prompt": "a cat", "model": "flux-klein", "reference_url": "https://x/cat.png"}
    )
    assert params["reference_url"] == "https://x/cat.png"
