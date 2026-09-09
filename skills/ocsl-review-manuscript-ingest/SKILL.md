---
name: ocsl-review-manuscript-ingest
description: Convert manuscript files into a location-stable review model and report extraction loss.
---

# Manuscript ingestion

Use this skill before claim extraction whenever the source is PDF, DOCX, LaTeX, HTML, or a bundle of manuscript and supplement files.

## Runtime

Primary environment: `review-documents`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-manuscript-ingest \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review ingest --help
```

## Required model

Create `manuscript.json` with immutable document and location identifiers, ordered text blocks, clause boundaries, citation clusters, bibliography entries, footnotes, equations, table cells, figure captions, supplement links, and cross-references. Preserve visible text exactly in a dedicated field while keeping normalized text separate.

The canonical output is `.ocsl-review/manuscript.json`; its `ingestion` record carries extraction coverage and limitations. Do not invent a separate `ingestion-report.json` output. Preserve source hashes, unreadable regions, citation diagnostics, and object counts supported by the parser.

## Procedure

1. Hash inputs before parsing and never alter the originals.
2. Prefer structural sources over rendered text when both are supplied; retain mappings back to the rendered page.
3. Detect repeated headers, hyphenation, ligatures, hidden text, multi-column order, and OCR uncertainty.
4. Bind a citation marker to the narrowest recoverable clause. Mark ambiguity rather than attaching it to an entire paragraph.
5. Reconcile bibliography labels with in-text clusters without resolving scholarly identity yet.
   Expand `[1-10]` to all ten members. Preserve raw spans and distinguish citation occurrences from their expanded members. Check mixed lists, dash variants, superscripts, and table reference columns. Missing range interiors, duplicate labels, malformed groups, and doubtful superscripts remain diagnostics; never silently resolve only a range's endpoints.
6. Stop downstream “complete review” claims when required pages, references, figures, or supplements were not ingested.

## Boundary

Ingestion is local for confidential manuscripts. Do not upload files or excerpts to public parsers. This skill records text and structure but does not decide whether a claim is true or cited adequately. When exact location cannot be recovered, abstain from clause-level conclusions, set the location confidence accordingly, and require human inspection rather than guessing.

Use `$OCSL_REVIEW_HOME/docs/literature-review.md` for downstream commands and
`$OCSL_REVIEW_HOME/docs/workflow-regressions.md` for parser acceptance cases.
