<!-- SPDX-License-Identifier: Apache-2.0 -->
# Resumable citation-support measurements

The census keeps model observations separate from expert audit-graph verdicts.
Its unit is a claim–citation occurrence–reference member, not a PMID, reference
list entry, paper, or first three sentences. Repeated references and every
member of a numeric range retain their own tasks. Missing sources and unresolved
bindings remain uncertain observations in the denominator.

## Prepare complete evidence

Follow [literature-review.md](literature-review.md) to ingest the manuscript,
inventory claims, record an actual extensive-scope confirmation, and acquire
sources. Existing authorization can be recorded; never invent a response.
Local `.xml`/`.jats` manuscripts and `.html`/`.htm` publisher articles have
structural citation-target binding. The parser retains original target IDs,
printed labels, DOI/PMID fields, tails, captions, table cells, boxes, lists and
footnotes. Unowned anchors, missing range members and unresolved targets stop a
claim of complete ingestion. HTML must contain a recognized article container;
this command does not fetch publisher pages or certify every publisher layout.

In the selected workspace, use the mapped review-literature environment:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" review census prepare \
  --workspace "$PWD" --mode full-text --worker-config "$census_worker_id" --json
```

Select `census_worker_id` from the platform's configured workers, visible with
`node tools/ocsl-workers.mjs configs`. The deployed Lab materializes the helper
inside the selected chat workspace at `tools/ocsl-workers.mjs`; census resolves
that exact workspace path by default. An operator may set `OCSL_WORKERS_CLI`
to an explicit absolute helper path. The protected UNIX socket is provided
by `PUPPENCLAW_ORCHESTRATION_SOCKET`. Credentials stay with the broker.

Prepare reads only the broker's public configuration. It pins the selected
worker ID, revision, public settings hash, prompt, output schema, manuscript
hash, binding universe, complete evidence units and source hashes. The manifest
and tasks live under `.ocsl-review/census/<runId>/`. Identical preparation is
idempotent. `--limit N` or repeated `--reference-id ID` explicitly labels a pilot.
Preparation needs a valid confirmed literature ledger and complete manuscript
ingestion; unverified identity, unavailable evidence and incomplete sources are
recorded as blocked tasks, never submitted for a content judgment.

`--mode abstract` uses the complete acquired abstract independently of available
full text. `--mode full-text` requires complete full text. `--mode panel` records
a separate full-text pass with the selected configuration; it does not invent
panel members or consensus. Select an actual panel configuration or perform
separate model runs and a separately specified adjudication pass. Mode changes
create separate immutable runs.

## Submit, reconnect and retry

Read the returned `runId`, then:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" review census run \
  --workspace "$PWD" --run "$census_run_id" --json
"$OCSL_REVIEW_HOME/bin/ocsl-review" review census status \
  --workspace "$PWD" --run "$census_run_id" --json
"$OCSL_REVIEW_HOME/bin/ocsl-review" review census wait \
  --workspace "$PWD" --run "$census_run_id" --timeout-ms 60000 --json
```

API-capable configurations use a bulk job with strict output schema. Native
Claude/Codex configurations use one recorded agent job per locus, with the same
schema embedded in the task and independently validated on import. Agent task
files contain complete evidence. A provider context limit is a failure to
resolve with a suitable configuration, never permission to trim the source.

For bulk jobs, `run --concurrency N` requests a positive safe-integer number of
concurrent calls (at most 9007199254740991). The broker still enforces its live
capacity and aggregate provider limit; the flag cannot grant extra capacity.
Omit it to retain the broker's default. This execution control does not change
prompts, evidence, reasoning, schemas or the pinned scientific instrument, and
is rejected for native agent jobs. It applies to a new submission, including
`run --retry`, not an already submitted job.

An operator may change the advertised `bulkConcurrencyLimit` without making an
otherwise unchanged pinned worker configuration obsolete. The stored snapshot
and its original hash remain intact and are still validated. Only that live
capacity field is ignored when comparing settings; revision, model, assignment,
reasoning and disclosure-policy changes continue to require a new prepared run.

Wait is bounded to one job and writes the helper's `--out` JSONL inside the
workspace, avoiding a huge stdout payload. For native or multi-job runs, select
`--job ID` from status. Use `review census list --workspace "$PWD" --json` to
recover run IDs after reopening the session. Repeating `run` does not duplicate
already submitted tasks; an interrupted native submission continues remaining
tasks. Each accepted job submission is checkpointed immediately.

`resume --run ID [--job ID]` imports completed old results, rechecks source
hashes, egress and pinned settings, and records any replacement job ID returned
by the broker. It preserves the earlier job and assessment history. `cancel`
has the same run/job selection. After fixing a rejected output, `run --retry`
creates a new attempt only for tasks without accepted assessments. Settings
changes require a new prepared run. Provider quota, approval, interruption and
failure states remain explicit; they cannot count as scientific success.

