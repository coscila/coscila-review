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
the transport is on premises: `--on-premises` with `--policy`, an operator egress
policy whose `onPremEndpoints` lists the judge endpoint ([claim screen](claim-check.md)). Calls hold five entries from one review;
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
`no-truncation`. The sample is enriched for cut abstracts, so its share is
compared only with the primary verdicts on the same entries (paired, for all,
cut and uncut entries); uncut entries are no replay either, because the
neighbouring abstracts in their call are no longer cut.

## Inputs and extraction

`study prepare` reads JSON lines, one review per line:

```json
{"journal": "...", "doi": "...", "title": "...", "year": 2021, "firstOnline": "2021-03-01",
 "stratum": "Review", "provenanceClass": "public",
 "document": {"path": "reviews/x.xml", "kind": "jats", "sha256": "...",
              "tables": [{"number": 1, "path": "reviews/x-table-1.html", "sha256": "..."}]},
 "works": {"work-key": {"doi": "...", "pmid": "...", "pmcid": "...", "title": "...", "venue": "...",
                        "year": 2019, "retracted": false, "abstract": "...", "abstractSource": "europe-pmc-core"}},
 "identities": {"12": {"workKey": "work-key", "identity": "resolved", "method": "supplied"}}}
```

Records written by `study acquire` also carry an `acquisition` block (where the
document came from, see below) and records of a fresh enumeration an
`enumeration` block (the group and the digest of the provider page that listed
the review); a record may pin its document by `documentSha256`.

`study abstracts --input reviews.jsonl --output reviews.abstracts.jsonl` gives
every cited work the abstract of one provider, Europe PMC `resultType=core`, 40
DOIs per query with a PMID fallback, and records the outcome per work
(`europePmcOutcome`: `europe-pmc-core`, `europe-pmc-core-pmid`,
`europe-pmc-no-abstract`, `europe-pmc-no-hit`, `europe-pmc-failed`,
`no-identifier`); requests carry identifiers only. A Europe PMC abstract
replaces whatever a work carried; where Europe PMC definitively has none, an
abstract supplied from another provider is kept as a recorded fallback
(`abstractSource` `fallback:<provider>`, outside the Europe-PMC-only ladder).
A failed lookup changes nothing but the outcome; running the command again on
its output looks up exactly those works, and `study prepare` skips a review
with a failed lookup until then. Records stream one line at a time into
`<output>.partial`, which an interrupted run resumes; the output appears when
every record is written.

Document paths are relative to the manifest. A review without a document, or
whose document cannot be read (missing file, digest mismatch, an inconsistent
record), is recorded with provenance class `unavailable` and the reason; a
document that cannot be extracted (for example colliding bibliography labels)
is recorded with the extraction error. Both stay in the coverage tables and
facts, and preparation continues with the next review. Some publishers render a table's
body on a page of its own and leave only the caption and a link on the article
page: `document.tables` supplies those pages for an HTML review. Their cells,
captions and table footnotes become citing units in section `Table N` with the
table flag, after the body; a caption already on the article page counts once.
The extraction diagnostics list the linked tables, the pages supplied and the
linked tables still missing (`tablePagesMissing`). Extraction is
`structured.extract` (recursive serialisation, range expansion, tables,
legends, boxes and lists, each anchor owned by exactly one block). Each citing
unit is rendered with one marker per target, written `⟨target⟩`, typographic
quotes and dashes normalised, and split into sentences; only loci under the
article body count as in-body citations. Without supplied identities, the
identity ladder uses markup DOI or PMID, then a printed DOI, then title
containment, then a label where numbering is verified: `resolved` (identifier
and title agree), `resolved_unverified` (identifier only, title containment
only, or a verified label) or `unresolved`.

Gates: `uncovered` must be 0 for every review (every anchor owned once, every
target resolved), and `study cross-validate --jats X --html Y` compares the
JATS and publisher HTML readings of one review (share of JATS loci whose
sentence matches an HTML locus of the same label; the gate is a median share
across reviews).

## Inputs from byte caches and live routes

