---
name: ocsl-review-systematic-review
description: Audit the full conduct and reporting chain of a systematic review or meta-analysis.
---

# Systematic review audit

Use for systematic, scoping, rapid, and quantitative evidence syntheses. Select methods appropriate to the declared review type.

## Runtime

Primary environment: `review-statistics`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-systematic-review \
  --workspace "$PWD" \
  -- <command>
```

## Audit sequence

1. Compare the question, eligibility criteria, outcomes, analyses, and dates with the registration or protocol; log justified and unexplained deviations.
2. Reconstruct provider coverage, complete query strings, limits, update date, deduplication, screening, exclusions, reviewer independence, and flow counts.
3. Sample or fully verify extraction against primary reports according to the approved plan, including study identity and shared cohorts.
4. Evaluate risk-of-bias tool selection and application separately from reporting quality.
5. Recalculate bounded synthesis components when data permit: effect orientation, standard errors, weighting, heterogeneity, transformations, dependency handling, and sensitivity analyses.
6. Check publication-bias analyses, subgroup credibility, certainty judgments, and whether conclusions exceed the evidence base.

## Citation audit integration

Claims made by the review manuscript still require claim-source edges. A review's included-study table does not prove its narrative interpretation, and several reports from one cohort are not independent evidence.

## Limits and confidentiality

Record inaccessible search histories, extraction forms, protocols, or primary reports as explicit coverage gaps and abstain on the affected checks. Do not infer a favorable verdict from abstracts or summary metadata. Keep unpublished synthesis files and reviewer notes local and refer editorial judgments to humans.
