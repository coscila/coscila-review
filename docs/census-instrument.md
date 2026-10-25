<!-- SPDX-License-Identifier: Apache-2.0 -->
# Census instrument, scores database, estimands and facts

This page describes the registered measurement instruments and the study
commands that run an entry-level citation census with them. The instrument is
fixed and versioned; studies choose populations, protocol seeds and estimands
around it. Model verdicts are measurements on a predeclared instrument, not
human-labelled truth.

## Instrument registry

Each instrument is a bundle in `instruments/<name>/<version>/instrument.json`
(schema: `schemas/instrument.schema.json`). A bundle declares its prompt and
schema files with sha256 digests, its evidence policy, geometry, request
settings, required model, validator, class map, deterministic rows and
predeclared sensitivities. The bundle digest is the sha256 of the canonical
manifest, every file digest and the digest of every builder and validator
module. A changed prompt, schema, geometry, setting or validator is therefore a
different instrument. Transport, retries and storage are outside the digest.

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" instruments list
"$OCSL_REVIEW_HOME/bin/ocsl-review" instruments show citation-census@1
"$OCSL_REVIEW_HOME/bin/ocsl-review" instruments verify
```

| Instrument | Status | Role |
|---|---|---|
| `citation-census@1` | frozen | entry-level census: abstract tier and full-text tier |
| `citation-support@2` | active | per-locus support with exact quotes; claim checking; registered unchanged, so analyses pinned to its prompt and schema still verify |
| `claim-check@1` | active | one verdict per citation against the whole sentence that carries it, with retrieved evidence (`ocsl-review screen`, [claim screen](claim-check.md)); citation-support@2 prompt, schema and validator |

A compact per-locus `citation-support@3`, alternative batch geometries and an
equivalence gate between instruments are deferred.

`instruments.study_bundle_digest` pins a study by its protocol, study prompt,
template, instrument digests, dataset versions and package tags; the code tag
of every call is recorded in its receipt. `ocsl-review corpus init` and
`corpus branch` pin new corpus analyses this way (see
[corpus review](corpus-review.md)).

## citation-census@1

**Unit.** One verdict per printed bibliography entry.

**Abstract tier.** For each in-body entry with a resolved identity, the judge
sees at most three citing sentences (document order, one per marker
occurrence, with their section trail), the cited work's title, venue, year and
retraction flag, and its abstract cut to 2,200 characters after whitespace
normalisation. The cut is recorded per entry (`abs_cut`,
`abs_sent_characters`). Identifiers, the printed entry and the abstract
provider are recorded but never sent.

- Geometry: up to 40 entries per call, all from one review, in bibliography
  order; the batch position (1-40) is stored.
- Request: chat completions, temperature 0, reasoning effort `none`,
  `max_tokens = max(3000, 700 * entries)`.
- One repair pass re-asks only for labels without a parseable line, including
  the items of a call that failed after its transport attempts. An entry
  without a parseable line after repair is an invalid output (class unsure).
- The system prompt is fixed; only the journal of the reviews under study is
  substituted. The instrument's label format is one JSON line per item with
  category T/F/N/U, support type, quote for F, escalation flag and rationale.

**Full-text tier.** Entries the judge escalated are re-read against a
claim-targeted window of at most 11,000 characters of the cited work's full
text, in a dedicated FULL TEXT field: the opening paragraphs and the abstract,
then paragraphs scored by the claim's keywords. Sources are Europe PMC
fullTextXML by PMCID and entitled full text; entitled text is sent only when
the transport is declared on premises. Calls hold five entries from one review;
the order is a seeded shuffle with the protocol seed, grouped by review in
first-seen order; reasoning effort is `low`; there is no repair pass. A failed
retrieval is recorded (`cache`, `europe-pmc-fulltextxml`, `entitled`,
`fetch-failed`, `no-pmcid`, `entitled-blocked`) and never becomes a support
verdict. Every full-text verdict is bound to the cited work's identity version
and the sha256 of the window that was sent.

**Deterministic rows.** An entry never cited in the body is `not_cited`: it is
reported separately and is never a support verdict. An in-body entry without a
resolved identity is `unsure`.

**Classes.** Abstract tier: `sure_supported` = T without escalation,
`sure_not_supported` = F without escalation, `unsure` = N, U, escalated,
unresolved or invalid. Full-text tier: T, F, otherwise unsure. Confidence
derived from category and escalation is stored and never reported as
calibrated.

**Served model.** The instrument names the model it requires. A response from
any other model pauses the run with `model_drift`; resuming continues only
when the required model serves again. Each receipt records the served model and
the server fingerprint when the server offers one.

**No-truncation sensitivity.** Predeclared: whole review batches that contain
at least one cut abstract are drawn with the protocol seed until the sample
covers the declared number of entries (`sampleEntries`), then re-run unchanged
except that abstracts are not cut. Verdicts go to the history as variant
`no-truncation`; uncut entries in the same batches measure replay agreement.

## Inputs and extraction

`study prepare` reads JSON lines, one review per line:

```json
{"journal": "...", "doi": "...", "title": "...", "year": 2021, "firstOnline": "2021-03-01",
 "stratum": "Review", "provenanceClass": "public",
 "document": {"path": "reviews/x.xml", "kind": "jats", "sha256": "..."},
 "works": {"work-key": {"doi": "...", "pmid": "...", "pmcid": "...", "title": "...", "venue": "...",
                        "year": 2019, "retracted": false, "abstract": "...", "abstractSource": "europe-pmc-core"}},
 "identities": {"12": {"workKey": "work-key", "identity": "resolved", "method": "supplied"}}}