Dispatch requires the exact trusted scholarly workspace context. The default
confidential plan blocks manuscript evidence and worker assignments before
submission. If disclosure is already authorized, an operator must provide the
separately reviewed, hash-bound plan described in
[confidentiality.md](confidentiality.md), permitting `citation_census` for the
approved material. A local scope confirmation alone does not authorize this
disclosure; do not bypass the gate with shell networking or another provider.

## Validate observations and preserve history

The pinned schema requires a verdict, confidence, rationale, read-unit IDs,
verbatim quotes and abstract-sufficiency reason. Import checks complete reading
unit coverage and exact quoted substrings against hashed stored evidence.
Specific numeric, causal, quotation, method, software, dataset and priority
claims cannot receive a substantive abstract-based verdict. Silence in an
abstract cannot establish `not-addressed`. Abstention and partial evidence
remain uncertain; model-reported confidence is retained, not a gold label.

Both accepted and rejected results stay in the append-only SQLite ledger with
actual provider/model, prompt receipt, raw usage and diagnostics. Native job
receipts can include every underlying request digest. Replaying the identical
result is idempotent; distinct attempts and revised judgments remain separate.
Ordinary `review literature record` also retains content-addressed immutable
history, including source identity and evidence tier, while updating its active
audit projection.

To import a broker result file already inside the workspace:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" review census import worker-results.jsonl \
  --workspace "$PWD" --run "$census_run_id" --job "$census_job_id" --json
```

The job mapping is essential for native job IDs and for distinguishing calls
across retries. Unknown task IDs, changed input/schema/source hashes, incomplete
reading and invented quotes cannot produce accepted scientific observations.
Mechanical reading declarations do not prove that a model attended to all
content; scientific validity still needs independently adjudicated evaluation.

## Export reproducible facts

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" review census export \
  --workspace "$PWD" --run "$census_run_id" --output results/census --json
```

The export writes `facts.json`, `facts.sqlite3` and `assessments.jsonl`. Each
fact carries its value, one scalar `SELECT`, method description, relative source
path and SQLite SHA-256. `measurements` contains the latest accepted observation
per task; pending/blocked/abstained/partial tasks stay uncertain. `worker_calls`
contains every distinct recorded attempt with provider/model, input/output/total
tokens, cache subsets, reasoning, quality and original usage JSON. Missing
counters remain NULL. Cache and reasoning counters are subsets whose provider
semantics must not be added blindly to totals. Summed recorded counters are
known subtotals; `calls_missing_total_tokens` exposes incomplete accounting.

`unsupported_share` divides adverse observations by classifiable observations,
excluding uncertain ones and returning NULL when that denominator is zero.
Always report both denominator and uncertain count. These are model-measured
support categories, not a prevalence estimate or a confidence-calibrated human
judgment. Facts verification in the writing companion checks the database hash
and reruns SQL before rendering numeric prose; this checks derivation, not truth.

For abstract/full-text, panel or model comparisons, use matched loci and source
versions, retain paired direction changes and all missingness, and account for
clustering within papers. This exporter handles one pinned run; cross-run
statistics require an explicitly specified analysis. Judgments of other runs
are not human labels. Parser tests, schema checks and fixture success do not
establish live-provider scientific accuracy.

## Replay a registered-instrument census

A `citation-census@1` study (`ocsl-review study`, see
[census-instrument.md](census-instrument.md)) records every verdict a model call
produced in the scores database's append-only `history`: with the call and the
position in the call, or, for an abstract-tier entry still without a parseable
line after the answered first and repair calls of its batch, as `invalid`
without a call. `study replay` re-derives those verdicts from the responses the
broker stores for the settled calls, without a model call:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" study replay --census work/census.sqlite3 \
  --broker-socket "$OCSL_BROKER_SOCKET"
"$OCSL_REVIEW_HOME/bin/ocsl-review" study replay --census results/scores.sqlite3 \
  --responses results/items.jsonl --output results/replay.sqlite3
