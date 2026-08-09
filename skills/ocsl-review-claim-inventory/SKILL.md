---
name: ocsl-review-claim-inventory
description: Inventory atomic manuscript claims and determine their evidence obligations.
---

# Atomic claim inventory

Use after successful ingestion and before assessing references. The unit of work is a proposition that can receive one evidence verdict.

## Runtime

Primary environment: `review-nlp`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-claim-inventory \
  --workspace "$PWD" \
  -- <command>
```

## Claim record

For each material proposition, emit its stable location, exact clause text, normalized atomic statement, origin, claim type, modality, population, intervention or exposure, comparator, outcome, direction, magnitude, timeframe, causal strength, bound citation markers, and citation-required decision with rationale.

## Procedure

1. Split coordinated sentences when different clauses could have different sources or verdicts.
2. Label manuscript-derived results separately from external background, methods provenance, interpretation, policy, novelty, and recommendation claims.
3. Do not require an external citation merely because the manuscript reports its own result. Do require provenance for reused methods, datasets, instruments, software, scales, and external facts.
4. Treat quotations, exact numbers, universal statements, clinical recommendations, and priority language as high-verification claims.
5. Record uncited material claims, misplaced citation markers, and clauses whose binding remains ambiguous.
6. Preserve hedges and negation; never normalize association into causation or possibility into certainty.

## Boundary

Claim segmentation is not a support verdict. Keep confidential text local, and use only opaque claim IDs outside the workspace. If syntax or extraction loss prevents a reliable atomic statement, abstain, retain the original span, and assign human clause resolution.

