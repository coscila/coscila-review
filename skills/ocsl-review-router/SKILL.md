---
name: ocsl-review-router
description: Route a scientific manuscript review into bounded evidence, methods, reporting, and synthesis passes.
---

# Scientific review router

Use this skill at the beginning of a manuscript, reference, revision, or evidence-check request. It plans work; it does not itself declare that a citation supports a claim.

## Runtime

Primary environment: `review-core`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-router \
  --workspace "$PWD" \
  -- <command>
```

## Routing procedure

1. Identify the supplied artifacts and whether extraction can preserve page, section, paragraph, sentence, clause, table, figure, and citation-marker locations.
2. Default an unpublished or peer-review manuscript to confidential. Record the approved egress mode before any provider call.
3. Translate the user request into explicit deliverables and denominators. A request to check references normally needs ingestion, atomic claims, identity reconciliation, support verification, and completeness assessment.
4. Select only the specialist passes needed, but record any omitted pass and the resulting limitation.
5. Put high-risk decisions first: fabricated identities, publication-status problems, false support, scope inflation, and confidentiality violations.
6. Write `review-plan.json` with selected skills, dependencies, expected artifacts, egress class, stopping rules, and human approvals.

## Decision rules

- Do not route drafting or stylistic polishing away from the laboratory's existing writing skills. This companion owns evidence-chain audit work.
- Treat “cited correctly” and “cited enough” as separate questions with separate counts.
- Require full-text evidence for conclusions whose scope, number, quotation, or causal language cannot be verified from an abstract.
- If required input is missing, choose an abstention or a narrower pass instead of inventing coverage.

## Boundary

Only identifiers and minimized search concepts may leave a confidential workspace by default. Manuscript prose, figures, reviewer notes, and unpublished title-author combinations require an approved egress plan. Every downstream finding must retain its audit-graph identifiers and a required human action.