```

For each call that decided a recorded verdict, replay reads the call's item (the
job and item of its `call_transport` row) over the run principal's socket
(`GET /jobs/<jobId>/results?after&limit`, as the judging steps read results;
`--broker-socket`, by default `OCSL_BROKER_SOCKET`), or from the JSON lines
`{"jobId","itemId","state","output","outputDigest","requestSha256","servedModel"}`
the lab exports from the daemon's job results, with `errorCode`, `httpStatus`
and `attempts` for an item that did not complete (`--responses`; a later line
for the same job and item replaces an earlier one, and an absent `output` stays
absent). The daemon keeps the items of settled jobs, also once it has archived
them. The response the daemon stores for a settled call is the item's output
when the item completed, or the judge server's refusal of the request (HTTP 400,
413 or 422 after a provider call), which the census counts as an answer without
labels. Before anything is parsed the item is checked against the receipts:

- its `requestSha256` equals both the broker's request digest in the call's
  receipt (`broker_request_sha256`) and the canonical digest of the request the
  instrument built (`request_sha256`);
- a completed item was served by the instrument's required model, and its
  `outputDigest` equals the digest of its output computed as the daemon computes
  it (a string as its UTF-8 bytes, any other value as `JSON.stringify` writes
  it);
- a refused item stores no output (the receipt's output digest is the digest of
  no output), and the call's receipt records the same refusal (error code and
  HTTP status).

The census database must be pinned to the installed instrument's digest, so that
the parser is the census's.

A response that passes is parsed exactly as the census parsed it: the
instrument's label parser at the call's tier, for first and repair calls of an
abstract-tier batch, full-text calls and the sensitivity variants; an empty
completion or a refusal yields no verdict (a full-text call's entries are then
`missing`). Each re-derived entry is compared with the history row of that call
and position on outcome, label, category, support verdict and escalation, and on
the quote unless the recorded quote was redacted. An entry recorded as `invalid`
replays when an answered repair call carried it and none of the answered calls
of its batch that carried it, first and repair calls, yields a parseable line
for it. A primary batch writes each answered call's verdicts with its receipt,
so all of its answered calls count; a sensitivity batch that did not settle runs
again whole, so the calls of its last run count (the census numbers the calls of
each batch and kind, and the call ids give that order back). Every history row
gets one outcome:

| Outcome | Meaning |
| --- | --- |
| `replayed` | the re-derived verdict equals the recorded one |
| `mismatched` | it differs (an invalid entry for which an answer yields a line, or that no answered repair call carried), or the answer yields a verdict the history lacks (counted as an entry as well) |
| `unavailable` | the call has no stored response: a direct call, a history call id without a receipt, a receipt without a broker job, an item the broker does not hold, or one that did not settle or failed in transport |
| `digest-failure` | the stored response failed a check: request digests, output digest, served model, recorded refusal or instrument digest |
| `outside-replay` | no answered call decided the entry: decided without a model call (an entry never cited or without an identified work), or a primary batch's entry closed after none of its calls was answered (`invalid:no-response`) |

Over the socket only `WORKER_NOT_FOUND` means that the broker holds no such job
(the call's entries are then unavailable). A socket that cannot serve the run's
results at all (`PRINCIPAL_REVOKED`, a socket without the principal's worker
routes) is invalid input. A socket that stops answering, or keeps refusing a
result page for any other reason (a daemon-side error, a server error), is an
outage, never a call without a stored response: the command then ends as a
transient failure and records nothing, so it can run again.

The results go to `replay_calls` (one row per call: job, item, outcome, the
output and request digests and the checks), `replay_entries` (one row per entry:
the recorded and the re-derived verdict as compared, counted with the call whose
response decided it) and `replay_meta` (the record: summary, report digest, the
history rows replayed and the last history id). They are new tables of the
census database, which must then be writable and not frozen (the working
database, under its writer lock and in one transaction; a later replay replaces
them; a read-only file or directory is refused before any lock file is made), or
with `--output` a new SQLite file, written beside its final name and renamed
into place read-only, while the census database (a frozen copy, for example) is
only read; that file also records the sha256 of the census database it read, and
a census database that changes meanwhile ends the replay as a transient failure.
Neither table holds output text: verdict fields are compared one by one, and
quotes are kept as `sha256:` digests.

The command prints one JSON summary: the entries `replayed`, `mismatched`,
`outsideReplay`, `unavailable` and `digestFailures`, the `calls` replayed, all
`entries`, and `reportSha256`, the sha256 over both result tables' rows in a
fixed order (recomputable from them). It exits 0 when every entry in scope
replays, 3 when any entry is mismatched, unavailable or a digest failure, 2 on
invalid input and 75 on an outage (nothing is recorded then). When the scores
database holds a replay (an in-place replay before `study freeze`) that covers
its verdict history, `study facts` exports
`census.<journal>.replay.{entries,replayed,mismatched,unavailable,digest_failures,outside_replay,share_replayed}`
and the same over all journals (`census.replay.*`) as SQL facts, which
`study verify-facts` executes again; each derivation checks that the replay
compared the frozen history (its rows and last id). A replay made before more
verdicts were recorded exports no replay facts (the export's result says why):
replay the census again before freezing. `share_replayed` divides the replayed
entries by the entries an answered call decided (all but those outside replay).

Synthetic integration coverage is in `tests/test_census.py`, with original
Apache-2.0 JATS fixture `tests/fixtures/citation_census_canary.xml`. No historical
project scripts, research results or publisher manuscript text were copied.
