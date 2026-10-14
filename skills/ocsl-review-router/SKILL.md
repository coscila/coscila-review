---
name: ocsl-review-router
description: Route a scientific manuscript review into bounded evidence, methods, reporting, and synthesis passes.
---

# Scientific review router

Use this skill at the beginning of a manuscript, reference, revision, or evidence-check request. It plans work; it does not itself declare that a citation supports a claim.

For a corpus-wide reference-analysis study or reuse of a saved literature
database, route first to `ocsl-review-reference-analysis`. Keep the declared
corpus, measurement protocol and outputs; do not automatically add manuscript
peer-review lenses, missing-literature searches or journal-fit checks. Existing
explicit authorization for the full corpus does not need repeated confirmation
for each constituent review.

## Runtime

Primary environment: `review-core`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-router \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review prepare --help
```

## Routing procedure

1. Identify the supplied artifacts and whether extraction can preserve page, section, paragraph, sentence, clause, table, figure, and citation-marker locations.
2. Default an unpublished or peer-review manuscript to confidential. Record the approved egress mode before any provider call.
3. For a request to check references or citations, prepare the local inventory and run `review literature prepare`. Show its bibliography and citation counts and propose reading every accessible reference plus targeted missing or contrary evidence. Ask for confirmation of this extensive scope unless the user already explicitly authorized it; record the actual answer with the returned scope digest. A generic request to check citations is not that confirmation.
4. Select only the specialist passes needed, but record any omitted pass and the resulting limitation.
5. Put high-risk decisions first: fabricated identities, publication-status problems, false support, scope inflation, and confidentiality violations.
6. Use `.ocsl-review/review-plan.json` and the managed `.ocsl-review/literature-audit.json`; keep correctness and completeness decisions separate in `.ocsl-review/audit-graph.json`.

For the runnable preparation, confirmation, acquisition, reading, and recording
sequence, read `$OCSL_REVIEW_HOME/docs/literature-review.md`. Resolve this path
from the source root even when this skill is installed in a global mirror.

For an authorized systematic census or model-judgment study, use
`ocsl-review-citation-census` after reference acquisition and binding. Its worker
observations have their own history and facts export; they do not certify the
manuscript graph's scientific verdicts.

## Decision rules

- Do not route drafting or stylistic polishing away from the laboratory's existing writing skills. This companion owns evidence-chain audit work.
- Treat “cited correctly” and “cited enough” as separate questions with separate counts.
- Require full-text evidence for conclusions whose scope, number, quotation, or causal language cannot be verified from an abstract.
- If required input is missing, choose an abstention or a narrower pass instead of inventing coverage.
- After confirmation, continue through every bibliography entry and preserve missing-source outcomes. Analyze title and complete abstract at lower confidence when appropriate; a full-text requirement still forces abstention. Obtaining a document does not count as reading or judging it.

## Boundary

Only identifiers and minimized search concepts may leave a confidential workspace by default. Manuscript prose, figures, reviewer notes, and unpublished title-author combinations require an approved egress plan. Every downstream finding must retain its audit-graph identifiers and a required human action.
