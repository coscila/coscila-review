---
name: ocsl-review-reference-analysis
description: Build and analyse a reproducible literature corpus with preserved reference identities, citation loci, evidence and versioned assessments; resume it or fork its database for a different research question.
---

# Reference analysis

<!-- SPDX-License-Identifier: Apache-2.0 -->

Use for corpus-wide reference analysis, a citation-support meta-study, or reuse
of an existing literature database. A corpus is durable research state; the
controller chat coordinates it but is not its database. Do not turn a fixed
corpus study into a generic manuscript peer review or add a journal-fit pass.

## Runtime and starting point

Primary environment: `review-literature`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-reference-analysis \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review census --help
```

1. Establish whether this is a **new study**, **resume of the same analysis**, or
   **new question over a saved corpus**. Locate its manifest and database rather
   than reconstructing scope from chat recollection. Preserve existing explicit
   authorization; do not repeatedly ask for approval of unchanged scope.
2. Record the question, article/date/stratum inclusion rules, extraction and
   identity versions, evidence tiers, estimands, denominator definitions,
   validation design, model roles and output requirements in one versioned
   study specification. Counts are measured outcomes, not fixed success targets.
3. For the oncology study, use the deployed advanced-orchestration template
   **meta meta literature review** and its study prompt. The skill itself is
   reusable for other journals, corpora and questions. A template does not prove
   that provider access or corpus execution works in this deployment.
   For a different question over an inherited corpus, use the user's new
   scientific scope and deliverables; do not reapply the oncology census prompt
   or its five-figure paper requirement. Pin task-appropriate worker assignments,
   schemas and protocol in the new analysis, retaining the code ownership and
   evidence-provenance boundaries.

Read [the historical solution and its limits](references/claude-census-lessons.md)
when planning the first run or comparing against the earlier oncology paper.
Read [the corpus protocol](references/corpus-protocol.md) before extraction or
judging, and [the database and fork contract](references/database-and-forks.md)
before initialization, export or reuse.

## Execution contract

- Preserve the full eligible manifest, including unavailable papers. Track
  review articles, printed reference entries, distinct works, citation loci,
  acquired sources and accepted assessments separately. Never call acquisition
  or a pilot a completed analysis of the corpus.
- Use structured JATS/publisher HTML, independent extraction checks, verified
  identities, cached evidence and per-locus assessment records. Never join
  manuscript versions merely by reference number, drop range interiors or
  classify a parser omission as an unsupported citation.
- Store complete acquired evidence and exact provenance before inference.
  Abstract agreement, excerpt-based assessment and full-text verification are
  distinct measurements. Specific claims require adequate full text; abstain
  when it is missing. Do not silently shorten evidence to fit a model.
- For this template, Claude owns executable code, orchestration decisions,
  statistics, figures and manuscript prose. Qwen workers return bounded,
  structured evidence assessments through the configured bulk worker path;
  they do not write or modify scripts. Validation model roles come from the
  approved study specification, not a historical paper's benchmark design.
- Pin prompts, schemas, provider/model receipts and evidence hashes; persist
  accepted and rejected attempts. Resume incomplete work without relabelling
  historical judgments as fresh or repeating already accepted assessments.
- Advance one study through durable stage checkpoints with a bounded provider
  queue. Measure concurrency and costs on the current protocol; historical
  throughput is not a guarantee. A routine assistant reply is not completion.

Use `ocsl-review-reference-integrity` for identity and publication status,
`ocsl-review-citation-census` for the existing per-manuscript worker lifecycle,
and the writing companion, when installed, for a manuscript derived from
verified facts. Real commands and deployment boundaries are documented in
[runtime integration](references/runtime-integration.md). The database helper
bundled here stores and forks research state; it does not implement acquisition,
automated corpus scheduling or a new MCP server.

Unpublished material is confidential by default. Every provider call and
worker submission must carry the deployment's authorization for the actual
workspace and evidence; never bypass a refusal with another networking path.

## Reusable deliverable

Deliver a verified database **and its source bundle**, study specification,
versioned code/environment information, worker receipts, reproducible facts and
requested reports. A scores-only table cannot support a new scientific question.
Use the bundled snapshot helper and retain its manifest digest in the handoff.
For a different question, branch the saved corpus into a new analysis namespace:
reuse verified sources and identities, retain old judgments as labelled history,
and produce new task definitions, assessments and outputs. Lab chat forks
preserve workspace files, but use the snapshot helper for a verified SQLite
handoff. Forks do not transfer live broker jobs or authorize new data disclosure.

Report completion only when every eligible paper, reference and required locus
has an explicit terminal outcome, and every claimed result is reproducible from
the pinned database and methods. Unavailable evidence stays visible in coverage
and uncertainty counts; it does not silently disappear from the study.
