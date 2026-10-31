# Follow-up audit: analysis commands

<!-- SPDX-License-Identifier: Apache-2.0 -->

These are the analysis commands of a follow-up audit of claims and citations in a corpus of review articles (see
`docs/followup-audit.md` for the branch, the protocol and the evidence commands). Each one works on one follow-up
branch (`--database`), returns one JSON result and, with the global `--result-file`, writes it as
`ocsl-review-result-v1` for a Study Run step. Every size, threshold, share and seed comes from the protocol; none of
these commands has a default for them. Sizes and rates are measured from the run's own data.

Order within the pipeline:

```
status-resolve -> hotspots -> strata --freeze -> support, function, dependency, citedby (evidence commands)
               -> numeric -> second-gen -> lag -> estimands -> facts
```

`numeric` and `second-gen` are judging commands: they refuse a branch whose strata are not frozen, take the study
commands' judge options (`--model`, `--base-url`, `--api-key-env`, `--broker-socket`, `--broker-worker`,
`--concurrency`, `--on-premises`, `--policy`, `--transport-retries`, `--transport-cooldown`, `--variant`, `--scope`,
`--public-only`, `--limit`) and require `--model` to be the protocol's pinned served model. Entitled text reaches only
judges the operator's policy lists as on premises; every item carries its provenance class (public or entitled), which
is never sent.

## followup hotspots

Works the census shows to be cited without support beyond their base (the S2 selection), rule `s2-beta-binomial@1`.

- **Units:** cited works. For a work, its classifiable parent entries are the in-body entries with an identity class the
  protocol lists (`strata.S1.identities`), judged by the census model with an abstract-tier category of true or false,
  in the protocol's journals and years. `k` counts the false entries, `n` the true and false ones.
- **Base:** the false share of every base cell (`strata.S2.base`: journal, year or both) over all classifiable entries.
  A work's expected rate is the mean of its cells' base rates weighted by its entries in each cell.
- **Candidates:** works whose distinct citing reviews reach `strata.S2.minCitingReviews` and that belong to no family
  in `strata.S2.excludeFamilies` (the family's versioned title rules, `strata.S3.familyRules`, case-insensitive regular
  expressions over the work title).
- **Test:** a beta-binomial null whose intraclass correlation is the moment estimate over the candidates (clipped to the
  unit interval, excluding one); the upper-tail probability of the work's false count; Benjamini-Hochberg q-values over
  the candidates; a candidate is selected when its q-value does not exceed `strata.S2.fdr`.
- **Output:** `fu_hotspots`, one row per cited work with classifiable entries (citing reviews, `n`, `k`, expected,
  excess, the correlation, p- and q-values for candidates, the candidate, family and selection flags, the model id).
- **Refused** after the strata freeze.

## followup strata [--freeze]

The frame, the strata, their inclusion probabilities and sizes, and the freeze.

- **Inputs:** the parent census (loci, entries, reviews, works), `fu_work_status` (status-resolve must have given every
  cited work of the frame its row; otherwise the command refuses) and `fu_hotspots` (the hotspot test must have run).
- **Frame** (`fu_frame`): one row per parent locus in the protocol's journals and years, with the cited work, the
  citing sentence's claim id and digest (loci of one sentence share the claim id), the unit, section, review year,
  first-online date, topic and article-type stratum, the provenance class of the citing text, the identity class, the
  in-body flag and the parent's categories. A locus is eligible when it names a cited work, its identity class is listed
  and, with `strata.S1.inBodyOnly`, its entry is cited in the body.
- **S1:** eligible loci whose work status is one of `strata.S1.statuses`. Census (inclusion probability one).
- **S2:** eligible loci citing a selected hotspot work; excluded families stay out. Census.
- **S3:** eligible loci citing a work of the family `strata.S3.family`. Built only when
  `strata.S3.licensingVerified` is true; otherwise dropped, with its eligible count in `fu_stratum_sizes` (selected
  none) and a note in the freeze.
- **co-cited:** for every S1 and S2 locus, the other eligible loci of its citing sentence that cite another work
  (`fu_cocited` keeps every anchor and co-cited pair). Census.
- **S0:** eligible loci in none of S1, S2 or the family whose work status is `none`. `strata.S0.size` units are
  allocated over the cells of `strata.S0.matchOn` (journal, year, topic) in proportion to S1's distribution over the
  same cells (largest remainders, ties in cell order), each cell capped by its eligible loci, what the caps leave over
  given to S1's cells in cell order; a shortfall is reported. Within a cell the loci are drawn with the protocol seed.
  The inclusion probability is the cell's selected over eligible loci; the cell is recorded.
