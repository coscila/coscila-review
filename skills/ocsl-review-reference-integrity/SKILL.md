---
name: ocsl-review-reference-integrity
description: Reconcile reference identities, versions, metadata, and publication status.
---

# Reference integrity

Use this skill to establish what each bibliography entry actually identifies before reading it as evidence.

## Runtime

Primary environment: `review-literature`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-reference-integrity \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review literature --help
```

## Procedure

1. Resolve supplied DOI, PMID, PMCID, accession, registry, dataset, and software identifiers directly where possible.
2. Reconcile title, authors, year, venue, volume, pages, identifier, and type across source-explicit providers. Preserve disagreements rather than selecting silently.
3. Link preprint, accepted manuscript, version of record, correction, retraction, and related versions as one version family; do not count them as independent evidence.
4. Check current correction, expression-of-concern, withdrawal, and retraction metadata, recording provider and retrieval time.
5. Detect plausible fabrications, DOI-to-title mismatches, duplicate entries, citations to the wrong version, and bibliography entries never cited in text.
6. Emit resolution confidence and the exact fields needing human confirmation.

Use the confirmed workflow in `$OCSL_REVIEW_HOME/docs/literature-review.md`.
`review literature fetch` records provider comparisons and acquisition outcomes
in the managed ledger and audit graph. Strong printed identity requires two
distinct providers and corroboration of title, year, and venue against the
printed entry, plus supplied volume/pages. A shared author/year or guessed
venue expansion is insufficient. Preserve raw metadata beside decoded display
fields. Duplicate identifiers may share a source file but retain every
bibliography entry. Only call an entry uncited after structural binding is
complete; treat uncited bibliography entries as integrity findings, not
automatic unsupported claims.

Publication status uses directed structured notices and known-positive route
controls. An unavailable or failed control means surveillance is unproven;
zero returned notices cannot establish a healthy route. Never invent or recall
an identifier to fill an unresolved entry.

## Verdict limits

JATS/publisher HTML ingestion keeps original target IDs, printed numbering and
publication identifiers in `sourceIdentity` and `structuredSource`. Reconcile
those with canonical reference IDs and acquired version families; an anchor
target alone does not certify the current publication. Census runs pin source
hashes and public worker settings and retain earlier tier/version observations.

Identity metadata establishes existence and status, not evidentiary support. A matching title or abstract may not yield a “supports” edge. If providers disagree or no authoritative identity is available, abstain on identity, mark the reference unresolved, and prevent an unconditional “cited correctly” result.

## Confidentiality and role

For confidential manuscripts, send approved identifiers or minimized concepts through the egress gate. Raw bibliographic fields need the applicable authorization. This skill resolves records; claim-level reading belongs to `ocsl-review-claim-source-verification`.
