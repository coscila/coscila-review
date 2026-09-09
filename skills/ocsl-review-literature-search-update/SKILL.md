---
name: ocsl-review-literature-search-update
description: Perform a reproducible and confidentiality-aware literature update.
---

# Literature search update

Use when currency, missing evidence, or a dated search is part of the review question.

## Runtime

Primary environment: `review-literature`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-literature-search-update \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review literature --help
```

## Search protocol

Define the question concepts, eligible designs, populations, outcomes, date span, languages, providers, field restrictions, and stopping conditions before searching. Keep the scientific protocol locally; audit `searchRuns` record query digests, disclosure class, dates, providers, counts, and coverage limits. Never describe a single-provider convenience query as exhaustive.

Deduplicate by identifiers and reconciled version families, not titles alone. Preserve provider provenance and the reason each candidate was included, excluded, or left uncertain. Search for contrary, null, replication, correction, and retraction evidence rather than only confirmatory terms.

Use backward and forward links only with recorded bounds. For guidelines, policies, datasets, and software, resolve the authoritative current object and version rather than relying on secondary mentions.

For an extensive reference audit, first obtain the scope confirmation described
in `$OCSL_REVIEW_HOME/docs/literature-review.md`. Target gap searches to claims
that need missing, current, contrary, or broader evidence. Add identified DOI
candidates using `review literature fetch --additional-doi` with `--claim-id`;
read and assess them before using their source examinations for completeness.
Preserve provider errors and zero-result searches, and continue with accessible
sources. Keep search records and assessments in `.ocsl-review/audit-graph.json`
and acquisition history in `.ocsl-review/literature-audit.json`.

## Egress boundary

For confidential work, derive minimized generic concepts locally. Do not transmit manuscript sentences, figures, reviewer notes, or unpublished title-author combinations. Log the sanitized request and its approval class before a network call.

## Interpretation limit

Search retrieval is not support verification. A candidate joins the audit graph only after identity reconciliation and, where needed, full-text examination. Report provider outages and inaccessible sources as coverage limitations and abstain from claims of completeness they prevent.
