# Durable browser Create request IDs

This additive protocol hardens retries when a browser cannot tell whether its
Create POST arrived. The existing lost-response reload flow successfully recovered
a real late-admitted run; a temporary 404 never proves that an original POST
cannot arrive later.

Clients may supply `request_id`, a UUID, to `POST /api/v1/generate`. Allocate and
persist the ID **and the exact request body before sending**. The ID becomes both
`run_id` and `task_id`. A retry must send the original body and ID, rather than
rebuilding controls from current UI settings. Clients that omit the ID keep the
existing behavior and response shape; an already-sent legacy request cannot be
retrofitted with a new ID safely.

Successful initial and replay responses remain202. An identical retry returns
the original task/current durable run status, even if its runtime is now
unavailable. It starts no worker and never implicitly resumes stopped work.
Explicit Resume remains a separate operation. Additive response/run fields are:

```json
{
  "request_id": "the-client-UUID",
  "request_payload_sha256": "64-lowercase-hex-characters",
  "request_hash_version": 1
}
```

POST responses also include `idempotent_replay` (`false` for initial admission,
`true` for replay). `GET /api/v1/runs/{request_id}` recovers an admitted run. A404
means no run is visible at that instant; retain the intent and offer Check Again,
Retry with the same body/ID, or durable cancellation.

To abandon an unknown request safely, use:

```text
POST /api/v1/generation-requests/{request_id}/cancel
{}
```

Only a200 acknowledgement matching the intended ID permits forgetting that
intent and returning to editing:

```json
{"request_id":"the-client-UUID","cancelled":true,"run_id":null}
```

`run_id` is the same UUID when the request already admitted a run. The endpoint
flushes an immutable cancellation tombstone before acknowledging, including when
no Create POST has arrived. A late original POST then cannot admit work. A first
cancellation also requests cancellation of an existing initial worker. Already
completed audio is retained. Repeating an old cancellation acknowledgement does
not cancel a later explicit Resume of the same run.

Errors have `error` (human-readable), `code`, and `request_id`:

| HTTP | code | Meaning |
| --- | --- | --- |
|409|`request_id_conflict`|This UUID belongs to different validated settings or another run.|
|409|`request_cancelled`|Durable proof that this Create request was cancelled; use a new ID for new work.|
|409|`request_in_progress`|The short per-request lock is busy; retry the same ID or check again.|
|410|`request_run_deleted`|The admitted run is unavailable; never recreate it through this old request.|

The410 response also includes `run_id`. Failed network calls and other errors are
not cancellation acknowledgements.

## Identity, concurrency, and recovery

Hash version1 serializes the API's validated `GenerationRequest` to JSON with
typed defaults included and `None` fields excluded, excludes `request_id`, and
strips outer whitespace from `text`. It hashes UTF8 JSON with sorted keys,
compact separators, and finite values only. The receipt stores both that complete
canonical client body and its SHA256, and both must match on replay. It excludes
dynamic installation readiness and resolved pacing-profile hashes; the existing
separate web-launch envelope still retains those resolved launch settings.

The small ledger is `runs/.web-requests/{request_id}/`:

- `request.json`: immutable validated body, hash version, and checksum.
- `cancel.json`: immutable tombstone, allowed before `request.json` exists.
- `admitted.json`: immutable mapping to the same run UUID.

A per-request OS lock serializes registration, conflict detection, cancellation,
and final admission across processes. It is separate from the run's generation
lock. Dynamic readiness checks run outside this short lock; cancellation and
existing admission are checked again before commit.

The initial run record, canonical client receipt, and resolved web-launch envelope
are flushed in a staging directory, then published together by directory rename.
Only afterward is the ledger's admitted mapping committed and the supervisor
started. A retry after a crash before publication can safely admit the same ID;
there was no worker. A crash after publication repairs the mapping from the
matching canonical receipt and returns that run without launching. Existing
startup reconciliation makes an abandoned queued run explicitly resumable.
Unpublished staging evidence is never treated as a run.

The ledger remains outside the run directory so moving a run to trash cannot
allow an old POST to resurrect it. Do not remove ledger entries merely because a
run was deleted. Corrupt or mismatched evidence fails closed.

For first launches with request IDs, the supervisor serializes its final
cancellation check and owned process registration with that request lock. While
waiting for its child it polls the tombstone at bounded intervals, closing the
cross-process gap before the CLI has written ownership metadata. It stops only
its own process handle/group. Recovered arbitrary PIDs are not signalled by this
protocol. Original tombstones do not apply to explicit Resume.
