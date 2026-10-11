# Acceptance and definition of done

Acceptance is evidence about a particular checkout and deployment, not a label
in a catalog. This document separates local implementation tests from the
network, environment, integration, and scientific-evaluation work that cannot
be proven by tracked files alone.

## Evidence levels

### 1. Static contract validation

Run:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" catalog validate
"$OCSL_REVIEW_HOME/bin/ocsl-review" skills validate
```

Acceptance at this level means the tracked catalogs, descriptors, schemas,
input locks, launchers, and cross-catalog mappings are internally coherent. It
does not mean an environment exists or an MCP can start.

### 2. Dependency-light tests

Run from the companion checkout with Python 3.11 or newer:

```bash
PYTHONPATH="$OCSL_REVIEW_HOME/src" \
  python3 -m unittest discover -s tests -v
```

The suite exercises manuscript ingestion, claim inventory, audit invariants,
summary blockers, confidentiality policy, scholarly-client normalization with
an injected transport, MCP protocol behavior, environment lifecycle with a
fake Conda executable, rendering, calibration, catalog validation, immutable
source/standards behavior, and reversible integration.

A passing local suite is necessary but intentionally uses synthetic providers,
temporary roots, and controlled subprocesses. It is not a substitute for the
operator checks below.

### 3. Environment acceptance

For every one of the six catalogued environments on the target platform:

- the input-lock hash must match the catalog;
- a real Conda-compatible build must complete;
- all declared health checks must pass;
- an exact checksummed `@EXPLICIT` lock must be exported;
- the build identity and receipt must recompute;
- the promoted prefix and exact lock must be read-only;
- `current` must resolve to the validated build;
- ordinary ensure must use offline copy materialization without a solve;
- source and chat prefixes must share zero regular-file inodes;
- `clone` and `reproducible-rebuild` forks must pass their provenance, health,
  and inode checks;
- all descriptors in a full chat-fork rematerialization must be recreated for
  the target workspace.

Retain machine-readable output and receipt paths for the deployment record. A
template is accepted only for the platform and build ID in that output.

### 4. Read-only source acceptance

Run synchronization and strict verification for the pinned repository and
standards catalogs. Confirm exact commits, extracted tree hashes, content
digests, manifests, pointer safety, and absence of write bits.

No `NOASSERTION` archive may be imported, executed, or redistributed. An update
candidate is not accepted until a maintainer reviews both code/protocol changes
and license evidence and intentionally updates the tracked lock.

### 5. MCP acceptance

For `ocsl_review_catalog` and `ocsl_scholar`, both the source launcher and the
actual staged deployment entry must pass:

- command and launcher resolution;
- initialize handshake;
- nonempty `tools/list`;
- exact canonical schema hash;
- bounded live call;
- expected error response for an unknown tool;
- verified client timeout path;
- clean shutdown.

Run:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" mcp doctor \
  --live \
  --staged-config /path/to/staged-config.json \
  --strict
```

The acceptance record must say `acceptanceComplete: true`. A result without a
staged path or live calls is diagnostic only.

### 6. Integration acceptance

Use disposable skill roots and MCP configuration first, then the intended
deployment. Demonstrate:

- preservation of unrelated MCP entries;
- exact backup and restoration of pre-existing same-name MCP values;
- collision-free backup of distinct skill roots;
- hashes and modes for every installed skill node;
- failure and full rollback when staging returns nonzero;
- detection and rollback when either sibling's tracked Git state changes;
- idempotent repeated install and repeated removal;
- refusal to uninstall an edited skill or owned MCP value;
- restoration of previous skill targets and owned MCP values on clean removal;
- successful deployment restaging after install and removal.

Do not test destructive cases against the only copy of a deployment config.
Keep the private transaction state and staging output with the acceptance
record.

### 7. Confidentiality acceptance

Before reviewing unpublished work, test at least these egress attempts through
the real scholarly MCP path:

- DOI, PMID, and PMCID resolution is allowed and logged without values;
- a short sanitized concept query is allowed;
- a paragraph, sentence-like query, title/author pair, caption, table, figure,
  reviewer note, and workspace path are each rejected before transport;
- an unapproved operation is rejected under an operation allowlist;
- a confidential plan with shared caching is rejected;
- the workspace ledger contains only time, provider, operation, argument names,
  decision, and payload digest;