A study may read two byte caches: a `literature-evidence@2` store (the
label-free projection, see [storage](storage.md)) and a provider response cache
(the response database of an earlier corpus acquisition with its
content-addressed artifact directory). `study acquire`, `study abstracts`,
`study prepare` and `study full-text` take `--store DIR` (absolute path;
`--store-root ROOT` requires that Merkle root, the pinned dataset version) and
`--provider-cache FILE`. Both are only ever byte caches: a cached copy stands in
for a download, and everything the study measures is derived again from the
bytes.

**The guard.** Each cache is ATTACHed with `mode=ro&immutable=1` behind a
SQLite authorizer (`store_client.GuardedReader`). A statement may read the
cache's allowlisted tables and nothing else: for the store, the projection's
kept tables (works, identifiers, source artifacts, retrievals, sections,
documents, the retained-source index and its metadata); for the provider cache,
its `responses` table only, so leases, rate slots and recorded failures are
never reused. A read of any other table, a view over one, a schema table, a
table-valued function, a write, a pragma or a further ATTACH is refused with an
error naming the table, never answered with an empty result, so no verdict,
score, locus, attempt, summary or bibliography property can be read even from
a store that carried one. Opening the store also verifies its Merkle root, that
it declares the projection format with the current rules, and the projection
gate (no row outside the kept tables). Every byte read from either cache must
hash to the digest the cache records and is refused if it carries legacy
verdict vocabulary. The provider cache must be quiescent (no journal beside it);
its database digest is its version.

**Review documents.** `study acquire --input enumeration.jsonl --output
inputs/reviews.jsonl` looks up each review by identity, DOI first: the store's
retained-source index and its documents of the work (found through the
identifier rows and the store's own work-id rule; a work whose other identifiers
contradict the record is refused), then the provider cache (Europe PMC
fullTextXML by PMCID, the publisher page through doi.org). A cached document is
accepted only when

- its bytes hash to the digest the cache records (and to the record's
  `documentSha256`, when it pins one);
- it declares the review's DOI (a JATS document without a DOI may be confirmed
  by its PMCID or PMID instead; any identifier it shares with the record must
  agree);
- it is the article: a page without in-text citation markers or a bibliography,
  such as a landing or paywall page, is refused. An article whose structured
  extraction fails is kept only when no other candidate extracts; `study
  prepare` then records the extraction failure.

