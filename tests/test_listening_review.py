from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

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
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
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
        records.append(
            {
                "candidate": f"candidate-{status}",
                "output": f"{prefix}/verification/listening/{status}.wav",
                "output_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "input": f"{prefix}/voices/{status}/raw.wav",
                "original_raw_verification": f"{prefix}/verification/original.json",
                "verification": f"{prefix}/verification/policy-v3/{status}.json",
                "status": status,
                "full_file_normalized_word_edit_count": 28 if status == "review" else 0,
            }
        )
    (listening / "manifest-policy-v3-strict.json").write_text(json.dumps({"records": records}))
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
                    }
                ]
            }
        )
    )
    return root


def test_page_preserves_statuses_and_raw_proof_links(
    benchmark: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    page = ReviewPage(build(benchmark).read_text())

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


def test_page_refuses_changed_listening_copy(benchmark: Path) -> None:
    (benchmark / "verification/listening/review.wav").write_bytes(b"changed")

    with pytest.raises(ValueError, match="Listening copy changed"):
        build(benchmark)

    assert not (benchmark / "review.html").exists()


def test_cli_selects_manifest_and_writes_outside_read_only_evidence_root(
    benchmark: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = benchmark / "verification/listening/manifest-policy-v3-strict.json"
    selected = json.loads(manifest.read_text())
    selected["records"] = [record for record in selected["records"] if record["status"] == "review"]
    chosen = tmp_path / "chosen.json"
    chosen.write_text(json.dumps(selected))
    original_bytes = {path: path.read_bytes() for path in benchmark.rglob("*") if path.is_file()}
    output = tmp_path / "audition.html"
    monkeypatch.setattr(
        sys, "argv", ["build", str(benchmark), "--manifest", str(chosen), "--output", str(output)]
    )

    main()

    page = ReviewPage(output.read_text())
    assert [card.status for card in page.cards] == ["review", "review"]
    assert {
        path: path.read_bytes() for path in benchmark.rglob("*") if path.is_file()
    } == original_bytes
