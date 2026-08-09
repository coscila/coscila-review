---
name: ocsl-review-reporting-guidelines
description: Apply the appropriate reporting checklist with location-level evidence.
---

# Reporting guideline compliance

Use to assess transparency for the actual study design. Do not use checklist completion as a proxy for methodological soundness.

## Runtime

Primary environment: `review-core`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-reporting-guidelines \
  --workspace "$PWD" \
  -- <command>
```

## Procedure

1. Classify the study and select the guideline edition applicable on the manuscript's target date, recording source, version, and retrieval date.
2. Translate each checklist item into a concrete disclosure question without rewriting its normative meaning.
3. Record exact manuscript locations for present items and distinguish absent, partly reported, not applicable, and not assessable.
4. Consider supplements, protocols, registrations, statistical analysis plans, and flow diagrams when the checklist allows distributed reporting.
5. Describe the consequence of an omission for interpretation or reproducibility; do not inflate minor formatting issues into major validity concerns.
6. Route design flaws discovered during this work to the methods skill and evidence claims to the claim audit.

## Limits and confidentiality

A transparent report may still describe a biased study, and a poorly reported item may be methodologically sound but unverifiable. Keep those judgments separate. Do not send confidential manuscript text to checklist websites or public services. If the correct study classification or guideline version is uncertain, abstain from a compliance score, record alternatives, and require human selection.
