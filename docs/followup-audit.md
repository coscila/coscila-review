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
text supplies them, and a sentence is accepted only when its digest matches the parent's. The private copy is needed
only by the commands that read citing sentences (support, function, numeric, the candidate and judging stages of
second-gen, a private export): those refuse while a branch made with it does not see it mounted, every other command
runs without it, and `verify` notes it as not mounted instead of failing.

`followup init --study-bundle <bundle file or sha256>` (or `followup pin-bundle` before the first judge call) pins the
study bundle (`ocsl-study-bundle-v1`): a bundle file must name the branch's protocol and pin every instrument the
protocol names at its registry digest. Every receipt records the pinned bundle, a broker run needs one, and before any
call each instrument's registry digest is checked against the bundle and against the protocol's
`instruments.digests`, so a changed builder, validator or bundle never runs silently under a new digest.
`--allow-incomplete-parent` (tests and pilots only, never a study run) is recorded, and `followup gates` reports
`parentComplete`.

`followup verify` recomputes the parent's sha256 (it must equal the value recorded at branch time), checks the schema
and checks that every follow-up row refers to a row the parent or the frame holds. `followup describe` lists the
dataset versions (parent, protocol, status snapshots, the Retraction Watch snapshot, the cited-by graph, the table
source), the protocol freezes and row counts.

## Protocol and freeze

