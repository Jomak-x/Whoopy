from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from whoopy.adapters.llm.llama_cpp import (
    SESSION_GRAMMAR_STRATEGY,
    LlamaCppScriptGenerator,
    _whole_session_grammar,
)
from whoopy.meditation.recipes import allocate_slots, route_session
from whoopy.meditation.whole_session import _response_schema
from whoopy.ports import FatalAdapterError, InvalidAdapterOutput, ScriptGenerationRequest


def session_schema() -> dict[str, Any]:
    session = route_session("A seated grounding practice with sound.", 180)
    return _response_schema(session, allocate_slots(session, 180, 133))


def response(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "sections": [
            {
                "section_id": item["properties"]["section_id"]["const"],
                "paragraphs": ['Words with a café and an escaped "quote".']
                * item["properties"]["paragraphs"]["minItems"],
            }
            for item in schema["properties"]["sections"]["prefixItems"]
        ]
    }


def adapter(tmp_path: Path, runner: Any, runtime: str = "b10142") -> LlamaCppScriptGenerator:
    executable, model = tmp_path / "llama-cli", tmp_path / "model.gguf"
    executable.touch()
    model.touch()
    return LlamaCppScriptGenerator(
        executable_path=executable,
        model_path=model,
        versioned_model_id="fixture/model@1",
        runtime_version=runtime,
        license_id="Apache-2.0",
        device="cpu",
        process_runner=runner,
    )


@pytest.mark.parametrize("failure", [False, True])
def test_pinned_session_transport_retains_schema_grammar_strategy_and_process_failure(
    tmp_path: Path, failure: bool
) -> None:
    schema = session_schema()
    grammar_inputs: list[Path] = []

    def runner(command: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        argv = list(command)
        assert "--json-schema-file" not in argv
        assert argv[argv.index("--reasoning") + 1] == "off"
        grammar_path = Path(argv[argv.index("--grammar-file") + 1])
        assert grammar_path.read_text() == _whole_session_grammar(schema)
        grammar_inputs.append(grammar_path)
        output = Path(argv[argv.index("--output-file") + 1])
        output.write_text(json.dumps(response(schema)), encoding="utf-8")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="retained stdout",
            stderr="common_sampler_init: error initializing grammar sampler" if failure else "",
        )

    writer = adapter(tmp_path, runner)
    request = ScriptGenerationRequest(
        prompt="Private request.",
        system_prompt="Private system.",
        response_json_schema=schema,
        raw_invocation_directory=tmp_path / "raw",
    )
    if failure:
        with pytest.raises(InvalidAdapterOutput, match="runtime sampler failure"):
            writer.generate(request)
    else:
        assert json.loads(writer.generate(request).text) == response(schema)
    evidence = tmp_path / "raw/001"
    retained_schema = (evidence / "response-schema.json").read_bytes()
    retained_grammar = (evidence / "response-grammar.gbnf").read_bytes()
    assert json.loads(retained_schema) == schema
    assert json.loads((evidence / "structured-output.json").read_text()) == {
        "strategy": SESSION_GRAMMAR_STRATEGY,
        "schema_sha256": hashlib.sha256(retained_schema).hexdigest(),
        "grammar_sha256": hashlib.sha256(retained_grammar).hexdigest(),
    }
    assert (evidence / "prompt.txt").read_text() == request.prompt
    assert (evidence / "system.txt").read_text() == request.system_prompt
    assert (evidence / "stdout.txt").read_text() == "retained stdout"
    if failure:
        assert "sampler" in (evidence / "stderr.txt").read_text()
    assert not grammar_inputs[0].exists()
    assert f"session_schema_transport={SESSION_GRAMMAR_STRATEGY}" in writer.metadata.settings
    without_strategy = writer.metadata.model_copy(
        update={
            "settings": tuple(
                item
                for item in writer.metadata.settings
                if not item.startswith("session_schema_transport=")
            )
        }
    )
    assert writer.metadata.cache_identity != without_strategy.cache_identity


