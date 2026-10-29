#!/usr/bin/env python3
"""Render actual campaign receipts as a local listening report. No inference."""

# HTML/CSS template strings retain readable layout, matching build_listening_review.py.
# ruff: noqa: E501
from __future__ import annotations

import argparse
import hashlib
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def safe_path(root: Path, value: str | Path | None, *, directory: bool = False) -> Path:
    """Every served artifact must resolve inside this specific campaign."""
    if value is None:
        raise ValueError("Artifact path is missing")
    candidate = Path(value)
    path = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=True)
    path.relative_to(root)
    if not (path.is_file() or (directory and path.is_dir())):
        raise ValueError("Not a regular artifact")
    return path


def url(root: Path, path: Path) -> str:
    return quote(path.relative_to(root).as_posix(), safe="/")


def link(root: Path, value: str | Path | None, label: str, *, directory: bool = False) -> str:
    try:
        path = safe_path(root, value, directory=directory)
    except (ValueError, OSError, TypeError):
        return f'<span class="missing">{esc(label)} unavailable</span>'
    return f'<a href="{url(root, path)}">{esc(label)}</a>'


def load(root: Path, value: str | Path | None) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = json.loads(safe_path(root, value).read_text(encoding="utf-8"))
        return payload
    except (ValueError, OSError, TypeError):
        return {}


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def playback_evidence(
    root: Path, value: str | Path | None, report: dict[str, Any], *, label: str
) -> tuple[Path | None, str | None]:
    """Only label screening current when its receipt binds the playable bytes."""
    try:
        path = safe_path(root, value)
    except (ValueError, OSError, TypeError):
        return None, f"{label} is missing, unavailable, or outside this campaign."
    if not report:
        return path, None
    expected = report.get("audio_sha256")
    if not isinstance(expected, str) or not expected:
        return (
            path,
            f"{label} ASR receipt has no audio SHA256; screening evidence is unbound.",
        )
    try:
        if file_digest(path) != expected:
            return (
                path,
                f"{label} hash differs from retained ASR receipt; screening evidence is stale.",
            )
    except OSError:
        return (
            None,
            f"{label} could not be read; screening evidence cannot be verified.",
        )
    return path, None


def details(label: str, value: Any) -> str:
    return f"<details><summary>{esc(label)}</summary><pre>{esc(json.dumps(value, indent=2, ensure_ascii=False))}</pre></details>"


def status_for(record_status: str | None, reports: list[dict[str, Any]]) -> str:
    """Never promote absent evidence, retained failures, or word disagreements."""
    levels = [record_status] + [r.get("status") for r in reports]
    if "fail" in levels or any(r.get("failure_reasons") for r in reports):
        return "Fail"
    for report in reports:
        content = report.get("content") or {}
        if (
            content.get("possible_truncation")
            or content.get("word_error_rate", 0) > 0.35
            or content.get("ordered_word_coverage", 1) < 0.65
            or content.get("longest_contiguous_error_span", 0) >= 8
        ):
            return "Fail"
    if "review" in levels or any(
        r.get("review_reasons")
        or any(
            (r.get("content") or {}).get(k, 0) for k in ("insertions", "deletions", "substitutions")
        )
        for r in reports
    ):
        return "Review"
    if (
        record_status in {None, "pass"}
        and reports
        and all(
            r.get("status") == "pass"
            and isinstance((r.get("asr") or {}).get("text"), str)
            and (r.get("content") or {}).get("expected_word_count", 0) > 0
            for r in reports
        )
    ):
        return "Pass"
    return "Not screened"


STYLE = """body{font:17px/1.5 system-ui;margin:0;background:#f4f1e9;color:#202526}main{max-width:1100px;margin:auto;padding:28px}article,section.case{background:white;border:1px solid #d6d5ce;border-radius:12px;padding:22px;margin:22px 0}article{border-left:7px solid #8b969a}article.pass{border-left-color:#36705b}article.review{border-left-color:#ae7a16}article.fail{border-left-color:#a73c33}audio{width:100%}a{color:#175c63}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f6f3;padding:12px}table{border-collapse:collapse;width:100%}th,td{padding:7px;border:1px solid #ccc;text-align:left;vertical-align:top}.delete,.substitute{background:#fce8e4}.insert{background:#fff0c9}.missing,small{color:#626966}.badge{font-weight:700}summary{cursor:pointer}h1,h2,h3{line-height:1.2}code{overflow-wrap:anywhere}li{margin:7px 0}:focus-visible{outline:3px solid #286b65;outline-offset:3px}"""


def document(title: str, body: str) -> str:
    return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>'


