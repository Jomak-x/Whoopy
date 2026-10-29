"""Build a local, offline audition page from verified listening-copy evidence."""

# HTML/CSS/JS template lines retain their natural layout.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
from pathlib import Path
from urllib.parse import quote


def local_path(value: str, root: Path) -> Path:
    root = root.resolve()
    path = Path(value)
    anchor = ("evaluations", "benchmarks", root.name)
    for index in range(len(path.parts) - len(anchor) + 1):
        if path.parts[index : index + len(anchor)] == anchor:
            path = Path(*path.parts[index + len(anchor) :])
            break
    path = (root / path).resolve()
    path.relative_to(root)
    return path


def local_url(value: str, root: Path) -> str:
    return quote(local_path(value, root).relative_to(root.resolve()).as_posix())


def screening_label(status: str) -> str:
    return {
        "pass": "Pass — no normalized word mismatches in retained ASR evidence.",
        "review": "Review — listening required to resolve screening concerns.",
        "fail": "Fail — retained hard-fail screening result; not qualified.",
    }[status]


def evidence_links(original: str, current: str, root: Path) -> str:
    links = [(current, "Current policy screening"), (original, "Original ASR evidence")]
    report = json.loads(local_path(original, root).read_text())
    if report.get("full_file_report"):
        links.append((report["full_file_report"], "Full-file ASR disagreement"))
    if report.get("assembly_proof"):
        links.append((original, "Exact audio assembly proof"))
    for index, segment in enumerate(report.get("segments", []), 1):
        links.extend(
            [
                (segment["segment"], f"Paragraph {index} raw audio"),
                (segment["report"], f"Paragraph {index} ASR evidence"),
            ]
        )
    return " · ".join(f'<a href="{local_url(path, root)}">{label}</a>' for path, label in links)


def audio_problem(value: str, expected_hash: str | None, label: str, root: Path) -> str | None:
    try:
        artifact = local_path(value, root)
        if not artifact.is_file():
            return f"{label} missing."
        if not expected_hash:
            return f"{label} hash missing."
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected_hash:
            return f"{label} changed."
    except (OSError, ValueError):
        return f"{label} unavailable."
    return None


def write_current_report(output: Path, page: str) -> None:
    content = page.encode("utf-8")
    if output.exists():
        previous = output.read_bytes()
        if previous == content:
            return
        history = output.with_name(f"{output.name}.history")
        history.mkdir(exist_ok=True)
        archived = history / f"{hashlib.sha256(previous).hexdigest()}.html"
        try:
            with archived.open("xb") as stream:
                stream.write(previous)
        except FileExistsError:
            if archived.read_bytes() != previous:
                raise ValueError(f"Historical report changed: {archived}") from None
    output.write_bytes(content)


