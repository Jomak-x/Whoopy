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


def build(root: Path, *, manifest_path: Path | None = None, output: Path | None = None) -> Path:
    root = root.resolve()
    manifest_path = manifest_path or root / "verification/listening/manifest-policy-v3-strict.json"
    manifest = json.loads(manifest_path.read_text())
    records = manifest["records"]
    random.Random(42).shuffle(records)
    cards = []
    for index, record in enumerate(records):
        audio = local_path(record["output"], root)
        if hashlib.sha256(audio.read_bytes()).hexdigest() != record["output_sha256"]:
            raise ValueError(f"Listening copy changed: {audio}")
        key = html.escape(record["candidate"], quote=True)
        label = f"Voice {chr(65 + index)}"
        proof = evidence_links(record["original_raw_verification"], record["verification"], root)
        status = record["status"]
        description = screening_label(status)
        if record.get("full_file_normalized_word_edit_count"):
            description += (
                f" Full-file ASR has {int(record['full_file_normalized_word_edit_count'])} "
                "normalized word edits. This is a recognition disagreement, not proof that "
                "speech is missing. Exact paragraph recognition and PCM assembly remain "
                "positive evidence."
            )
        cards.append(f'''<article data-key="{key}" data-status="{status}">
<h3>{label} <small class="identity">{html.escape(record["candidate"])}</small></h3>
<p>{description} Listening copy: -23 LUFS, constant gain only.</p>
<audio controls preload="none" aria-label="Listen to {label}" src="{local_url(str(audio), root)}"></audio>
<p><a href="{local_url(record["input"], root)}">Original audio</a> ·
{proof}</p>
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
        writing = "".join(
            f'<article data-status="{sample["status"]}"><h3>{html.escape(sample["label"])}</h3>'
            f"<p>{screening_label(sample['status'])}</p>"
            f'<audio controls preload="none" src="{local_url(sample["audio"], root)}"></audio>'
            f'<p><a href="{local_url(sample["audio"], root)}">Original audio</a> · '
            f'<a href="{local_url(sample["script"], root)}">Script</a> · '
            f"{evidence_links(sample['original_verification'], sample['verification'], root)}</p></article>"
            for sample in samples
        )
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
    output = output or root / "review.html"
    output.write_text(page, encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(build(args.root, manifest_path=args.manifest, output=args.output))


if __name__ == "__main__":
    main()
