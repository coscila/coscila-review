---
name: ocsl-review-calibration
description: Evaluate review behavior on adversarial evidence and confidentiality cases.
---

# Review calibration

Use for fixture-based acceptance and regression testing, never to tune against a live confidential manuscript's desired outcome.

## Runtime

Primary environment: `review-statistics`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-calibration \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" calibrate --help
```

## Evaluation design

Maintain immutable fixture inputs and adjudicated expected records for fabricated identities, mismatched identifiers, version-family duplication, retracted or corrected sources, species and population mismatch, association-to-causation inflation, clause-level partial support, review laundering, narrow evidence for broad claims, omitted null evidence, outdated guidance, number and quotation drift, inadequate priority searches, citation padding, manuscript-result citation exemptions, inaccessible full text, attempted confidential egress, and unsupported claims introduced by a report.

Measure false-support rate as the primary high-cost error. Also report abstention precision and recall, scope-mismatch detection, citation-required accuracy, completeness findings, status recall, audit coverage, and factuality of generated review text. Stratify by access level and claim type and include denominators and confidence intervals where meaningful.

Also measure false adverse verdicts and both changes of direction after an
identity repair. Preserve initial and repaired decisions separately. State
whether denominators include abstentions and unresolved cases; a stress-test
sample is not a population error-rate estimate. Zero-row runs cannot pass.
Use `$OCSL_REVIEW_HOME/docs/workflow-regressions.md` to select applicable W1–W46
checks, keeping source access, evidence actually used, and verdict requirements
consistent. Scoring weights and category sensitivity need an explicit rationale.

## Release gate

For census studies, also retain independent abstract/full-text/panel runs and
actual provider/model receipts with token counts. Check every supplied reading
unit and quoted substring mechanically. These checks cannot certify that a
model attended to all units or establish a human gold label; matched-locus
comparisons require a separately specified study design. Export SQL/hash facts
through `$OCSL_REVIEW_HOME/docs/citation-census.md` before drafting numeric prose.

Pin fixture, provider-recording, schema, environment, and implementation versions. Separate deterministic offline tests from bounded live-provider tests. A skipped or unavailable case cannot count as passed, and a metric without its denominator cannot satisfy the gate.

## Boundary

Use synthetic or properly licensed public material; confidential manuscripts must not enter shared fixtures, caches, or logs. Calibration detects system limitations and does not override human adjudication. Where gold labels are disputed, retain multiple judgments or abstain from scoring that item.