Otherwise, with `--live`, the review is acquired through its journal's route,
`--route "JOURNAL=public"` or `--route "JOURNAL=entitled"`, optionally with the
document kinds the route reads in order (`:jats,html`, or one kind only, which
then also limits the cached candidates): Europe PMC fullTextXML by PMCID, which
is always public, and the publisher page through doi.org under the route's
class (entitled: the institution's licence). The same identity and article
checks apply. Linked table pages of an HTML review come from the store, the
provider cache or the route, in that order; a table page that declares a DOI
must declare the review's. Without a route a journal is read from the caches
only. Documents are written content-addressed to `documents/` beside the output,
records stream through `<output>.partial` and resume, a throttled or failing
route leaves the record in state `retry` for the next run on the output, and
`--retry-unavailable` tries the unavailable ones again.

Every review records its provenance class: `cache-hit-public`,
`cache-hit-entitled`, `live-public`, `live-entitled` or `unavailable` (and
`supplied` for a document an older manifest carried without an acquisition
record). Europe PMC copies are public; retained captures and publisher pages
take the class of the journal's route, and entitled when the journal has none.
The record's `acquisition` block names the cache and its version, the source
digest, the identifiers that agreed, the table pages and every rejected
candidate with its reason; `provenanceClass` follows from the label, and
entitled reviews reach only on-premises judges.

**Cited-work abstracts.** `study abstracts --store DIR --provider-cache FILE`
asks the caches before Europe PMC. From the provider cache it takes the
response to the request the live lookup would make (by DOI, else by PMID); the
first record carrying that identifier decides, as it does live, and its raw
`abstractText` is used unchanged with the outcome class the live lookup would
give. From the store it takes only an abstract of record, a complete Europe PMC
record whose original provider response is retained; legacy normalised
abstract columns are never used. A record whose other identifiers conflict with
the work is refused; a cached record without an abstract is no hit, and the
live lookup decides. Each work records `abstractProvenance` (`cache-hit-public`
with the cache, its version and the source digest, `live-public`, `supplied`
for a recorded fallback, or `unavailable`) bound to the digest of its text; text
changed after the lookup counts as supplied.

**Cited-work full texts.** `study full-text --store DIR --provider-cache FILE`
reads cached Europe PMC fullTextXML first (the provider cache by the PMCID's
fullTextXML address, the store's Europe PMC documents) and accepts it when its
PMCID or DOI agrees with the work; otherwise Europe PMC live
(`--europe-pmc`), then entitled text. Each escalated entry records
`ft_provenance`: the class of the source behind its window, or `unavailable`
when no window was sent.

**Preparation.** `study prepare` with the same caches pins them (`inputStore`,
`inputProviderCache` in the run metadata; a later change is refused) and
re-checks every review cache hit: the claimed cache version must be the pinned
one and it must hold exactly these bytes for this review, otherwise the record
is skipped as `skipped-provenance-unverified`. An acquired document that
declares a different DOI is refused. Provenance lands in `reviews.provenance`
and in `input_provenance`, one row per review document, abstract evidence unit
and full-text source (cache, cache version, source digest, identity check,
provider, address, retrieval time, whether the cache hit was re-checked against
the pinned caches, and the acquisition record). `study status` reports the
classes per journal and per item kind and the cache hits not re-checked; the
facts carry them per journal and stratum (see Facts).

## Fresh enumeration

A study that must not import an earlier population builds it live and compares
the stored manifest only afterwards:

```bash
ocsl-review study enumerate --spec protocol/enumeration.json --output study/enumeration.jsonl
ocsl-review study acquire --input study/enumeration.jsonl --output study/reviews.jsonl \
  --store "$STORE" --provider-cache "$PROVIDER_CACHE" --route "$JOURNAL_A=public" --route "$JOURNAL_B=entitled" --live
ocsl-review study prepare --database work/census.sqlite3 --seed "$SEED" --input study/reviews.works.abstracts.jsonl \
  --store "$STORE" --provider-cache "$PROVIDER_CACHE" \
  --require-fresh-enumeration --enumeration study/enumeration.jsonl.receipt.json
ocsl-review study gates --database work/census.sqlite3 --require-fresh-enumeration
ocsl-review study compare-manifest --database work/census.sqlite3 --stored old/manifest.csv \
  --output results/manifest-comparison.json --label stored
```

The enumeration spec lists groups, each with a journal label, a Europe PMC
query, a default `stratum` and optionally `strata` mapping publication types to
stratum labels, plus a `window` (`from`, `until`, field `FIRST_PDATE` by
default), `pageSize` and `resultType`. `study enumerate` pages through Europe
PMC search with cursors, keeps every raw page content-addressed in
`<output>.pages/` and writes the manifest and, last, the receipt
`<output>.receipt.json` (spec, effective queries, hit counts, page digests and
the population digest). A population that is not exhausted or whose hit count
changes while paging is refused; a review listed twice is kept once and records
without a DOI are listed by provider id. Requests carry identifiers only.

`study prepare --require-fresh-enumeration --enumeration RECEIPT` verifies the
receipt (every kept page against its digest, the population re-derived from
the pages), pins it (`enumerationSha256` in the run metadata and the
`enumeration` table) and prepares only records that descend from it: an
enumerated journal and DOI, a kept page of this enumeration, the stratum, year
and first-online date the enumeration gave, a document taken through `study
acquire` with its provenance, and cited-work records (`works`). Cache-hit claims
that cannot be re-checked because a cache was not given are refused. Once a
study is pinned to an enumeration, every later `study prepare` must name it.
`study gates --require-fresh-enumeration` adds checks that the enumeration is
pinned, that no review lies outside it, that every review has acquisition
provenance, that every cache hit (review, abstract, full text) names the pinned
cache version, and that every abstract comes from a recorded lookup (a cache
hit, a live lookup, a recorded fallback or a definitive absence), not from text
an earlier manifest carried.

`study compare-manifest` reads only the DOI, journal and stratum of a stored
manifest (JSON lines, JSON, CSV or TSV with a `doi` column, or one DOI per
line) and counts reviews in both, only in the fresh enumeration and only in the
stored manifest, per journal and stratum (stored-only reviews of journals
outside the enumeration are unattributed). It is a diagnostic, reported and
never tuned: it runs only on a study pinned to a fresh enumeration whose census
run is complete, unless `--allow-incomplete`, which the comparison records. The
counts go to `manifest_comparisons`, the per-review lists to the report, and
nothing of the stored manifest enters the study.

## Running a study

```bash
ocsl-review study acquire --input inputs/manifest.jsonl --output inputs/reviews.jsonl --store "$STORE" \
  --provider-cache "$PROVIDER_CACHE" --route "$JOURNAL_A=public" --route "$JOURNAL_B=entitled" --live
ocsl-review study abstracts --input inputs/reviews.jsonl --output inputs/reviews.abstracts.jsonl --store "$STORE" \
  --provider-cache "$PROVIDER_CACHE"
ocsl-review study prepare --database work/census.sqlite3 --seed "$SEED" --input inputs/reviews.abstracts.jsonl \
  --store "$STORE" --provider-cache "$PROVIDER_CACHE"
ocsl-review study judge --database work/census.sqlite3 --seed "$SEED" --base-url "$JUDGE_URL" \
  --api-key-env JUDGE_KEY_VARIABLE --concurrency 60
ocsl-review study full-text --database work/census.sqlite3 --seed "$SEED" --base-url "$JUDGE_URL" --europe-pmc \
  --store "$STORE" --provider-cache "$PROVIDER_CACHE"
ocsl-review study no-truncation --database work/census.sqlite3 --seed "$SEED" --base-url "$JUDGE_URL"
ocsl-review study replicate --database work/census.sqlite3 --seed "$SEED" --base-url "$JUDGE_URL" --entries "$N"
ocsl-review study gates --database work/census.sqlite3
ocsl-review study freeze --database work/census.sqlite3 --output results/scores.sqlite3
ocsl-review study export --scores results/scores.sqlite3 --output results/scores-export
ocsl-review study facts --scores results/scores.sqlite3 --output results/facts \
  --journal "$JOURNAL_A" --journal "$JOURNAL_B" --ratio "$JOURNAL_A" "$JOURNAL_B" --bootstrap-seed "$SEED"
ocsl-review study verify-facts --facts results/facts/facts.json
```

Runs are resumable per batch; receipts are written per call, and one process
writes a database at a time (a lock beside it refuses a second writer). A call
that gets no response after its transport attempts leaves its entries open,
never invalid: an entry is invalid only when a response arrived (a completion,
an empty completion, or a refusal of the request itself: HTTP 400, 413 or 422,
sent once) and no parseable line followed the repair pass. Authentication,
not-found, throttling and server errors are not answers and leave entries open. A batch whose first call was answered
but whose repair call was not resumes with that repair call. When consecutive
batches get no response at all, the pass stops dispatching; open entries are
retried in further passes after a growing cooldown (`--transport-retries`,
`--transport-cooldown`), and the run then pauses as `paused:transport` until
the next invocation. In the full-text tier, throttled or failed fetches (429,
server errors, timeouts, dropped connections) keep their entries open the same
way; a 404 is a failed retrieval, and `--retry-failed` reopens those. `replicate`
re-runs whole primary batches, unchanged, in a sample stratified by journal and
first-online year and allocated in proportion to judged entries; its verdicts go
to the history as variant `replicate`, and the estimands report category and
class agreement and the T/F kappa against the primary verdicts. `status` and
`gates` read the database without writing (also the frozen one). `gates` checks
invalid outputs (at most 1 %), transport failures (at most 0.5 % of calls),
uncovered anchors (0), duplicate judgments (0), complete abstract and full-text
tiers, and a fingerprint on every receipt that received a response (all tiers
and variants); it notes when the server never offers `system_fingerprint`. The transport sends the request body bytes exactly as built and
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
q-values), `sanitizer_findings`, `input_provenance` (where each review document,
abstract evidence unit and full-text source came from), `enumeration` (a pinned
fresh enumeration), `manifest_comparisons` (counts of a stored manifest compared
afterwards) and `data_dictionary`; `reviews.provenance` and
`citations.ft_provenance` carry the provenance class of the review document and
of the full text behind each window. A working database created before these
columns gains them when a study command opens it. Citing sentences that are
sent, every abstract (once per database) and every full-text window pass the
sanitizer in detect mode: hidden characters and prompt-injection sentences are
recorded by offset, rule and digest, and the text the judge receives stays the
instrument's, byte for byte. `study freeze` writes the closed, read-only
copy (rollback journal, evidence text removed unless `--keep-text`). It refuses
a paused run or one with pending reviews unless `--allow-incomplete`, which is
recorded (`complete` = 0) so that `study facts` refuses it too unless told
otherwise. Without `--keep-text` the frozen copy also replaces the entitled
reviews' citing sentences by `sha256:` digests and removes their printed
entries, rationales and full-text quotes from entitled text; `study export`
refuses a database that kept entitled text unless `--allow-entitled-text`.
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
  extraction route, no truncation), coverage tables by year, stratum, route,
  provenance class and acquisition class, and a decomposition of any cell by provenance, route,
  abstract provider, identity class, position band, served model and cut.

