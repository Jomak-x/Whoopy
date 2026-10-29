#!/usr/bin/env python3
"""Link explicit benchmark datasets without pooling different evidence scopes."""

# HTML templates match the existing listening-report convention.
# ruff: noqa: E501
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def artifact(root: Path, value: str | Path | None) -> Path:
    if value is None:
        raise ValueError("Path has not been supplied")
    path = Path(value)
    if not path.is_absolute() and path.parts[:2] == ("evaluations", "benchmarks"):
        path = Path(*path.parts[2:])
    resolved = (path if path.is_absolute() else root / path).resolve(strict=True)
    resolved.relative_to(root)
    if not resolved.is_file():
        raise ValueError("Not a regular benchmark artifact")
    return resolved


def artifact_link(
    root: Path, base_url: str, value: str | Path | None, label: str, fragment: str = ""
) -> str:
    try:
        path = artifact(root, value)
    except (ValueError, OSError, TypeError):
        return f'<span class="missing">{esc(label)} unavailable</span>'
    href = base_url + quote(path.relative_to(root).as_posix(), safe="/")
    if fragment:
        href += "#" + quote(fragment, safe="")
    return f'<a href="{href}">{esc(label)}</a>'


def read_report(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Benchmark receipt must be an object")
    return value, hashlib.sha256(raw).hexdigest()


def source_version(
    root: Path, report_path: Path, data: dict[str, Any], entry: dict[str, Any], base_url: str
) -> str:
    head = entry.get("source_commit")
    head_path = report_path.parent / "git-head.txt"
    if head is None and head_path.is_file():
        head = artifact(root, head_path).read_text().strip()
    writing = data.get("plan", {}).get("writing", {})
    prompt_version = writing.get("whole_session_prompt_version")
    prompt = report_path.parent / "source-snapshot/config/prompts/whole_session_system.md"
    if prompt_version is None and prompt.is_file():
        text = artifact(root, prompt).read_text()
        matched = re.search(r"^version:\s*(\d+)\s*$", text, re.MULTILINE)
        prompt_version = int(matched.group(1)) if matched else None
    fields = {
        "source_commit": head or "Not retained / not supplied",
        "whole_session_prompt_version": prompt_version,
        "timing_compiler_algorithm_version": writing.get("timing_compiler_algorithm_version"),
        "runner_sha256": data.get("runner_sha256"),
        "legacy_manifest_producer_sha256": data.get("manifest_writer_source_sha256"),
        "policy_id": data.get("policy_id"),
    }
    body = "<p>Source commit: <code>" + esc(fields["source_commit"]) + "</code>"
    if prompt_version is not None:
        body += f" · whole-session prompt v{esc(prompt_version)}"
    if fields["timing_compiler_algorithm_version"] is not None:
        body += f" · timing compiler v{esc(fields['timing_compiler_algorithm_version'])}"
    body += "</p><p>" + artifact_link(root, base_url, head_path, "Source commit receipt")
    body += " · " + artifact_link(root, base_url, prompt, "Frozen whole-session prompt") + "</p>"
    return (
        body
        + "<details><summary>Additional version / policy evidence</summary><pre>"
        + esc(json.dumps(fields, indent=2))
        + "</pre></details>"
    )


def saved_label(value: Any) -> str:
    return {"pass": "Pass", "review": "Review", "fail": "Fail"}.get(str(value), "Not screened")


def campaign(
    root: Path, base_url: str, path: Path, data: dict[str, Any], entry: dict[str, Any]
) -> str:
    records = data.get("records", [])
    counts = Counter(saved_label(record.get("status")) for record in records)
    complete = 0
    rows = []
    review = path.parent / "review.html"
    for index, record in enumerate(records):
        try:
            run = Path(record["attempt"]) / "production-runs" / record["run_id"]
            artifact(root, run / "narration.wav")
            has_audio = True
            complete += 1
        except (OSError, ValueError, KeyError, TypeError):
            has_audio = False
        label = saved_label(record.get("status"))
        mode = record.get("mode")
        mode_label = (
            "Fixed-script voice comparison"
            if mode == "fixed_script_voice_comparison"
            else "Prompt → audio"
        )
        play = artifact_link(root, base_url, review, "Listen / ASR details", f"sample-{index:03d}")
        if has_audio:
            play += " · " + artifact_link(root, base_url, run / "narration.wav", "Final WAV")
        duration = record.get("actual_seconds")
        duration_label = (
            f"{float(duration):.3f}s" if duration is not None else "No completed duration"
        )
        rows.append(
            f'<tr><td>{esc(record.get("case"))}<br>{esc(record.get("candidate"))}<br><small>{esc(mode_label)}</small></td><td class="{label.lower().replace(" ", "-")}">{esc(label)}<br>{"Final audio present" if has_audio else "Final audio absent"}</td><td>{esc(duration_label)}</td><td>{esc(record.get("human_review", "pending"))}; no score supplied</td><td>{play}<br><code>{esc(record.get("run_id"))}</code><br>{esc(record.get("error", ""))}</td></tr>'
        )
    count_text = ", ".join(
        f"{counts[label]} {label}" for label in ("Pass", "Review", "Fail", "Not screened")
    )
    body = f"<p>Queue state: <strong>{esc(data.get('status', 'Unknown'))}</strong>. {len(records)} attempt records; {complete} final audio files present.</p><p>Saved screening labels for this campaign only: <strong>{esc(count_text)}</strong>. Failed or incomplete generation attempts remain included.</p>"
    body += "<p>These are retained manifest labels, not a new ASR run or a human acceptance. The linked detailed listening report performs additional artifact checks. Source versions are specific to this campaign.</p>"
    body += source_version(root, path, data, entry, base_url)
    body += (
        "<table><thead><tr><th>Case / voice / mode</th><th>Saved speech screening</th><th>Actual duration</th><th>Human review</th><th>Exact evidence</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    if data.get("error") or data.get("stop_queue_reason"):
        body += (
            '<p class="fail">Queue stop: '
            + esc(data.get("error") or data.get("stop_queue_reason"))
            + "</p>"
        )
    return body


def legacy(
    root: Path, base_url: str, path: Path, data: dict[str, Any], entry: dict[str, Any]
) -> str:
    records = data.get("records", [])
    counts = Counter(saved_label(record.get("status")) for record in records)
    body = "<p>Earlier fixed-text audition / listening-copy set. This is separate from the new full-session campaigns.</p>"
    body += (
        f"<p>{len(records)} listed auditions: "
        + ", ".join(
            f"{counts[label]} {label}" for label in ("Pass", "Review", "Fail", "Not screened")
        )
        + ". Human quality: Unreviewed; no ratings imported.</p>"
    )
    body += (
        "<p>"
        + artifact_link(root, base_url, entry.get("review"), "Open all original audition players")
        + "</p>"
    )
    body += source_version(root, path, data, entry, base_url)
    body += "<table><thead><tr><th>Audition</th><th>Saved screening</th><th>Evidence</th></tr></thead><tbody>"
    for record in records:
        label = saved_label(record.get("status"))
        body += (
            f'<tr><td>{esc(record.get("candidate"))}</td><td class="{label.lower().replace(" ", "-")}">{esc(label)}</td><td>'
            + artifact_link(root, base_url, record.get("verification"), "Current policy receipt")
            + " · "
            + artifact_link(root, base_url, record.get("output"), "Listening copy")
            + "</td></tr>"
        )
    return (
        body
        + "</tbody></table><p>Excluded/failed raw attempts remain in the original review page and receipts. Listening-copy screening does not qualify every historical synthesis attempt.</p>"
    )


def writing(
    root: Path, base_url: str, path: Path, data: dict[str, Any], entry: dict[str, Any]
) -> str:
    body = (
        "<p>Script-writing benchmark only. Generated / failed are writing outcomes. This benchmark does not synthesize or independently recognize audio, so no speech Pass/Review/Fail total is assigned. Human content review: "
        + esc(data.get("human_review", "pending"))
        + ".</p>"
    )
    body += source_version(root, path, data, entry, base_url)
    body += (
        "<p>Profile: "
        + esc(data.get("profile"))
        + "; seed: "
        + esc(data.get("seed"))
        + ".</p><table><thead><tr><th>Case / method</th><th>Writing outcome</th><th>Words / estimated duration</th><th>Evidence</th></tr></thead><tbody>"
    )
    for result in data.get("results", []):
        body += (
            f"<tr><td>{esc(result.get('case'))}<br>{esc(result.get('writing_method'))}</td><td>{esc(result.get('status'))}<br>{esc(result.get('error', ''))}</td><td>{esc(result.get('words', 'Unavailable'))} words<br>{esc(result.get('estimated_duration_seconds', 'Unavailable'))} estimated seconds</td><td>"
            + artifact_link(root, base_url, result.get("script"), "Generated script")
            + "<br>SHA256: <code>"
            + esc(result.get("script_sha256", "Unavailable"))
            + "</code></td></tr>"
        )
    return body + "</tbody></table>"


def build(root: Path, catalog_path: Path, output: Path, base_url: str) -> Path:
    root = root.resolve(strict=True)
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port != 8772
        or parsed.path != "/"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Use the local benchmark server root: http://127.0.0.1:8772/")
    catalog, catalog_sha256 = read_report(catalog_path)
    body = (
        "<h1>Whoopy morning listening hub</h1><p>Generated "
        + esc(datetime.now(UTC).isoformat())
        + ". Each dataset below keeps its own source version and evidence scope. There is no combined pass rate or overall winner.</p><p><strong>Human voice / teaching quality remains unreviewed. Automated speech screening is not a human quality score.</strong> Every Review needs listening, and failures stay visible.</p>"
    )
    body += (
        "<p>Explicit catalog SHA256: <code>"
        + esc(catalog_sha256)
        + "</code>. Paths are selected by this catalog, not by newest-folder discovery.</p>"
    )
    renderers = {"legacy": legacy, "campaign": campaign, "writing": writing}
    for entry in catalog.get("datasets", []):
        body += "<section><h2>" + esc(entry.get("label", "Dataset")) + "</h2>"
        if entry.get("note"):
            body += "<p>" + esc(entry["note"]) + "</p>"
        if entry.get("path") is None:
            body += "<p>Dataset path has not been supplied. No results or links have been invented.</p></section>"
            continue
        try:
            path = artifact(root, entry["path"])
            data, digest = read_report(path)
            renderer = renderers[entry["kind"]]
            body += (
                "<p>"
                + artifact_link(root, base_url, path, "Exact input receipt")
                + "<br>Receipt SHA256: <code>"
                + esc(digest)
                + "</code></p>"
            )
            body += renderer(root, base_url, path, data, entry)
        except (ValueError, OSError, KeyError, TypeError) as error:
            body += (
                '<p class="fail">Dataset unavailable: '
                + esc(error)
                + ". No result has been inferred.</p>"
            )
        body += "</section>"
    style = "body{font:17px/1.5 system-ui;max-width:1250px;margin:auto;padding:25px;color:#202526;background:#f4f1e9}section{background:white;border:1px solid #d6d5ce;border-radius:12px;padding:20px;margin:25px 0}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:9px;text-align:left;vertical-align:top}.pass{color:#216146}.review{color:#855b05}.fail{color:#a12f2f}.missing,.not-screened,small{color:#626966}pre,code{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#175c63}"
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Whoopy morning listening hub</title><style>'
        + style
        + "</style></head><body>"
        + body
        + "</body></html>"
    )
    with output.open("x", encoding="utf-8") as stream:
        stream.write(document)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmarks_root", type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8772/")
    args = parser.parse_args()
    print(build(args.benchmarks_root, args.catalog, args.output, args.base_url))


if __name__ == "__main__":
    main()
