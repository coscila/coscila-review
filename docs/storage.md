<!-- SPDX-License-Identifier: Apache-2.0 -->
# Corpus storage: packs, snapshots, branches and stores

A literature library is a SQLite database (`corpus.sqlite3`), its artifact
bytes and a small `bundle.json`. This page describes how the bytes are stored,
how a library is frozen and forked, how a study reads a shared store, and how
the label-free store projection is built. Storage commands never call models
or providers.

## Packs

New libraries store artifact bytes in append-only packs under `packs/`
(`corpus_packs.py`). A pack is a sequence of self-describing records (SHA-256,
raw and stored length, codec, then the bytes); `packs/index.sqlite3` maps each
SHA-256 to its pack and offset. Compressible bytes are stored with zlib;
already compressed media are stored as they are.

- **Durability before visibility.** A writer appends under an exclusive lock
  and fsyncs the pack before it commits the index row; only then is the
  artifact registered in `source_artifacts`. Bulk writers use a batch that
  fsyncs once per pack flush. Bytes past the last committed offset (an
  interrupted writer) are dropped by the next writer.
- **Sealing.** A pack is sealed when it reaches its target size (256 MiB by
  default) or when a snapshot is taken: it is fsynced, hashed and made 0444,
  and its SHA-256 and stat signature are recorded.
- **Every read is checked.** A blob's bytes are decoded and hashed on every
  read; damage raises an error and is never returned.
- **File count.** A study keeps its database, the index and one file per pack,
  so even a large corpus stays far below 10,000 files.

Libraries written before packs keep their loose `sources/<sha256>` files, and
those stay readable. `ocsl-review corpus pack --workspace . --library LIB`
copies loose registered artifacts into packs (an offline step; the loose files
are left in place). The registry still names every artifact `sources/<sha256>`;
that is its logical name, not a file path.

## Snapshots

`ocsl-review corpus snapshot --workspace . --library LIB --output DEST`
freezes a library into a new directory:

| File | Content |
|---|---|
| `corpus.sqlite3` | closed copy of the database (SQLite backup API, rollback journal, 0444) |
| `packs/index.sqlite3` | closed pack index for exactly the listed packs (0444) |
| `packs/pack-<id>.pack` | the sealed packs, hard-linked from the library (copied only across filesystems) |
| `manifest.json` | format `ocsl-literature-snapshot`, version 2 |

The version 2 manifest lists `files` (path, SHA-256, size of the database, the
index and every pack), `database` (the same entry as in version 1), `counts`,
`analyses`, `active_analysis_id`, an `artifacts` summary and a `merkle` block:
a binary SHA-256 Merkle tree (`sha256-files-v1`) over the listed files in path
order, leaf `H(0x00 || path || 0x00 || sha256 || size)`, node
`H(0x01 || left || right)`. The manifest has no per-artifact list, so it stays
small; the per-blob listing is the closed index, which the manifest hashes.
The SHA-256 of `manifest.json` remains the snapshot's lineage identifier.

Sealed packs are checked by their stat signature when a snapshot is taken and
rehashed only if that signature changed; loose artifacts are packed into the
snapshot. The snapshot result reports the wall time of each phase.

Verification (`ocsl-review corpus verify --level light|standard|deep`):

- **light**: manifest structure, database and index digests, the presence and
  size of every pack, no unlisted pack, and the Merkle root;
- **standard** (default): light, plus every pack's digest, the index against the
  manifest, table counts, analyses and that every registered artifact is
  indexed with its recorded size;
- **deep**: standard, plus SQLite integrity and foreign key checks, every
  section and protocol hash, and every blob decoded and hashed.

Snapshots written before packs (manifest version 1, loose sources) are still
verified the old way. A snapshot must not acquire SQLite `-wal` or `-journal`
data; verification refuses one that does.

## Branches

`ocsl-review corpus branch --workspace . --library SNAPSHOT --output DEST ...`
creates a delta database: the parent snapshot's schema with no parent rows and
the new analysis. The parent is never copied. `bundle.json` records the parent
path, the parent manifest's SHA-256 and its Merkle root; a changed parent
manifest refuses to open the branch.

Each branch connection ATTACHes the parent database with
`mode=ro&immutable=1`. The branch keeps its own rows of the shared corpus
tables in prefixed tables (`ocsl_delta_<table>`), and TEMP views under the
plain table names show the branch's rows plus every parent row the branch does
not override, so existing queries work unchanged:

- inserts go to the branch, and no write is dropped silently. When a row's
  key or unique columns match a parent row that the branch shows, a plain
  `INSERT` raises `sqlite3.IntegrityError` (`CHECK constraint failed:
  works.work_id exists in the parent snapshot`) and `INSERT OR IGNORE` skips
  the row, as on a full copy of the parent. `INSERT OR REPLACE` raises too,
  because replacing would delete a parent row; an `UPDATE` overrides a parent
  row instead. A parent row that the branch overrides clashes as its branch
  copy (`UNIQUE constraint failed: ocsl_delta_works.work_id`). For runs,
  analyses and corpora a key clash raises even with `OR IGNORE`;
