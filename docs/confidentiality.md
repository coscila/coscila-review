# Confidential review and egress

## Public literature workers

Lab worker configurations can explicitly declare `evidencePolicy: public-literature`
(the **Allow public literature evidence** option). This is stored in the versioned
worker configuration and portable presets. It takes effect only for the controller
in an approved execution attempt, never for planning or child workers.

The census reads the authorization directly from the scoped broker, verifies the
attempt, workspace and pinned worker configuration, then checks the published
manuscript's nonconfidential declaration and acquired public evidence. Sources
must have remote acquisition provenance, and payload evidence must match their
verified hashes and extracted units. The receipt records attempt, configuration,
source-manifest and payload hashes. Neither a local public flag nor a skill grants
this permission. Explicit operator egress restrictions take precedence. Private
attachments and confidential manuscripts continue to require the operator plan
described below. This route does not relax metadata-provider egress rules.

The approved controller remains responsible for correct public/private
classification and acquisition provenance. These checks bind its declared public
material to a configured permission; they are not an independent publisher
attestation service. Manuscript classification and extracted content are pinned
when the census is prepared, so relabelling or editing them afterwards requires
a new run and cannot change an existing authorization.

The `meta meta literature review` preset includes this permission for its public
literature workers. Loading a preset does not execute any work or bypass normal
plan approval. Changing permission requires preparing a new pinned census run.

## Operator plans

An uploaded, unpublished manuscript is confidential unless a person with the
right authority records a different decision. Confidentiality is enforced at
the provider-call boundary; it is not merely an instruction in a review skill.

