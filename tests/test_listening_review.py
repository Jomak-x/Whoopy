from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

import pytest

from scripts.build_listening_review import build, main


@dataclass
class Card:
    status: str
    text: str = ""
    links: set[str] = field(default_factory=set)
    audio: list[str] = field(default_factory=list)


class ReviewPage(HTMLParser):
    def __init__(self, page: str) -> None:
        super().__init__()
        self.cards: list[Card] = []
        self.active: Card | None = None
        self.urls: list[str] = []
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"a", "audio"}:
            self.urls.append(attributes["href" if tag == "a" else "src"] or "")
        if tag == "article":
            self.active = Card(status=attributes["data-status"] or "")
            self.cards.append(self.active)
        if self.active is not None:
            if tag == "a":
                self.active.links.add(attributes["href"] or "")
            elif tag == "audio":
                self.active.audio.append(attributes["src"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self.active = None

    def handle_data(self, data: str) -> None:
        if self.active is not None:
            self.active.text += data


@pytest.fixture
def benchmark(tmp_path: Path) -> Path:
    root = tmp_path / "evaluations" / "benchmarks" / "2026-10-28"
    listening = root / "verification" / "listening"
    listening.mkdir(parents=True)
    prefix = "evaluations/benchmarks/2026-10-28"
    original = root / "verification" / "original.json"
    original.write_text(
        json.dumps(
            {
                "full_file_report": f"{prefix}/verification/full-file.json",
                "assembly_proof": {"equal": True},
                "segments": [
                    {
                        "segment": f"{prefix}/voices/segment-01.wav",
                        "report": f"{prefix}/verification/paragraph-01.json",
                    }
                ],
            }
        )
    )
    records = []
    for status in ("pass", "review", "fail"):
        audio = listening / f"{status}.wav"
        audio.write_bytes(status.encode())
        raw = root / "voices" / status / "raw.wav"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(f"original {status}".encode())
        records.append(
            {
                "candidate": f"candidate-{status}",
                "output": f"{prefix}/verification/listening/{status}.wav",
                "output_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "input": f"{prefix}/voices/{status}/raw.wav",
                "input_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                "original_raw_verification": f"{prefix}/verification/original.json",
                "verification": f"{prefix}/verification/policy-v3/{status}.json",
                "status": status,
                "full_file_normalized_word_edit_count": 28 if status == "review" else 0,
            }
        )
    (listening / "manifest-policy-v3-strict.json").write_text(json.dumps({"records": records}))
    writing_audio = root / "writing" / "raw.wav"
    writing_audio.parent.mkdir()
    writing_audio.write_bytes(b"writing sample")
    (root / "writing-listening-policy-v3.json").write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "label": "Writing sample needs review",
                        "audio": f"/archived/checkout/{prefix}/writing/raw.wav",
                        "script": f"/archived/checkout/{prefix}/writing/script.md",
                        "original_verification": (
                            f"/archived/checkout/{prefix}/verification/original.json"
                        ),
                        "verification": f"{prefix}/verification/policy-v3/writing.json",
                        "status": "review",
                        "audio_sha256": hashlib.sha256(writing_audio.read_bytes()).hexdigest(),
                    }
                ]
            }
        )
    )
    for relative in (
        "verification/full-file.json",
        "verification/paragraph-01.json",
        "verification/policy-v3/pass.json",
        "verification/policy-v3/review.json",
        "verification/policy-v3/fail.json",
        "verification/policy-v3/writing.json",
        "voices/excluded-candidates.json",
    ):
        artifact = root / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("{}")
    (root / "voices/segment-01.wav").write_bytes(b"paragraph audio")
    (root / "writing/script.md").write_text("Writing script")
    (root / "voices/LICENSE-AUDIT.txt").write_text("License audit")
    (root / "voice-script.txt").write_text("Shared script")
    return root


def assert_local_links(output: Path, root: Path) -> None:
    page = ReviewPage(output.read_text())
    assert page.urls
    for url in page.urls:
        resolved = urlsplit(urljoin(output.as_uri(), url))
        assert resolved.scheme == "file"
        assert not resolved.netloc
        target = Path(url2pathname(resolved.path)).resolve()
        assert target.is_relative_to(root.resolve())
        assert target.is_file(), f"Broken link from {output}: {url}"


