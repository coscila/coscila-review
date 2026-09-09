---
name: ocsl-review-journal-policy
description: Compare a manuscript package with current, source-captured journal requirements.
---

# Journal policy

Use when a target journal, article type, or editorial policy is known. Policies change, so every requirement needs a source and retrieval date.

## Runtime

Primary environment: `review-literature`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-journal-policy \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review --help
```

## Procedure

1. Identify the journal, article type, submission stage, and policy date relevant to the task.
2. Capture authoritative requirements for scope, length, structure, references, reporting checklists, data and code, ethics, registration, disclosures, preprints, images, and supplementary materials.
3. Store a bounded source locator, retrieval time, and policy version or page hash for each rule.
4. Map requirements to exact manuscript-package locations and classify compliant, noncompliant, not applicable, or not assessable.
5. Separate mandatory rules from recommendations and editorial discretion.
6. Route scientific validity questions to the methods or evidence skills rather than treating formatting compliance as scientific approval.

## Limits and confidentiality

Do not submit manuscript text to journal portals during review. Public policy lookup may use the journal and article type, not confidential title-author combinations. If policy pages conflict, are inaccessible, or lack a stable date, abstain on compliance, report the ambiguity, and require editorial confirmation instead of inventing a rule.
