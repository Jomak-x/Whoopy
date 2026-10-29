"""Executable boundaries that carry the new writer into production and Lab."""

from whoopy.cli import (
    _build_parser,
    _llm_settings_for_writing_method,
    _planning_words_per_minute,
)
from whoopy.pipeline.generation import GenerationRunSettings
from whoopy.webui.api import GenerationRequest
from whoopy.webui.evaluation import LocalProductionExperimentExecutor


def test_draft_cli_accepts_explicit_new_and_legacy_methods() -> None:
    parser = _build_parser()
    for method in ("plan_first", "recipe_whole_session"):
        args = parser.parse_args(["draft", "A sound meditation", "--writing-method", method])
        assert args.writing_method == method


def test_request_accepts_whole_session_method_without_changing_legacy_records() -> None:
    request = GenerationRequest(
        mode="prompt",
        text="A seated sound practice",
        minutes=3,
        writing_method="recipe_whole_session",
    )
    assert request.writing_method == "recipe_whole_session"
    assert GenerationRequest.model_validate_json(request.model_dump_json()) == request
    assert "recipe_whole_session" in LocalProductionExperimentExecutor.PRODUCTION_WRITING_METHODS
    methods = LocalProductionExperimentExecutor.capabilities()["writing_methods"]
    assert any(
        item["id"] == "recipe_whole_session" and item["production_backed"] for item in methods
    )
    # Historical records remain readable with their established method default.
    assert GenerationRunSettings.model_fields["writing_method"].default == "plan_first"


def test_whole_session_context_keeps_room_for_full_revision() -> None:
    assert _llm_settings_for_writing_method("recipe_whole_session").context_tokens == 32_768
    assert _llm_settings_for_writing_method("plan_first").context_tokens == 8_192


def test_new_kokoro_planning_accounts_for_native_speed_and_pace() -> None:
    assert _planning_words_per_minute("kokoro", 0.6, "slow", "recipe_whole_session") == 95
    assert _planning_words_per_minute("kokoro", 0.6, "natural", "recipe_whole_session") == 116
    assert _planning_words_per_minute("kokoro", 0.6, "slow", "plan_first") == 123


def test_larger_writer_profile_is_carried_by_public_request() -> None:
    request = GenerationRequest(
        mode="prompt",
        text="A seated sound practice",
        minutes=3,
        writing_method="recipe_whole_session",
        profile="high",
    )
    assert GenerationRequest.model_validate_json(request.model_dump_json()).profile == "high"
    assert _build_parser().parse_args(["draft", "Focus", "--profile", "high"]).profile == "high"