This design is consistent with the [COPE ethical guidelines for peer
reviewers](https://doi.org/10.24318/cope.2019.1.9), which treat material received
for review as confidential. Local policy or a journal agreement can impose
stricter requirements and always takes precedence.

## Default behavior

With no egress-plan file, `ocsl_scholar` creates a confidential plan. Before an
HTTP transport is used, the gate examines the operation, every argument name,
and every leaf value. A rejected request raises a policy error without sending
the payload.

Confidential mode permits only:

- well-formed DOI, PMID, PMCID, ORCID, ISBN, ISSN, and dataset or software DOI
  fields;
- non-content control fields such as bounded result count, provider, page,
  cursor, year range, source, and sort order;
- a minimized query or sanitized-query field when sanitized searches are
  enabled.

A minimized query must be a short concept expression, not a sentence. The
default maximum is twelve terms; queries are capped at 240 characters, reject
unusually long tokens, line breaks, sentence-like punctuation, and substantial
quoted passages.

The following are rejected in confidential mode regardless of convenience:

- manuscript files, paragraphs, sentences, abstracts, or upload paths;
- unpublished title and author combinations;
- tables, figures, captions, and their descriptions;
- reviewer notes, report text, and revision correspondence;
- arbitrary fields not recognized by the policy;
- any request outside an explicit operation allowlist, when one is configured.

Do not move sensitive text into a field named `query` to bypass the rule. The
query-content checks are a secondary defense, not permission to transmit a
paraphrased unpublished result.

## Egress plan

The MCP process reads the plan path from `OCSL_REVIEW_EGRESS_PLAN` and the
private workspace path from `OCSL_REVIEW_WORKSPACE`. If either is supplied,
the workspace must be an existing, non-symlinked directory inside
`LAB_WORKSPACE_ROOT`; an explicit plan must be a regular, non-symlinked file
inside that exact workspace. A plan without a workspace, an external plan, or
a workspace overlapping source or operator state is rejected before a
scholarly client is constructed. A conservative plan can narrow the default
further:

```json
{
  "confidential": true,
  "allowSanitizedQueries": true,
  "maxQueryTerms": 8,
  "approvedOperations": [
    "resolve_identifier",
    "get_publication_status",
    "get_corrections_retractions",
    "acquire_full_text",
    "search_pubmed",
    "search_europe_pmc"
  ],
  "approvedFields": [],
  "sharedCache": false
}
```

The normal chat runtime may omit both review-specific variables. When the MCP
process starts with its current working directory strictly below the validated
`LAB_WORKSPACE_ROOT`, that exact directory becomes the workspace; the root
container itself is never treated as a chat. The server then uses
`<workspace>/.ocsl-review/egress-plan.json` when that fixed, non-symlinked file
exists, otherwise it uses the confidential default plan. It does not search
parents for a plan or infer a different workspace. This keeps separate chat
working directories, plans, and ledgers from crossing into one another.
An automatically discovered plan is accepted only when it preserves or narrows
the confidential default: `confidential` remains true, `sharedCache` remains
false, `approvedFields` is empty, `maxQueryTerms` is at most twelve, and
sanitized queries or public operations may only be restricted. A writable chat
therefore cannot promote its own plan into broader disclosure authority.

A source-side doctor launched with a current directory outside
`LAB_WORKSPACE_ROOT` retains the confidential diagnostic default and has no
workspace ledger target. Starting it from the workspace-root container itself
is rejected as ambiguous rather than sharing one container-level ledger.

An explicitly configured plan inside the exact workspace may also work without
a digest when it meets the same narrowing-only contract. Any broader explicit
plan requires `OCSL_REVIEW_EGRESS_PLAN_SHA256` to contain the lowercase SHA-256
of the exact bounded file bytes. The server reads, hashes, and parses those
bytes through one no-follow file descriptor; an absent, malformed, stale, or
mismatched digest stops startup before provider I/O. This is the operator's
immutable authorization for that plan content, not an authority delegated to
the writable workspace.

`approvedFields` can extend ordinary structured fields only in such a
hash-bound explicit plan. It never overrides the confidential-mode hard block
on manuscript-like field names, including sensitive names nested inside other
argument objects or attached to empty values. If a competent authority
explicitly allows disclosure of manuscript content, record that decision
outside the manuscript and use a separately reviewed, hash-bound
`"confidential": false` plan for only the approved operation. Do not silently
change the whole review's classification. An approved public posting, for
example, does not necessarily make reviewer notes or an unpublished revision
non-confidential.

A confidential plan with `sharedCache: true` is invalid. Review content must
not enter a cross-workspace cache.

## What the audit log contains

When a workspace is selected explicitly or from the trusted process working
directory, allowed provider requests append an event to the non-symlinked,
owner-only ledger directory:

```text
<workspace>/.ocsl-review/egress-events.jsonl          active segment
<workspace>/.ocsl-review/egress-events.000001.jsonl   sealed segments, read-only
```

The files are created with private permissions. Each event contains:

- timestamp, provider, operation, and policy decision;
- the sorted names of arguments;
- a SHA-256 digest of the canonical argument object.

The argument values themselves are not written. The digest supports later
correlation without creating another copy of an identifier or search concept.
It is not proof that a query was scientifically adequate; adequacy belongs in a
search-run record in the audit graph.

The ledger is append-only. Each line is one canonical JSON record holding the
event, its index and the SHA-256 of the previous record, so editing, reordering
or removing an earlier event breaks the chain. Appends are serialized by a lock
file and never rewrite earlier lines. When a segment would exceed 8 MiB it is
sealed read-only under the next number and the chain continues in a new active
segment. A ledger written by earlier versions as one JSON array
(`egress-events.json`) is never rewritten; it remains readable, and the chain
starts from its SHA-256. `verify_egress_ledger` checks every segment and link,
and `read_egress_events` returns all events in order after that check.

`ocsl-review screen` keeps its own chained `egress.jsonl` in the run directory
(see [claim screen](claim-check.md)) and appends every one of its decisions to
the workspace ledger as well (`source: claim-check@1`, linked by
`runLedgerSha256`): judge calls, registry checks and refusals. Provider
fetches are recorded once there, by the scholarly client.

Each event reaches the operating system before the request it authorizes is
sent, so it survives a crash of the process. The ledger calls `fdatasync` at
most once per second per process and at the latest one second after an
unsynced event, when a segment is sealed and at interpreter exit; a power
failure can therefore lose at most about the last second of events at the end
of the ledger, never an event inside the chain. An incomplete final line is
refused, not repaired automatically.

The extensive CLI workflow uses this same validated context and ledger. Its
user confirmation records permission for the review's scope; it does not
broaden egress. Every complete-source request, including each redirect, passes
the gate with its public source destination and identifiers. Downloads use
validated public HTTPS addresses and isolate provider credentials. An egress
rejection is not permission to retry through shell networking.

## Local handling

Confidentiality also affects local files:

- ingested manuscripts default to `confidential: true`;
- confidential JSON and rendered review packages are written with owner-only
  permissions where supported;
- review environment descriptors live in the current workspace;
- chat Conda prefixes are separated by a sanitized workspace identity plus a
  digest, preventing name collisions;
- a workspace cannot be the repository, deployment state, or filesystem root;
- the catalog MCP is read-only, while the scholarly MCP has no filesystem
  access through its declared server contract.

The CLI additionally retains `.ocsl-review/literature-audit.json`, immutable
source bytes and extracted document records under `.ocsl-review/literature/`,
and content-addressed graph snapshots. These are private workspace state, not
tracked repository artifacts or shared provider caches. Original local source
attachments are copied, and symlinks, workspace escapes, and shared mutable
inodes are rejected. Source/graph digests bind recorded evidence to the version
actually examined. Reviewer notes, actual scope confirmation, and assessment
JSON remain local.

The companion does not implement document-rights management, encrypted storage,
or identity and access management. Operators must still protect the workspace,
backups, process environment, terminal history, deployment logs, and any
private full text acquired outside the public providers.

## Safe review patterns

### Resolve known references

Prefer an identifier already present in the bibliography. DOI/PMID/PMCID lookup
can reconcile identity and publication-status signals without revealing the
manuscript's prose.

### Search for evidence

Construct a compact concept query that cannot reconstruct an unpublished
sentence, novel result, title, cohort name, or author identity. Record the
scientific search strategy locally. The provider sees only the minimized form;
the graph stores its digest and disclosure class rather than confidential full
text.

### Inspect sources

Public metadata, complete abstracts, and available complete sources may be
retrieved after the applicable scope confirmation. The returned access level
sets a ceiling on conclusions. A complete abstract may inform an explicitly
abstract-sufficient claim at lower confidence. If a disputed number or method
requires inaccessible full text, the graph must abstain and record the missing
evidence while other sources continue. Keep complete downloaded text private;
only bounded evidence excerpts belong in deliverable audit links.

### Prepare a review report

Keep the report and reviewer-added assertions inside the workspace. A report
must not send its own draft claims back to a public search endpoint. Search
using identifiers or independently minimized concepts, then cite verified
evidence in the graph.

## Incident response

If confidential material appears to have crossed the boundary:

1. Stop further provider calls and preserve the workspace event ledger.
2. Record the provider, operation, time range, process configuration, and
   suspected data class without copying the exposed text into shared logs.
3. Notify the responsible review authority and follow its disclosure policy.
4. Rotate credentials if any were included, and isolate affected caches or
   telemetry stores.
5. Add a regression case that uses synthetic content only.

An egress rejection is a successful safety outcome. It should be resolved by
minimizing the request or obtaining authority, never by bypassing the gate.
