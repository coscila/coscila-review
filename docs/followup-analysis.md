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

`lag` and `estimands` read the frozen strata and refuse a branch without a freeze; `hotspots` and `strata` refuse one
with a freeze. `numeric` and `second-gen` are judging commands: they refuse a branch whose strata are not frozen, take
the study commands' judge options (`--model`, `--base-url`, `--api-key-env`, `--broker-socket`, `--broker-worker`,
`--concurrency`, `--on-premises`, `--policy`, `--transport-retries`, `--transport-cooldown`, `--variant`, `--scope`,
`--public-only`, `--limit`) and require `--model` to be the protocol's pinned served model. Entitled text reaches only
judges the operator's policy lists as on premises; every item carries its provenance class (public or entitled), which
is never sent. As in the evidence commands, the judge is prepared before any call or download (a broker judge checks
that the instrument's verbatim bundle is installed and granted), closed when the command ends, and named in the
result (`judge`); the result carries the census step contract at the top level (`state`, `reason`, `resumeAfter`,
`transport`, `accepted`, `invalid`, `calls`, `reviews`; see `docs/followup-evidence.md`), with the runner's summary in
`run`. A halted broker transport stops all further sending.

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
  protocol seed and `strata.replicate.share` within replicate groups (`replicate_group`: the unit's first stratum in the
  order S1, S2, S3, S0, co-cited, and within S1 its work's status), the ceiling of share times the group's units, so
  every stratum and status present gets replicate units; judging commands use it with `--variant replicate`.
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
  locator. A small, domain-neutral built-in synonym list (`table-synonyms@1`: the condition `all` as all diseases or
  all conditions, and common spellings of a few geographies) applies under the source's own lists; labels of a
  particular field belong in the source's block.
- **Items:** the locus id, the marker of the cited work, the citing sentence and the cited work's title and year. Table
  values, status, stratum and verdicts are never sent. A redacted sentence without the private text copy, or a sentence
  whose marker cannot be told, is recorded as blocked and never sent.
- **Parse rules** (validator): a parsed claim gives condition, metric, the number as written (`valueText`, which must
  state a number: a digit or an ordinal word) and an exact quote that contains it; the condition (unless `all`), the
  geography and the year must be written in the sentence. A parse that breaks a rule is sent again in the repair pass.
- **Comparison**, in order: not epidemiological gives `non-epidemiological`; an abstention is unassessable
  `abstained`; a cited work that is no report of the source is unassessable `no-matching-table`; a report without cells
  for the cited edition is unassessable `table-unavailable`; a `valueText` without a number is unassessable
  `invalid-output`; numeric content the rule does not read (a range, a ratio such as "1 in 8" or "1/3", a second
  number) is unassessable `unsupported-value-form`, never its first number; a cell of the cited edition for the claimed
  condition, metric, geography and year whose value, rounded to the claim's stated precision, equals the claimed number
  gives `consistent`; the claimed number in a cell of the cited edition for another geography gives `wrong-geography`;
  the claimed statistic in a cell of another edition of the report gives `wrong-edition`; a cell for the claimed
  statistic in the cited edition whose value differs gives `number-absent-or-different`; when no cell of the source
  holds the claimed condition, metric, geography and year at all (an unmapped label, a metric the tables do not carry
  such as `other` or `trend`, a year without a row), the claim is unassessable `no-matching-cell`: a failure to compare
  is never an adverse class. The matched cells (or, for `no-matching-cell`, the labels looked for) are recorded.
- **Numbers:** separators between groups of three digits (comma, point or space), a decimal comma, thousand, million
  and billion, percent signs and words, a rate base ("per" and a number), and ranks written as ordinals are read; the
  parsed unit supplies percent, scale and rate base when `valueText` states none. The stated precision is the place of
  the last significant digit as written: every written decimal counts, and the trailing zeros of a whole number without
  a decimal mark do not (a whole number with three trailing zeros is compared at thousands, "40" at tens, "40.0" at
  tenths). A percentage is compared only with percentage cells, a rate only with cells of the same base; a cell unit in
  thousands or millions is scaled.
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
  full text are not asked again; failed fetches are retried on the next run. A throttling Europe PMC stops the stage
  with a paused result naming the articles left.
- **candidates:** the citing article is extracted; the bibliography entry whose DOI or PMID is the review's yields its
  citing sentences with the review's marker as rendered (`fu_secondgen_sentences`). One BM25 index per anchor over the
  sentences of its sampled citing works; the anchor's citing sentence without its markers is the query, searched per
  citing work; the top `secondGeneration.k` are the candidates and each search keeps its retrieval receipt
  (`fu_secondgen_candidates`). Sentences beyond `k` are no candidates (`not-judged`), never assumed different; the
  estimand counts articles, not sentences (below). The candidate and judging stages read the anchor's citing sentence
  and need the private text copy mounted when the branch was made with one; the sample and acquire stages do not.
- **judge:** claim-match@1 items carry the anchor's claim, the review's title, the candidate sentence, its marker and
  section. Rows in `fu_secondgen_matches` hold the rank and score, the match and reliance classes with the exact quote
  or the reason (`abstained`, `invalid-output`, `not-judged`, `text-redacted`), and the edge's inclusion probability.
- **External requests** go through the follow-up's scholarly client: identifiers only, the tool's User-Agent, its retry
  policy and a per-host interval; no contact identity unless the operator configured `OCSL_REVIEW_CONTACT_EMAIL`;
  `--workspace` names the workspace whose egress ledger records the requests. Entitled acquisition is not part of this
  build.

## followup lag

The retraction-lag model, rule `retraction-lag@1`.

- **Rows** (`fu_lag`): every S1 locus with the deciding notice's date and the citing review's first-online date. A date
  given to the day, month or year names that period. The review is after the notice when its period starts at or after
  the end of the notice's period plus the protocol's grace days (`timing.graceDays`, none by default: an article online
  shortly after a notice was written before it), not after it when its period ends at or before the start of the
  notice's period plus the grace days, and otherwise undecidable (`after_notice` empty, reason
  `undecidable-at-precision`); without grace days this is the comparison at the coarser of the two precisions. A
  missing date gives `notice-date-unknown` or `first-online-unknown`. Days are counted only when both dates are given to
  the day.
- **Estimates** (`fu_estimates`, keys `followup.lag.*`):
  - `lag.after-notice`: the share of decidable S1 loci whose review went online after the notice, by journal and year;
  - `lag.days-q1`, `lag.days-median`, `lag.days-q3`: quartiles of the lag in days of post-notice citations;
  - `lag.rate-ratio` (descriptive): citing reviews per census review published after the notice over citing reviews
    per census review published before it, overall and per status and journal. The exposure is the census volume: per
    work, the census reviews in the protocol's journals and years (of the cell's journal) published decidably before
    the notice and not before the work's publication year, and those published decidably after the notice plus the
    grace days; the notice's own period is in neither exposure, for reviews and citations alike. The interval resamples
    works jointly with their four sums (`followup_stats.paired_ratio_bootstrap`), so a work's citations before and
    after its notice stay paired. Citation ageing is not modelled; the detail records the four totals;
  - `lag.trend`: the least-squares slope of the yearly after-notice share (`estimands.year_trend`).