## Facts

`study facts` writes a closed `facts.sqlite3` (the frozen tables plus an
`estimates` table) and `facts.json` with `schemaVersion: 1`. Every fact is one
scalar SQL query over the closed file, recomputed at export, with the file's
sha256, the input hashes (scores database, instrument digest) and the code
revision; interval bounds are separate facts, and a `derivation` block records
estimand, seed and B. Input provenance is exported as facts too: per journal,
reviews by acquisition class (`census.<journal>.provenance.reviews.<class>`,
`.reviews_read.<class>`, and per stratum `.provenance.stratum.<stratum>.reviews.<class>`,
every class including zero), entries by the class of their abstract
(`.provenance.abstracts.<class>`) and of their full-text window
(`.provenance.full_text.<class>`), coverage cells by acquisition class
(`.coverage.acquisition.<class>.*`), the fresh enumeration
(`.enumeration.reviews`, `.in_study`, `.outside`, per stratum) and each
recorded manifest comparison (`.manifest_comparison.<label>.<stratum|all>.<count>`
and `census.manifest_comparison.<label>.<count>` over all journals). The pinned
store root, provider cache digest and enumeration digest are recorded with the
input hashes. The export passes `ocsl-writing facts verify` unchanged;
`ocsl-writing facts lint` then requires every numeral in a manuscript template
to come from a fact token.