```

`study abstracts --input reviews.jsonl --output reviews.abstracts.jsonl` fills
missing abstracts from one provider, Europe PMC `resultType=core`, 40 DOIs per
query with a PMID fallback, and records the provider outcome per work
(`europe-pmc-core`, `europe-pmc-core-pmid`, `europe-pmc-no-abstract`,
`europe-pmc-no-hit`, `europe-pmc-failed`, `no-identifier`); requests carry
identifiers only.

Document paths are relative to the manifest. A review without a document is
recorded with provenance class `unavailable`. Extraction is
`structured.extract` (recursive serialisation, range expansion, tables,
legends, boxes and lists, each anchor owned by exactly one block). Each citing
unit is rendered with one marker per target, written `⟨target⟩`, typographic
quotes and dashes normalised, and split into sentences; only loci under the
article body count as in-body citations. Without supplied identities, the
identity ladder uses markup DOI or PMID, then a printed DOI, then title
containment, then a label where numbering is verified: `resolved` (identifier
and title agree), `resolved_unverified` or `unresolved`.

Gates: `uncovered` must be 0 for every review (every anchor owned once, every
target resolved), and `study cross-validate --jats X --html Y` compares the
JATS and publisher HTML readings of one review (share of JATS loci whose
sentence matches an HTML locus of the same label; the gate is a median share
across reviews).

## Running a study

```bash
ocsl-review study prepare --database work/census.sqlite3 --seed "$SEED" --input inputs/reviews.jsonl
ocsl-review study judge --database work/census.sqlite3 --seed "$SEED" --base-url "$JUDGE_URL" \
  --api-key-env JUDGE_KEY_VARIABLE --concurrency 60
ocsl-review study full-text --database work/census.sqlite3 --seed "$SEED" --base-url "$JUDGE_URL" --europe-pmc
ocsl-review study no-truncation --database work/census.sqlite3 --seed "$SEED" --base-url "$JUDGE_URL"
ocsl-review study replicate --database work/census.sqlite3 --seed "$SEED" --base-url "$JUDGE_URL" --entries "$N"
ocsl-review study gates --database work/census.sqlite3
ocsl-review study freeze --database work/census.sqlite3 --output results/scores.sqlite3
ocsl-review study export --scores results/scores.sqlite3 --output results/scores-export
ocsl-review study facts --scores results/scores.sqlite3 --output results/facts \
  --journal "$JOURNAL_A" --journal "$JOURNAL_B" --ratio "$JOURNAL_A" "$JOURNAL_B" --bootstrap-seed "$SEED"
