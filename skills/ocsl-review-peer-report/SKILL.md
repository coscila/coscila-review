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
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review package --help
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

Run the package and render commands in
`$OCSL_REVIEW_HOME/docs/literature-review.md`; canonical files are
`.ocsl-review/review-package.json`, `.ocsl-review/audit-summary.json`, and
`.ocsl-review/review-report.*`. Preserve every citation locus and verdict in the
rendered appendix, including correctly supported and abstained rows. Use graph
counts for all prose and tables, and verify the render manifest against fresh
artifacts. Keep obtained, read, and evidence-used counts distinct. Never rank
papers or combine correctness and completeness into an unsupported score.

## Boundary

Census model observations are a separate measurement ledger. Do not promote
their support counts into expert correctness claims without adjudication. For
study prose, use the exported `facts.json` SQL/hash derivations with the writing
companion's facts verifier when available; preserve NULL facts as unassessable.

The reviewer report may recommend action but does not make the editor's accept/reject decision. Do not reveal reviewer notes or confidential manuscript content outside approved deliverables, shared caches, or public tools. Writing polish may be delegated to existing laboratory writing skills only after evidence links are frozen.
