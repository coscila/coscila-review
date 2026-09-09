---
name: ocsl-review-ethics-transparency
description: Audit ethics, registration, governance, funding, conflict, and authorship disclosures.
---

# Ethics and transparency

Use to locate and reconcile required declarations. This is an editorial audit, not legal advice or a misconduct finding.

## Runtime

Primary environment: `review-core`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-ethics-transparency \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review --help
```

## Procedure

Inventory human or animal approvals, consent, trial or protocol registration, prospective versus retrospective status, privacy and data governance, biosafety, conflicts, funding, sponsor role, authorship, contributorship, acknowledgements, and use of automated tools.

Cross-check identifiers, dates, populations, outcomes, and analysis plans across the manuscript, supplement, registration, protocol, and availability statements. Distinguish a missing statement, an internal inconsistency, a potentially consequential deviation, and an issue that requires institutional or editorial inquiry.

Tie each finding to an exact location and applicable dated requirement when one is known. Phrase actions as requests for clarification, documentation, correction, or escalation; do not infer intent.

## Limits and confidentiality

Do not search personal information beyond what the review task and policy authorize. Keep reviewer notes, participant information, and unpublished content local. Provider metadata cannot resolve substantive compliance questions. Where jurisdiction, policy version, or documentation is unavailable, abstain and refer the decision to the editor, institution, or qualified human authority.