@pytest.mark.parametrize(
    ("runtime", "schema"),
    [("b10142", {"type": "object"}), ("future-runtime", session_schema())],
)
def test_other_schemas_and_runtimes_keep_native_schema_transport(
    tmp_path: Path, runtime: str, schema: dict[str, Any]
) -> None:
    def runner(command: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        argv = list(command)
        assert "--grammar-file" not in argv
        native_schema = Path(argv[argv.index("--json-schema-file") + 1])
        assert json.loads(native_schema.read_text()) == schema
        Path(argv[argv.index("--output-file") + 1]).write_text("{}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert (
        adapter(tmp_path, runner, runtime)
        .generate(ScriptGenerationRequest(prompt="Write it.", response_json_schema=schema))
        .text
        == "{}"
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("additionalProperties",), True),
        (("required",), []),
        (("properties", "sections", "items"), {}),
        (("properties", "sections", "maxItems"), 100),
        (("properties", "sections", "prefixItems", 0, "title"), "annotation"),
        (("properties", "sections", "prefixItems", 0, "properties", "paragraphs", "maxItems"), 0),
        (
            (
                "properties",
                "sections",
                "prefixItems",
                0,
                "properties",
                "paragraphs",
                "items",
                "minLength",
            ),
            True,
        ),
        (
            ("properties", "sections", "prefixItems", 0, "properties", "section_id", "const"),
            'quote"',
        ),
    ],
)
def test_unsupported_session_constraints_fail_before_process_launch(
    tmp_path: Path, path: tuple[Any, ...], value: Any
) -> None:
    schema = session_schema()
    destination: Any = schema
    for key in path[:-1]:
        destination = destination[key]
    destination[path[-1]] = value

    def never(*_args: Any) -> Any:
        pytest.fail("Unsupported session schema launched a process")

    with pytest.raises(FatalAdapterError, match="does not support this session schema shape"):
        adapter(tmp_path, never).generate(
            ScriptGenerationRequest(prompt="Write it.", response_json_schema=schema)
        )


@pytest.mark.skipif(
    not os.environ.get("GBNF_TEST_VALIDATOR"),
    reason="Optional model-free pinned native GBNF validator was not supplied",
)
def test_native_b10142_grammar_enforces_structure_without_a_model(tmp_path: Path) -> None:
    """The tiny native diagnostic links only grammar code, loading no weights."""

    schema = session_schema()
    grammar = tmp_path / "response.gbnf"
    grammar.write_text(_whole_session_grammar(schema))
    valid = response(schema)
    candidates: dict[str, tuple[str, bool]] = {"valid": (json.dumps(valid), True)}
    for name in ("id", "order", "missing", "extra", "count", "empty", "key"):
        candidate = copy.deepcopy(valid)
        sections = candidate["sections"]
        if name == "id":
            sections[0]["section_id"] = "invented"
        elif name == "order":
            sections[0], sections[1] = sections[1], sections[0]
        elif name == "missing":
            sections.pop()
        elif name == "extra":
            sections.append(copy.deepcopy(sections[-1]))
        elif name == "count":
            sections[0]["paragraphs"].append("Extra words.")
        elif name == "empty":
            sections[0]["paragraphs"][0] = ""
        else:
            sections[0]["invented"] = "extra property"
        candidates[name] = (json.dumps(candidate), False)
    candidates["trailing"] = (json.dumps(valid) + "unconstrained prose", False)
    candidates["template-prefix"] = ("<|im_start|>assistant\n<think>\n\n</think>\n\n", False)
    for name, (text, accepted) in candidates.items():
        source = tmp_path / f"{name}.json"
        source.write_text(text, encoding="utf-8")
        result = subprocess.run(
            [os.environ["GBNF_TEST_VALIDATOR"], str(grammar), str(source)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == (0 if accepted else 1), (name, result.stdout, result.stderr)
