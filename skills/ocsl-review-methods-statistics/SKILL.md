---
name: ocsl-review-methods-statistics
description: Audit whether study design and statistical analysis support the manuscript's inferences.
---

# Methods and statistics

Use for methodological validity. Reporting checklist compliance is handled separately and cannot substitute for this pass.

## Runtime

Primary environment: `review-statistics`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-methods-statistics \
  --workspace "$PWD" \
  -- <command>
```

## Audit sequence

1. State the research question, target population, experimental or observational unit, design, estimand, allocation, clustering, pairing, repeated measures, and analysis population.
2. Reconstruct each major model and contrast from the methods. Identify rank deficiency, confounding, pseudoreplication, leakage, post-treatment adjustment, inappropriate dichotomization, and outcome switching.
3. Check assumptions and diagnostics appropriate to the model, missing-data handling, multiplicity, stopping rules, exclusions, sensitivity analyses, and uncertainty intervals.
4. For prediction, require split-aware preprocessing, grouped or temporal validation where relevant, calibration, uncertainty, and external validation claims that match the design.
5. Compare causal language with identification assumptions and study design. An association does not become causal through statistical significance.
6. Tie every concern to an exact manuscript location, inference affected, severity, and feasible repair or limitation.

## Evidence and role limits

Recalculation may reveal internal inconsistency but does not authorize silently replacing the authors' analysis. Distinguish missing reporting from invalid design and from unverifiable implementation. If data, code, or detail is insufficient, abstain on the disputed result and request the smallest needed artifact.

Keep confidential data and manuscript content local. This skill supplies technical findings to the report; it does not make the editor's decision or claim external methodological consensus without a verified citation edge.