ocsl-review study verify-facts --facts results/facts/facts.json
```

Runs are resumable per review; receipts are written per call. `replicate`
re-runs whole primary batches, unchanged, in a sample stratified by journal and
first-online year and allocated in proportion to judged entries; its verdicts go
to the history as variant `replicate`, and the estimands report category and
class agreement and the T/F kappa against the primary verdicts. `gates` checks
invalid outputs (at most 1 %), transport failures (at most 0.5 % of calls),
uncovered anchors (0), duplicate judgments (0) and fingerprints on every
receipt. The transport sends the request body bytes exactly as built and
records their sha256 in the receipt; the key comes from the named environment
variable, never from a file.

## Scores database

One SQLite file per study: `reviews` (counts per tier and class), `citations`
(one row per printed entry, with identity, in-body flag, loci count, abstract
and full-text verdicts, classes, call ids and positions), `loci`, `works`,
`identity`, `evidence`, `calls` (one receipt per call: instrument digest,
served model, fingerprint, settings digest, system, user, message-list and body
digests, tokens, finish reason, attempts), `history` (append-only),
`review_scores` and `work_scores` (beta-binomial excess and Benjamini-Hochberg
q-values) and `data_dictionary`. `study freeze` writes the closed, read-only
copy (rollback journal, evidence text removed unless `--keep-text`).
`study export` writes TSV for every table and Parquet when `pyarrow` is
installed; otherwise the manifest says that Parquet is unavailable.

## Estimands

`estimands.py` reads a frozen database. Denominator 2 is in-body entries with a
resolved identity (resolved or resolved_unverified) that the model judged; the
headline is F / (T + F) at the abstract tier, escalated F included.

- Intervals: Wilson with the effective sample size n / DEFF, where
  DEFF = 1 + (m - 1) ICC, m is the mean number of classifiable entries per
  review and the ICC is the one-way ANOVA estimate measured per journal; plus a
  review-clustered percentile bootstrap (default B = 10,000, fixed seed).
- Three classes per tier, never cited separately, and a secondary composite
  (full-text class, else the non-escalated abstract class, else unsure) with
  availability strata.
- Panels by year, stratum, topic (versioned title keyword map) and
  corresponding-author country (descriptive), each with both intervals.
- Ratio of two journals' headline shares with a bootstrap interval; the year
  trend is the OLS slope of the yearly share with a bootstrap stratified by year.
- Predeclared sensitivity ladder (escalated F without full text, F without a
  quote or with a quote absent from the abstract, resolved identity only,
  position bands, cut and uncut abstracts, abstract provider, stratum,
  extraction route, no truncation), coverage tables by year, stratum, route and
  provenance class, and a decomposition of any cell by provenance, route,
  abstract provider, identity class, position band, served model and cut.

## Facts

`study facts` writes a closed `facts.sqlite3` (the frozen tables plus an
`estimates` table) and `facts.json` with `schemaVersion: 1`. Every fact is one
scalar SQL query over the closed file, recomputed at export, with the file's
sha256, the input hashes (scores database, instrument digest) and the code
revision; interval bounds are separate facts, and a `derivation` block records
estimand, seed and B. The export passes `ocsl-writing facts verify` unchanged;
`ocsl-writing facts lint` then requires every numeral in a manuscript template
to come from a fact token.

## Fidelity tests

`tests/test_citation_census_fidelity.py` pins the instrument to recorded sha256
values: system prompts, first and repair calls, the label parser, JATS
conversion, full-text windows, full-text call geometry and prompts built from
fixture documents through the maintained extraction. It reads inputs only.
Setting `OCSL_REVIEW_CENSUS_FIDELITY_RECORDS` to a JSON-lines file of recorded
calls (`title`, `journal`, `items`, `note`, `userSha256`, `systemSha256`) adds
a check against a private record set.

## Limits

- The direct chat-completions transport is for on-premises judges and probes;
  broker bulk jobs need a verbatim instrument mode before they can carry this
  instrument unchanged.
- Model identity is the served model string and the server fingerprint when
  offered; there is no weights hash.
- Country extraction is new and descriptive only; the topic map is a versioned
  keyword classifier.
