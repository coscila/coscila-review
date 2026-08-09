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
  -- <command>
```

## Procedure

1. Resolve supplied DOI, PMID, PMCID, accession, registry, dataset, and software identifiers directly where possible.
2. Reconcile title, authors, year, venue, volume, pages, identifier, and type across source-explicit providers. Preserve disagreements rather than selecting silently.
3. Link preprint, accepted manuscript, version of record, correction, retraction, and related versions as one version family; do not count them as independent evidence.
4. Check current correction, expression-of-concern, withdrawal, and retraction metadata, recording provider and retrieval time.
5. Detect plausible fabrications, DOI-to-title mismatches, duplicate entries, citations to the wrong version, and bibliography entries never cited in text.
6. Emit resolution confidence and the exact fields needing human confirmation.

## Verdict limits

Identity metadata establishes existence and status, not evidentiary support. A matching title or abstract may not yield a “supports” edge. If providers disagree or no authoritative identity is available, abstain on identity, mark the reference unresolved, and prevent an unconditional “cited correctly” result.

## Confidentiality and role

For confidential manuscripts, send identifiers or isolated bibliographic fields only. Do not submit an unpublished title-author combination without approval. This skill resolves records; claim-level reading belongs to `ocsl-review-claim-source-verification`.