def test_page_preserves_statuses_and_raw_proof_links(
    benchmark: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    output = build(benchmark)
    page = ReviewPage(output.read_text())

    assert_local_links(output, benchmark)
    assert len(page.cards) == 4
    for status in ("pass", "review", "fail"):
        card = next(card for card in page.cards if f"candidate-{status}" in card.text)
        assert card.status == status
        assert status.title() in card.text
        assert card.audio == [f"verification/listening/{status}.wav"]
        assert card.links == {
            f"voices/{status}/raw.wav",
            f"verification/policy-v3/{status}.json",
            "verification/original.json",
            "verification/full-file.json",
            "voices/segment-01.wav",
            "verification/paragraph-01.json",
        }
    writing = page.cards[-1]
    assert writing.status == "review"
    assert "Review" in writing.text
    assert writing.audio == ["writing/raw.wav"]
    assert {
        "writing/raw.wav",
        "writing/script.md",
        "verification/policy-v3/writing.json",
    } <= writing.links
    assert "verification/original.json" in writing.links
    vox = next(card for card in page.cards if "candidate-review" in card.text)
    assert "28 normalized word edits" in vox.text
    assert "recognition disagreement, not proof that speech is missing" in vox.text
    assert "Exact paragraph recognition and PCM assembly remain positive evidence" in vox.text


def assert_report_history(output: Path, previous: bytes) -> None:
    history = output.with_name(f"{output.name}.history")
    assert {path.name: path.read_bytes() for path in history.iterdir()} == {
        f"{hashlib.sha256(previous).hexdigest()}.html": previous
    }


@pytest.mark.parametrize(
    ("field", "label"), [("input", "Original audio"), ("output", "Listening copy")]
)
@pytest.mark.parametrize("damage", ["changed", "missing", "unbound"])
@pytest.mark.parametrize("status", ["pass", "review"])
def test_page_downgrades_damaged_voice_audio_and_preserves_evidence(
    benchmark: Path, field: str, label: str, damage: str, status: str
) -> None:
    output = build(benchmark)
    existing_page = output.read_bytes()
    relative_path = (
        f"voices/{status}/raw.wav" if field == "input" else f"verification/listening/{status}.wav"
    )
    audio = benchmark / relative_path
    manifest = benchmark / "verification/listening/manifest-policy-v3-strict.json"
    if damage == "changed":
        audio.write_bytes(b"replacement audio")
    elif damage == "missing":
        audio.unlink()
    else:
        payload = json.loads(manifest.read_text())
        record = next(record for record in payload["records"] if record["status"] == status)
        del record[f"{field}_sha256"]
        manifest.write_text(json.dumps(payload))
    evidence = {
        path: path.read_bytes()
        for path in benchmark.rglob("*")
        if path.is_file() and path != output
    }

    page = ReviewPage(build(benchmark).read_text())

    damaged = next(card for card in page.cards if f"candidate-{status}" in card.text)
    assert damaged.status == "fail"
    assert label in damaged.text
    assert ("hash missing" if damage == "unbound" else damage) in damaged.text
    assert not damaged.audio
    assert not damaged.links
    assert "remain positive evidence" not in damaged.text
    unaffected = "review" if status == "pass" else "pass"
    valid = next(card for card in page.cards if f"candidate-{unaffected}" in card.text)
    assert valid.status == unaffected
    assert valid.audio == [f"verification/listening/{unaffected}.wav"]
    assert page.cards[-1].status == "review"
    assert {path: path.read_bytes() for path in evidence} == evidence
    assert_report_history(output, existing_page)
    current_page = output.read_bytes()

    build(benchmark)

    assert output.read_bytes() == current_page
    assert_report_history(output, existing_page)
    assert {path: path.read_bytes() for path in evidence} == evidence


@pytest.mark.parametrize("damage", ["changed", "missing", "unbound"])
def test_page_downgrades_damaged_writing_audio_and_preserves_history(
    benchmark: Path, damage: str
) -> None:
    output = build(benchmark)
    existing_page = output.read_bytes()
    manifest = benchmark / "writing-listening-policy-v3.json"
    audio = benchmark / "writing/raw.wav"
    if damage == "changed":
        audio.write_bytes(b"replacement audio")
    elif damage == "missing":
        audio.unlink()
    else:
        payload = json.loads(manifest.read_text())
        del payload["samples"][0]["audio_sha256"]
        manifest.write_text(json.dumps(payload))
    evidence = {
        path: path.read_bytes()
        for path in benchmark.rglob("*")
        if path.is_file() and path != output
    }

    page = ReviewPage(build(benchmark).read_text())

    writing = page.cards[-1]
    assert writing.status == "fail"
    assert "Writing sample audio" in writing.text
    assert ("hash missing" if damage == "unbound" else damage) in writing.text
    assert not writing.audio
    assert not writing.links
    assert {card.status for card in page.cards[:-1]} == {"pass", "review", "fail"}
    assert all(card.audio for card in page.cards[:-1])
    assert {path: path.read_bytes() for path in evidence} == evidence
    assert_report_history(output, existing_page)
    current_page = output.read_bytes()

    build(benchmark)

    assert output.read_bytes() == current_page
    assert_report_history(output, existing_page)
    assert {path: path.read_bytes() for path in evidence} == evidence


@pytest.mark.parametrize("entrypoint", ["callable", "cli"])
def test_selected_manifest_and_report_filename_keep_links_beside_evidence(
    benchmark: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    monkeypatch.chdir(tmp_path)
    manifest = benchmark / "verification/listening/manifest-policy-v3-strict.json"
    selected = json.loads(manifest.read_text())
    selected["records"] = [record for record in selected["records"] if record["status"] == "review"]
    chosen = tmp_path / "chosen.json"
    chosen.write_text(json.dumps(selected))
    original_bytes = {path: path.read_bytes() for path in benchmark.rglob("*") if path.is_file()}
    filename = Path("audition.html")
    output = benchmark / filename

    if entrypoint == "cli":
        monkeypatch.setattr(
            sys,
            "argv",
            ["build", str(benchmark), "--manifest", str(chosen), "--output", str(filename)],
        )
        main()
    else:
        assert build(benchmark, manifest_path=chosen, output=filename) == output

    page = ReviewPage(output.read_text())
    assert [card.status for card in page.cards] == ["review", "review"]
    assert_local_links(output, benchmark)
    assert {
        path: path.read_bytes()
        for path in benchmark.rglob("*")
        if path.is_file() and path != output
    } == original_bytes


@pytest.mark.parametrize("entrypoint", ["callable", "cli"])
@pytest.mark.parametrize(
    "destination", ["external", "absolute-root", "nested", "parent", "dot", "parent-only"]
)
def test_unsupported_output_paths_are_rejected_before_writes(
    benchmark: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    destination: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    external = tmp_path / "audition.html"
    external.write_text("Existing external report")
    (benchmark / "audition.html").write_text("Existing local report")
    (benchmark / "nested").mkdir()
    output = {
        "external": external,
        "absolute-root": benchmark / "audition.html",
        "nested": Path("nested/audition.html"),
        "parent": Path("../audition.html"),
        "dot": Path("."),
        "parent-only": Path(".."),
    }[destination]
    paths = set(tmp_path.rglob("*"))
    contents = {path: path.read_bytes() for path in paths if path.is_file()}

    with pytest.raises(ValueError, match="report filename directly inside the benchmark root"):
        if entrypoint == "cli":
            monkeypatch.setattr(sys, "argv", ["build", str(benchmark), "--output", str(output)])
            main()
        else:
            build(benchmark, output=output)

    assert set(tmp_path.rglob("*")) == paths
    assert {path: path.read_bytes() for path in contents} == contents


@pytest.mark.parametrize("entrypoint", ["callable", "cli"])
@pytest.mark.parametrize("destination", ["output", "history"])
def test_output_symlinks_are_rejected_before_writes(
    benchmark: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    destination: str,
) -> None:
    output = benchmark / "review.html"
    if destination == "output":
        target = tmp_path / "outside.html"
        target.write_text("Existing external report")
        link = output
    else:
        output.write_text("Existing local report")
        target = tmp_path / "history"
        target.mkdir()
        link = benchmark / "review.html.history"
    try:
        link.symlink_to(target, target_is_directory=destination == "history")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    paths = set(tmp_path.rglob("*"))
    contents = {path: path.read_bytes() for path in paths if path.is_file()}

    with pytest.raises(ValueError, match="must not be symbolic links"):
        if entrypoint == "cli":
            monkeypatch.setattr(sys, "argv", ["build", str(benchmark)])
            main()
        else:
            build(benchmark)

    assert set(tmp_path.rglob("*")) == paths
    assert {path: path.read_bytes() for path in contents} == contents