The protocol (`schemas/followup-protocol.schema.json`) holds parameters only: the status sources and the notice kinds
each covers, the strata definitions and their parameters, the instruments (with the request settings of
citation-support@2 when the protocol pins it for support), the pinned on-premises judge, the grace days after a notice,
the estimands with their numerators, denominators, breakdowns and contrasts, the rules and the gates. Sizes and rates
are measured from the run's own data. `followup strata
--freeze` writes the strata, their sizes, the estimands and the denominators into the protocol digest; judging
commands refuse a branch without a freeze, and `followup gates` checks that no judge call predates it. What decides
the strata (`followup status-resolve`, `followup hotspots`, `followup strata`) refuses to run again after the freeze;
dependency, cited-by traversal, the lag model and the estimands need it.

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
source that answers without a notice records `none` for the notice kinds it can report; a source that cannot answer
records `unknown`. `followup status-resolve` gives every cited work one status row under the rule
`status-resolution@1`: retracted when a retraction notice stands (a later reinstatement is recorded and reverses it),
otherwise expression of concern, otherwise corrected, otherwise `none` when every notice kind was covered by a source
that answered, otherwise `unknown`. Which kinds an answer covers comes from the protocol (`status.covers`) or the
defaults: the Retraction Watch snapshot records corrections only selectively, so its silence covers retractions,
expressions of concern and reinstatements, and a correction counts as covered through Crossref or PubMed. The covered
kinds and their basis are recorded per source. A status snapshot is registered as a dataset version once its fetch
finished; a fetch stopped by a throttling provider or a failure stays open and `status-fetch --continue` resumes it.

## Instruments

| Instrument | Unit | What the judge sees |
|---|---|---|
| citation-support-verbatim@1, or citation-support@2 (unchanged); the protocol pins one | locus x cited work, abstract and full-text tiers | the claim, its context and the cited work's evidence units |
| citation-function@1 | locus | the citing sentence with the work's marker, its neighbours, the section, the work's title (without status markers) and year; never its status, the stratum or a verdict |
| numeric-check@1 | S3 locus | the citing sentence and the work's title and year; the comparison with the tables is deterministic |
| claim-match@1 | original claim x later sentence | the claim, the review's title, the later sentence with the review's marker |

The follow-up instruments run at temperature zero with reasoning off, send their messages verbatim, validate every
quote against the field it names, and repair invalid outputs once. `ocsl-review instruments export` prints their broker
bundles. Support has two registered forms with the same prompt, output schema, validator and class map:
citation-support-verbatim@1 declares its request settings per tier (temperature zero, no seed, reasoning off at the
abstract tier and low at the full-text tier, its output-token rule), one item per call, the served model it requires
(another served model pauses the run) and the verbatim message transport, and exports one broker bundle per tier;
citation-support@2 declares no request settings and stays unchanged, so a protocol that pins it predeclares its
reasoning effort per tier (`instruments.supportSettings`), which a protocol pinning citation-support-verbatim@1 leaves
out. Every support request carries its settings and the result reports them. Entitled text reaches only judges the
operator's policy lists as on premises.

## Runs

Every judging command (`support`, `function`, `numeric`, `second-gen`) takes `--limit` for a smoke test, `--scope
pilot` for the frozen pilot subsample and `--variant replicate` for the blind replicate. The replicate units of every
judging command follow one rule (`followup_strata.replicate_keys`): within each replicate group of the units in scope
(the unit's first stratum in the order S1, S2, S3, S0, co-cited, and within S1 its work's status), the ceiling of the
protocol's replicate share times the group's number, ranked by the sha256 of the protocol seed, the instrument id, the
group and the unit key, so every stratum and status present gets replicate units. Replicate verdicts stay in the
judgment ledger and never replace a primary row; `followup estimands` reports their agreement with the primary
verdicts per instrument, tier and group (pairs, raw agreement, Cohen's kappa over the instrument's classes, with
review-clustered bootstrap intervals), and `followup gates` checks it (`replicateAgreementReported`). Agreement of two
runs of one model measures stability, not accuracy.

A judging command resumes where it stopped: items already accepted under the same instrument digest, tier and variant
are skipped, and items of calls without a response are sent again in further passes. Every judging result carries the
census step contract at the top level: `state` (completed, or paused when the run stopped or items are still open), a
`reason` with the census prefixes (`model_drift:`, `fidelity:`, `transport: broker halted:`, `transport: N items still
open`), `resumeAfter` and the broker `transport` description when there are any, and the totals `accepted`,
`invalid`, `calls` and `reviews` (the units decided, which are loci or candidate sentences here); `run` holds the
runner's summary and `judge` the judge. Once a broker transport halts (a fidelity stop, a budget, provider-limit,
revocation or cancellation halt) nothing more is sent: no further batch, repair or pass.

Every judging command can run through the run principal's broker socket, one verbatim bundle job per call, with an
instrument that exports broker bundles: citation-function@1, numeric-check@1, claim-match@1 and
citation-support-verbatim@1 (one bundle per tier). citation-support@2 has no verbatim bundle and runs only on a judge
called directly. With the census's review documents (`--documents`), the support and
function judges also see the sentences around each citing sentence; without them the neighbouring sentences are empty.
`followup dependency --scope pilot` and `followup estimands --scope pilot` give the pilot's results: the judged
estimands over the pilot subsample, weighted by the pilot probabilities, under `followup.pilot.*` keys.

## Rules

- Abstract silence and failed or missing downloads never count as unsupported.
- An unassessable unit keeps its reason (`fu_support.reason`, `fu_function.reason`, ...) and goes into the coverage
  tables (`fu_coverage`), never into a numerator.
- Dependency (`dependency-aggregation@1`): sole when the citing sentence cites no other work; redundant when a
  co-cited work without a notice of its own supports the claim at full text; essential when every co-cited work has a
  decided full-text verdict and none without a notice supports the claim; undetermined otherwise. A verdict of
  citation-support@2's uncertain class (partial-indirect) is unsettled support, not a decided verdict; support from a
  co-cited work that carries a retraction, expression of concern or correction itself never makes a claim redundant.
  The row keeps the class each other reading would give, for a predeclared sensitivity.
- Numeric comparison (`numeric-comparison@1`): a number is read at its significant figures (the trailing zeros of a
  whole number are not significant), ranges and ratios are never read as their first number, and a claim for which no
  cell of the table source holds the claimed statistic is unassessable (`no-matching-cell`), never "number absent or
  different".
- Estimates are weighted by realised inclusion probabilities, with review-clustered bootstrap intervals (the protocol's
  replicates and seed; `--b` and `--bootstrap-seed` make exploratory estimates, which the facts refuse to mix and the
  gates report).
- The compromised share is reported over every in-body identity-resolved locus, a work of unknown status included
  (the design's estimand), with the share over loci of known status as the labelled `available-case` sensitivity;
  sole-or-essential needs an assessable full-text verdict of the anchor's own work; second-generation propagation is
  counted per sampled later article; the lag model's rate ratio is descriptive (census reviews per period as exposure).
- Integrity classes (never cited, unresolved identity) stay apart from support verdicts.

## External requests

Provider requests go through the review companion's scholarly client: the tool's User-Agent, bounded responses, the
retry policy for throttling, NCBI's request ceiling, and a per-host interval for bulk traversal. A host that throttles
(HTTP 429 or 503) pushes its next request slot by the server's Retry-After, or by a capped exponential backoff, and the
bulk loops stop asking it after a run of throttles, with a paused result; running the command again continues.
Requests carry identifiers only. The only contact identity is the operator's `OCSL_REVIEW_CONTACT_EMAIL`; none is ever inferred, and
recorded receipts drop contact and key parameters.

## Outputs

`followup facts` writes a closed `facts.sqlite3` of aggregates only (per-family summaries of the branch, the estimates,
coverage, census denominators and dataset versions) and a schemaVersion 1 `facts.json` (one scalar SQL query per fact,
the inputs recorded once in the provenance); `followup verify-facts` checks it, and the writing companion's verifier
accepts it. `followup export` writes the compromised-claim database (with an `alerts` table), one alert record per
affected claim for the authors of the citing work and for study teams (claim, cited work, status with source and
date, evidence, confidence, suggested action) and the propagation graph; it writes nothing into the branch, so the
branch digest a facts export recorded still holds.
