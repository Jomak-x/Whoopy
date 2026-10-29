#!/usr/bin/env python3
"""Read retained evidence; no inference or changes."""

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("evidence", type=Path)
args = parser.parse_args()
manifest = json.loads((args.evidence / "manifest.json").read_text())
print("Queue:", manifest["status"])
if manifest.get("stop_queue_reason") or manifest.get("error"):
    print("Stop:", manifest.get("stop_queue_reason") or manifest.get("error"))
for record in manifest["records"]:
    print(f"\n{record['case']} | {record['candidate']} | {record['status']} | {record['run_id']}")
    print("Generation seconds:", round(record.get("generation", {}).get("wall_seconds", 0), 2))
    if "actual_seconds" in record:
        print("Target/audio seconds:", record["target_seconds"], round(record["actual_seconds"], 3))
    if record.get("error"):
        print("Error:", record["error"])
    attempt = Path(record["attempt"])
    run = attempt / "production-runs" / record["run_id"]
    for name in (
        "narration.wav",
        "script.md",
        "timeline.json",
        "resolved-config.json",
        "model-metadata.json",
        "expected-spoken.txt",
    ):
        if (run / name).exists():
            print(name + ":", run / name)
    print("Full narration ASR:", attempt / "verify-final.json")
    for native in record.get("native_verification", []):
        report = native["verification"]
        print("Native", native["segment_id"], report["status"], native["metadata"])
        for reason in report.get("failure_reasons", []) + report.get("review_reasons", []):
            print("  ", reason)
    final = record.get("final_verification", {})
    for reason in final.get("failure_reasons", []) + final.get("review_reasons", []):
        print("  ", reason)
for skipped in manifest.get("skipped", []):
    print("Skipped:", skipped)
print(
    "\nEvery Review requires listening. Pass is strict ASR screening, not a voice/teaching rating."
)
