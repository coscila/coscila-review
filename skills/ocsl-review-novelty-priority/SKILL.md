---
name: ocsl-review-novelty-priority
description: Audit first, novel, and no-prior-study claims with a reproducible search.
---

# Novelty and priority

Use for explicit or implied priority claims. The output is a bounded search conclusion, never proof that no earlier work exists.

## Runtime

Primary environment: `review-literature`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-novelty-priority \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review literature --help
```

## Procedure

1. Rewrite the priority claim into its defining dimensions: population or system, intervention or exposure, outcome, method, setting, and claimed distinction.
2. Search multiple relevant providers with synonyms, historical terminology, broader and narrower formulations, and cited-neighbor traversal.
3. Record exact queries, dates, bounds, and provider failures so another reviewer can repeat the attempt.
4. Reconcile preprints, conference records, registrations, protocols, and journal versions before comparing dates or independence.
5. Compare candidate predecessors dimension by dimension; topical similarity alone neither establishes nor defeats novelty.
6. Recommend scoped language such as “we found no earlier report under this protocol” when coverage is incomplete.

## Decision limits

Never return an unconditional “first” finding from one database or metadata-only screening. Inaccessible records, ambiguous dates, limited language coverage, or provider gaps require a calibrated abstention or narrowed claim and a human priority check.

Record searches and candidate examinations in `.ocsl-review/audit-graph.json`
through `$OCSL_REVIEW_HOME/docs/literature-review.md`. A sufficient novelty
assessment requires dated searches from at least two providers and actual
examination of the relevant sources. A targeted update does not certify a
systematic search of every literature database.

## Confidentiality and role

Construct generic concept queries locally for unpublished work. An unpublished title, author list, abstract, or distinctive sentence may leave the workspace only under explicit approval. This skill audits priority; it does not draft promotional novelty language.
