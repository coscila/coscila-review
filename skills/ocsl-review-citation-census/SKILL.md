---
name: ocsl-review-citation-census
description: Run resumable per-locus citation-support measurements, compare pinned abstract and full-text passes, and export traceable facts for study reporting.
---

# Citation-support census

<!-- SPDX-License-Identifier: Apache-2.0 -->

Use for an explicitly scoped, systematic citation audit or a study of model
judgments. This produces model measurements with evidence and uncertainty;
human-adjudicated labels need separate provenance.

For an entry-level census with a registered census instrument
(`citation-census@1`: one verdict per printed bibliography entry, with the
instrument's fixed evidence policy, batch geometry and request settings), use
`ocsl-review study`: prepare, abstracts, judge, full-text, the predeclared
sensitivities, gates, freeze and facts. A study that builds its population
fresh enumerates it with population-level queries, acquires with the journals'
live routes, derives each review's cited works with `study works`, records any
protocol exclusion with `study exclude` and compares an earlier manifest only
at the end. Read `$OCSL_REVIEW_HOME/docs/census-instrument.md` first; the
instrument runs unchanged and its decisions are recorded per entry.

For a multi-paper corpus and a database that can support future questions, start
with `ocsl-review-reference-analysis`. This census skill also supplies the
per-manuscript **v1** measurement lifecycle; per-locus corpus studies use the
maintained `ocsl-review corpus` v2 workflow through that reference-analysis
skill. The measurement rules below apply to those per-locus lifecycles, not to a
registered census instrument, whose own evidence policy governs. Do not rewrite
old assessments when changing instruments. No path transfers live broker jobs
when a chat is forked.

## Runtime

Primary environment: `review-literature`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-citation-census \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review census --help
```

Read `$OCSL_REVIEW_HOME/docs/citation-census.md` for preparation, worker
submission, reconnection, retries, and facts export. Acquisition and scope
confirmation remain in `docs/literature-review.md`; dispatch still requires the
deployment's independent egress approval.

## Measurement rules

- Reconcile every citation anchor before claiming a census. Prefer supplied
  JATS or publisher HTML to PDF extraction; retain printed reference labels,
  target IDs, source hashes, and article version identities.
- Keep one task per claim, citation occurrence, and reference member. Repeated
  DOIs and numeric-range interiors retain distinct loci. Inaccessible or
  unverified entries stay in the denominator as uncertain.
- Send the complete stored evidence units. A source too large for the selected
  model is a recorded failure requiring a capable configuration; do not trim
  the source or select its first sentences. Complete reading declarations and
  exact quotations are mechanically checked, not proof of a model's attention.
- Pin worker configuration, prompt, output schema, source identity and sampling
  design. Abstract, full-text and panel runs retain independent histories.
  Methods, numeric, quotation, causal and priority claims require full text;
  abstract silence cannot establish an adverse verdict.
- Mark filtered or limited runs as pilots. For comparisons, match loci,
  evidence versions and settings; report disagreements, abstentions, coverage,
  and dependence between citations in one paper. Labels imported from earlier
  runs are comparators, not human ground truth.
- Export facts and regenerate every reported count from its recorded SQL and
  SQLite hash. Report provider/model token counts with unknown values intact.
  A successful transport or parser check does not establish scientific accuracy.

Use the writing companion's facts verification and reporting skills for prose
when installed. Census outputs do not silently rewrite the review audit graph;
an expert verification pass must adjudicate findings intended for a peer report.

## Confidentiality boundary

Manuscripts are confidential by default. Worker submission and resume must
pass the independent, exact-workspace egress gate before sending complete
evidence. Scope confirmation does not grant disclosure; never switch to an
ungated model or networking script after a denial. Keep inputs and histories
private within the selected workspace.
