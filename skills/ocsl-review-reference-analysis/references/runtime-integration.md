<!-- SPDX-License-Identifier: Apache-2.0 -->
# Existing runtime and integration boundaries

The skill and database helper organize reusable research state. Claude builds
the corpus coordinator and database imports inside the research workspace,
using the maintained acquisition and worker interfaces below. Verify a complete
path through accepted judgments before scaling; source downloads alone are not
an execution test.

## Maintained commands

Resolve documentation from `$OCSL_REVIEW_HOME` even when the skill is installed
in a global mirror:

- `docs/literature-review.md`: manuscript ingestion, scope confirmation,
  identity resolution and acquisition in a selected workspace.
- `docs/citation-census.md`: pinned, per-manuscript census preparation,
  submission, result import, resumption and fact export.
- `docs/confidentiality.md`: trusted workspace and operation-specific egress
  authorization. A public corpus still needs the deployment to carry the
  appropriate authorization into worker dispatch.

The current census interface is real; recover a run before inventing another:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" review census list --workspace "$PWD" --json
"$OCSL_REVIEW_HOME/bin/ocsl-review" review census status \
  --workspace "$PWD" --run "$census_run_id" --json
```

Preparation pins the selected worker configuration:

```bash
node tools/ocsl-workers.mjs configs
"$OCSL_REVIEW_HOME/bin/ocsl-review" review census prepare \
  --workspace "$PWD" --mode abstract --worker-config "$census_worker_id" --json
```

The worker ID is the configured ID returned by the deployment, not a model
nickname. Obtain the returned run ID; `run`, `wait`, `resume`, `import` and
`export` are documented in `docs/citation-census.md`. Prepare a separate
`--mode full-text` run for that tier. A `--limit` run is explicitly a pilot.
Do not invent a `review corpus run` command or claim the per-manuscript exporter
automatically performs cross-corpus aggregation.

## Preflight on the real integration path

Before scaling, verify identity agreement, acquired source identity and abstract
provenance. The maintained resolver preserves provider-declared venue aliases
and ISSNs, reconciles printed identifier candidates, and selects publication
status from verified identifiers. Complete abstracts carry structured-provider
provenance and hashes shared by acquisition and census preparation. Older cached
records lacking that provenance need an acquisition retry; do not patch in a
completeness flag or change an already pinned run.

The saved template explicitly permits public literature evidence on its workers
(`evidencePolicy: public-literature`). The broker enables this permission only
for the controller in approved execution, binding the current chat, attempt and
worker revision. The census verifies the published manuscript's public label,
remote source provenance and evidence hashes, recording a disclosure receipt.
A local `--public` flag alone grants no permission. Private attachments and
confidential manuscripts retain the explicit operator-plan route, and operator
restrictions always prevail. If a permission changes, prepare a new pinned run;
do not rewrite the original run. No separate approval is needed for every paper
inside the already authorized public study.

Use byte-bounded job submissions below the broker's 64 MiB request bound and
an explicit study concurrency budget. The broker also enforces its configured
aggregate provider limit. Read compact progress with `status --job ID --view
summary` and incremental outputs with `results --job ID --after CURSOR --limit
100 --out work/result-page.jsonl`; persist the returned cursor only after the
corresponding results are imported. Poll a single job or changed summaries,
not the full accumulated history.

The runner waits for submitted jobs and continues Claude in the same attempt.
Use that continuation to integrate results and submit the next bounded batch.
Claude can implement the corpus coordinator using these primitives; a new
platform scheduler is not required. Do not launch an untracked background
supervisor to keep the chat alive. Test interruption/resumption, retain accepted
assessments and receipts, and make retry decisions explicit.

For a fork, carry the database snapshot and source manifest, not credential
files, sockets or broker job IDs. Pending execution requires new jobs owned by
the destination chat and its current authorized worker configuration. Same-model
replication, a model-family comparison and human adjudication are different
validation designs; preserve the selected one in the analysis specification.
