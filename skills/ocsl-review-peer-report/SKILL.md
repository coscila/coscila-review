---
name: ocsl-review-peer-report
description: Build a prioritized peer-review report from traceable audit findings.
---

# Peer review report

Use only after the selected audit passes have produced evidence records. This skill synthesizes; it does not fill gaps by intuition.

## Runtime

Primary environment: `review-documents`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-peer-report \
  --workspace "$PWD" \
  -- <command>
```

## Composition procedure

1. Validate audit denominators and unresolved gates before drafting conclusions about citation correctness or sufficiency.
2. Prioritize findings by consequence for validity, interpretation, evidence integrity, reproducibility, ethics, and policy—not by ease of editing.
3. For every major point, state the exact location, problem, evidence or audit-edge IDs, consequence, and an actionable request.
4. Separate required corrections from optional improvements and confidential comments to the editor from author-facing comments.
5. Preserve calibrated language for inaccessible sources, ambiguous binding, incomplete searches, and disputed methods.
6. Check every external factual statement introduced by the report against its own verified source record.

## Output rules

Keep `audit-summary.json` machine-readable with explicit totals and processed counts. Do not announce “yes” for cited-correctly or cited-enough when any mandatory gate forbids it. Include material abstentions and remaining human actions, not only adverse findings.

## Boundary

The reviewer report may recommend action but does not make the editor's accept/reject decision. Do not reveal reviewer notes or confidential manuscript content outside approved deliverables, shared caches, or public tools. Writing polish may be delegated to existing laboratory writing skills only after evidence links are frozen.
