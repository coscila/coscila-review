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
statistics require an explicitly specified analysis. Historical oncology audit
judgments may be comparators, not undisputed human labels. Parser tests, schema
checks and fixture success do not establish live-provider scientific accuracy.

Synthetic integration coverage is in `tests/test_census.py`, with original
Apache-2.0 JATS fixture `tests/fixtures/citation_census_canary.xml`. No historical
project scripts, research results or publisher manuscript text were copied.
