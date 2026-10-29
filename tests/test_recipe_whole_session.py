"""Behavioral gates exercised through the real generation entry point.

The deterministic adapter creates budget-sized fixture prose, not quality evidence.
It lets tests mutate otherwise valid complete sessions and inspect actual retries.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict
from uuid import UUID

import pytest

from whoopy.meditation import (
    GenerationError,
    GenerationWorkspace,
    LocalMeditationGenerator,
    MeditationGenerationResult,
    load_prompt_bundle,
)
from whoopy.meditation.models import RawGenerationAttempt
from whoopy.ports import (
    AdapterMetadata,
    ScriptGenerationRequest,
    ScriptGenerationResult,
    ScriptGenerationTelemetry,
)

RUN_ID = UUID("44444444-4444-4444-8444-444444444444")


class FixtureSection(TypedDict):
    section_id: str
    paragraphs: list[str]


class FixtureDraft(TypedDict):
    sections: list[FixtureSection]


class WholeSessionAdapter:
    metadata = AdapterMetadata(
        adapter_id="test.whole-session",
        versioned_model_id="fixture@1",
        runtime_id="python",
        runtime_version="test",
        license_id="CC0-1.0",
        device="fixture",
    )

    def __init__(
        self,
        mutation: Callable[[FixtureDraft], None] | None = None,
        *,
        always_mutate: bool = False,
        corrupt: bool = False,
        envelope: Callable[[str], str] | None = None,
    ) -> None:
        self.requests: list[ScriptGenerationRequest] = []
        self.outputs: list[FixtureDraft] = []
        self.mutation = mutation
        self.always_mutate = always_mutate
        self.corrupt = corrupt
        self.envelope = envelope

    def generate(self, request: ScriptGenerationRequest) -> ScriptGenerationResult:
        self.requests.append(request)
        contract = json.JSONDecoder().raw_decode(request.prompt.split("\n", 1)[1])[0]
        seeds = {
            "comfort": "You can adjust or move, noticing support",
            "orientation": "Hear surrounding sounds and feel contact with support",
            "arrival": "Rest attention on familiar support, allowing quiet interest",
            "anchor": "Listen with interest, allowing the sound to change",
            "body": "Notice forehead, shoulders, hands, then feet with interest",
            "practice": "Notice nearby sounds with patient interest and contact",
            "reflection": "What mattered in one ordinary moment today",
            "response": "Allow the question to remain, noticing familiar contact",
            "release": "Return from that moment to contact with support",
            "integration": "Rest attention with familiar sound and bodily support",
            "taper": "Allow quiet rest with familiar contact and support",
            "return": "Hear surrounding sounds; optionally move fingers, noticing contact",
        }
        output: FixtureDraft = {"sections": []}
        for beat_index, beat in enumerate(contract["beats_in_order"]):
            paragraphs = []
            for paragraph_index, slot in enumerate(beat["paragraphs"]):
                seed = seeds[beat["section_id"]]
                if (
                    beat["section_id"] == "practice"
                    and "Recall one bounded" in contract["main_practice"]
                ):
                    seed = "What mattered in one ordinary moment today"
                if seed.startswith("What ") and paragraph_index > 0:
                    seed = "Allow the question to remain with familiar contact"
                # Unique complete fixture sentences expose accidental repetition or surgery.
                words = [*seed.split(), f"interval{beat_index}part{paragraph_index}"]
                target = max(len(words), slot["target_words"])
                words += ["gently"] * (target - len(words))
                punctuation = "?" if seed.startswith("What ") else "."
                paragraphs.append(" ".join(words) + punctuation)
            output["sections"].append({"section_id": beat["section_id"], "paragraphs": paragraphs})
        if self.mutation and (len(self.requests) == 1 or self.always_mutate):
            self.mutation(output)
        self.outputs.append(output)
        text = "This is not JSON" if self.corrupt else json.dumps(output)
        if self.envelope is not None:
            text = self.envelope(text)
        return ScriptGenerationResult(text=text, metadata=self.metadata, elapsed_seconds=0.01)


def generator(
    adapter: WholeSessionAdapter, *, workspace: GenerationWorkspace | None = None
) -> LocalMeditationGenerator:
    return LocalMeditationGenerator(
        adapter, load_prompt_bundle(Path("config/prompts")), workspace=workspace
    )


def run(
    adapter: WholeSessionAdapter,
    prompt: str = "Steady me before a meeting",
    duration: int = 360,
    *,
    workspace: GenerationWorkspace | None = None,
) -> MeditationGenerationResult:
    return generator(adapter, workspace=workspace).generate(
        prompt=prompt,
        duration_seconds=duration,
        run_id=RUN_ID,
        writing_method="recipe_whole_session",
    )


@pytest.mark.parametrize("duration", [60, 300, 600, 1800])
def test_complete_draft_preserved_with_exact_bounded_timeline(duration: int) -> None:
    adapter = WholeSessionAdapter()
    result = run(adapter, duration=duration)
    assert len(adapter.requests) == 1
    assert result.writing_method == "recipe_whole_session"
    assert result.estimated_duration_seconds == pytest.approx(duration, abs=0.02)
    assert (
        max(
            segment.duration_ms for segment in result.timeline.segments if segment.type == "SILENCE"
        )
        <= 16_000
    )
    for expected, actual in zip(adapter.outputs[0]["sections"], result.sections, strict=True):
        assert actual.text == "\n\n".join(expected["paragraphs"])
        assert all(paragraph in result.script for paragraph in expected["paragraphs"])


def test_sleep_reflection_has_all_prior_and_future_beats_in_single_prompt() -> None:
    adapter = WholeSessionAdapter()
    result = run(adapter, "A ten minute good-night reflection after a busy day", 600)
    request = adapter.requests[0]
    contract = json.JSONDecoder().raw_decode(request.prompt.split("\n", 1)[1])[0]
    assert [beat["section_id"] for beat in contract["beats_in_order"]] == [
        "comfort",
        "orientation",
        "arrival",
        "body",
        "reflection",
        "response",
        "release",
        "taper",
    ]
    assert contract["original_user_need"] == "A ten minute good-night reflection after a busy day"
    assert contract["posture"] == "lying_down"
    assert contract["ending"] == "sleep_taper"
    assert result.plan.blueprint_id == "good-night-reflection@1"
    assert result.plan.posture == "lying_down"
    assert result.estimated_duration_seconds == pytest.approx(600, abs=0.02)


def test_no_breath_focus_routes_sound_and_preserves_explicit_sitting() -> None:
    adapter = WholeSessionAdapter()
    result = run(adapter, "Help me sleep while sitting, no breath focus please", 300)
    assert result.plan.posture == "sitting"
    assert result.plan.anchor == "sound"
    assert result.plan.breath_mode == "external_anchor"
    assert '"breath_focus": "excluded"' in adapter.requests[0].prompt


@pytest.mark.parametrize(
    ("need", "anchor"),
    [
        (
            "A seated grounding meditation after an overstimulating workday. "
            "Use contact and ordinary sounds, develop one simple practice, "
            "and return gently to the rest of the day.",
            "sound",
        ),
        ("A seated grounding practice with bodily contact and support", "contact"),
    ],
)
def test_grounding_writer_contract_keeps_one_selected_anchor(need: str, anchor: str) -> None:
    adapter = WholeSessionAdapter()
    result = run(adapter, need, 180)
    contract = json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
    practice = next(beat for beat in contract["beats_in_order"] if beat["section_id"] == "practice")

    assert result.plan.meditation_kind == "grounding"
    assert result.plan.anchor == contract["anchor"] == anchor
    assert contract["main_practice"] == (
        "Explore the selected anchor as it is, returning to that same anchor after wandering."
    )
    assert practice["required_content"] == [
        contract["main_practice"],
        f"Continuity with the earlier {anchor} anchor",
    ]


def test_writer_receives_invitation_and_daytime_ending_editorial_contract() -> None:
    adapter = WholeSessionAdapter()
    result = run(adapter, "Seated focus with sound and no breathing", 180)
    system_prompt = adapter.requests[0].system_prompt

    assert system_prompt is not None
    for guidance in (
        "orientation cues, not a paired practice",
        "an explicitly optional replacement, not a second anchor",
        "never asserts achieved attention, independence or ability",
        "do not assert that an anchor remains steady, unchanged or available",
        "without naming an expected sound source",
        "without prescribing a fixed arrangement of body parts",
        "accepting change or no change without supplying the outcome",
        "one concrete cue to use the same familiar practice",
        "within the ending word budget and question_count",
    ):
        assert guidance in system_prompt
    assert result.plan.writing_prompt_version == 6
    # These assertions cover the writer's input, not the fixture's editorial quality.
    assert len(adapter.requests) == 1


def test_revision_receives_entire_prior_draft_and_every_gate_failure() -> None:
    def mutate(output: FixtureDraft) -> None:
        output["sections"][0]["paragraphs"][0] = "Sit upright with feet flat on the floor."
        output["sections"][-1]["paragraphs"][0] = "Open your eyes and wake up."

    adapter = WholeSessionAdapter(mutate)
    result = run(adapter, "Good night, I am lying down", 600)
    assert len(adapter.requests) == 2
    revision = adapter.requests[1].prompt
    assert "posture contradicts lying_down" in revision
    assert "waking reorientation contradicts the sleep ending" in revision
    assert "Sit upright with feet flat on the floor." in revision
    assert '"section_id": "reflection"' in revision
    assert result.sections[0].text == "\n\n".join(adapter.outputs[1]["sections"][0]["paragraphs"])
    assert result.raw_attempts[0].validation_error
    assert result.raw_attempts[-1].validation_error is None


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "order",
        "posture",
        "ending",
        "breath",
        "hold",
        "silence",
        "markup",
        "persistence",
        "safety",
    ],
)
def test_hard_contract_failures_reject_after_one_whole_revision(failure: str) -> None:
    def mutate(output: FixtureDraft) -> None:
        if failure == "missing":
            output["sections"].pop(2)
        elif failure == "order":
            output["sections"][2:4] = reversed(output["sections"][2:4])
        else:
            phrases = {
                "posture": "Sit upright with feet flat on the floor.",
                "ending": "Open your eyes and wake up.",
                "breath": "Focus on breathing and notice each breath.",
                "hold": "Hold your breath for four seconds.",
                "silence": "Rest.",
                "markup": "Notice sound. [pause: 90s]",
                "persistence": "Continue even if you feel dizzy and uncomfortable.",
                "safety": "Remember that you are safe and rest with support.",
            }
            index = -1 if failure == "ending" else 0
            output["sections"][index]["paragraphs"][0] = phrases[failure]

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    with pytest.raises(GenerationError, match="after one holistic revision"):
        run(adapter, "Good night, lying down, without breath focus", 600)
    assert len(adapter.requests) == 2


def test_corrupt_output_is_never_promoted() -> None:
    adapter = WholeSessionAdapter(corrupt=True)
    with pytest.raises(GenerationError, match="Corrupt structured output"):
        run(adapter)
    assert len(adapter.requests) == 2


def test_explicit_box_request_reports_unsupported_reviewed_protocol() -> None:
    adapter = WholeSessionAdapter()
    with pytest.raises(GenerationError, match=r"Box breathing is not supported.*natural breathing"):
        run(adapter, "Five minutes after work with box breathing", 300)
    assert not adapter.requests


def test_completed_whole_session_resumes_without_drafting_again(tmp_path: Path) -> None:
    adapter = WholeSessionAdapter()
    workspace = GenerationWorkspace(tmp_path)
    first = run(adapter, workspace=workspace)
    second = run(adapter, workspace=workspace)
    assert len(adapter.requests) == 1
    assert first.script == second.script
    assert first.sections == second.sections
    assert (tmp_path / "whole-session.json").exists()


@pytest.mark.parametrize("corrupt", [False, True])
def test_failed_draft_retries_preserve_every_attempt_and_telemetry(
    tmp_path: Path, corrupt: bool
) -> None:
    class MeasuredAdapter(WholeSessionAdapter):
        def generate(self, request: ScriptGenerationRequest) -> ScriptGenerationResult:
            result = super().generate(request)
            return result.model_copy(
                update={
                    "elapsed_seconds": float(len(self.requests)),
                    "telemetry": ScriptGenerationTelemetry(
                        output_tokens=100 + len(self.requests),
                        output_tokens_unavailable_reason=None,
                    ),
                }
            )

    def omit_section(output: FixtureDraft) -> None:
        output["sections"].pop()

    adapter = MeasuredAdapter(omit_section, always_mutate=True, corrupt=corrupt)
    workspace = GenerationWorkspace(tmp_path)
    prior_files: dict[Path, bytes] = {}
    for invocation in range(2):
        with pytest.raises(GenerationError, match="after one holistic revision"):
            run(adapter, workspace=workspace)
        assert len(adapter.requests) == 2 * (invocation + 1)
        assert all(path.read_bytes() == content for path, content in prior_files.items())
        prior_files = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}

    adapter.corrupt = False
    adapter.mutation = None
    completed = run(adapter, workspace=workspace)

    assert all(path.read_bytes() == content for path, content in prior_files.items())
    attempts = sorted(
        (
            RawGenerationAttempt.model_validate_json(path.read_bytes())
            for path in (tmp_path / "raw-model-output").glob("*.json")
        ),
        key=lambda attempt: attempt.elapsed_seconds,
    )
    assert len(attempts) == len(adapter.requests) == 5
    assert [attempt.elapsed_seconds for attempt in attempts] == [1, 2, 3, 4, 5]
    assert [attempt.telemetry.output_tokens for attempt in attempts] == [101, 102, 103, 104, 105]
    assert [attempt.request_seed for attempt in attempts] == [
        request.seed for request in adapter.requests
    ]
    assert all(attempt.validation_error for attempt in attempts[:4])
    assert attempts[-1] == completed.raw_attempts[0]
    if corrupt:
        assert all(attempt.text == "This is not JSON" for attempt in attempts[:4])
    else:
        assert [json.loads(attempt.text) for attempt in attempts] == adapter.outputs


def test_concurrent_attempt_publication_keeps_all_records(tmp_path: Path) -> None:
    workspace = GenerationWorkspace(tmp_path)
    attempts = [
        RawGenerationAttempt(
            stage="whole-session", attempt=1, text=f"draft {index}", elapsed_seconds=index
        )
        for index in range(12)
    ]

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(workspace.save_attempt, attempts))

    saved = [
        RawGenerationAttempt.model_validate_json(path.read_bytes())
        for path in (tmp_path / "raw-model-output").glob("*.json")
    ]
    assert sorted(saved, key=lambda attempt: attempt.elapsed_seconds) == attempts


def test_prompt_content_change_invalidates_completed_checkpoint(tmp_path: Path) -> None:
    from whoopy.meditation.workspace import WorkspaceError

    adapter = WholeSessionAdapter()
    workspace = GenerationWorkspace(tmp_path)
    first_generator = generator(adapter, workspace=workspace)
    first_generator.generate(
        prompt="Steady me before a meeting",
        duration_seconds=360,
        run_id=RUN_ID,
        writing_method="recipe_whole_session",
    )
    prompt_document = first_generator.prompts.whole_session
    assert prompt_document is not None
    changed = first_generator.prompts.model_copy(
        update={
            "whole_session": prompt_document.model_copy(
                update={"text": prompt_document.text + "\nNew guidance."}
            ),
        }
    )
    second_generator = LocalMeditationGenerator(adapter, changed, workspace=workspace)
    with pytest.raises(WorkspaceError, match="different inputs or model settings"):
        second_generator.generate(
            prompt="Steady me before a meeting",
            duration_seconds=360,
            run_id=RUN_ID,
            writing_method="recipe_whole_session",
        )
    assert len(adapter.requests) == 1


def test_recipe_contract_change_invalidates_completed_checkpoint(tmp_path: Path) -> None:
    from whoopy.meditation.recipes import load_recipe_library
    from whoopy.meditation.workspace import WorkspaceError

    adapter = WholeSessionAdapter()
    workspace = GenerationWorkspace(tmp_path)
    run(adapter, workspace=workspace)
    library = load_recipe_library()
    recipe = library.recipes[-1]
    changed_beat = recipe.beats[2].model_copy(
        update={"purpose": "A newly reviewed anchor instruction."}
    )
    changed_recipe = recipe.model_copy(
        update={"beats": [*recipe.beats[:2], changed_beat, *recipe.beats[3:]]}
    )
    changed_library = library.model_copy(
        update={"recipes": [*library.recipes[:-1], changed_recipe]}
    )

    changed_writer = LocalMeditationGenerator(
        adapter,
        load_prompt_bundle(Path("config/prompts")),
        recipes=changed_library,
        workspace=workspace,
    )
    with pytest.raises(WorkspaceError, match="different inputs or model settings"):
        changed_writer.generate(
            prompt="Steady me before a meeting",
            duration_seconds=360,
            run_id=RUN_ID,
            writing_method="recipe_whole_session",
        )
    assert len(adapter.requests) == 1


def test_route_changes_main_practice_for_distinct_needs() -> None:
    contracts = []
    for need in (
        "Focus before a meeting",
        "I am anxious about persistent thoughts",
        "Help with a setback and self-criticism",
    ):
        adapter = WholeSessionAdapter()
        result = run(adapter, need)
        contracts.append(
            json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
        )
        assert result.plan.writing_prompt_id == "whoopy.recipe_whole_session"
        assert result.plan.writing_prompt_sha256 is not None
        assert len(result.plan.writing_prompt_sha256) == 64
        assert result.plan.recipe_sha256 is not None
        assert len(result.plan.recipe_sha256) == 64
    assert len({contract["main_practice"] for contract in contracts}) == 3


def test_installed_recipe_generation_works_outside_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import whoopy.locations
    from whoopy.meditation import load_recipe_library

    adapter = WholeSessionAdapter()
    writer = generator(adapter)
    installed_root = tmp_path / "installed" / "share" / "whoopy"
    config = installed_root / "config"
    config.mkdir(parents=True)
    (config / "default.yaml").write_text("{}\n", encoding="utf-8")
    (config / "session_recipes.yaml").write_text(
        Path("config/session_recipes.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    outside_checkout = tmp_path / "working-directory"
    outside_checkout.mkdir()
    monkeypatch.setattr(whoopy.locations, "installed_data_root", lambda: installed_root)
    monkeypatch.chdir(outside_checkout)

    library = load_recipe_library()
    assert any(recipe.versioned_id == "good-night-reflection@1" for recipe in library.recipes)
    result = writer.generate(
        prompt="Good-night reflection after a busy day",
        duration_seconds=600,
        run_id=RUN_ID,
        writing_method="recipe_whole_session",
    )
    assert result.plan.blueprint_id == "good-night-reflection@1"
    assert len(adapter.requests) == 1
    assert result.estimated_duration_seconds == pytest.approx(600, abs=0.02)


def test_holistic_revision_stays_in_production_drafting_stage() -> None:
    def mutate(output: FixtureDraft) -> None:
        output["sections"][0]["paragraphs"][0] = "Rest."

    stages: list[str] = []

    def production_callback(stage: str) -> None:
        if stage not in {"planning", "drafting"}:
            raise ValueError(f"unsupported durable generation stage: {stage}")
        stages.append(stage)

    adapter = WholeSessionAdapter(mutate)
    writer = LocalMeditationGenerator(
        adapter,
        load_prompt_bundle(Path("config/prompts")),
        progress_callback=production_callback,
    )
    result = writer.generate(
        prompt="A steady practice before a meeting",
        duration_seconds=360,
        run_id=RUN_ID,
        writing_method="recipe_whole_session",
    )
    assert len(adapter.requests) == 2
    assert stages == ["drafting", "drafting"]
    assert result.raw_attempts[-1].stage == "whole-session-revision"
    assert result.raw_attempts[-1].validation_error is None


def test_emitted_budget_contract_is_complete_and_consistent() -> None:
    adapter = WholeSessionAdapter()
    result = run(adapter, duration=180)
    contract = json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
    paragraph_slots = [slot for beat in contract["beats_in_order"] for slot in beat["paragraphs"]]
    assert contract["total_target_words"] == sum(slot["target_words"] for slot in paragraph_slots)
    assert contract["total_maximum_words"] == sum(slot["maximum_words"] for slot in paragraph_slots)
    for beat, section in zip(contract["beats_in_order"], result.sections, strict=True):
        assert beat["paragraph_count"] == len(beat["paragraphs"])
        assert beat["required_content"]
        assert beat["question_count"] == 0
        assert section.section_id == beat["section_id"]
        for slot in beat["paragraphs"]:
            assert slot["minimum_words_guidance"] <= slot["target_words"] <= slot["maximum_words"]
    assert result.plan.writing_prompt_version == 6


def test_unconditional_safety_assurance_is_reported_for_holistic_revision() -> None:
    def mutate(output: FixtureDraft) -> None:
        paragraph = output["sections"][0]["paragraphs"][0]
        output["sections"][0]["paragraphs"][0] = "You are safe. " + paragraph

    adapter = WholeSessionAdapter(mutate)
    result = run(adapter, duration=180)
    assert len(adapter.requests) == 2
    assert result.raw_attempts[0].validation_error == (
        "comfort paragraph 1: do not assert the listener is safe"
    )
    assert result.raw_attempts[-1].validation_error is None


@pytest.mark.parametrize("language", ["json", ""])
def test_single_complete_json_fence_preserves_prose_and_raw_provenance(language: str) -> None:
    baseline = run(WholeSessionAdapter())
    adapter = WholeSessionAdapter(envelope=lambda text: f"```{language}\n{text}\n```")
    fenced = run(adapter)
    assert len(adapter.requests) == 1
    assert fenced.sections == baseline.sections
    assert fenced.script == baseline.script
    assert fenced.raw_attempts[0].text == f"```{language}\n{json.dumps(adapter.outputs[0])}\n```"


@pytest.mark.parametrize(
    "case", ["commentary", "trailing", "multiple", "duplicate", "fenced_duplicate"]
)
def test_json_envelope_rejects_extra_content_and_duplicate_fields(case: str) -> None:
    def malformed(text: str) -> str:
        if case == "commentary":
            return f"Here is your session:\n```json\n{text}\n```"
        if case == "trailing":
            return f"```json\n{text}\n```\nFinished."
        if case == "multiple":
            return f"```json\n{text}\n```\n```json\n{text}\n```"
        duplicate = '{"sections": [], ' + text[1:]
        return f"```json\n{duplicate}\n```" if case == "fenced_duplicate" else duplicate

    adapter = WholeSessionAdapter(envelope=malformed)
    with pytest.raises(GenerationError, match="Corrupt structured output"):
        run(adapter)
    assert len(adapter.requests) == 2


@pytest.mark.parametrize(
    ("need", "duration", "expected_beats"),
    [("Focus before a meeting", 180, 6), ("Good-night reflection after a busy day", 600, 8)],
)
def test_adapter_receives_dynamic_exact_order_response_schema(
    need: str,
    duration: int,
    expected_beats: int,
) -> None:
    adapter = WholeSessionAdapter()
    result = run(adapter, need, duration)
    schema = adapter.requests[0].response_json_schema
    assert schema is not None
    assert schema["required"] == ["sections"]
    assert schema["additionalProperties"] is False
    section_array = schema["properties"]["sections"]
    assert section_array["minItems"] == section_array["maxItems"] == expected_beats
    assert "items" not in section_array
    for section_schema, drafted, fixture in zip(
        section_array["prefixItems"],
        result.sections,
        adapter.outputs[0]["sections"],
        strict=True,
    ):
        assert section_schema["additionalProperties"] is False
        assert set(section_schema["required"]) == {"section_id", "paragraphs"}
        assert section_schema["properties"]["section_id"]["const"] == drafted.section_id
        paragraphs = section_schema["properties"]["paragraphs"]
        assert paragraphs["minItems"] == paragraphs["maxItems"] == len(fixture["paragraphs"])
        assert paragraphs["items"]["type"] == "string"
        assert drafted.text == "\n\n".join(fixture["paragraphs"])
    assert result.plan.response_schema_algorithm_version == 1
    assert result.plan.response_schema_sha256 is not None
    assert len(result.plan.response_schema_sha256) == 64


@pytest.mark.parametrize(
    ("section_id", "paragraph"),
    [
        (
            "orientation",
            "Listen to the sounds around you. They may be near or far. "
            "Also notice where your body touches the seat.",
        ),
        (
            "orientation",
            "Listen to the sounds around you. Notice your body touching the cushion.",
        ),
        (
            "return",
            "Check in with the sounds around you. You might lift your hands or shift "
            "your weight slightly before opening your eyes. Whenever you\u2019re ready, "
            "return to the rest of your day.",
        ),
        ("return", "Listen to surrounding sounds. You may lift your hands slightly."),
        ("return", "Listen to surrounding sounds. You may shift your weight slightly."),
        ("return", "Listen to surrounding sounds. You may move your body gently."),
        ("return", "Listen to surrounding sounds. You may stretch your arms gently."),
        ("return", "Listen to surrounding sounds. You may move your hands gently."),
        ("return", "Listen to surrounding sounds. You may stretch your fingers gently."),
        ("return", "Listen to surrounding sounds. You may move your foot gently."),
        ("return", "Listen to surrounding sounds. You may wiggle your toes gently."),
        ("return", "Listen to surrounding sounds. You may flex your fingers gently."),
        ("return", "Listen to surrounding sounds. You may curl your toes gently."),
    ],
)
def test_concrete_contact_and_physical_return_cues_preserve_authored_prose(
    section_id: str, paragraph: str
) -> None:
    def mutate(output: FixtureDraft) -> None:
        section = next(item for item in output["sections"] if item["section_id"] == section_id)
        section["paragraphs"][0] = paragraph

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    result = run(adapter, "Grounding with ordinary sounds and bodily contact", 180)

    assert len(adapter.requests) == 1
    assert result.raw_attempts[0].validation_error is None
    section = next(item for item in result.sections if item.section_id == section_id)
    assert section.text == paragraph
    assert paragraph in result.script


@pytest.mark.parametrize(
    ("section_id", "paragraph", "failure"),
    [
        (
            "orientation",
            "Listen to the sounds around you. They may be near or far.",
            "orientation: establish bodily support or contact",
        ),
        (
            "orientation",
            "Listen to surrounding sounds as your attention touches on a memory.",
            "orientation: establish bodily support or contact",
        ),
        (
            "return",
            "Listen to the sounds around you before continuing the day.",
            "return: include surrounding sound and an optional small movement",
        ),
        (
            "return",
            "You may shift your weight slightly before continuing the day.",
            "return: include surrounding sound and an optional small movement",
        ),
        (
            "return",
            "Listen to the sounds around you as they shift and change.",
            "return: include surrounding sound and an optional small movement",
        ),
        (
            "return",
            "Listen to the sounds around you. You may shift your attention to them.",
            "return: include surrounding sound and an optional small movement",
        ),
        (
            "return",
            "Listen to the sounds around you and let them lift your spirits.",
            "return: include surrounding sound and an optional small movement",
        ),
        (
            "return",
            "Listen to the surrounding sounds as you move into the rest of your day.",
            "return: include surrounding sound and an optional small movement",
        ),
        (
            "return",
            "Listen to the surrounding sounds and stretch your attention into the day.",
            "return: include surrounding sound and an optional small movement",
        ),
        (
            "return",
            "Listen to surrounding sounds and notice your fingers resting on the chair.",
            "return: include surrounding sound and an optional small movement",
        ),
        (
            "return",
            "Listen to surrounding sounds and notice your toes resting on the floor.",
            "return: include surrounding sound and an optional small movement",
        ),
        (
            "return",
            "Listen to surrounding sounds and notice your body resting on the chair.",
            "return: include surrounding sound and an optional small movement",
        ),
    ],
)
def test_contact_and_return_gates_still_require_the_actual_practice_components(
    section_id: str, paragraph: str, failure: str
) -> None:
    def mutate(output: FixtureDraft) -> None:
        section = next(item for item in output["sections"] if item["section_id"] == section_id)
        section["paragraphs"][0] = paragraph

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    with pytest.raises(GenerationError, match=failure):
        run(adapter, "Grounding with ordinary sounds and bodily contact", 180)
    assert len(adapter.requests) == 2
    assert failure in adapter.requests[1].prompt


def test_external_sound_anchor_rejects_switch_to_breath_attention() -> None:
    def mutate(output: FixtureDraft) -> None:
        original = output["sections"][2]["paragraphs"][0]
        output["sections"][2]["paragraphs"][0] = original.replace("Listen", "Notice your breath")

    adapter = WholeSessionAdapter(mutate)
    result = run(adapter, "Grounding with ordinary sounds and bodily contact", 180)
    assert len(adapter.requests) == 2
    assert result.plan.anchor == "sound"
    assert result.plan.breath_mode == "external_anchor"
    assert result.raw_attempts[0].validation_error == (
        "anchor paragraph 1: use the selected sound anchor without breath guidance"
    )
    assert result.raw_attempts[-1].validation_error is None


@pytest.mark.parametrize(
    "ending",
    [
        "Keep returning to that contact whenever your attention shifts. "
        "Let guidance fade softly, with no need to move or open your eyes.",
        "There is no need to open your eyes. Allow quiet rest with familiar contact.",
        "You do not have to wake up. Rest with familiar contact.",
        "Sleep may come gently. You don\u2019t need to wake or adjust.",
        "You don't need to open your eyes. Rest with familiar contact.",
        "You do not need to wake up. Rest with familiar contact.",
        "You needn't wake up. Rest with familiar contact.",
        "You needn\u2019t open your eyes. Rest with familiar contact.",
        "Do not open your eyes or become alert. Allow quiet rest with familiar contact.",
    ],
)
def test_sleep_ending_accepts_explicitly_negated_waking_actions_without_prose_changes(
    ending: str,
) -> None:
    def mutate(output: FixtureDraft) -> None:
        output["sections"][-1]["paragraphs"][-1] = ending

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    result = run(adapter, "Good-night reflection in bed with contact", 300)

    assert len(adapter.requests) == 1
    assert result.raw_attempts[0].validation_error is None
    assert result.sections[-1].text.endswith(ending)
    assert ending in result.script


@pytest.mark.parametrize(
    "ending",
    [
        "Open your eyes, and rest with familiar contact.",
        "There is no need to move, but open your eyes and notice the room.",
        "Do not open your eyes; wake up and notice the room.",
        "You do not have to open your eyes. Become alert and notice the room.",
        "No need to move or open your eyes, then wake up and rest with contact.",
        "Do not worry and open your eyes to notice the room.",
        "You don't need to move; wake up and notice the room.",
        "You do not need to open your eyes, but become alert and notice the room.",
    ],
)
def test_sleep_ending_still_rejects_positive_and_mixed_waking_clauses(ending: str) -> None:
    def mutate(output: FixtureDraft) -> None:
        output["sections"][-1]["paragraphs"][-1] = ending

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    with pytest.raises(GenerationError, match="waking reorientation contradicts the sleep ending"):
        run(adapter, "Good-night reflection in bed with contact", 300)
    assert len(adapter.requests) == 2
    assert all(
        "waking reorientation contradicts the sleep ending" in request.prompt
        for request in adapter.requests[1:]
    )


@pytest.mark.parametrize(
    "reflection",
    [
        "Recall an ordinary moment when you paused for just a few breaths. "
        "What did you notice? Rest with that moment.",
        "There is no need to focus on breathing. "
        "What mattered in one ordinary moment today? Rest with that moment.",
    ],
)
def test_sleep_breath_mentions_remain_strict_even_when_negated(reflection: str) -> None:
    def mutate(output: FixtureDraft) -> None:
        section = next(item for item in output["sections"] if item["section_id"] == "reflection")
        section["paragraphs"][0] = reflection

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    with pytest.raises(
        GenerationError, match="use the selected sound anchor without breath guidance"
    ):
        run(adapter, "Good-night reflection in bed without breathing", 300)
    assert len(adapter.requests) == 2


def test_schema_algorithm_change_invalidates_completed_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whoopy.meditation.workspace import WorkspaceError

    adapter = WholeSessionAdapter()
    workspace = GenerationWorkspace(tmp_path)
    run(adapter, workspace=workspace)
    monkeypatch.setattr("whoopy.meditation.whole_session.RESPONSE_SCHEMA_ALGORITHM_VERSION", 2)
    with pytest.raises(WorkspaceError, match="different inputs or model settings"):
        run(adapter, workspace=workspace)
    assert len(adapter.requests) == 1


def test_modest_word_underflow_reallocates_quiet_without_prose_changes() -> None:
    brief = "Return to the familiar sound."

    def mutate(output: FixtureDraft) -> None:
        output["sections"][3]["paragraphs"][1] = brief

    adapter = WholeSessionAdapter(mutate)
    result = run(adapter, "Grounding with ordinary sounds and bodily contact", 180)
    contract = json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
    slot = contract["beats_in_order"][3]["paragraphs"][1]
    assert len(brief.split()) < slot["minimum_words_guidance"]
    assert len(adapter.requests) == 1
    assert brief in result.sections[3].text
    assert result.estimated_duration_seconds == 180
    for section, fixture in zip(result.sections, adapter.outputs[0]["sections"], strict=True):
        assert section.text == "\n\n".join(fixture["paragraphs"])
    pauses = [
        segment.duration_ms for segment in result.timeline.segments if segment.type == "SILENCE"
    ]
    assert all(1_000 <= pause <= 16_000 for pause in pauses)
    assert result.plan.timing_compiler_algorithm_version == 3


def test_extreme_under_speech_fails_global_quiet_capacity() -> None:
    def mutate(output: FixtureDraft) -> None:
        for section in output["sections"]:
            section["paragraphs"] = [
                " ".join(word for word in paragraph.rstrip(".?").split() if word != "gently")
                + ("?" if paragraph.endswith("?") else ".")
                for paragraph in section["paragraphs"]
            ]

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    with pytest.raises(GenerationError, match="bounded prepared capacity"):
        run(adapter, "A steady practice before a meeting", 600)
    assert len(adapter.requests) == 2


@pytest.mark.parametrize("sleep", [False, True])
def test_supported_comfort_with_permission_prepares_initial_quiet(sleep: bool) -> None:
    comfort = (
        "Lie down with the bed supporting your back and legs. "
        "You can adjust your position at any time."
        if sleep
        else "Find a supported seat with your feet on the ground. "
        "You may adjust your position whenever you need."
    )

    def mutate(output: FixtureDraft) -> None:
        output["sections"][0]["paragraphs"][0] = comfort

    adapter = WholeSessionAdapter(mutate)
    result = run(
        adapter,
        "Good-night reflection after a busy day" if sleep else "Grounding after work",
        600 if sleep else 180,
    )
    assert len(adapter.requests) == 1
    assert comfort in result.sections[0].text
    pauses = [
        segment.duration_ms for segment in result.timeline.segments if segment.type == "SILENCE"
    ]
    assert 8_000 <= pauses[0] <= 16_000
    assert max(pauses) <= (22_000 if sleep else 16_000)
    assert result.estimated_duration_seconds == (600 if sleep else 180)


def test_timing_compiler_change_invalidates_completed_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whoopy.meditation.workspace import WorkspaceError

    adapter = WholeSessionAdapter()
    workspace = GenerationWorkspace(tmp_path)
    run(adapter, workspace=workspace)
    monkeypatch.setattr("whoopy.meditation.whole_session.TIMING_COMPILER_ALGORITHM_VERSION", 4)
    with pytest.raises(WorkspaceError, match="different inputs or model settings"):
        run(adapter, workspace=workspace)
    assert len(adapter.requests) == 1


def test_custom_recipe_library_controls_draft_and_checkpoint(tmp_path: Path) -> None:
    from whoopy.meditation.recipes import load_recipe_library
    from whoopy.meditation.workspace import WorkspaceError

    library = load_recipe_library(Path("config/session_recipes.yaml"))
    recipes = [
        recipe.model_copy(update={"version": recipe.version + 1}) for recipe in library.recipes
    ]
    custom = library.model_copy(update={"recipes": recipes})
    workspace = GenerationWorkspace(tmp_path)
    adapter = WholeSessionAdapter()
    writer = LocalMeditationGenerator(
        adapter, load_prompt_bundle(Path("config/prompts")), recipes=custom, workspace=workspace
    )
    result = writer.generate(
        prompt="Steady me before a meeting",
        duration_seconds=360,
        run_id=RUN_ID,
        writing_method="recipe_whole_session",
    )
    contract = json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
    assert result.plan.blueprint_version == 2
    assert contract["recipe"] == result.plan.blueprint_id
    with pytest.raises(WorkspaceError, match="different"):
        run(WholeSessionAdapter(), workspace=workspace)


@pytest.mark.parametrize("paragraph_format", ["plain", "emphasis", "split"])
def test_paragraph_quiet_bounds_survive_pacing_and_real_speech_fitting(
    paragraph_format: str,
) -> None:
    from whoopy.audio.models import PcmAudio
    from whoopy.evaluation.pacing_profiles import PacingProfileRegistry, apply_pacing_profile
    from whoopy.meditation.models import DraftedSection
    from whoopy.meditation.pacing import sentence_word_count
    from whoopy.meditation.whole_session import recipe_quiet_limits
    from whoopy.pipeline.worker import WorkerError, fit_generated_timeline_to_real_speech
    from whoopy.timeline import SilenceSegment, SpeechSegment, build_script_timeline

    result = run(WholeSessionAdapter(), "Help me sleep tonight", 600)
    sections = list(result.sections)
    if paragraph_format != "plain":
        for index, drafted in enumerate(sections):
            paragraphs = []
            for paragraph in drafted.text.split("\n\n"):
                if paragraph_format == "split":
                    paragraph = "An ordinary moment unfolds. " * 30 + paragraph
                paragraphs.append(f"**{paragraph}**")
            text = "\n\n".join(paragraphs)
            sections[index] = DraftedSection(
                section_id=drafted.section_id,
                text=text,
                word_count=sentence_word_count(text),
            )
    section = sections[2]
    paragraphs = section.text.split("\n\n")
    paragraphs[0] = "This ordinary moment is enough."
    edited_text = "\n\n".join(paragraphs)
    sections[2] = DraftedSection(
        section_id=section.section_id,
        text=edited_text,
        word_count=sentence_word_count(edited_text),
    )
    authored_paragraphs = [
        paragraph for section in sections for paragraph in section.text.split("\n\n")
    ]
    timeline = build_script_timeline(
        run_id=RUN_ID,
        script="\n\n".join(f"{paragraph}\n\n[pause: 1s]" for paragraph in authored_paragraphs),
        created_at=result.timeline.created_at,
        source="generated_prompt",
    )
    if paragraph_format == "split":
        assert sum(isinstance(segment, SpeechSegment) for segment in timeline.segments) > len(
            authored_paragraphs
        )
    caps = recipe_quiet_limits(timeline, result.plan, sections)
    assert 7_999 in caps.values()
    assert 16_000 in caps.values()
    assert 22_000 in caps.values()
    silences = [segment for segment in timeline.segments if isinstance(segment, SilenceSegment)]
    timeline = timeline.model_copy(
        update={
            "segments": [
                segment.model_copy(update={"duration_ms": caps[segment.id]})
                if isinstance(segment, SilenceSegment)
                else segment
                for segment in timeline.segments
            ]
        }
    )
    paced = apply_pacing_profile(
        timeline,
        PacingProfileRegistry.load_bundled().get("progressive-quiet-v2"),
        quiet_limits=caps,
    )
    assert [
        segment.duration_ms for segment in paced.segments if isinstance(segment, SilenceSegment)
    ] == [caps[segment.id] for segment in silences]
    target_seconds = (sum(caps.values()) + 999) // 1_000
    speech_ms = target_seconds * 1_000 - sum(caps.values())
    audio = PcmAudio(pcm_s16le=b"\x01\x00" * 24 * speech_ms)
    shorter_quiet = paced.model_copy(
        update={
            "segments": [
                segment.model_copy(update={"duration_ms": round(segment.duration_ms * 0.75)})
                if isinstance(segment, SilenceSegment)
                else segment
                for segment in paced.segments
            ]
        }
    )
    fitted = fit_generated_timeline_to_real_speech(
        shorter_quiet,
        speech_audio={"speech": audio},
        target_seconds=target_seconds,
        quiet_limits=caps,
    )
    assert [
        segment.duration_ms for segment in fitted.segments if isinstance(segment, SilenceSegment)
    ] == [caps[segment.id] for segment in silences]
    with pytest.raises(WorkerError, match="unprepared long silence"):
        fit_generated_timeline_to_real_speech(
            paced,
            speech_audio={"speech": audio},
            target_seconds=target_seconds + 1,
            quiet_limits=caps,
        )


def test_draft_cli_uses_selected_recipe_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    import yaml

    from whoopy.adapters.llm import LlamaCppScriptGenerator
    from whoopy.cli import main
    from whoopy.hardware import HardwareSnapshot

    config = tmp_path / "config"
    shutil.copytree(Path("config"), config)
    recipe_path = config / "session_recipes.yaml"
    document = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    for recipe in document["recipes"]:
        recipe["version"] = 9
    recipe_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    snapshot = HardwareSnapshot(
        operating_system="linux",
        architecture="x86_64",
        cpu_count=8,
        total_ram_gb=16,
        available_ram_gb=9,
        free_disk_gb=20,
        accelerators=["cpu"],
    )
    adapter = WholeSessionAdapter()
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda _path: snapshot)
    monkeypatch.setattr(
        LlamaCppScriptGenerator,
        "from_artifact_store",
        classmethod(lambda _cls, **_kwargs: adapter),
    )
    output = tmp_path / "drafts"
    assert (
        main(
            [
                "draft",
                "Steady me before a meeting",
                "--config-dir",
                str(config),
                "--writing-method",
                "recipe_whole_session",
                "--minutes",
                "6",
                "--profile",
                "standard",
                "--models-dir",
                str(tmp_path / "models"),
                "--output-dir",
                str(output),
                "--json",
            ]
        )
        == 0
    )
    response = json.loads(capsys.readouterr().out)
    plan = json.loads((output / response["draft_id"] / "plan.json").read_text(encoding="utf-8"))
    contract = json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
    assert plan["blueprint_id"] == "daytime-steadying@9"
    assert plan["blueprint_version"] == 9
    assert contract["recipe"] == plan["blueprint_id"]


@pytest.mark.parametrize("wpm", [80, 123, 220])
@pytest.mark.parametrize(
    ("need", "duration"),
    [("Seated focus with sound and no breathing", 180), ("Good-night reflection in bed", 300)],
)
def test_duration_word_budget_uses_planning_rate(wpm: int, need: str, duration: int) -> None:
    import math

    from whoopy.meditation.recipes import allocate_slots, route_session

    adapter = WholeSessionAdapter()
    writer = LocalMeditationGenerator(
        adapter,
        load_prompt_bundle(Path("config/prompts")),
        articulation_words_per_minute=wpm,
    )
    result = writer.generate(
        prompt=need,
        duration_seconds=duration,
        run_id=RUN_ID,
        writing_method="recipe_whole_session",
    )
    contract = json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
    slots = [
        slot
        for beat in allocate_slots(route_session(need, duration), duration, wpm)
        for slot in beat
    ]
    expected_minimum = max(
        4 * len(slots),
        math.ceil((duration * 1_000 - sum(s.maximum_quiet_ms for s in slots)) * wpm / 60_000),
    )
    assert contract["words_per_minute"] == wpm
    assert contract["total_minimum_words"] == expected_minimum
    assert expected_minimum <= contract["total_target_words"] <= contract["total_maximum_words"]
    assert contract["total_target_words"] == sum(
        slot["target_words"] for beat in contract["beats_in_order"] for slot in beat["paragraphs"]
    )
    assert "floor" in contract["word_budget_guidance"]
    system_prompt = adapter.requests[0].system_prompt
    assert system_prompt is not None
    assert "Aim near the contract's total_target_words" in system_prompt
    assert result.estimated_duration_seconds == pytest.approx(duration, abs=0.02)
    assert len(adapter.requests) == 1


def test_short_sleep_revision_explains_expansion_and_retains_exact_attempts() -> None:
    from whoopy.meditation.pacing import sentence_word_count

    def sparse(output: FixtureDraft) -> None:
        for section_index, section in enumerate(output["sections"]):
            section["paragraphs"] = [
                " ".join(word for word in text.split() if word.rstrip(".?") != "gently").rstrip(
                    ".?"
                )
                + ("?" if text.endswith("?") else ".")
                + f" This interval{section_index}part{index} is ordinary."
                for index, text in enumerate(section["paragraphs"])
            ]
        # This independent failure reduces the actual capacity and should be named.
        output["sections"][3]["paragraphs"][0] = "An ordinary moment is enough."

    adapter = WholeSessionAdapter(sparse)
    result = run(adapter, "Good-night reflection after a busy day", 300)
    initial_words = sum(
        sentence_word_count(text)
        for section in adapter.outputs[0]["sections"]
        for text in section["paragraphs"]
    )
    error = result.raw_attempts[0].validation_error
    assert error is not None
    assert f"Too little spoken guidance: {initial_words} words" in error
    assert "bounded prepared capacity" in error
    assert "additional words" in error
    assert "Expand the complete session" in error
    assert "body paragraph 1" in error
    assert "Shorten the complete session" not in error
    contract = json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
    assert (
        f"duration-derived minimum {contract['total_minimum_words']}; "
        f"target {contract['total_target_words']}; "
        f"absolute maximum {contract['total_maximum_words']}"
    ) in adapter.requests[1].prompt
    assert error in adapter.requests[1].prompt
    assert result.raw_attempts[0].text == json.dumps(adapter.outputs[0])
    assert result.raw_attempts[1].text == json.dumps(adapter.outputs[1])
    assert len(adapter.requests) == 2
    for section, fixture in zip(result.sections, adapter.outputs[1]["sections"], strict=True):
        assert section.text == "\n\n".join(fixture["paragraphs"])


def test_overlong_revision_explains_shortening_instead_of_expansion() -> None:
    def verbose(output: FixtureDraft) -> None:
        for section in output["sections"]:
            section["paragraphs"] = ["gently " * 80 + text for text in section["paragraphs"]]

    adapter = WholeSessionAdapter(verbose)
    result = run(adapter, "Good-night reflection after a busy day", 300)
    error = result.raw_attempts[0].validation_error
    assert error is not None
    assert "Too much spoken guidance" in error
    assert "fewer words" in error
    assert "Shorten the complete session" in error
    assert "Expand the complete session" not in error
    assert all(message in adapter.requests[1].prompt for message in error.split("; "))
    assert len(adapter.requests) == 2
    assert result.raw_attempts[1].validation_error is None


@pytest.mark.parametrize("marker", ["*", "**", "_", "__"])
def test_emphasized_complete_question_gets_plain_prose_feedback(marker: str) -> None:
    def emphasize(output: FixtureDraft) -> None:
        reflection = next(
            section for section in output["sections"] if section["section_id"] == "reflection"
        )
        reflection["paragraphs"][0] = marker + reflection["paragraphs"][0] + marker

    adapter = WholeSessionAdapter(emphasize)
    result = run(adapter, "Good-night reflection after a busy day", 300)
    error = result.raw_attempts[0].validation_error
    assert error is not None
    assert "reflection paragraph 1: Markdown emphasis is not spoken prose" in error
    assert "finish the final sentence" not in error
    assert error in adapter.requests[1].prompt
    assert result.raw_attempts[0].text == json.dumps(adapter.outputs[0])
    assert len(adapter.requests) == 2
    assert result.raw_attempts[1].validation_error is None


def test_unfinished_plain_sentence_still_gets_completion_feedback() -> None:
    def unfinished(output: FixtureDraft) -> None:
        output["sections"][0]["paragraphs"][0] = output["sections"][0]["paragraphs"][0][:-1]

    adapter = WholeSessionAdapter(unfinished)
    result = run(adapter, duration=180)
    assert (
        result.raw_attempts[0].validation_error == "comfort paragraph 1: finish the final sentence"
    )
    assert len(adapter.requests) == 2
