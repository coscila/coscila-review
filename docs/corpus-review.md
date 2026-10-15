<!-- SPDX-License-Identifier: Apache-2.0 -->
# Reusable corpus workflow (v2)

`ocsl-review corpus` is the maintained corpus acquisition and judgment workflow.
The literature library holds raw sources and a SQLite database; the controller
coordinates the approved question and interprets the exported facts. Existing
`review census` v1 runs and their stricter assessment rules remain unchanged.

Use the installed `ocsl-review-reference-analysis` skill and the configured
advanced-orchestration workers. Library and output paths are within the selected
workspace; `import-snapshot` may read an explicitly supplied external seed bundle.
Every corpus command accepts `--workspace` and `--library`; inspect its `--help`
for optional controls. Provider and broker operations still require the actual
deployment's authorization; installing the commands does not establish access.

## Initialize and acquire

Freeze a protocol JSON with eligibility, source policy, evidence tiers, estimands,
model settings, sampling and validation limits. Record the exact research question.
Create the eligible review manifest from recorded provider queries and retain
excluded, unresolved and inaccessible records. JSON accepts a list or a
`{"reviews": [...]}` object; CSV accepts equivalent fields. Each review needs a
stable `doi`, `pmid`, `pmcid` or `id`; retain `title`, `journal`, `date`, `label`,
`stratum` and `inclusion_state` (`included`, `excluded` or `unresolved`) as available.
The manifest is an input, not an assumption that every listed paper is readable.
`corpus enumerate --query QUERY --label LABEL` records a paginated provider
query in the library; use the frozen journal/date/stratum queries and reconcile
provider article types with publisher labels before treating its output as the
eligible study population.

```bash
ocsl-review corpus init --workspace . --library work/literature \
  --question "How does citation support vary across the declared journal strata?" \
  --analysis citation-support-v2 --protocol work/study/protocol.json
ocsl-review corpus import-manifest work/study/reviews.json \
  --workspace . --library work/literature
ocsl-review corpus acquire --workspace . --library work/literature
ocsl-review corpus prepare --workspace . --library work/literature --tier abstract
```

Acquisition shares verified cited-work evidence across reviews, retaining raw
responses, identity decisions, source versions and access outcomes. It does not
merge reference slots or citation loci. Structured JATS and publisher HTML retain
numeric-range members and display citations; unresolved extraction remains
visible. Cache reuse retains source provenance. An old transient failure must
not become a permanent assertion that a source does not exist.

For an exact Europe PMC identifier match with a consistent title and complete
structured abstract, the batch response is reused directly. It does not trigger
another Crossref/PubMed lookup for each reference. Missing or conflicting
records use the broader resolver; explicit independent verification and targeted
full-text acquisition remain available. Stored publication status states its
coverage: indexed retraction flags are positive signals, while a missing or
negative flag is **unknown**, not a global assertion that the paper is current.
Cache requests and portable metadata redact credentials and retain only an
authorization-scope fingerprint; provider bodies that echo supplied credentials
are rejected before persistence.

Use `corpus import-legacy PATH --kind historical|census` for an explicitly selected
historical database or census input. The source is imported as labelled history,
not overwritten or counted as fresh v2 judgments. Import reports and raw inputs
must reconcile the original records. A scores-only import cannot supply absent
source material for a new question.

Retained structured review files are re-parsed with the maintained extractor after
their content hashes and article DOIs are checked. Complete retained provider
abstracts can be reused with their original evidence and printed-reference identity
checks. Legacy abstract fields without sufficient provenance remain incomplete and
require acquisition; an old publication-status observation does not establish a
paper's current status.

For a verified reusable seed supplied by the operator, import the external
snapshot, then branch it into a new analysis. The operator supplies the actual
seed location; `/external/seed` below is an example, not a deployed path.

```bash
ocsl-review corpus import-snapshot /external/seed \
  --workspace . --library work/seed
ocsl-review corpus branch --workspace . --library work/seed \
  --output work/literature --question "The new study question" \
  --analysis citation-support-v2 --protocol work/study/protocol.json
```

`import-legacy --kind evidence` imports supported original evidence databases
and raw captures through a verified adapter. Review its reconciliation before
judging. A seed contributes source material and labelled history; it never
pre-accepts new tasks. Initialization and branching register the implementation
files, schemas, prompts, dependency versions and licenses as immutable artifacts.
The run's code revision identifies the source tree, and dispatch rejects a
changed implementation under the same pinned analysis.

## Calibrate the instrument, then execute

Verify actual worker IDs with `node tools/ocsl-workers.mjs configs`. The template
uses `none` for primary abstract assessment, `low` for targeted full-text
assessment, and `none` for selected blind replication. All share the same
aggregate capacity budget of 60. The launch default is **one locus per call**.

```bash
ocsl-review corpus calibrate --workspace . --library work/literature \
  --worker-config w1 --low-worker-config w2 --sample-size 300 --validation-cap 1000 \
  --execute --until-complete
ocsl-review corpus status --workspace . --library work/literature
```

Without `--execute`, calibration only prepares or reports local state. With it,
the command imports existing results and advances one bounded round through the
authorized broker. `--until-complete` runs the maintained polling loop, emits
progress and stops if the provider pauses; a later invocation resumes saved
work. Pending variants are not a passed gate.

