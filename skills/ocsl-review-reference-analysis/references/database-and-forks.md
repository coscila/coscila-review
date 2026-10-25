<!-- SPDX-License-Identifier: Apache-2.0 -->
# A database a future thread can reuse

The portable deliverable is a bundle: `corpus.sqlite3`, registered evidence in
content-addressed packs (`packs/`), and a snapshot `manifest.json` with a Merkle
root. A PDF or table of final scores alone does not retain the evidence needed
to ask a different scientific question.

## Separate evidence from interpretation

The bundled `assets/literature-schema.sql` defines the initial schema; the
maintained `schemas/corpus-v2.sql` adds corpus document, task, batch and import
ledgers without rewriting original observations. The shared library keeps
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

Register acquired bytes through the maintained commands: they are written to
append-only content-addressed packs and registered by exact hash and size only
after they are durable (the registry names them `sources/<SHA256>`). Loose
`sources/<SHA256>` files of older libraries stay readable, and `corpus pack`
copies them into packs. Retain structured source documents and provider payloads,
not just text excerpts. Evidence is reusable across analyses, but judgments
remain tied to the question, locus, evidence and instrument that generated
them. Use explicit analysis filters in every analytical query. Never obtain a
new question's results by silently selecting the last judgment for each DOI.

Existing `.ocsl-review` ledgers and historical exports enter through
`corpus import-legacy --kind census|historical`; `init` does not populate the
database. Import reconciliation and raw inputs remain registered provenance.
Historical records never satisfy fresh v2 task completion. Preserve external
run IDs as provenance, never as live job ownership.

For original evidence databases and raw source captures, use the supported
`corpus import-legacy --kind evidence` adapter. An external verified seed is
imported with `corpus import-snapshot /operator/supplied/seed --workspace .
--library work/seed`, then branched into the new analysis. Obtain the actual
seed path from the operator; no historical absolute path is a runtime default.
These imports retain source bytes and provenance without completing new tasks.
A sealed store mounted read-only can instead be attached as a byte cache with
`ocsl-review store attach --store /mounted/store --workspace . --library
work/literature`. Lookups ask the study first; what the store holds is copied
into the study with an adoption receipt, and provider records are re-checked
before that evidence is used.

## Maintained corpus commands

Use the maintained command from `$OCSL_REVIEW_HOME/bin/ocsl-review`. Storage
commands below do not call models or providers; `corpus acquire`, `calibrate`
and `run` have their separate authorized execution contracts.

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" corpus init \
  --workspace "$PWD" --library work/literature --analysis citation-support-v2 \
  --protocol work/study-specification.json \
  --question "How does citation support vary across the declared journal strata?"
```

Create the approved `work/study-specification.json` before initialization and
record the actual code revision in that specification. Capture the returned
`analysis_id` in the controller handoff. Protocol and analysis records are
append-only. Populate the database through maintained corpus import, acquisition
and assessment commands. Source files,
raw responses, specification, prompts, schemas, environment locks and analysis
code needed for reproduction should all be registered in the bundle.

Create a consistent snapshot, including live SQLite WAL data, and verify it:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" corpus snapshot \
  --workspace "$PWD" --library work/literature --output results/literature-snapshot
"$OCSL_REVIEW_HOME/bin/ocsl-review" corpus verify \
  --workspace "$PWD" --library results/literature-snapshot
```

The manifest records the schema version, table counts, analysis namespaces, the
digests of the closed database, the pack index and every pack, and a Merkle root
over them; the pack index lists every artifact's hash and size. Packs are shared
with the library by hard link, so a snapshot copies the database but not the
evidence bytes. `corpus verify --level light|standard|deep` checks a snapshot at
increasing depth and must detect missing or altered evidence. Keep the snapshot immutable by workflow convention;
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
"$OCSL_REVIEW_HOME/bin/ocsl-review" corpus branch \
  --workspace "$PWD" --library results/literature-snapshot --output work/followup \
  --analysis evidence-trends-v1 \
  --protocol work/followup-specification.json \
  --question "How have the study designs of the cited evidence changed over time?"
```

Prepare the new question's protocol file first. Branching creates a delta
database with the fresh analysis and records the parent manifest digest and
Merkle root in `bundle.json`; the parent database is attached read-only and never
copied. Queries see parent and branch rows together. New rows and new artifacts
belong to the branch, and parent rows never change: an update copies the row into
the branch first, and references are checked across both. Historical records stay labelled history; the new question's tasks do
not become complete. The parent is unchanged. New extractions, provider updates
and judgments receive new versioned records, while verified evidence can be
read again without downloading it. A different question requires its own task
schema and method version. The maintained storage module supports core schema
versions 1 and 2. V2 migration is additive and preserves a v1 backup; immutable
snapshots must be branched before migration. Never increment `user_version` by
hand. The original skill-local `scripts/literature_db.py` remains a storage-only
compatibility entry point using that same maintained module; existing command
spelling there is unchanged.

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
another workspace or export it, snapshot the branch first; this writes one closed
database with the parent and branch rows and links all their packs into a
self-contained bundle. Moving only the branch
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
