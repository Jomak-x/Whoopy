from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from whoopy.evaluation import ExperimentDefinition, load_evaluation_set
from whoopy.webui.api import create_api
from whoopy.webui.evaluation import LocalProductionExperimentExecutor
from whoopy.webui.server import LocalWebApplication

ROOT = Path(__file__).resolve().parents[1]
DEFINITIONS = (
    ROOT / "config/evaluation/experiments/lab-llm-grounding-3m-v1.json",
    ROOT / "config/evaluation/experiments/lab-voice-kokoro-grounding-3m-v1.json",
    ROOT / "config/evaluation/experiments/lab-pacing-kokoro-grounding-3m-v1.json",
)


def _definitions() -> list[ExperimentDefinition]:
    return [
        ExperimentDefinition.model_validate_json(path.read_text(encoding="utf-8"))
        for path in DEFINITIONS
    ]


def test_checked_in_pr20_single_case_definitions_are_production_capable() -> None:
    evaluation = load_evaluation_set(ROOT / "config/evaluation/local-v1.yaml")
    grounding = next(case for case in evaluation.cases if case.id == "grounding-3m")
    definitions = _definitions()

    assert grounding.blueprint_id == "grounding@1"
    assert grounding.duration_seconds == 180
    assert {definition.experiment_type for definition in definitions} == {
        "llm",
        "voice",
        "pacing",
    }
    for definition in definitions:
        assert definition.evaluation_id == evaluation.evaluation_id
        assert definition.evaluation_version == evaluation.version
        assert definition.seed == 42
        LocalProductionExperimentExecutor.validate_definition(definition)


def test_checked_in_pr20_definitions_are_accepted_by_the_local_api(tmp_path: Path) -> None:
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=ROOT / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )

    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        capabilities = client.get("/api/v1/lab/capabilities")
        created = [
            client.post(
                "/api/v1/lab/experiments",
                json=definition.model_dump(mode="json"),
            )
            for definition in _definitions()
        ]
        saved = client.get("/api/v1/lab/experiments")

    assert capabilities.status_code == 200
    available = capabilities.json()["experiment_types"]
    assert all(available[kind]["available"] for kind in ("llm", "voice", "pacing"))
    assert all(response.status_code == 201 for response in created)
    assert {item["definition"]["experiment_id"] for item in saved.json()["experiments"]} == {
        definition.experiment_id for definition in _definitions()
    }
