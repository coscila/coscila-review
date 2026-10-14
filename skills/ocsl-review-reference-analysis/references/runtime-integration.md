<!-- SPDX-License-Identifier: Apache-2.0 -->
# Existing runtime and integration boundaries

The new skill and database helper organize reusable research state. They do not
by themselves repair the production identity resolver, acquisition-to-census
schema, authorization handoff, corpus scheduler or orchestration lifecycle.
Verify these capabilities in the deployment before advertising a full run.

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

Before scaling, verify provider-declared venue aliases survive reconciliation,
all identifier candidates resolve to the acquired work, and complete abstracts
carry the completeness fields consumed by census preparation. These defects
blocked the October 2026 deployment despite successful source downloads.

The authorization check must use the exact selected workspace, approved public
material, operation and worker provider. If it fails, report the relevant
operation and scope to the operator; neither a local `--public` flag nor a
study-scope confirmation is an egress bypass. Do not request repeated study
approval when an implementation defect is preventing an existing authorization
from reaching the gate.

Check aggregate concurrency across jobs; per-job concurrency limits can multiply
unintentionally. Validate accepted real results and explicit uncertainty before
scaling, then test interruption/resumption. No input, source or accepted
assessment may be silently replaced while recovering a runner.

For a fork, carry the database snapshot and source manifest, not credential
files, sockets or broker job IDs. Pending execution requires new jobs owned by
the destination chat and its current authorized worker configuration. Same-model
replication, a model-family comparison and human adjudication are different
validation designs; preserve the selected one in the analysis specification.
