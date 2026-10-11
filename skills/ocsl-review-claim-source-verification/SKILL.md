---
name: ocsl-review-claim-source-verification
description: Verify whether a located source supports the exact manuscript clause and scope.
---

# Claim-to-source verification

Use this skill only after claim boundaries and reference identities are stable. False support is the highest-cost error.

## Runtime

Primary environment: `review-literature`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-claim-source-verification \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review literature --help
```

## Evidence procedure

1. Follow the confirmed acquisition workflow in `$OCSL_REVIEW_HOME/docs/literature-review.md`. Read every block of the complete stored source, including late sections, tables, captions, and relevant supplements. Track each actually read block with `readUnitIds`; a download or preview is not complete reading.
2. Locate exact supporting or conflicting evidence for each citation locus. Original source bytes and full extracted text remain private workspace artifacts; audit links carry actual unit IDs, bounded excerpts, and verified digests. Record missing graphics or supplements when they limit the claim.
3. Compare species, setting, population, sample, exposure or intervention, comparator, outcome, timeframe, direction, magnitude, uncertainty, and causal strength.
4. Use the schema verdicts `direct-support`, `scope-caveat`, `partial-indirect`, `contradicted`, `not-addressed`, and `abstain`. Select `very-confident`, `confident`, or `not-confident` explicitly; access only sets a ceiling and never supplies a verdict or confidence label.
5. Check every clause bound to a citation cluster; one supported clause cannot launder unsupported neighboring text.
6. Trace review articles to the primary study when the manuscript represents a primary result, and identify shared datasets or version families.
7. Reproduce quoted wording and numeric values against the source, including denominators and units.

## Abstention rules

For repeated or large-scope worker measurements, use
`ocsl-review-citation-census` and `$OCSL_REVIEW_HOME/docs/citation-census.md`.
Keep its abstract/full-text/panel observations separate; adjudicate evidence
before promoting a worker finding into a peer-review audit edge. Ordinary
`review literature record` calls also retain immutable, source-pinned history.

Metadata never proves support. When full text is unavailable, inspect title and complete abstract and continue at lower confidence. A substantive abstract-based verdict requires an explicitly justified `abstract-sufficient` claim and complete abstract reading, with confidence at most `confident`. Methods, subgroups, tables, quotations, numbers, causal and priority claims still require full text; emit `abstain` with the missing evidence and next action. Apply the same evidence bar to adverse verdicts: limited access cannot prove `not-addressed` or `contradicted`. Partial source previews cannot establish complete examination.

## Confidentiality and role

Keep the manuscript clause local. Provider calls may use identifiers; sanitized concepts require the recorded egress plan. This skill produces audit edges, not polished reviewer prose, and must not introduce new literature claims without their own evidence records.
