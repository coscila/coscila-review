<!-- SPDX-License-Identifier: Apache-2.0 -->
# A database a future thread can reuse

The portable deliverable is a bundle: `corpus.sqlite3`, registered evidence in
`sources/`, and a snapshot `manifest.json`. A PDF or table of final scores alone
does not retain the evidence needed to ask a different scientific question.

## Separate evidence from interpretation

The bundled `assets/literature-schema.sql` defines the initial schema. It keeps
the population and works (`corpora`, `works`, `reviews`), verified identifiers
(`work_identifiers`), retrieval provenance and immutable source artifacts
(`retrievals`, `source_artifacts`, `source_sections`), printed reference slots
(`bibliography_entries`) and all claim–citation loci (`claim_loci`).

Interpretation belongs to a named analysis and run: `analyses`, `runs`,
`model_receipts`, `assessments`, `assessment_evidence`, `work_observations`,
`work_observation_evidence`, `processing_state` and `facts`. Store the complete study specification, protocol/prompt/code versions,
evidence hashes, settings and outputs in the corresponding records or registered
artifacts. Keep document versions in registered source provenance and preserve
version relationships in work metadata. Keep provider candidates and reasons
for accepting/rejecting them in bibliography entries and their identity-evidence
artifacts; the verified identifier table alone is not an adequate identity audit.

Write acquired files under `sources/<SHA256>` and register their exact hash and
size after writing. Retain structured source documents and provider payloads,
not just text excerpts. Evidence is reusable across analyses, but judgments
remain tied to the question, locus, evidence and instrument that generated
them. Use explicit analysis filters in every analytical query. Never obtain a
new question's results by silently selecting the last judgment for each DOI.

The schema is an interchange foundation. Existing `.ocsl-review` ledgers and
census exports require an explicit, verified import mapping; `init` does not
magically populate this database or import the historical oncology results.
Preserve source IDs and external run IDs as provenance, never as live job
ownership. Store import code, row-count reconciliation and rejected rows.

## Included helper

The helper uses Python's standard library and writes only within the explicitly
selected workspace. It creates, verifies and branches bundles; it performs no
network requests or model calls. Resolve its path through `$OCSL_REVIEW_HOME`:

```bash
literature_db="$OCSL_REVIEW_HOME/skills/ocsl-review-reference-analysis/scripts/literature_db.py"
python3 "$literature_db" --workspace "$PWD" init work/literature \
  --analysis-id citation-support-v1 \
  --protocol work/study-specification.json \
  --question "How does citation support vary across the declared journal strata?"
```

Create the approved `work/study-specification.json` before initialization; add
`--code-revision` when the implementation revision is known. Capture the returned
`analysis_id` in the controller handoff. Protocol and analysis records are
append-only. Omitting the protocol creates an explicitly unconfigured run,
which is a storage scaffold, not permission to judge; configure a fresh analysis
with its actual protocol before inference. Populate the database through the
approved, versioned import/analysis code. Source files,
raw responses, specification, prompts, schemas, environment locks and analysis
code needed for reproduction should all be registered in the bundle.

Create a consistent snapshot, including live SQLite WAL data, and verify it:

```bash
python3 "$literature_db" --workspace "$PWD" snapshot \
  work/literature results/literature-snapshot
python3 "$literature_db" --workspace "$PWD" verify results/literature-snapshot
```

The manifest records the schema version, database hash, registered artifact
hashes and sizes, table counts and analysis namespaces. Verification must detect
missing or altered evidence. Keep the snapshot immutable by workflow convention;
verification detects edits but does not turn an ordinary directory into a
read-only storage service. Register sources completely before taking a snapshot;
do not race it against file replacement. Use a new destination for later
snapshots and keep both manifests.

Verification establishes the integrity of registered material, not its scientific
coverage or the correctness of a model's reading. A section marked complete is
not necessarily the whole paper. Keep actual source coverage, supplied evidence
and quote validation explicit. Immutable snapshots must not acquire SQLite WAL
or rollback-journal data; verification rejects those sidecars.

## Resume versus a different question

For **resume**, reopen the same analysis and its checkpointed task state. Check
the current deployment's compatible code/configuration and import finished
results before resubmitting. Changed scientific settings require a new analysis
or protocol version, not overwriting accepted observations.

For a **different question**, make the verified snapshot available within the
destination chat's authorized workspace, then branch it:

```bash
python3 "$literature_db" --workspace "$PWD" branch \
  results/literature-snapshot work/followup \
  --analysis-id evidence-trends-v1 \
  --protocol work/followup-specification.json \
  --question "How have the study designs of the cited evidence changed over time?"
```

Prepare the new question's protocol file first. Branching copies the database,
adds a fresh analysis and records the parent manifest digest. Its `bundle.json`
uses a workspace-relative pointer to the verified parent snapshot's immutable
sources, avoiding a second copy of the corpus. New source files belong to the
branch. Historical records stay labelled history; the new question's tasks do
not become complete. The parent is unchanged. New extractions, provider updates
and judgments receive new versioned records, while verified evidence can be
read again without downloading it. A different question requires its own task
schema and method version. The helper currently supports core database schema
version 1; changing that core requires a compatible helper and a tested,
versioned migration. Do not increment `user_version` without that support.

For work-level questions such as trial design or cohort size, use
`work_observations` with the new analysis ID, work ID, property, structured value,
versioned method and optional model receipt. Link source sections, quotations and
locators through `work_observation_evidence`. Define the property's schema in the
new protocol: for cohort size, distinguish participants from specimens and
enrolled from analyzed populations; for reports containing multiple studies,
include verified study/cohort/arm identifiers in the structured value. Preserve
accepted and rejected observations; derive the active selection explicitly.
Do not fabricate citation loci for these observations. The original
`assessments` remain the historical claim-to-source measurements.

For example, query a newly populated follow-up analysis without mixing it with
the original census:

```sql
SELECT work_id, property, value_json, method_version
FROM work_observations
WHERE analysis_id = 'evidence-trends-v1'
  AND property IN ('study_design', 'cohort_size')
  AND validation_state = 'accepted';
```

Keep the parent snapshot while using a branch. To move the follow-up alone to
another workspace or export it, snapshot the branch first; this materializes all
its registered sources into a self-contained bundle. Moving only the branch
directory leaves its parent-source dependency unresolved and verification fails.

Lab chat forks materialize the source workspace's files in an independent
writable layer. This preserves a saved literature bundle; take a consistent
SQLite snapshot before forking rather than relying on copying a live database.
Runtime-managed tools, skills, logs and environment state have separate fork
handling, and environment inheritance is an explicit option. Verify the bundle
in the destination and use its current managed runtime. Shared project storage
must be explicitly mounted and authorized. Existing broker jobs remain owned by
their original chat; reconstruct pending tasks under the new execution when
needed, without assuming inherited provider permissions.

## Handoff and reproducibility claim

The handoff names the manifest digest, corpus/run/analysis IDs, specification
and code versions, stage counts, last accepted task checkpoint, unresolved
outcomes, facts export and build commands. It contains no credentials. Keep
the source bundle in the user's selected durable project area or exported
artifact storage, not solely in transient conversation context.

Distinguish reproducibility levels: stored model responses allow exact replay
of reported results; pinned SQL/code regenerates statistical outputs; a fresh
model call can vary even with nominally identical parameters. A complete
literature database enables new questions without pretending that historical
model judgments are newly observed facts or that exact fresh inference is
guaranteed.

Facts must refer to an earlier frozen input manifest plus the registered
derivation code. Generate the final output snapshot after writing those facts;
do not try to embed a snapshot's own digest inside its database.