- Intervals resample reviews within journal (works for the rate ratio) with the protocol's replicates and seed.

## followup estimands [--scope pilot|all] [--b <replicates>] [--bootstrap-seed <seed>]

The protocol's estimands, by journal, by year and by status where the estimand's `by` names them (the cross of the
named breakdowns, with all-journal and all-year margins). Journal and status cells form the protocol's full grid; a year
cell without any unit is not written.

| Estimand | Numerator | Denominator |
|---|---|---|
| `compromised-share` | in-body identity-resolved loci whose work status is one of the S1 statuses (each status a level when `by` names status) | every in-body identity-resolved locus, a work of unknown status included (the design's estimand); sensitivity `available-case`: loci with a known status only |
| `sole-or-essential` | S1 loci with dependency sole or essential; level `anchor-supported`: of those, the loci whose own cited work supports the claim at full text | S1 loci whose own full-text verdict is assessable and whose dependency is sole, essential or redundant |
| `cited-after-notice` | S1 loci whose review went online after the notice (plus the grace days) | S1 loci with a decidable comparison; the lag quartiles are in the detail; sensitivity `grace-<N>d` with `timing.sensitivityGraceDays` |
| `acknowledges-notice` | post-notice S1 loci with function notes-retraction | post-notice S1 loci with an assessable function; sensitivity as above |
| `second-generation` | sampled later articles (cited-by edges) with a judged candidate that restates the claim and cites the review for it | sampled later articles whose candidates were all judged or one of them confirmed, weighted by the edge sample |
| `distortion-type` | adverse S2 loci per adjudicated distortion label | adjudicated adverse S2 loci |
| `numeric-classes` | S3 loci per numeric class | assessable S3 loci |

- **Weights:** Hajek ratios, the sum of weights times outcomes over the sum of weights, each weight one over the unit's
  realised inclusion probability.
- **Intervals:** percentile bootstrap resampling reviews with replacement within journal, with the protocol's
  replicates and seed; every estimand, variant and cell has its own scope tuple. The after-notice share uses the lag
  model's scope, so equal replicates and seed give equal intervals. `--b` and `--bootstrap-seed` make exploratory
  estimates (recorded, reported as `bootstrapOverride`): `followup facts` refuses estimates with mixed settings and
  `followup gates` checks `bootstrapAsProtocol`. A Study Run never offers them.
- **Contrasts:** an estimand that declares `contrasts: ["journal"]` gets every later journal of the protocol against
  its first, as `journal-ratio` and `journal-difference` of the two shares with intervals from the same bootstrap
  (reviews resampled within each journal, the journals independent).
- **Pilot:** `--scope pilot` computes sole-or-essential, cited-after-notice, acknowledges-notice and second-generation
  over the pilot subsample only, each unit weighted by one over its pilot probability (the realised probability of the
  seeded pilot draw per status), under `followup.pilot.<estimand>...`; the census estimands have no pilot form
  (`noPilotForm`). The full-scope estimates are left as they are.
- **Replicate agreement:** for every instrument digest and tier with replicate verdicts, `replicate-agreement` rows
  (`followup.replicate-agreement.<instrument>.<tier>[.<group>].<agreement|kappa>.all.all.<value|lo|hi|n>`): the pairs of
  replicate and primary verdicts of the same item, the raw agreement and Cohen's kappa over the instrument's classes
  (citation-support@2's class map, which citation-support-verbatim@1 shares, the function class, the numeric
  decision, the match class), overall and per replicate group, with review-clustered percentile intervals; replicate
  verdicts without a primary verdict are counted as unpaired in the coverage. `followup gates` reports
  `replicateAgreementReported`. Agreement of two runs of one model measures stability, not accuracy.
- **Keys:** `followup[.pilot].<estimand>[.<variant>][.<level>][.<status>].<journal-slug|all>.<year|all>.<stat>` with
  stat `share`, `lo`, `hi`, `n` (unweighted units in the denominator), `numerator` and `denominator` (weighted); a cell
  without a denominator reports the share as `not-measured`. `detail_json` records the definitions, scope, variant,
  weights, replicates, seed, bootstrap scope and interval method.
- **Distortion types** come from adjudication, which this build does not hold: the estimand reports `not-measured`
  with the adverse S2 loci (full-text verdict contradicted or not addressed) awaiting labels.
- **Coverage** (`fu_coverage`): for every estimand, variant and cell, the assessable units and every unassessable unit
  with its reason; for a status breakdown the estimand column reads `<estimand>[.<variant>].<status>`. Units without a
  verdict never enter a numerator or a denominator. The compromised share's denominator units of unknown status appear
  as assessable with the reason `status-unknown`.

## Coverage reasons

| Reason | Meaning |
|---|---|
| `identity-unresolved` | the entry's cited work is not resolved (outside the compromised share's denominator) |
| `status-unknown` | no answering source covered every notice kind for the cited work (in the compromised share's denominator as an assessable note; in its available-case sensitivity, coverage) |
| `text-redacted`, `no-citing-text` | the citing sentence is redacted without the private text copy, missing, or its marker cannot be told |
| `abstained`, `invalid-output` | the judge abstained, or no valid output survived the repair pass |
| `not-judged` | not judged yet (also an anchor without a full-text verdict of its own for sole-or-essential) |
| `undetermined` | dependency could not be decided (a co-cited work without an assessable full-text verdict) |
| `excluded-by-protocol` | the stratum was dropped by the protocol (S3 without verified table licensing) |
| `no-matching-table`, `table-unavailable` | the cited work is no report of the table source, or its edition has no cells |
| `no-matching-cell`, `unsupported-value-form` | no cell holds the claimed statistic, or the stated number has a form the rule does not read |
| `unpaired` | a replicate verdict without a primary verdict of the same item (replicate agreement) |
| `no-open-full-text`, `fetch-failed`, `unavailable`, `citation-not-located`, `not-acquired` | a sampled later article could not be read, or does not cite the review |
| `notice-date-unknown`, `first-online-unknown`, `undecidable-at-precision` | the lag comparison cannot be decided |
| `awaiting-labels` | an adverse S2 locus without an adjudicated distortion label |

Abstract silence and failed downloads are reasons, never "unsupported".
