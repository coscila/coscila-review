---
name: ocsl-review-evidence-balance
description: Test whether a manuscript represents the available evidence fairly and independently.
---

# Evidence balance

Use after the cited evidence set is identified, especially for contested, clinical, policy, or high-certainty claims.

## Runtime

Primary environment: `review-literature`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-evidence-balance \
  --workspace "$PWD" \
  -- <command>
```

## Procedure

Build an evidence map by study design, population, outcome, direction, publication status, version family, cohort or dataset lineage, and likely independence. Search deliberately for null, adverse, contrary, replication, and more authoritative evidence. Distinguish a legitimate scope choice from selective omission.

Compare the manuscript's certainty and breadth with the whole located set. Flag duplicated cohorts counted as multiple confirmations, review-mediated citation laundering, exclusion of a relevant higher-quality design, obsolete guidance, and asymmetric treatment of favorable and unfavorable studies.

For each finding, record the affected claim ID, omitted or overweighted source IDs, independence rationale, likely consequence, confidence, and a specific human action. Avoid informal vote counting; direction alone does not capture precision, bias, or design quality.

## Limits and confidentiality

Provider metadata may discover candidates but cannot establish their findings. Read the necessary source sections or abstain. Keep manuscript wording and reviewer interpretation local; external searches receive identifiers or minimized concepts only. This skill identifies balance problems and does not dictate editorial acceptance.
