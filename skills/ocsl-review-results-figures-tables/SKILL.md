---
name: ocsl-review-results-figures-tables
description: Reconcile manuscript result statements with figures, tables, and analysis descriptions.
---

# Results, figures, and tables

Use for internal consistency of reported findings and displays, not for judging whether external citations support background claims.

## Runtime

Primary environment: `review-statistics`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-results-figures-tables \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review --help
```

## Procedure

Create links among every central result sentence, model or contrast, table cell, figure panel, caption, supplement, and reported sample size. Check denominators, missing observations, units, axes, scales, transformations, uncertainty, significance labels, rounding, percentages, totals, and direction of effect.

Verify that captions identify analysis units, summaries, error bars, tests, adjustments, and exclusions sufficiently to interpret the display. Look for truncated axes, hidden distributions, duplicated panels, incompatible color legends, selective time points, and image processing that changes interpretation.

Recompute only bounded arithmetic that the supplied data support, preserving the original value and calculation trace. Classify discrepancies by whether they affect presentation, a local result, or a central conclusion.

Inspect the rendered artifact when visual interpretation matters: text
extraction and caption coverage do not prove that figure panels were examined.
Do not infer missing table values or hide unreadable displays. The companion's
report renderer produces DOCX/Markdown; bespoke publication figures and PDF
layout production require separately scoped tooling. See the applicability
notes in `$OCSL_REVIEW_HOME/docs/workflow-regressions.md`.

## Limits and boundary

Do not flag the manuscript's own result as needing an external citation. A visually plausible graph is not proof of the underlying analysis, and an image anomaly is not misconduct evidence without investigation. When source data, resolution, or extraction is inadequate, abstain and request the original display or data. Keep figures and tables inside the confidential workspace.
