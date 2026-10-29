This guide runs a serial local macOS MLX benchmark and prepares an offline listening report. Generation requires an explicit --execute flag; printing the plan and building a report never load a model.

Before launch, verify the experimental MLX adapter/worker integration and its pinned offline installation manifest, and pass the stub tests. The `--tts-model mlx-qwen3-custom-voice-1.7b-8bit` and `--tts-model mlx-qwen3-voice-design-1.7b-8bit` choices must be available in the selected source. Existing native Qwen and previous Standard Qwen4B results remain separate evidence; this queue generates fresh HIGH Ministral 3 14B whole-session scripts.

The runner first saves source-snapshot/ (src/scripts/config/pyproject only) and copied configuration with the fresh campaign, excluding local.yaml and bytecode. Snapshot file count and exact byte count are recorded in manifest.json. A campaign-local .gitignore containing `*` is created before any evidence or snapshot. It uses the original verified models with explicit --models-dir. Inside the original checkout, outputs are permitted only in a fresh child folder under evaluations/benchmarks/2026-10-29; the date root itself, older benchmarks, models/, recovery/, and the separate source/publication checkouts remain forbidden. Other fresh external folders are also supported. It strips WHOOPY_* overrides from child environments, disables HF network access, and uses no real provider configuration, downloads, paid API, or authentication/Keychain operation. Model installation verification in the integrated backend must remain fail-closed and offline.

The cases are seated grounding at three minutes and sound-anchored focus without breath guidance at three minutes. The recommended launch also allows one five-minute sleep trial only after the earlier queue has no hard failure. Each case starts with one actual durable CLI prompt-to-audio HIGH + MLX Ryan run. The existing whole-session validator allows an initial draft plus one holistic revision; it saves both and uses seeds 42/43. There is no added prose, padding, editing, token extension, additional retry, or bypass. If generation fails, that case's comparisons are skipped and all partial artifacts remain.

The two mandatory HIGH prompt-to-audio cases run first. For each accepted generated script, the queue then creates separate full-session script-file renders with portable Kokoro am_michael at .8, then MLX Ryan, Aiden, and Serena. Grounding also gets one full-session deep male VoiceDesign render using `--tts-model mlx-qwen3-voice-design-1.7b-8bit --qwen-voice-design-preset grounded_meditation --qwen-language English`; it does not pass --qwen-speaker. These are fresh run IDs and fresh run/cache roots. It checks that the final typed spoken segment texts exactly match the accepted script's final timeline. Script-file comparisons use Basic to avoid reloading an LLM. Explicit --speed .8 and --pace slow avoid an implicit pacing-profile mutation; MLX's actual native controls are recorded by its worker and durable metadata. Estimated articulation planning for the MLX backend remains the existing 133 WPM behavior. Different native speech duration is measured, not corrected by changing prose.

The requested seed is 42. Writer settings are the existing CLI's 32768 context, temperature .7, top_p .9, reasoning off, and 300-second per-request timeout. Deterministic configuration is recorded; identical output across native Metal/runtime versions is not claimed. The manifest retains source hashes/snapshot, git HEAD/status/diff, exact commands, run IDs, model metadata and resolved settings, script/timeline hashes, wall times, and SHA256 inventories. Raw model drafts and failures remain in each production run/workspace. The MLX installation manifest/worker records the pinned model/runtime and exact native generation arguments.

Every final narration gets independent local Whisper ASR. Every selected MLX native FLOAT WAV also gets its own ASR. Expected words come only from final timeline SPEECH text; SILENCE/BREATH events, headings, and pause/delivery markup never enter expected text. The existing verifier uses the preinstalled snapshot, English, temperature zero, no previous-text conditioning, and no expected-text prompt. Every normalized word mismatch requires Review. Large omissions, a missing ending, no recognized speech, eight contiguous word edits, and other existing hard failures stay Fail. ASR disagreement is evidence for listening, not proof of a synthesis defect. Voice comfort, pacing and teaching quality remain pending human listening.

Selected native receipts are resolved from each delivered audio-manifest cache key through native-speech/cache-references. The runner verifies raw WAV/sidecar hashes, natural codec EOS, 24 kHz FLOAT format, segment IDs/text, and native-to-processed PCM hashes. All earlier native attempt metadata remains retained and listed separately. Portable Kokoro does not export a native FLOAT WAV through its current adapter; its final WAV/cache PCM are preserved and clearly identified as processed. No native evidence is invented for that control.

One serial coordinator owns /tmp/whoopy-overnight-e2e.lock. The MLX worker owns /tmp/whoopy-benchmark-gpu.lock during its lifecycle; the ASR worker owns the same lock internally. The runner owns that GPU lock around portable Kokoro only. **Never wrap the MLX CLI or ASR invocation in an outer lock on that same file: it would deadlock.** The writer stage has no benchmark GPU flock. Before every stage, the runner probes both GPU and shared models/runtime-slot/heavyweight.lock and rejects known external inference commands. The coordinator must stop/avoid every external inference job for the whole queue; these cooperative locks cannot exclude unrelated applications or future external writer commands. Do not run benchmarks, other Whoopy generations, or ASR alongside it.