- **Pilot:** a seeded share `strata.pilot.s1Share` of S1 within each status (the ceiling of share times loci, so a
  status with loci is never left out) and `strata.pilot.s0Count` S0 loci; each pilot locus records its pilot
  probability (inclusion probability times the pilot's realised share). Judging commands select the pilot with
  `--scope pilot`.
- **Replicate:** `followup_strata.replicate_keys` draws the blind-replicate units of any judging command from the
  protocol seed and `strata.replicate.share` (the ceiling of share times units); judging commands use it with
  `--variant replicate`.
- **Seeded selections** rank keys by the sha256 of the protocol seed, a scope (stratum, cell, status, anchor) and the
  key, so they depend only on the seed, the scope and the set of keys.
- **Sizes** (`fu_stratum_sizes`): eligible, selected and pilot units per stratum and journal, and for all journals.
- **`--freeze`** rebuilds the strata from the current inputs and writes `fu_protocol_freeze`: the protocol digest, a
  digest over the ordered membership rows (strata and co-cited pairs), the status snapshot the strata used, the parent
  digest (recomputed; a changed parent refuses the freeze), the protocol's estimands with their numerators and
  denominators, and the sizes. After a freeze the strata (and the hotspot test) cannot be built again.

## followup numeric --tables <source> [--allow-synthetic-tables]

numeric-check@1 parses of the S3 claims and the deterministic comparison `numeric-comparison@1` with a statistics-table
source.

- **Licensing first.** The command refuses a protocol that drops S3, a source whose licensing is `unverified` (such
  tables are never ingested), a `synthetic` source unless `--allow-synthetic-tables` (tests only, never a study run),
  and a source whose sha256 or declared version differs from `strata.S3.tableSource`. The source is registered as a
  `table-source` dataset version.
- **Table sources** are pluggable (`followup_model.TableSource`). `followup_tables.JsonTableSource` reads format
  `ocsl-followup-tables-v1`: `name`, `licensing`, `declaredVersion`, optional versioned `synonyms` for condition,
  metric and geography, and `reports`, each with identifiers and `editions`; an edition names how it is cited
  (`citedAs`: DOI, PMID or title) and holds cells with condition, metric, geography, reference year, value, unit and
  locator. A small built-in synonym list (`table-synonyms@1`) applies under the source's own lists.
- **Items:** the locus id, the marker of the cited work, the citing sentence and the cited work's title and year. Table
  values, status, stratum and verdicts are never sent. A redacted sentence without the private text copy, or a sentence
  whose marker cannot be told, is recorded as blocked and never sent.
- **Parse rules** (validator): a parsed claim gives condition, metric, the number as written (`valueText`) and an exact
  quote that contains it; the condition (unless `all`), the geography and the year must be written in the sentence.
- **Comparison**, in order: not epidemiological gives `non-epidemiological`; an abstention is unassessable
  `abstained`; a cited work that is no report of the source is unassessable `no-matching-table`; a report without cells
  for the cited edition is unassessable `table-unavailable`; a `valueText` the rule cannot read as a number is
  unassessable `invalid-output`; a cell of the cited edition for the claimed condition, metric, geography and year whose
  value, rounded to the claim's stated precision, equals the claimed number gives `consistent`; the claimed number in a
  cell of the cited edition for another geography gives `wrong-geography`; the claimed statistic in a cell of another
  edition of the report gives `wrong-edition`; otherwise `number-absent-or-different`.
- **Numbers:** separators between groups of three digits (comma, point or space), a decimal comma, thousand, million
  and billion, percent signs and words, and ranks written as ordinals are normalised; the stated precision is the number
  of decimals as written; a percentage is compared only with percentage cells; a cell unit in thousands or millions is
  scaled.
- **Output:** `fu_numeric`, one row per S3 locus (parse fields, the table source, the class or the reason, the matched
  cells only, the rule version), rewritten from the primary judgments on every run; a replicate run leaves it as it is.

## followup second-gen [--stage sample|acquire|candidates|judge|all] [--workspace]

Whether later articles restate a review claim that rests on a compromised work, citing the review for it.

- **sample:** anchors are S1 loci whose dependency is sole or essential (the dependency command must have run). The
  cited-by edges of each anchor's review, one per citing work, are sampled with the protocol seed, at most
  `secondGeneration.edges`; the inclusion probability is sampled over available edges (`fu_secondgen_sample`).
- **acquire:** the public full text of each sampled citing work from Europe PMC (`fullTextXML` for an open-access
  PMCID, looked up by DOI or PMID when the edge has none). Outcomes in `fu_secondgen_documents`: `obtained`,
  `no-open-full-text`, `fetch-failed`, `unavailable` (not readable) and `citation-not-located`; every response or
  failure is a receipt in `fu_requests` (a failure class, never response text). Works already obtained or without open
  full text are not asked again; failed fetches are retried on the next run.
- **candidates:** the citing article is extracted; the bibliography entry whose DOI or PMID is the review's yields its
  citing sentences with the review's marker as rendered (`fu_secondgen_sentences`). One BM25 index per anchor over the
  sentences of its sampled citing works; the anchor's citing sentence without its markers is the query, searched per
  citing work; the top `secondGeneration.k` are the candidates and each search keeps its retrieval receipt
  (`fu_secondgen_candidates`). Sentences beyond `k` are unassessable `not-judged`, never assumed different.
- **judge:** claim-match@1 items carry the anchor's claim, the review's title, the candidate sentence, its marker and
  section. Rows in `fu_secondgen_matches` hold the rank and score, the match and reliance classes with the exact quote
  or the reason (`abstained`, `invalid-output`, `not-judged`, `text-redacted`), and the edge's inclusion probability.
- **External requests** go through the follow-up's scholarly client: identifiers only, the tool's User-Agent, its retry
  policy and a per-host interval; no contact identity unless the operator configured `OCSL_REVIEW_CONTACT_EMAIL`;
  `--workspace` names the workspace whose egress ledger records the requests. Entitled acquisition is not part of this
  build.

## followup lag

The retraction-lag model, rule `retraction-lag@1`.

- **Rows** (`fu_lag`): every S1 locus with the deciding notice's date and the citing review's first-online date. The
  two are compared at the coarser of their precisions (day, month, year); equal at that precision is undecidable
  (`after_notice` empty, reason `undecidable-at-precision`); a missing date gives `notice-date-unknown` or
  `first-online-unknown`. Days are counted only when both dates are given to the day.
- **Estimates** (`fu_estimates`, keys `followup.lag.*`):
  - `lag.after-notice`: the share of decidable S1 loci whose review went online after the notice, by journal and year;
  - `lag.days-q1`, `lag.days-median`, `lag.days-q3`: quartiles of the lag in days of post-notice citations;
  - `lag.rate-ratio`: citing reviews per work-year after the notice over citing reviews per work-year before it within
    the census window, overall and per status, with a work-clustered bootstrap (`estimands.ratio_bootstrap`); a notice
    dated to the month or year counts from the middle of that period;
  - `lag.trend`: the least-squares slope of the yearly after-notice share (`estimands.year_trend`).
- Intervals resample reviews within journal (works for the rate ratio) with the protocol's replicates and seed.

## followup estimands [--b <replicates>] [--bootstrap-seed <seed>]

The protocol's estimands, by journal, by year and by status where the estimand's `by` names them (the cross of the
named breakdowns, with all-journal and all-year margins).

| Estimand | Numerator | Denominator |
|---|---|---|
| `compromised-share` | frame loci whose work status is one of the S1 statuses (each status a level when `by` names status) | in-body identity-resolved frame loci with a known status |
| `sole-or-essential` | S1 loci with dependency sole or essential | S1 loci with dependency sole, essential or redundant |
| `cited-after-notice` | S1 loci whose review went online after the notice | S1 loci with a decidable comparison; the lag quartiles are in the detail |
| `acknowledges-notice` | post-notice S1 loci with function notes-retraction | post-notice S1 loci with an assessable function |
| `second-generation` | judged later sentences that restate the claim and cite the review for it | judged later sentences, weighted by the edge sample |
| `distortion-type` | adverse S2 loci per adjudicated distortion label | adjudicated adverse S2 loci |
| `numeric-classes` | S3 loci per numeric class | assessable S3 loci |

- **Weights:** Hajek ratios, the sum of weights times outcomes over the sum of weights, each weight one over the unit's
  realised inclusion probability.
- **Intervals:** percentile bootstrap resampling reviews with replacement within journal, with the protocol's
  replicates and seed (`--b` and `--bootstrap-seed` override them and are recorded); every estimand and cell has its own
  scope tuple. The after-notice share uses the lag model's scope, so equal replicates and seed give equal intervals.
- **Keys:** `followup.<estimand>[.<level>][.<status>].<journal-slug|all>.<year|all>.<stat>` with stat `share`,
  `lo`, `hi`, `n` (unweighted units in the denominator), `numerator` and `denominator` (weighted); a cell without a
  denominator reports the share as `not-measured`. `detail_json` records the definitions, weights, replicates, seed,
  scope and interval method.
- **Distortion types** come from adjudication, which this build does not hold: the estimand reports `not-measured`
  with the adverse S2 loci (full-text verdict contradicted or not addressed) awaiting labels.
- **Coverage** (`fu_coverage`): for every estimand and cell, the assessable units and every unassessable unit with its
  reason; for a status breakdown the estimand column reads `<estimand>.<status>`. Units without a verdict never enter a
  numerator or a denominator.

## Coverage reasons

| Reason | Meaning |
|---|---|
| `identity-unresolved` | the entry's cited work is not resolved (outside the compromised share's denominator) |
| `status-unknown` | no answering source covered every notice kind for the cited work |
| `text-redacted`, `no-citing-text` | the citing sentence is redacted without the private text copy, missing, or its marker cannot be told |
| `abstained`, `invalid-output` | the judge abstained, or no valid output survived the repair pass |
| `not-judged` | not judged yet, or beyond the candidate cut-off |
| `undetermined` | dependency could not be decided (a co-cited work without an assessable full-text verdict) |
| `excluded-by-protocol` | the stratum was dropped by the protocol (S3 without verified table licensing) |
| `no-matching-table`, `table-unavailable` | the cited work is no report of the table source, or its edition has no cells |
| `no-open-full-text`, `fetch-failed`, `unavailable`, `citation-not-located`, `not-acquired` | a sampled later article could not be read, or does not cite the review |
| `notice-date-unknown`, `first-online-unknown`, `undecidable-at-precision` | the lag comparison cannot be decided |
| `awaiting-labels` | an adverse S2 locus without an adjudicated distortion label |

Abstract silence and failed downloads are reasons, never "unsupported".