def build(root: Path, *, manifest_path: Path | None = None, output: Path | None = None) -> Path:
    root = root.resolve()
    output = output if output is not None else Path("review.html")
    if output.parent != Path(".") or output.name in {"", ".."}:
        raise ValueError("Output must be a report filename directly inside the benchmark root")
    output = root / output
    if output.is_symlink() or output.with_name(f"{output.name}.history").is_symlink():
        raise ValueError("Output and report history must not be symbolic links")
    manifest_path = manifest_path or root / "verification/listening/manifest-policy-v3-strict.json"
    manifest = json.loads(manifest_path.read_text())
    records = manifest["records"]
    random.Random(42).shuffle(records)
    cards = []
    for index, record in enumerate(records):
        problems = [
            problem
            for field, label in (("input", "Original audio"), ("output", "Listening copy"))
            if (problem := audio_problem(record[field], record.get(f"{field}_sha256"), label, root))
        ]
        key = html.escape(record["candidate"], quote=True)
        label = f"Voice {chr(65 + index)}"
        status = "fail" if problems else record["status"]
        if problems:
            description = (
                "Fail — "
                + " ".join(problems)
                + " Historical screening does not qualify current audio."
            )
            playback = ""
            proof = ""
        else:
            description = screening_label(status) + " Listening copy: -23 LUFS, constant gain only."
            playback = f'<audio controls preload="none" aria-label="Listen to {label}" src="{local_url(record["output"], root)}"></audio>'
            proof = (
                f'<p><a href="{local_url(record["input"], root)}">Original audio</a> · '
                + evidence_links(record["original_raw_verification"], record["verification"], root)
                + "</p>"
            )
        if not problems and record.get("full_file_normalized_word_edit_count"):
            description += (
                f" Full-file ASR has {int(record['full_file_normalized_word_edit_count'])} "
                "normalized word edits. This is a recognition disagreement, not proof that "
                "speech is missing. Exact paragraph recognition and PCM assembly remain "
                "positive evidence."
            )
        cards.append(f'''<article data-key="{key}" data-status="{status}">
<h3>{label} <small class="identity">{html.escape(record["candidate"])}</small></h3>
<p>{description}</p>
{playback}
{proof}
<label>Your decision <select data-field="decision"><option value="">Unrated</option>
<option>Keep</option><option>Maybe</option><option>Reject</option></select></label>
<label>Calm, natural and comfortable (1-5) <input data-field="comfort" type="number" min="1" max="5"></label>
<label>Notes <textarea data-field="notes" rows="2"></textarea></label></article>''')
    writing = (
        "<p>Writing comparison audio is still being prepared. Refresh for the final package.</p>"
    )
    samples_path = root / "writing-listening-policy-v3.json"
    if samples_path.exists():
        samples = json.loads(samples_path.read_text())["samples"]
        writing_cards = []
        for sample in samples:
            problem = audio_problem(
                sample["audio"], sample.get("audio_sha256"), "Writing sample audio", root
            )
            status = "fail" if problem else sample["status"]
            if problem:
                description = (
                    f"Fail — {problem} Historical screening does not qualify current audio."
                )
                playback = ""
                proof = ""
            else:
                description = screening_label(status)
                playback = f'<audio controls preload="none" src="{local_url(sample["audio"], root)}"></audio>'
                proof = (
                    f'<p><a href="{local_url(sample["audio"], root)}">Original audio</a> · '
                    f'<a href="{local_url(sample["script"], root)}">Script</a> · '
                    f"{evidence_links(sample['original_verification'], sample['verification'], root)}</p>"
                )
            writing_cards.append(
                f'<article data-status="{status}"><h3>{html.escape(sample["label"])}</h3>'
                f"<p>{description}</p>{playback}{proof}</article>"
            )
        writing = "".join(writing_cards)
    script = """const key='whoopy-audition-2026-10-28';
let saved={};try{saved=JSON.parse(localStorage.getItem(key)||'{}')}catch{}
for(const card of document.querySelectorAll('[data-key]')){
 for(const field of card.querySelectorAll('[data-field]')){
  field.value=saved[card.dataset.key]?.[field.dataset.field]||'';
  field.addEventListener('input',()=>{
   saved[card.dataset.key]??={};saved[card.dataset.key][field.dataset.field]=field.value;
   localStorage.setItem(key,JSON.stringify(saved));});}}
document.querySelector('#names').addEventListener('change',e=>document.body.classList.toggle('reveal',e.target.checked));
document.querySelector('#export').addEventListener('click',()=>{
 const blob=new Blob([JSON.stringify({human_ratings:saved},null,2)],{type:'application/json'});
 const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='whoopy-listening-notes.json';a.click();URL.revokeObjectURL(a.href);});"""
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Whoopy listening lab</title>
<style>body{{font:17px/1.55 system-ui;margin:0;background:#f3f0e8;color:#222}}main{{max-width:1000px;margin:auto;padding:32px}}h1{{font-size:2.4rem;line-height:1.1}}article{{background:white;border:1px solid #d8d4ca;border-radius:14px;padding:22px;margin:18px 0}}audio{{width:100%}}label{{display:block;margin:12px 0}}select,input,textarea,button{{font:inherit;padding:7px;border:1px solid #aaa;border-radius:5px}}textarea{{display:block;width:95%}}a{{color:#315c57}}.identity{{display:none}}.reveal .identity{{display:block;color:#666;font-size:.85rem}}button{{cursor:pointer}}:focus-visible{{outline:3px solid #286b65;outline-offset:3px}}small{{font-weight:normal}}</style>
<main><h1>Whoopy listening lab</h1><p>One original control script, several local voices.
Each sample shows its current screening status. Every normalized word mismatch requires listening review;
retained hard failures remain failures. These labels reassess existing receipts without new recognition
and do not establish word-perfect speech. Warmth, pacing and teaching quality still need your judgment.</p>
<p>Names are hidden initially to reduce expectation bias; this page is an informal audition, not a completed blind study.
Your notes stay in this browser. No ratings are sent anywhere.</p>
<label><input id="names" type="checkbox"> Reveal voice names and runtime variants</label>
<button id="export">Export my notes</button><h2>Voice auditions</h2>{"".join(cards)}
<h2>Old and new writing</h2>{writing}
<h2>Failures and exclusions</h2><p>The native Qwen Ryan sample failed localized speech matching.
Dots was stopped for memory pressure; Fish Drama 3 was excluded by memory preflight and Higgs by license fit.</p>
<p><a href="voices/LICENSE-AUDIT.txt">License audit</a> · <a href="voices/excluded-candidates.json">Exclusions</a> ·
<a href="voice-script.txt">Shared voice script</a></p></main><script>{script}</script></html>"""
    write_current_report(output, page)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        metavar="FILENAME",
        help="Report filename inside the benchmark root (default: review.html)",
    )
    args = parser.parse_args()
    print(build(args.root, manifest_path=args.manifest, output=args.output))


if __name__ == "__main__":
    main()
