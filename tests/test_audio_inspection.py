from __future__ import annotations

import wave
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tests.test_recipe_whole_session import FixtureDraft, WholeSessionAdapter
from whoopy.audio import TimelineWaveRenderer
from whoopy.audio.models import AudioManifest
from whoopy.meditation import (
    DraftedSection,
    GenerationWorkspace,
    LocalMeditationGenerator,
    MeditationPlan,
    PlannedSection,
    load_prompt_bundle,
)
from whoopy.meditation.pacing import pace_section
from whoopy.pipeline import RunStatus, RunStore
from whoopy.pipeline.worker import LocalWorker
from whoopy.timeline import BreathSegment, SpeechSegment, Timeline, build_script_timeline
from whoopy.webui.api import create_api
from whoopy.webui.audio_inspection import (
    AudioInspectionError,
    enrich_waveform_context,
    section_descriptors,
    section_wave_bytes,
    segment_wave_bytes,
    waveform_payload,
)
from whoopy.webui.server import LocalWebApplication

RUN_ID = UUID("71717171-7171-4171-8171-717171717171")
NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _artifacts(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    sections = [
        ("arrival", "Arrive", "arrival", "arrival", "Notice the ground beneath you."),
        (
            "practice",
            "Practice",
            "focused_attention",
            "practice",
            "Rest attention on one steady point. Return when attention wanders.",
        ),
        ("ending", "Return", "return", "ending", "Open your eyes when you are ready."),
    ]
    planned = [
        PlannedSection(
            id=section_id,
            title=title,
            purpose=f"Guide {title.casefold()} safely.",
            technique=technique,  # type: ignore[arg-type]
            beat=beat,  # type: ignore[arg-type]
            target_speech_seconds=20,
            pause_after_ms=(index + 1) * 5_000,
            minimum_words=8,
            maximum_words=30,
        )
        for index, (section_id, title, technique, beat, _text) in enumerate(sections)
    ]
    plan = MeditationPlan(
        title="Audition test",
        intention="Test exact crops.",
        requested_duration_seconds=180,
        sections=planned,
    )
    script = "\n\n".join(
        pace_section(text, section_pause_ms=section.pause_after_ms)
        for section, (*_metadata, text) in zip(planned, sections, strict=True)
    )
    timeline = build_script_timeline(
        run_id=RUN_ID,
        script=script,
        created_at=NOW,
        source="generated_prompt",
    )
    rendered = TimelineWaveRenderer().render(timeline)
    audio_path = root / "narration.wav"
    manifest_path = root / "audio-manifest.json"
    timeline_path = root / "timeline.json"
    plan_path = root / "plan.json"
    drafts_path = root / "draft-sections"
    drafts_path.mkdir(parents=True)
    audio_path.write_bytes(rendered.wave_bytes)
    manifest_path.write_text(rendered.manifest.model_dump_json(indent=2) + "\n")
    timeline_path.write_text(timeline.model_dump_json(indent=2) + "\n")
    plan_path.write_text(plan.model_dump_json(indent=2) + "\n")
    for section, (*_metadata, text) in zip(planned, sections, strict=True):
        words = len(text.replace(".", "").split())
        draft = DraftedSection(section_id=section.id, text=text, word_count=words)
        (drafts_path / f"{section.id}.json").write_text(draft.model_dump_json(indent=2) + "\n")
    return audio_path, manifest_path, timeline_path, plan_path, drafts_path


def test_waveform_is_derived_from_pcm_and_carries_exact_segment_bounds(tmp_path: Path) -> None:
    audio, manifest, *_rest = _artifacts(tmp_path)

    result = waveform_payload(audio, manifest, points=100)

    assert 1 <= len(result["peaks"]) <= 100
    assert any(high > 0 for _low, high in result["peaks"])
    assert result["segments"][0]["start_ratio"] == 0
    assert result["segments"][-1]["end_ratio"] == 1
    assert {item["segment_type"] for item in result["segments"]} == {"SPEECH", "SILENCE"}


def test_waveform_context_exposes_breath_sections_and_regeneration_without_guessing() -> None:
    timeline = Timeline(
        schema_version=4,
        run_id=RUN_ID,
        created_at=NOW,
        source="generated_prompt",
        segments=[
            SpeechSegment(id="speech-0001", text="Arrive here."),
            BreathSegment(
                id="breath-0001",
                duration_ms=3_000,
                phase="inhale",
                cycle=1,
                protocol_id="gentle-even@1",
            ),
        ],
    )
    payload = {
        "total_frames": 48_000,
        "segments": [
            {"segment_id": "speech-0001", "segment_type": "SPEECH"},
            {"segment_id": "breath-0001", "segment_type": "BREATH"},
        ],
    }

    result = enrich_waveform_context(
        payload,
        timeline=timeline,
        sections=[
            {
                "section_id": "arrival",
                "title": "Arrival",
                "start_frame": 0,
                "end_frame": 48_000,
            }
        ],
        regenerations={
            "speech-0001": {
                "status": "regenerated_selected",
                "take_count": 2,
                "selected_take_id": "t0002-ffffffffff",
            }
        },
    )

    assert result["segments"][0]["regeneration"] == {
        "status": "regenerated_selected",
        "take_count": 2,
        "selected_take_id": "t0002-ffffffffff",
    }
    assert result["segments"][1]["breath"] == {
        "phase": "inhale",
        "cycle": 1,
        "protocol_id": "gentle-even@1",
    }
    assert result["sections"][0]["start_ratio"] == 0
    assert result["sections"][0]["end_ratio"] == 1


def test_waveform_context_rejects_stale_segment_and_section_boundaries() -> None:
    timeline = Timeline(
        schema_version=3,
        run_id=RUN_ID,
        created_at=NOW,
        source="script_file",
        segments=[SpeechSegment(id="speech-0001", text="Arrive here.")],
    )
    payload = {
        "total_frames": 48_000,
        "segments": [{"segment_id": "speech-wrong", "segment_type": "SPEECH"}],
    }
    with pytest.raises(AudioInspectionError, match="segment order"):
        enrich_waveform_context(
            payload,
            timeline=timeline,
            sections=[],
            regenerations={},
        )

    payload["segments"] = [{"segment_id": "speech-0001", "segment_type": "SPEECH"}]
    with pytest.raises(AudioInspectionError, match="contiguous"):
        enrich_waveform_context(
            payload,
            timeline=timeline,
            sections=[
                {
                    "section_id": "arrival",
                    "title": "Arrival",
                    "start_frame": 100,
                    "end_frame": 48_000,
                }
            ],
            regenerations={},
        )


def test_local_application_waveform_composes_canonical_editor_context(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    store = RunStore(runs)
    store.create("Inspect canonical context.", run_id=RUN_ID)
    _artifacts(store.run_directory(RUN_ID))
    assert LocalWorker(store).process(RUN_ID).status is RunStatus.COMPLETED
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=runs,
    )

    result = application.waveform(str(RUN_ID), points=100)

    assert result is not None
    assert [section["section_id"] for section in result["sections"]] == [
        "arrival",
        "practice",
        "ending",
    ]
    assert result["sections"][0]["start_ratio"] == 0
    assert result["sections"][-1]["end_ratio"] == 1
    speech = [segment for segment in result["segments"] if segment["segment_type"] == "SPEECH"]
    assert speech
    assert {segment["regeneration"]["status"] for segment in speech} == {"original"}


def test_segment_and_reviewed_section_auditions_are_exact_valid_wav_crops(
    tmp_path: Path,
) -> None:
    audio, manifest, timeline, plan, drafts = _artifacts(tmp_path)
    segments = waveform_payload(audio, manifest, points=100)["segments"]
    segment = segments[0]

    segment_audio = segment_wave_bytes(audio, manifest, segment["segment_id"])
    descriptors = section_descriptors(
        timeline_path=timeline,
        manifest_path=manifest,
        plan_path=plan,
        draft_sections_path=drafts,
    )
    section_audio = section_wave_bytes(audio, manifest, descriptors[1])

    assert [value["section_id"] for value in descriptors] == [
        "arrival",
        "practice",
        "ending",
    ]
    for payload, expected_ms in (
        (segment_audio, segment["actual_duration_ms"]),
        (section_audio, descriptors[1]["duration_ms"]),
    ):
        with wave.open(__import__("io").BytesIO(payload), "rb") as source:
            actual_ms = source.getnframes() * 1_000 / source.getframerate()
        assert actual_ms == pytest.approx(expected_ms, abs=0.05)


def test_section_audition_refuses_stale_plan_alignment(tmp_path: Path) -> None:
    _audio, manifest, timeline, plan, drafts = _artifacts(tmp_path)
    draft_path = drafts / "practice.json"
    draft = DraftedSection.model_validate_json(draft_path.read_text())
    draft_path.write_text(
        draft.model_copy(
            update={"text": "This no longer matches.", "word_count": 4}
        ).model_dump_json(indent=2)
    )

    with pytest.raises(AudioInspectionError, match="no longer aligns"):
        section_descriptors(
            timeline_path=timeline,
            manifest_path=manifest,
            plan_path=plan,
            draft_sections_path=drafts,
        )


def test_whole_session_sections_follow_paragraphs_and_reject_stale_text(tmp_path: Path) -> None:
    def add_sentence_boundaries(draft: FixtureDraft) -> None:
        for section in draft["sections"]:
            for index, paragraph in enumerate(section["paragraphs"]):
                words = paragraph.split()
                middle = len(words) // 2
                section["paragraphs"][index] = (
                    " ".join(words[:middle]) + ". " + " ".join(words[middle:])
                )

    store = RunStore(tmp_path / "runs")
    store.create("A steady moment.", run_id=RUN_ID)
    result = LocalMeditationGenerator(
        WholeSessionAdapter(add_sentence_boundaries),
        load_prompt_bundle(Path("config/prompts")),
        workspace=GenerationWorkspace(store.run_directory(RUN_ID)),
    ).generate(
        prompt="Steady me before a meeting",
        duration_seconds=180,
        run_id=RUN_ID,
        created_at=NOW,
        writing_method="recipe_whole_session",
    )
    store.write_timeline(RUN_ID, result.timeline)
    store.draft_sections_path(RUN_ID).mkdir()
    for section in result.sections:
        (store.draft_sections_path(RUN_ID) / f"{section.section_id}.json").write_text(
            section.model_dump_json()
        )
    assert LocalWorker(store).process(RUN_ID).status is RunStatus.COMPLETED
    manifest = AudioManifest.model_validate_json(store.audio_manifest_path(RUN_ID).read_text())
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=store.root,
    )

    descriptors = application.sections(str(RUN_ID))
    waveform = application.waveform(str(RUN_ID), points=100)
    assert descriptors is not None and waveform is not None
    assert [item["section_id"] for item in descriptors] == [
        section.id for section in result.plan.sections
    ]
    assert len(waveform["sections"]) == len(result.plan.sections)
    assert descriptors[0]["start_frame"] == 0
    assert descriptors[-1]["end_frame"] == manifest.total_frames
    for descriptor in descriptors:
        payload = application.section_audition(str(RUN_ID), descriptor["section_id"])
        assert payload is not None
        with wave.open(__import__("io").BytesIO(payload), "rb") as source:
            assert source.getnframes() == descriptor["end_frame"] - descriptor["start_frame"]

    draft_path = store.draft_sections_path(RUN_ID) / f"{result.sections[0].section_id}.json"
    draft_path.write_text(
        result.sections[0]
        .model_copy(update={"text": "This no longer matches.", "word_count": 4})
        .model_dump_json()
    )
    with pytest.raises(AudioInspectionError, match="no longer aligns"):
        application.sections(str(RUN_ID))