System samples are written every .5 seconds. Fresh HIGH generation requires 16 GiB available, script renders/ASR 4 GiB, and initial disk preflight 20 GiB. Swap growth over 4 GiB, RAM below 2 GiB for two seconds, unavailable system monitoring, a 30-minute generation deadline, a 15-minute ASR deadline, or the eight-hour queue deadline stops the process tree and remaining queue. Descendants are explicitly signaled because adapters may create separate process groups. Partial stdout/stderr, durable run artifacts and resource samples survive stops. Process-RSS permission errors are recorded without weakening the system memory/swap guard.

Prepare and launch only after integration and verification:

```sh
PY=/Users/jakob/codestuff/Whoopy/.venv/bin/python
SOURCE=/Users/jakob/.codex/worktrees/meditation-quality-review/Whoopy
PRODUCERS="$SOURCE/scripts"
PLAN="$SOURCE/config/evaluation/overnight_e2e_plan.json"
"$PY" "$PRODUCERS/overnight_e2e.py" --plan "$PLAN" --source "$SOURCE" --include-sleep
BENCHMARKS=/Users/jakob/codestuff/Whoopy/evaluations/benchmarks
mkdir -p "$BENCHMARKS/2026-10-29"
EVIDENCE="$BENCHMARKS/2026-10-29/overnight-e2e-$(date -u +%Y%m%dT%H%M%SZ)-$(uuidgen)"
nohup caffeinate -is "$PY" "$PRODUCERS/overnight_e2e.py" \
  --plan "$PLAN" --source "$SOURCE" --output "$EVIDENCE" --execute --include-sleep \
  > "$EVIDENCE.launch.log" 2>&1 &
printf '%s\n' "$EVIDENCE"
```

The first command only prints a plan. The nohup command starts inference. Save the printed evidence path. Do not pre-create that directory: exclusive creation protects earlier attempts. Use a fresh path for every restart; failed evidence is never replaced. A source snapshot prevents later checkout edits from changing a running queue. The --plan path is explicit; scripts do not depend on a sibling plan.json filename.

Morning inspection (substitute the exact printed path):

```sh
python3 "$SOURCE/scripts/morning.py" "$EVIDENCE"
python3 "$SOURCE/scripts/listening_report.py" "$EVIDENCE"
```

Use the exact saved path as EVIDENCE if opening a new terminal. The text inspector lists every case/candidate, screening status, exact run ID, generation wall time, final WAV, script, timeline, resolved settings, and full/native ASR reasons. The second command creates a fresh review.html beside manifest.json, plus uniquely named word-diff pages. It does not infer, modify audio, change evidence, or overwrite an existing report. To render another version later, pass `--output review-second.html`.

The listening report contains players for all available full narrations and retained native attempts, shared accepted-script links, actual durations, model/voice/settings/provenance, exact normalized word-diff links, raw ASR receipts, process/resource logs, and retained failures. It identifies fresh prompt-to-audio generation separately from fixed-script voice comparisons. Automated speech screening and unrated human quality are separate labels. Every Review still needs listening; a stopped or failed queue remains evidence, not completed acceptance. Paths outside the campaign and escaping symlinks are never linked; all prompt/model/ASR text is HTML-escaped. Missing ASR cannot become Pass, and a final WAV changed since its ASR receipt becomes Fail.

Serve the entire benchmarks parent locally so the original nine October 28 auditions and the new October 29 report are reachable together:

```sh
BENCHMARKS=/Users/jakob/codestuff/Whoopy/evaluations/benchmarks
python3 -m http.server 8772 --bind 127.0.0.1 --directory "$BENCHMARKS"
```

Leave the existing 8771 server running. The old page stays at `http://127.0.0.1:8771/review.html` and is also available at `http://127.0.0.1:8772/2026-10-28/review.html`. Open `http://127.0.0.1:8772/2026-10-29/` and select the exact fresh campaign folder, then review.html. Nothing is copied or moved. The server binds only to localhost.

The October 28 review set and its nine raw native auditions stay intact. These full-session results form a separate fresh review set. Do not feed these files directly to the current scripts/build_listening_review.py. That producer contains fixed old benchmark claims and a -23 LUFS listening-copy assumption; new final narrations and native WAVs are not those listening copies. The new standalone listening_report.py consumes this campaign's actual receipts without altering that producer or the product UI. Audio payloads remain benchmark outputs; publishing any small reports is a separate root-controlled action.

Run `python -m pytest -q tests/test_overnight_e2e_runner.py tests/test_overnight_listening_report.py` from the project root. These checks use only ordinary Python stub children and receipts: no NumPy, MLX, ASR, model inference or network. They check process retention, resource stops, Git's actual campaign-ignore behavior, artifact links, HTML escaping, truthful statuses, changed-audio detection and no-overwrite behavior. Lock imports and live platform admission are lazy, so plan/report/stub-test imports work without fcntl on Windows. Full live inference requires a separate real launch. Runner exit codes are 0 for all screening Pass, 2 for a completed queue containing Review, and 1 for hard failure, stop, or a mandatory deadline exclusion.