- updating a parent row copies it into the branch first, so append-only tables
  still refuse the change; keys of shared rows cannot change, parent rows
  cannot be deleted, and an update that would repeat the unique columns of
  another row raises (`UPDATE OR IGNORE` skips it);
- analysis queues (`corpus_tasks`, `corpus_batches`, `processing_state`) and any
  table outside the shared list stay branch-local;
- SQLite cannot enforce foreign keys across databases, so branch connections
  check every declared reference of an inserted or updated row against the
  union, and verification repeats the check for all branch rows;
- views have no `rowid`; code that needs insertion order uses
  `Library.ordered(table)`, which returns an ordered companion view.

Branch artifacts go to the branch's own packs; reads fall back to the parent's
packs. Snapshotting a branch produces a portable snapshot: one closed database
with the parent and branch rows, and the parent's and branch's packs.

## Study root and dataset roots

Library paths are workspace-relative, or absolute inside `OCSL_STUDY_ROOT`, a
directory the platform may provide outside the checkpointed workspace (for a
study run's draft dataset). The root must be absolute, free of symlinks and
separate from the companion source and operator state. Parent snapshots and
stores may also be read from `OCSL_DATASET_ROOT` (an `os.pathsep`-separated
list of read-only mounts); nothing is created there.

## Stores

A store is a sealed snapshot used as a read-only byte cache, for example the
label-free projection below. `ocsl-review store attach --workspace . --library
LIB --store /mounted/store` verifies the store and pins its path and Merkle
root in the library's `bundle.json` (`store_client.py`).

Lookups are study-first. When the study has no row for a work's identifiers,
the acquisition step copies the store's work, identifiers, documents of the
requested tier, sections and the artifacts they reference into the study (row
identifiers unchanged, bytes verified again) and records an adoption receipt
naming the store version. The study then re-checks the provider records before
it uses any of it, exactly as for its own retained evidence; a frozen study
never depends on the mount. Retained review captures are found through the
store's retained-source index by DOI.

Census inputs read a store without adopting anything into a library
(`store_client.EvidenceStore`, and `ProviderCache` for a provider response
cache): both are ATTACHed `mode=ro&immutable=1` behind a SQLite authorizer that
refuses any table outside the cache's allowlist (for a store, the projection's
kept tables), and every byte is verified against its digest. Only the
label-free projection is accepted as a census store. See
[census instrument](census-instrument.md), "Inputs from byte caches and live
routes".

## The label-free projection `literature-evidence@2`

`ocsl-review store project --source SEALED_STORE --output NEW_DIR` (an
operator command; it refuses to run inside a chat workspace) builds a store
that keeps evidence and drops every interpretation (`store_projection.py`):

- **kept:** works (bibliographic metadata keys only), work identifiers,
  retrievals, source documents and sections, and the bytes of provider records,
  source documents, raw sources and archived original sources; archived legacy
  source kinds only when a kept row refers to them;
- **dropped:** every work observation (all `legacy.*` properties: scores, loci,
  attempts, summaries, bibliography rows and review manifests), assessments,
  model receipts, citation loci, bibliography entries, study selections and
  review states, task queues and batches, imports, processing state, facts,
  runs and analyses, documents whose provenance is a judged-sample selection,
  and the bytes of every other artifact kind (model requests and responses,
  task inputs, legacy databases and import records, among others);
- **derived:** `retained_sources(doi, capture_sha256, media_type, route,
  captured_at)`, a neutral index of retained review captures built from the
  legacy review manifests (route is derived from the media type; `captured_at`
  is the time the capture entered the source store). It holds nothing else.

The rules are deny-by-default and digested into the projection
(`store_meta.rules_sha256` and the manifest's `store` block). The source store
is opened read-only and immutable, its database must match its manifest, and
its stat signature must not change during the build; older manifests are read
by head and tail without loading their source lists. Rows are streamed in
keyset pages and bytes are copied blob by blob, each verified against its
SHA-256, so memory stays bounded.

The build fails closed, and `ocsl-review store verify --store DIR` repeats the
gate on any projection:

- the removed-row query must return no rows: no row in any dropped table, no
  `legacy.*` property, no withheld artifact kind and no denied document
  provenance;
- every reference must resolve (SQLite foreign key check);
- no kept text, and with `--level deep` no kept byte, may contain legacy verdict
  vocabulary (three-class labels, score columns, verdict fields, compact
  verdict records or legacy property names).

The build receipt printed to the operator lists what was dropped (counts per
table, property and artifact kind) and the source store's digests. It is not
written into the projection, which records its own rules and Merkle root only.

## Acceptance benchmark

`scripts/storage_benchmark.py --out NEW_DIR [--scale 1.0]` builds a synthetic
library shaped like a large review census (thousands of reviews, hundreds of
thousands of printed entries and cited works, millions of legacy observations
and artifacts), then measures snapshot time and manifest size, branch time with
the parent digest checked before and after branch writes, and the projection
with its gate. Every number it prints is measured on the store it just built;
it writes only below `--out`. `tests/test_storage_benchmark.py` runs a tiny
scale on every test run and the full scale when `OCSL_STORAGE_BENCH=1`.
