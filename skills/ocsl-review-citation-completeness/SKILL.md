---
name: ocsl-review-citation-completeness
description: Determine whether each citation-required claim has enough appropriate evidence.
---

# Citation completeness

Use after atomic claims and support edges exist. This is a coverage and adequacy assessment, not a citation-count score.

## Runtime

Primary environment: `review-literature`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-citation-completeness \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review literature --help
```

## Procedure

For every citation-required claim, evaluate coverage, directness, scope match, source role, independence, breadth, currency, balance, primary-source adequacy, contrary or null evidence, replication, methods and resource provenance, novelty-search adequacy, citation placement, and irrelevant padding.

Group preprints and later articles, companion analyses, overlapping cohorts, and repeated database extracts before counting independent lines of evidence. Match evidentiary burden to the claim: broad clinical or universal language needs more than a narrow model or single cohort, while a limited historical fact may need only one authoritative source.

Emit a reason-coded completeness verdict and a minimal repair action for each claim. Preserve the distinction between an absent citation, an attached but indirect source, insufficient breadth, and an outdated or unbalanced evidence set.

Use `.ocsl-review/audit-graph.json` `completenessAssessments`, separately from
`verificationLinks`. For targeted gaps, record dated `searchRuns`, acquire
identified candidates with `fetch --additional-doi` and `--claim-id`, read them,
and cite their `sourceExaminationIds` in the completeness assessment. These
additional sources do not alter bibliography denominators or become evidence
that the manuscript's existing citation supports a clause. Both sufficient
and adverse literature judgments need examined evidence; inaccessible gaps
remain `not-assessable`. Follow `$OCSL_REVIEW_HOME/docs/literature-review.md`.

## Summary gate

Populate explicit denominators in `audit-summary.json`. A global “yes” is prohibited when processing sampled only part of the manuscript, ingestion or binding is incomplete, needed sources are inaccessible, or full-text questions were judged from abstracts or metadata. Use `not-fully-verifiable` when access, not evidence, is the limiting factor.

## Boundary

Do not send confidential clauses during searches. Completeness findings remain audit records for human review; writing and tone belong to the report skill.