def test_waveform_refuses_same_length_pcm_corruption(tmp_path: Path) -> None:
    audio, manifest, *_rest = _artifacts(tmp_path)
    damaged = bytearray(audio.read_bytes())
    damaged[-8] ^= 0xFF
    audio.write_bytes(damaged)

    with pytest.raises(AudioInspectionError, match="digest differs"):
        waveform_payload(audio, manifest, points=100)


def test_versioned_api_exposes_waveform_and_inline_auditions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    wav = _artifacts(tmp_path / "audio")[0].read_bytes()
    monkeypatch.setattr(
        application,
        "waveform",
        lambda _run_id, *, points: {
            "duration_ms": 1000,
            "peaks": [[-0.1, 0.1]] * points,
            "segments": [],
        },
    )
    monkeypatch.setattr(application, "segment_audition", lambda *_args: wav)
    monkeypatch.setattr(
        application,
        "sections",
        lambda _run_id: [{"section_id": "arrival", "title": "Arrival"}],
    )
    monkeypatch.setattr(application, "section_audition", lambda *_args: wav)
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        waveform_response = client.get(
            f"/api/v1/runs/{RUN_ID}/waveform",
            params={"points": 100},
        )
        segment_response = client.get(f"/api/v1/runs/{RUN_ID}/segments/speech-0001/audition")
        sections_response = client.get(f"/api/v1/runs/{RUN_ID}/sections")
        section_response = client.get(f"/api/v1/runs/{RUN_ID}/sections/arrival/audition")

    assert waveform_response.status_code == 200
    assert len(waveform_response.json()["peaks"]) == 100
    assert segment_response.headers["content-type"].startswith("audio/wav")
    assert section_response.headers["content-type"].startswith("audio/wav")
    assert sections_response.json()["sections"][0]["section_id"] == "arrival"
