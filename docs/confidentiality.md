# Confidential review and egress

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

A source-side doctor launched with a current directory outside
`LAB_WORKSPACE_ROOT` retains the confidential diagnostic default and has no
workspace ledger target. Starting it from the workspace-root container itself
is rejected as ambiguous rather than sharing one container-level ledger.

`approvedFields` can narrow or extend ordinary structured fields, but it does
not override the hard block on manuscript-like field names. If a competent
authority explicitly allows disclosure of manuscript content, record that
decision outside the manuscript and use a separately reviewed
`"confidential": false` plan for only the approved operation. Do not silently
change the whole review's classification. An approved public posting, for
example, does not necessarily make reviewer notes or an unpublished revision
non-confidential.

A confidential plan with `sharedCache: true` is invalid. Review content must
not enter a cross-workspace cache.

## What the audit log contains

When `OCSL_REVIEW_WORKSPACE` is set, allowed provider requests append an event
to the non-symlinked, owner-only ledger directory:

```text
<workspace>/.ocsl-review/egress-events.json
```

The file is created with private permissions. Each event contains:

- timestamp, provider, operation, and policy decision;
- the sorted names of arguments;
- a SHA-256 digest of the canonical argument object.

The argument values themselves are not written. The digest supports later
correlation without creating another copy of an identifier or search concept.
It is not proof that a query was scientifically adequate; adequacy belongs in a
search-run record in the audit graph.

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

Public metadata and open sections may be retrieved, but the returned access
level controls what can be concluded. If a disputed number or method is visible
only in inaccessible full text, the graph must abstain and request human access.

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
