# Follow-up audit: compromised-evidence dependency

<!-- SPDX-License-Identifier: Apache-2.0 -->

A follow-up audit starts from a complete, frozen citation census of a corpus of review articles and asks a new
question of it: which claims of the reviews rest on cited works with a dated retraction, expression of concern or
correction, or on works that the census shows to be cited systematically without support, and how do such claims
propagate? It is research-integrity tooling for authors, reviewers and study teams. Its outputs are model
measurements with evidence, coverage and uncertainty; every adverse finding reads "check suggested".

The commands are `ocsl-review followup <command>`; the skill is `ocsl-review-followup-audit`. The evidence commands
are documented in `docs/followup-evidence.md`, the analysis commands in `docs/followup-analysis.md`.

## The branch

`followup init --database <branch> --parent <frozen scores database> [--parent-text <private copy>] --protocol
<protocol.json>` makes one SQLite file that holds only the follow-up's rows. Every connection ATTACHes the parent
census read-only and immutable as `census`; nothing of it is copied and no foreign key crosses databases. Rows name
the parent by value: a citation locus by journal, review DOI, bibliography label and locus order, a cited work by its
work key. A frozen census keeps entitled citing sentences only as digests; the optional private copy frozen with its
text supplies them, and a sentence is accepted only when its digest matches the parent's.

`followup verify` recomputes the parent's sha256 (it must equal the value recorded at branch time), checks the schema
and checks that every follow-up row refers to a row the parent or the frame holds. `followup describe` lists the
dataset versions (parent, protocol, status snapshots, the Retraction Watch snapshot, the cited-by graph, the table
source), the protocol freezes and row counts.

## Protocol and freeze

The protocol (`schemas/followup-protocol.schema.json`) holds parameters only: the status sources, the strata
definitions and their parameters, the instruments, the pinned on-premises judge, the estimands with their numerators
and denominators, the rules and the gates. Sizes and rates are measured from the run's own data. `followup strata
--freeze` writes the strata, their sizes, the estimands and the denominators into the protocol digest; judging
commands refuse a branch without a freeze, and `followup gates` checks that no judge call predates it.

## Strata

| Stratum | Units |
|---|---|
| S1 | in-body, identity-resolved loci citing a work whose resolved status is retracted, expression of concern or corrected |
| S2 | every citing locus of a work with at least the protocol's minimum of citing reviews whose share of census "not supported" verdicts exceeds its journal-by-year base (beta-binomial null, Benjamini-Hochberg false discovery rate at the protocol's level); excluded families stay out |
| S3 | loci citing the statistics-report family (versioned title rules); only when the protocol records verified table licensing, otherwise dropped before judging |
| S0 | ordinary loci sampled with the protocol seed, matched on the protocol's cells |
| co-cited | for every S1 and S2 locus, the other loci of the same citing sentence |

Each membership row carries its realised inclusion probability; the pilot is a seeded subsample with its own
probabilities.

## Status observations

One dated observation per cited work, source, status, notice DOI, notice date and retrieval time, from Crossref
(`updated-by` and `update-to` notices, with their origin: the publisher or Retraction Watch), the Retraction Watch CSV
snapshot (a dataset input, pinned by sha256), PubMed CommentsCorrections links and Europe PMC's retraction flag. A
source that answers without a notice records `none` for the notice kinds it covers; a source that cannot answer
records `unknown`. `followup status-resolve` gives every cited work one status row under the rule
`status-resolution@1`: retracted when a retraction notice stands (a later reinstatement is recorded and reverses it),
otherwise expression of concern, otherwise corrected, otherwise `none` when every notice kind was covered by a source
that answered, otherwise `unknown`.

## Instruments

| Instrument | Unit | What the judge sees |
|---|---|---|
| citation-support@2 (unchanged) | locus x cited work, abstract and full-text tiers | the claim, its context and the cited work's evidence units |
| citation-function@1 | locus | the citing sentence with the work's marker, its neighbours, the section, the work's title and year; never its status, the stratum or a verdict |
| numeric-check@1 | S3 locus | the citing sentence and the work's title and year; the comparison with the tables is deterministic |
| claim-match@1 | original claim x later sentence | the claim, the review's title, the later sentence with the review's marker |

The follow-up instruments run at temperature zero with reasoning off, send their messages verbatim, validate every
quote against the field it names, and repair invalid outputs once. `ocsl-review instruments export` prints their broker
bundles. Entitled text reaches only judges the operator's policy lists as on premises.

## Rules

- Abstract silence and failed or missing downloads never count as unsupported.
- An unassessable unit keeps its reason (`fu_support.reason`, `fu_function.reason`, ...) and goes into the coverage
  tables (`fu_coverage`), never into a numerator.
- Dependency (`dependency-aggregation@1`): sole when the citing sentence cites no other work; redundant when a
  co-cited work supports the claim at full text; essential when every co-cited work has an assessable full-text verdict
  and none supports the claim; undetermined otherwise.
- Estimates are weighted by realised inclusion probabilities, with review-clustered bootstrap intervals (the protocol's
  replicates and seed).
- Integrity classes (never cited, unresolved identity) stay apart from support verdicts.

## External requests

Provider requests go through the review companion's scholarly client: the tool's User-Agent, bounded responses, the
retry policy for throttling, NCBI's request ceiling, and a per-host interval for bulk traversal. Requests carry
identifiers only. The only contact identity is the operator's `OCSL_REVIEW_CONTACT_EMAIL`; none is ever inferred, and
recorded receipts drop contact and key parameters.

## Outputs

`followup facts` writes a closed `facts.sqlite3` and a schemaVersion 1 `facts.json` (one scalar SQL query per fact);
`followup verify-facts` checks it. `followup export` writes the compromised-claim database, one alert record per
affected claim for the authors of the citing work and for study teams (claim, cited work, status with source and
date, evidence, confidence, suggested action) and the propagation graph.
