<!-- SPDX-License-Identifier: Apache-2.0 -->
# Maintained runtime and integration boundaries

Use `$OCSL_REVIEW_HOME/bin/ocsl-review corpus` for v2 corpus execution. The
maintained coordinator uses the shared source library and durable queue; the
controller supplies the study specification, eligible manifest and scientific
decisions. It does not write a replacement coordinator in the chat workspace.

Read `$OCSL_REVIEW_HOME/docs/corpus-review.md` for command examples, protocol
versions, calibration and resumption. `docs/literature-review.md` documents the
underlying acquisition contracts, `docs/citation-census.md` the unchanged v1
per-manuscript lifecycle, and `docs/confidentiality.md` the egress boundaries.

## Preflight and worker roles

Read saved approval/run state and preserve unchanged authorization. Discover
workers with `node tools/ocsl-workers.mjs configs`; worker IDs are configured
IDs, not model nicknames. The **meta meta literature review** template assigns:

| Role | Reasoning | Use |
|---|---|---|
| Primary (`w1` after verifying IDs) | none | Abstract alignment; one item per call until calibration admits four |
| Escalation (`w2`) | low | Targeted full-text assessment; one item per call |
| Replication (`w3`) | none | Selected blind validation; no automatic second census |

These roles share one provider and an aggregate concurrency budget of 60. Do
not interpret 60 as a measured physical limit or allocate 60 independently to
every role. Pin the returned configuration revision, response-side model
identity and scientific settings. Changing a scientific instrument requires a
new analysis; changing queue concurrency does not make old results new.

The saved template permits public literature evidence on its workers
(`evidencePolicy: public-literature`). The broker enables this permission for
approved execution and binds it to the chat, attempt and worker revision. Source
identity, public provenance and evidence hashes must still be verified. A local
`--public` flag does not grant permission. Private attachments retain the explicit
operator-plan route. An already approved public study does not need a new
permission request for each paper. Report a real refusal without bypassing it.

## Durable execution

Run `corpus status`, then `corpus resume` to recover existing work. Use
`corpus execute` for the maintained acquisition-to-export loop and `corpus
calibrate` / `corpus validate` for their separately bounded samples. Import finished
results before retrying incomplete tasks. Use bounded job submissions and
incremental result imports; persist the result cursor only with the corresponding
observations. A failed peer in a multi-item response does not invalidate accepted
items. Missing evidence or an inadequate tier is a scientific escalation, not a
format-repair retry. Keep raw rejected responses and reasons for audit.

The runner owns submitted jobs and its normal worker-result continuation.
Follow this lifecycle rather than starting an untracked background supervisor
or detached heartbeat. Stop checkpoints and cancels managed jobs; normal
resumption reads durable state. Verify a bounded path through accepted judgments,
export and checkpoint/resume before advancing the full approved manifest.

Progress separates article acquisition, unique-source acquisition, citation
entries, loci, accepted judgments, pending tasks and terminal uncertainty. A
pilot, a successful HTTP request or an exported empty database is not a completed
census. No live v2 speed or accuracy benchmark is bundled with the source package.

## Legacy runs and forks

Recover an existing v1 run with `ocsl-review review census list/status/resume`
and its pinned v1 configuration. `corpus import-legacy` preserves those records
as historical provenance; it does not mark v2 tasks accepted. Do not rewrite v1
schemas or completeness flags to make them fit v2.

For a fork, carry a verified database-and-source snapshot. `corpus branch`
creates a new analysis and preserves inherited evidence and labelled history.
The destination chat owns new execution and authorization. Sockets, credentials
and live broker jobs are not scientific handoff artifacts. A new scientific
question also needs its own task schema and method; branching storage does not
make citation-support verdicts answer that new question.
