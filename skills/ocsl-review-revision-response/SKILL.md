---
name: ocsl-review-revision-response
description: Trace reviewer requests through author responses and actual manuscript revisions.
---

# Revision and response audit

Use with the prior manuscript, revised manuscript, reviewer comments, and response letter when available.

## Runtime

Primary environment: `review-documents`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-revision-response \
  --workspace "$PWD" \
  -- <command>
```

## Procedure

1. Hash and ingest each version independently, preserving location mappings and tracked-change uncertainty.
2. Decompose compound reviewer comments into action units and classify each as answer, analysis, text change, evidence request, policy item, or editorial decision.
3. Link each action to the author's response and exact revised locations; distinguish completed, partly completed, declined with rationale, contradicted by the file, and not assessable.
4. Re-run affected claim, citation, methods, result, or policy checks rather than accepting a response assertion.
5. Detect new unsupported claims, references, analyses, scope changes, or inconsistencies introduced during revision.
6. Draft responses that state what changed and where, without claiming an analysis or correction that is absent.

## Limits and boundary

Textual difference alone does not establish scientific adequacy. If the earlier version, supplement, response attachment, or necessary source is unavailable, abstain on completion and request it. Treat all review correspondence as confidential. This skill verifies and organizes responses; authors and editors retain responsibility for substantive choices.