Calibration selects up to 300 unique loci and compares four variants: `none`
one-item, `none` four-item, reordered `none` four-item, and `low` one-item. This
is up to **1,200 item judgments**, before separately recorded bounded format
repairs. The additional validation cap is **1,000 unique loci**, not a ceiling
of 1,000 calls for the 1,200-judgment calibration. Record calls, attempts and
tokens separately from sampled loci and keep this budget visible.

Four-item production requires at least 99% individually valid items and at
least 95% exact paired verdict agreement with the single-item baseline, including
the reordered position check. Retain sample IDs, item order, raw responses,
quotes and receipts. Agreement measures stability, not scientific accuracy;
compare with independently adjudicated evidence where available and describe
its limits. If the gate fails or has not run, retain batch size one. Source
tests do not establish that the live gate has passed.

After a qualifying recorded calibration, execute with `--batch-size 4`. Each item
retains its locus, task and evidence identity; a four-item call is transport
grouping, not a merged scientific unit. `--max-jobs N` bounds a continuation
segment; it does not declare the remaining corpus complete. Avoid multiplying
the concurrency budget by the number of reviewers or worker roles.

For managed end-to-end execution after preparation/calibration:

```bash
ocsl-review corpus execute --workspace . --library work/literature \
  --worker-config w1 --low-worker-config w2 --batch-size 1 --concurrency 60
```

This maintained loop acquires missing inputs, prepares and imports abstract
judgments, follows targeted full-text escalations and exports results. Use
`corpus run --worker-config w1 --tier abstract --batch-size 1` for one bounded
submission and `corpus resume` for import/recovery when controlling stages
explicitly. A single `run` call is not the entire-corpus loop.

After primary judgments are available, run the separately capped blind sample:

```bash
ocsl-review corpus validate --workspace . --library work/literature \
  --worker-config w3 --sample-size 1000 --execute --until-complete
```

This selects a frozen sample within the calibration manifest's additional
validation cap: half stratified and the remainder enriched for complex,
adverse or uncertain claims, with stratified fill when needed. It uses only
accepted primary abstract tasks, passes no earlier verdict to the worker, and
reports agreement separately by selection component. Run it after the intended
primary population is available; if fewer eligible loci exist, the recorded
sample is smaller. Repeated calls resume that same sample rather than adding
another 1,000 loci. The enriched mixture is not a population prevalence estimate.
Without `--execute`, it only prepares/reports; `--execute --until-complete`
uses the maintained recovery and polling loop. Agreement reports include
descriptive Wilson 95% intervals, which do not account for review clustering or
enriched selection; the manuscript needs its declared clustered uncertainty
analysis. Full-text follow-up remains a separate evidence
tier, and same-model reliability is not a human accuracy benchmark.

## Evidence and escalation

The `citation-support-v2` prompt permits abstract alignment for explicitly
quoted specific claims, including numbers or methods. A claim classifier is
descriptive, not an automatic abstention rule. Missing detail escalates to
full text. Abstract silence cannot establish contradiction or `not-addressed`.

Prepare full-text tasks with `corpus prepare --tier full-text`; use the selected
escalation role and single-item calls. Preserve the full acquired source, exact
supplied units and their coverage. Excerpts can support a positive or conflicting
statement, but only complete full-text coverage permits `not-addressed`.
If additional context is necessary, acquire it or record explicit uncertainty.

The response contains the item ID, verdict, short rationale, exact quote/unit
pairs, and explicit escalation with its reason. It contains no confidence score
or all-unit ID echo. Local validation checks each item independently. Accepted
peers survive invalid or missing peers; preserve rejected attempts. Format
repair is bounded. Missing evidence, changed source identity and inadequate
coverage require their own resolution rather than repeating the same inference.

## Resume, export and branch

```bash
ocsl-review corpus resume --workspace . --library work/literature
ocsl-review corpus export --workspace . --library work/literature --output results/corpus
ocsl-review corpus snapshot --workspace . --library work/literature --output results/literature-v2
ocsl-review corpus verify --workspace . --library results/literature-v2
ocsl-review corpus branch --workspace . --library results/literature-v2 \
  --output work/followup --question "How have study designs changed?" \
  --analysis study-design-v1 --protocol work/study/design-protocol.json
```

Resume imports existing results before resubmitting missing work. An accepted
assessment is not rerun solely because a chat reconnects. Changed scientific
inputs or settings require a new analysis; histories remain distinct. A snapshot
contains a consistent SQLite database and registered source artifacts. Verify
it before transfer and in the destination workspace. Never transfer credentials
or live job ownership as part of the corpus.

A branch reuses evidence while creating a new question/protocol namespace.
Citation-support tasks do not automatically answer study-design, cohort-size or
other questions; define appropriate task and output schemas in that analysis.
Use analysis filters in every query and preserve inherited observations as
history. Snapshot the branch before exporting it alone so parent-source
dependencies are materialized.

Completion requires terminal accounting for every eligible review, entry and
required locus, plus all requested deliverables. Report unavailable evidence
and unfinished queues separately. Facts, tables, figures and manuscript claims
must trace to the same frozen database, registered code and query definitions.
Fresh model inference can vary; preserved responses and deterministic analysis
code make the reported results replayable without claiming identical new calls.
