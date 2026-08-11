from pathlib import Path

from flux_gallery.config import load_prompts_config

PROMPTS_YAML = Path(__file__).resolve().parent.parent / "config" / "prompts.yaml"


def test_load_prompts_config_has_six_screens_with_independent_meta_prompts():
    config = load_prompts_config(PROMPTS_YAML)
    assert len(config.screens) == 6
    ids = {screen.id for screen in config.screens}
    assert ids == {"F", "B", "C", "D", "A", "E"}
    meta_prompts = [screen.meta_prompt for screen in config.screens]
    assert len(set(meta_prompts)) == 6


def test_base_config_defaults_match_prompts_yaml():
    config = load_prompts_config(PROMPTS_YAML)
    assert config.base.model == "black-forest-labs/FLUX.1-schnell"
    assert config.base.gemini_model == "gemini-flash-latest"
    assert config.base.num_inference_steps == 4
    assert config.base.queue_size == 5
    assert config.base.refill_when_below == 2


def test_base_config_backend_defaults_match_prompts_yaml():
    config = load_prompts_config(PROMPTS_YAML)
    assert config.base.backend == "local"
    assert config.base.fal_endpoint == "fal-ai/flux/schnell"
