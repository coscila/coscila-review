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
  -- <command>
```

## Evidence procedure

1. Open the strongest accessible source version and record access as full text, partial open section, abstract only, metadata only, or inaccessible.
2. Locate exact supporting or conflicting passages, tables, figures, supplements, or analyses. Store a bounded locator and a short evidence note rather than bulk source text.
3. Compare species, setting, population, sample, exposure or intervention, comparator, outcome, timeframe, direction, magnitude, uncertainty, and causal strength.
4. Separate `directly-supported`, `supported-with-scope-caveat`, `partial-or-indirect`, `contradicted`, `not-addressed`, and `not-verifiable` outcomes.
5. Check every clause bound to a citation cluster; one supported clause cannot launder unsupported neighboring text.
6. Trace review articles to the primary study when the manuscript represents a primary result, and identify shared datasets or version families.
7. Reproduce quoted wording and numeric values against the source, including denominators and units.

## Abstention rules

Metadata never proves support. Abstract-only access cannot settle details requiring methods, subgroup, table, quotation, number, or causal analysis. In those cases emit `not-verifiable`, the missing evidence, confidence, and required human action. Do not convert partial access into a favorable verdict.

## Confidentiality and role

Keep the manuscript clause local. Provider calls may use identifiers; sanitized concepts require the recorded egress plan. This skill produces audit edges, not polished reviewer prose, and must not introduce new literature claims without their own evidence records.

