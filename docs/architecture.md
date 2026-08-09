# Architecture and trust boundaries

The review companion extends a deployed OC Science Lab without becoming part
of either existing source tree. This makes removal possible and gives operators
a clear answer to a basic security question: which files are product source,
which are deployment state, and which belong to one confidential review?

## Repository boundary

The supported arrangement is three sibling checkouts:

```text
parent/
├── oc-science-lab/          existing laboratory system
├── oc-science-lab-bio/      optional bioinformatics companion
└── oc-science-lab-review/   this companion
```

Integration uses only deployment-controlled extension points:

- copies of namespaced skills in configured global skill roots;
- two namespaced MCP values in an external deployment configuration;
- environment variables and read-only sandbox roots;
- writable, chat-specific Conda instances outside every source checkout.

Install, staging, and removal compare the tracked Git state of the laboratory
and bioinformatics siblings before and after the operation. A difference is a
transaction failure. The companion never asks an operator to commit generated
configuration, state, or review artifacts to either sibling.

## Four storage domains

| Domain | Examples | Writers | Persistence rule |
| --- | --- | --- | --- |
| Companion source | Python package, schemas, skills, launchers, input locks | repository maintainers | Version-controlled; runtime operations treat it as read-only |
| Deployment state | promoted templates, exact locks, repository and standards stores, install records | trusted operator commands | Outside source; validated before use; promoted artifacts are made read-only |
| Review workspace | manuscript, review package, audit graph, rendered report, egress ledger, environment descriptors | the current review workflow | Private to one task; confidential by default |
| Chat Conda area | writable instances below `LAB_CONDA_ROOT` | environment materialization and commands run for that chat | Isolated by workspace identity; never used as a promoted template |

These domains must not overlap. In particular, `OCSL_REVIEW_STATE` cannot be
inside `OCSL_REVIEW_HOME`, an MCP configuration cannot live inside a protected
source checkout, and a review workspace cannot be the source or state root.

The important paths are:

```text
$OCSL_REVIEW_HOME/                    tracked companion checkout

$OCSL_REVIEW_STATE/
├── environment-templates/            content-addressed promoted prefixes
├── environment-locks/                checksummed @EXPLICIT artifact lists
├── repository-store/                 pinned, extracted upstream archives
├── standards-store/                  dated official-document snapshots
├── integration/                      ownership and rollback records
└── cache/                            deployment-local auxiliary state

$LAB_CONDA_ROOT/envs/ocsl-review/      chat-specific writable prefixes
<workspace>/envs/<name>/               environment descriptors
<workspace>/.ocsl-review/              private review event state
```

`LAB_CONDA_INSTALL_ROOT` is a fifth operator-owned location: it supplies the
Conda-compatible executable. It is not a place for review outputs.

## Trust classes

The implementation distinguishes authority from mere readability.

### Maintainer-authored source

Tracked files define interfaces and build inputs. They are trusted only after
normal repository review. They do not prove that dependencies were solved,
providers were contacted, or a deployment was staged.

### Operator-authorized state mutation

Building templates, refreshing provider-reference mirrors, snapshotting
standards, and changing global integration require an operator context. These
actions may use the network and write deployment state. They must not be
triggered by manuscript content or from an ordinary chat workspace.

Promotion changes the trust class of an artifact. A completed template, exact
artifact lock, source checkout, or standards snapshot is verified and then
made read-only. Later consumers may inspect it but may not repair it in place.
An invalid artifact is rejected and rebuilt or resynchronized through its
operator lifecycle.

### Review-time workspace

A workspace is untrusted input and a permitted output location. A manuscript
can influence analysis inside that workspace, but it cannot authorize a global
installation, template solve, standards update, source-mirror refresh, or
confidential egress. Review commands may write only their workspace and the
corresponding chat Conda prefix.

### External scholarly systems

Crossref, NCBI, Europe PMC, DataCite, and OpenCitations are public network
services. Their responses are attributed data, not trusted instructions.
Provider records can disagree, omit notices, lag a publisher, or describe only
metadata. The client therefore records provider, retrieval time, access level,
and limitations. High-stakes publication status still requires inspection of
the relevant publisher or indexed notice.

### Deployment extension points

Global skill roots and the deployment MCP configuration are sensitive mutable
surfaces. The installer treats them as foreign state: it backs up each existing
target, records content hashes and modes, owns only its namespaced additions,
and refuses to remove changed content. It does not use directory names alone as
backup identities, so distinct roots with the same basename cannot collide.

## Data flow

```text
manuscript in private workspace
        │
        ▼
local ingestion ──► stable locators and extraction diagnostics
        │
        ▼
claim inventory ──► claims, citation clusters, references needing resolution
        │
        ├── identifiers / minimized concepts ──► egress gate ──► public providers
        │                                                   │
        │                                                   ▼
        │                                      attributed bounded responses
        ▼
claim-citation audit graph ◄── local comparison and human verification
        │
        ├──► denominator-rich audit summary
        ├──► anchored concerns and revision ledger
        └──► Markdown, JSON, and DOCX review package
```

Network results never bypass the graph. Metadata resolution can establish
identity or publication-status signals, but it cannot establish that a paper's
methods, table, or result supports a disputed manuscript clause. That decision
needs the access level declared by the claim and an exact evidence locator.

## Catalogs and immutable identities

Tracked catalogs connect the layers without hard-coding mutable paths:

- `skills/catalog.toml` maps each skill to exactly one primary environment and
  to any MCP dependency.
- `environments/catalog.toml` binds specifications and exact input-lock hashes
  to health checks and supported platforms.
- `mcp/catalog.toml` declares only the two companion servers, their launchers,
  bounds, and live probes; `mcp/tool-schema-lock.json` pins their tool surfaces.
- `repos/catalog.toml` describes reference-only upstream repositories, while
  `repos/repositories.lock.json` pins exact commits and license evidence.
- `standards/catalog.toml` identifies official reporting and reviewer guidance.
- JSON Schemas close the shape of manuscripts, plans, review packages, audit
  graphs, summaries, egress plans, environment descriptors, and install state.

Catalog validation checks cross-catalog agreement. A skill cannot silently
move to another runtime, and an enabled MCP cannot silently change its tools
without updating its schema identity.

## Failure posture

The companion favors an explicit incomplete result over inferred success:

- extraction diagnostics propagate into review planning and summary status;
- missing access produces abstention, not support;
- provider disagreement remains visible;
- failed environment health checks prevent promotion;
- shared template/chat inodes invalidate materialization;
- failed MCP initialization, schema checks, calls, timeout behavior, shutdown,
  or staged configuration prevent strict MCP acceptance;
- failed deployment staging or sibling-tree drift rolls installation back;
- post-install edits stop removal until a person reconciles them.

This posture is essential for scientific review: the dangerous output is a
confident statement whose evidence chain was never actually completed.