- the staged deployment supplies the intended plan and workspace rather than
  silently falling back to a different policy.

Use synthetic secrets and manuscript text in acceptance fixtures.

### 8. Artifact acceptance

An end-to-end review fixture must demonstrate:

- deterministic source SHA-256 and stable local locators;
- extraction diagnostics and honest partial-ingestion status;
- correct treatment of original manuscript results that do not need outside
  citations;
- closed-schema validation of manuscript, plan, graph, summary, package, and
  egress plan;
- referential integrity from claims to citation clusters, references,
  verification links, completeness assessments, and search runs;
- abstention for metadata-only, inaccessible, and inadequate abstract access;
- exact source location for every positive support edge;
- propagation of coverage blockers into `not-fully-verifiable`;
- rejection of a rendered `yes` that conflicts with its denominators;
- validation of reviewer-added factual assertions;
- successful publication of consistent JSON, Markdown, and DOCX outputs without
  partial bundles.

Review rendered documents manually for locator usability and confidential
classification. Schema validity alone cannot detect a misleading explanation.

## Adversarial scientific evaluation

The bundled synthetic corpus covers eighteen high-risk patterns:

- invented reference identity and DOI/metadata conflict;
- preprint/version double counting and retracted work;
- animal-to-human and association-to-causation scope shifts;
- compound-clause partial support and secondary-source laundering;
- broad claims supported by narrow evidence;
- omitted null or contrary results and superseded guidance;
- numeric drift, weak priority searches, and irrelevant citation padding;
- a manuscript's own result incorrectly treated as externally citation-bound;
- inaccessible full text, confidential-text egress, and unsupported facts
  introduced by the review report.

Generate predictions with the candidate workflow; do not use the bundled gold
prediction file as though it were system output. Then run calibration in a
workspace:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" calibrate \
  --workspace "$PWD" \
  predictions.jsonl \
  --output calibration-report.json
```

The scorer requires exactly the same case IDs as the gold corpus and reports:

- false-support rate, counts, and denominator;
- abstention precision and recall;
- scope-mismatch detection;
- citation-needed accuracy;
- completeness-decision accuracy;
- publication-status recall;
- factuality of review-added claims;
- confidentiality-decision accuracy;
- per-case correctness.

False support is the highest-cost error. For the bundled acceptance corpus, the
release gate is zero false supports and complete case coverage. Record the
remaining metrics rather than hiding a weak dimension in one average. Any
future threshold changes must be agreed before evaluating a new candidate, not
after seeing its errors.

## Definition of done

The companion is ready for production review only when all of the following are
true for the target deployment:

- all twenty-one skills are substantive, validate, and map to one of the six
  catalogued environments through the required `env run` contract;
- all six real templates and both fork modes have current passing evidence;
- pinned repositories and official standards stores verify as read-only;
- both MCPs pass strict live source and staged acceptance with locked schemas;
- confidential egress tests pass through the staged scholarly server;
- the integration transaction and guarded uninstall pass against realistic
  pre-existing content while both sibling Git trees remain unchanged;
- the end-to-end review artifacts validate and expose complete denominators;
- candidate-generated adversarial predictions meet the false-support gate and
  report every other calibration metric;
- no required command is a placeholder, unconditional success path, or dead
  configuration entry;
- no material from the unlicensed reference project has been reused;
- operator documentation names the exact build IDs, staged config, test date,
  platform, and known limitations.

If any item lacks evidence, report that item as unverified. Partial readiness is
useful operational information; it must not be converted into a blanket claim
that the companion is complete.

## Handoff record

At minimum, archive these outputs outside source control and outside a shared
confidential cache:

```text
checkout commit and clean-room review note
catalog-validation report
unit-test output and Python version
environment build IDs, receipts, exact-lock hashes, and fork checks
repository and standards verification reports
MCP strict doctor report and staged-config identity
integration install/verify/uninstall reports and sibling Git snapshots
confidentiality egress test report
end-to-end artifact-validation report
calibration report
open limitations and responsible owner
```

The repository currently supplies the mechanisms and local fixtures for these
checks. It does not ship a deployment-specific handoff record, promoted
environments, or evidence of live provider calls.