## Fidelity tests

`tests/test_citation_census_fidelity.py` pins the instrument to recorded sha256
values: system prompts, first and repair calls, the label parser, JATS
conversion, full-text windows, full-text call geometry and prompts built from
fixture documents through the maintained extraction. It reads inputs only.
Setting `OCSL_REVIEW_CENSUS_FIDELITY_RECORDS` to a JSON-lines file of recorded
calls (`title`, `journal`, `items`, `note`, and any of `systemSha256`,
`userSha256` and `messagesSha256`, the sha256 of the JSON message list) adds a
check against a private record set. Every digest a record carries must match
the rebuilt call; a record whose user digest is the digest of an empty string
holds no rebuilt prompt and is counted as unusable, never as a pass, and the
check needs at least one usable record. Records for each journal have to come
from that journal's own recorded calls.

## Limits

- The direct chat-completions transport is for on-premises judges and probes;
  broker bulk jobs need a verbatim instrument mode before they can carry this
  instrument unchanged. Its calls are receipted in the scores database
  (`calls`), not in the workspace egress ledger.
- Cited-work records are not derived by these commands: a fresh-enumeration
  manifest needs `works` per review (identifiers and bibliographic metadata of
  the cited works) before `study abstracts` and `study prepare`, and `study
  prepare --require-fresh-enumeration` skips records without them.
- Cached full texts of cited works are read only as Europe PMC fullTextXML;
  entitled full text of cited works is acquired live, never from a cache.
- The provider cache is read as a quiescent copy (`mode=ro&immutable=1`); a
  cache that another process still writes must be copied first.
- Fresh-enumeration strata come from the provider's publication types through
  the spec's `strata` map; types the provider does not record cannot be
  distinguished.
- Model identity is the served model string and the server fingerprint when
  offered; there is no weights hash.
- Country extraction is new and descriptive only; the topic map is a versioned
  keyword classifier.