def diff_page(
    root: Path,
    destination: Path,
    label: str,
    report: dict[str, Any],
    *,
    playback_error: str | None = None,
) -> str:
    comparison = report.get("content") or {}
    rows = []
    for item in comparison.get("alignment", []):
        operation = item.get("operation", "unknown")
        css = operation if operation in {"match", "delete", "insert", "substitute"} else "unknown"
        rows.append(
            f'<tr class="{css}"><td>{esc(operation)}</td><td>{esc(item.get("expected_index", ""))}</td><td>{esc(item.get("expected") or "")}</td><td>{esc(item.get("observed_index", ""))}</td><td>{esc(item.get("observed") or "")}</td></tr>'
        )
    summary = {k: v for k, v in comparison.items() if k != "alignment"}
    body = f"<h1>{esc(label)}</h1><p>Exact normalized word alignment retained by independent ASR. Indexes are zero-based. Case and punctuation are normalized; this is not a human transcript or a listening decision.</p>"
    screening = "Fail" if playback_error else status_for(None, [report])
    body += f"<p>Screening: <strong>{esc(screening)}</strong></p>"
    if playback_error:
        body += f"<p>{esc(playback_error)}</p>"
    body += details("Comparison counts and error spans", summary)
    if rows:
        body += (
            "<table><thead><tr><th>Operation</th><th>Expected index</th><th>Expected word</th><th>ASR index</th><th>ASR word</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    else:
        body += (
            "<p>No word alignment is available in this receipt. No word diff has been invented.</p>"
        )
    body += details("Expected transcript", report.get("expected_transcript"))
    body += details("Independent ASR transcript", (report.get("asr") or {}).get("text"))
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(document(label, body))
    return link(root, destination, "Exact normalized word diff")


def build(root: Path, *, output: Path | None = None) -> Path:
    root = root.resolve(strict=True)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    output = output or root / "review.html"
    output = output if output.is_absolute() else root / output
    output = output.absolute()
    if output.parent.resolve(strict=True) != root:
        raise ValueError("The report must be saved directly alongside the campaign manifest")
    if output.exists():
        raise FileExistsError("Report already exists; use --output for a new report filename")
    artifacts = root / f"{output.stem}-details-{uuid4().hex[:8]}"
    artifacts.mkdir()
    records: list[dict[str, Any]] = manifest.get("records", [])
    cases: dict[str, list[str]] = {}
    failures: list[str] = []
    for index, record in enumerate(records):
        case = str(record.get("case", "Unknown case"))
        attempt = safe_path(root, record["attempt"], directory=True)
        run = (
            safe_path(root, attempt / "production-runs" / str(record["run_id"]), directory=True)
            if (attempt / "production-runs" / str(record["run_id"])).is_dir()
            else None
        )
        durable = load(root, run / "run.json") if run else {}
        config = record.get("resolved_config") or (
            load(root, run / "resolved-config.json") if run else {}
        )
        metadata = record.get("model_metadata") or (
            load(root, run / "model-metadata.json") if run else {}
        )
        quality = record.get("quality") or (load(root, run / "quality.json") if run else {})
        final = record.get("final_verification") or load(root, attempt / "verify-final.json")
        native = record.get("native_verification", [])
        reports = ([final] if final else []) + [
            n["verification"] for n in native if n.get("verification")
        ]
        screening = status_for(record.get("status"), reports)
        command = record.get("generation", {}).get("command", [])
        mode = record.get("mode") or (
            "fixed_script_voice_comparison"
            if "--script-file" in command or durable.get("source_kind") == "script_file"
            else "prompt_to_audio"
        )
        mode_label = (
            "Fixed-script voice comparison"
            if mode == "fixed_script_voice_comparison"
            else "Prompt → audio: fresh HIGH whole-session writing + speech"
        )
        tts = config.get("tts", {})
        tts_metadata = metadata.get("tts") or {}
        model = (
            tts_metadata.get("versioned_model_id")
            or tts.get("backend")
            or "Unavailable; see retained command"
        )
        voice = tts.get("voice_name") or record.get("candidate", "Unavailable")
        duration = record.get(
            "actual_seconds",
            quality.get("duration_ms", 0) / 1000
            if quality.get("duration_ms") is not None
            else None,
        )
        duration_label = (
            f"{float(duration):.3f} seconds"
            if duration is not None
            else "Unavailable; generation incomplete"
        )
        candidate = record.get("candidate", "Unknown candidate")
        card_id = f"sample-{index:03d}"
        reasons = list(final.get("failure_reasons", [])) + list(final.get("review_reasons", []))
        final_audio: Path | None = None
        final_error: str | None = None
        if final or (run and (run / "narration.wav").exists()):
            final_audio, final_error = playback_evidence(
                root, run / "narration.wav" if run else None, final, label="Final WAV"
            )
            if final_error:
                screening = "Fail"
                reasons.append(final_error)
        native_by_path: dict[str, dict[str, Any]] = {}
        native_playback: dict[str, tuple[Path | None, str | None]] = {}
        for selected in native:
            try:
                sidecar = safe_path(root, selected.get("metadata"))
                key = str(sidecar)
                native_by_path[key] = selected
                receipt = load(root, sidecar)
                native_report = selected.get("verification") or {}
                playback = playback_evidence(
                    root,
                    receipt.get("wav_path"),
                    native_report,
                    label="Native WAV",
                )
                if not native_report and playback[1] is None:
                    playback = (playback[0], "Selected native ASR receipt is missing.")
                native_playback[key] = playback
                native_error = playback[1]
            except (ValueError, OSError, TypeError):
                native_error = "Selected native metadata is missing or outside this campaign."
            if native_error:
                screening = "Fail"
                reasons.append(native_error)
        card = f'<article id="{card_id}" class="{screening.lower().replace(" ", "-")}"><h3>{esc(candidate)}</h3><p>{esc(mode_label)}</p><p class="badge">Automated speech screening: {esc(screening)}</p><p>Human quality: Unrated. No human listening score has been supplied.</p><p>Audio duration: {esc(duration_label)}. Requested target: {esc(record.get("target_seconds", "Unavailable"))} seconds.<br>Model: {esc(model)}<br>Voice / preset: {esc(voice)}<br>Run ID: <code>{esc(record.get("run_id"))}</code></p>'
        if final_audio:
            card += f'<audio controls preload="none" src="{url(root, final_audio)}"></audio><p>Actual final narration; no listening-copy normalization has been added.</p>'
        else:
            card += "<p>Final narration is unavailable. Retained partial/native artifacts are linked below.</p>"
        source_script = (
            command[command.index("--script-file") + 1]
            if "--script-file" in command and command.index("--script-file") + 1 < len(command)
            else None
        )
        script = (
            record.get("comparison_script")
            or source_script
            or record.get("accepted_script")
            or (run / "script.md" if run else None)
        )
        card += "<p>" + link(
            root,
            script,
            "Shared accepted script"
            if mode == "fixed_script_voice_comparison"
            else "Generated script",
        )
        if run:
            card += " · " + " · ".join(
                link(root, run / name, label)
                for name, label in (
                    ("timeline.json", "Final typed timeline"),
                    ("expected-spoken.txt", "Expected spoken text"),
                    ("resolved-config.json", "Actual settings"),
                    ("model-metadata.json", "Model provenance"),
                    ("quality.json", "Audio QC"),
                    ("audio-manifest.json", "Delivered audio manifest"),
                    ("run.json", "Durable run"),
                )
            )
        card += "</p>"
        if final:
            card += (
                "<p>"
                + link(root, attempt / "verify-final.json", "Full narration ASR receipt")
                + " · "
                + diff_page(
                    root,
                    artifacts / f"sample-{index:03d}-final.html",
                    f"{candidate}: final narration",
                    final,
                    playback_error=final_error,
                )
                + "</p>"
            )
        if reasons:
            card += "<ul>" + "".join(f"<li>{esc(r)}</li>" for r in reasons) + "</ul>"
        if record.get("error") or durable.get("error"):
            card += details("Retained failure", record.get("error") or durable.get("error"))
        card += details("Actual resolved settings", config)
        card += details("Exact command and wall time", record.get("generation", {}))
        card += details(
            "Model/runtime provenance and script/timeline hashes",
            {
                "metadata": metadata,
                "script_sha256": record.get("script_sha256"),
                "timeline_sha256": record.get("timeline_sha256"),
            },
        )
        if screening in {"Fail", "Not screened"} or record.get("error"):
            failures.append(
                f'<li><a href="#{card_id}">{esc(case)} / {esc(candidate)}: {esc(screening)}</a> — {esc(record.get("error") or durable.get("error") or "; ".join(reasons) or "No completed screening evidence")}</li>'
            )
        if run:
            native_files = sorted((run / "native-speech").glob("*/metadata.json"))
            if native_files:
                card += (
                    "<details><summary>All retained native attempts, including failures</summary>"
                )
            for native_index, sidecar in enumerate(native_files):
                receipt = load(root, sidecar)
                selected = native_by_path.get(str(sidecar.resolve()))
                native_report = selected.get("verification", {}) if selected else {}
                state = (
                    status_for(None, [native_report])
                    if selected
                    else "Not selected for delivered audio"
                )
                cached_playback = native_playback.get(str(sidecar.resolve()))
                if cached_playback is None:
                    cached_playback = playback_evidence(
                        root, receipt.get("wav_path"), {}, label="Native WAV"
                    )
                path, playback_error = cached_playback
                if playback_error:
                    state = "Fail: " + playback_error
                if receipt.get("eos_observed") is False:
                    state = "Fail: native codec EOS absent"
                    failures.append(
                        f'<li><a href="#{card_id}">{esc(case)} / {esc(candidate)}</a>: retained native attempt {esc(receipt.get("attempt_id", sidecar.parent.name))} failed natural codec EOS. {link(root, sidecar, "Native failure receipt")}</li>'
                    )
                card += f"<h4>Native attempt {esc(receipt.get('segment_id', native_index))}: {esc(state)}</h4>"
                if path:
                    card += f'<audio controls preload="none" src="{url(root, path)}"></audio>'
                else:
                    card += "<p>Native audio path is unavailable or outside this campaign.</p>"
                card += "<p>" + link(root, sidecar, "Native settings / EOS / provenance")
                if selected:
                    card += " · " + link(
                        root,
                        selected.get("selected_reference"),
                        "Selected native-to-processed mapping",
                    )
                    raw_asr = (
                        selected.get("report")
                        or attempt / f"verify-native-{native.index(selected):03d}.json"
                    )
                    card += " · " + link(root, raw_asr, "Native ASR receipt")
                    card += " · " + diff_page(
                        root,
                        artifacts / f"sample-{index:03d}-native-{native_index:03d}.html",
                        f"{candidate}: native {receipt.get('segment_id', native_index)}",
                        native_report,
                        playback_error=playback_error,
                    )
                card += "</p>"
            if native_files:
                card += "</details>"
            card += (
                "<p>"
                + link(
                    root, run / "raw-model-output", "All retained raw model drafts", directory=True
                )
                + " · "
                + link(
                    root,
                    attempt / "production-runs/.generation-workspaces",
                    "Generation workspaces / rejected attempts",
                    directory=True,
                )
                + "</p>"
            )
        card += (
            "<p>"
            + " · ".join(
                link(root, attempt / "generation" / name, label)
                for name, label in (
                    ("stdout.log", "Generation stdout"),
                    ("stderr.log", "Generation stderr"),
                    ("receipt.json", "Process receipt"),
                    ("resources.jsonl", "Memory / swap samples"),
                )
            )
            + " · "
            + link(root, attempt / "files.json", "Artifact hashes")
            + "</p></article>"
        )
        cases.setdefault(case, []).append(card)
    body = f"<h1>Whoopy: new full-session listening report</h1><p>Campaign state: <strong>{esc(manifest.get('status', 'Unavailable'))}</strong>. Report generated {esc(datetime.now(UTC).isoformat())}.</p>"
    body += "<p>Listen to the actual new prompt-to-audio sessions and fixed-script voice comparisons. Automated Pass means retained independent ASR screening found no normalized word mismatch; it does not establish warmth, naturalness, pacing, teaching quality, or word-perfect speech. Every Review needs listening. Hard failures and unscreened partial attempts remain visible. Nothing here is a human rating.</p>"
    body += (
        "<p>"
        + link(root, "manifest.json", "Campaign manifest")
        + " · "
        + link(root, "files.json", "Campaign artifact hashes")
        + " · "
        + link(root, "git-head.txt", "Source commit")
        + " · "
        + link(root, "git-diff.txt", "Source diff")
        + "</p>"
    )
    body += details(
        "Campaign writer / voice / ASR plan and source provenance",
        {
            "plan": manifest.get("plan"),
            "source": manifest.get("source"),
            "source_snapshot": manifest.get("source_snapshot"),
            "runner_sha256": manifest.get("runner_sha256"),
            "asr_weights_sha256": manifest.get("asr_weights_sha256"),
        },
    )
    if manifest.get("error") or manifest.get("stop_queue_reason"):
        body += details(
            "Queue stop evidence", manifest.get("error") or manifest.get("stop_queue_reason")
        )
    for case, cards in cases.items():
        body += f'<section class="case"><h2>{esc(case)}</h2>' + "".join(cards) + "</section>"
    if not records:
        body += "<p>No new full-session outputs have been recorded. No results or listening scores have been invented.</p>"
    body += "<section><h2>Retained failures and incomplete outputs</h2>" + (
        "<ul>" + "".join(failures) + "</ul>"
        if failures
        else "<p>No hard failure is listed in the available receipts. This is not a human quality acceptance.</p>"
    )
    body += details("Skipped jobs", manifest.get("skipped", [])) + "</section>"
    with output.open("x", encoding="utf-8") as stream:
        stream.write(document("Whoopy new full-session listening report", body))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(build(args.campaign, output=args.output))


if __name__ == "__main__":
    main()
