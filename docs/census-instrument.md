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
"$OCSL_REVIEW_HOME/bin/ocsl-review" instruments export citation-census@1 --tier abstract --param journal="$JOURNAL"
```

`instruments export` prints the bundle the execution daemon installs for one
tier of an instrument that declares `transport.messages: verbatim`, with its
broker digest (see [running a study through the broker](#running-a-study-through-the-broker)).

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
the review); a record may pin its document by `documentSha256`. A supplied
document without `provenanceClass` counts as entitled, as a journal without a
route does in acquisition, so it reaches only on-premises judges. Supplied
`identities`, `country` and `topic` are taken as given (recorded as
`supplied`); a fresh-enumeration study refuses records that carry them and
derives its cited works with `study works` (see [fresh enumeration](#fresh-enumeration)).

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
kept tables (corpora, works, identifiers, source artifacts, retrievals,
sections, documents, the retained-source index and its metadata); for the
provider cache, its `responses` table only, so leases, rate slots and recorded
failures are never reused. A read of any other table, a view over one, a schema
table or a table-valued function is refused with an error naming the table, and
writes, pragmas, recursive queries and further ATTACHes are refused as well;
nothing is ever answered with an empty result instead, so no verdict, score,
locus, attempt, summary or bibliography property can be read even from a store
that carried one. Opening the store also verifies its Merkle root, that
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
only. Documents are written content-addressed to `documents/` beside the output
(keep the study's later manifests, such as the abstracts output, in the same
directory so the document paths resolve), records stream through
`<output>.partial` and resume, a throttled or failing
route leaves the record in state `retry` for the next run on the output, and
`--retry-unavailable` tries the unavailable ones again.

Every review records its provenance class: `cache-hit-public`,
`cache-hit-entitled`, `live-public`, `live-entitled` or `unavailable` (and
`supplied` for a document an older manifest carried without an acquisition
record). Europe PMC copies are public; retained captures and publisher pages
take the class of the journal's route, and entitled when the journal has none.
The record's `acquisition` block names the cache and its version, the source
digest, the identifiers that agreed, the table pages, every rejected
candidate with its reason, and `liveRoute`: whether the journal's live route
was on, so that a cache hit only stood in for a download the acquisition would
have made anyway; `provenanceClass` follows from the label, and entitled
reviews reach only on-premises judges.

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
changed after the lookup counts as supplied. A work that `study works` derived
from the review's bibliography also takes its title, venue, year, PMCID and
retraction flag from the same record (`metadataProvenance`, bound to the digest
of those fields; `unavailable` without a record); metadata edited afterwards
counts as supplied. Works an input supplied keep their metadata.

**Cited-work full texts.** `study full-text --store DIR --provider-cache FILE
--europe-pmc` reads cached Europe PMC fullTextXML first (the provider cache by
the PMCID's fullTextXML address, the store's Europe PMC documents) and accepts
it when its PMCID or DOI agrees with the work; otherwise Europe PMC live, then
entitled text. The caches are given only together with `--europe-pmc`: earlier
runs fetched full texts only for their own escalations, so a cache-only tier
would limit full-text coverage to their selection, while with live retrieval a
cache hit only stands in for a download the run makes for every escalated work
(`liveRetrieval` in the source's provenance record). The source paths an
earlier census selected (`legacy-original-source` documents in the store) are
never candidates. Each escalated entry records `ft_provenance`: the class of
the source behind its window, or `unavailable` when no window was sent.

**Preparation.** `study prepare` with the same caches pins them (`inputStore`,
`inputProviderCache` in the run metadata; a later change is refused) and
re-checks every review cache hit: the claimed cache version must be the pinned
one and it must hold exactly these bytes for this review (a provider-cache hit
must answer this review's own request, its doi.org page or its PMCID's
fullTextXML), otherwise the record is skipped as `skipped-provenance-unverified`.
An acquired document is checked for identity again, as acquisition does: every
identifier it declares about itself (DOI, PMCID, PMID) must agree with the
record and one must be shared, otherwise it is refused. Every abstract cache
hit is read again from the pinned cache: the request the lookup makes for the
work must select the same entry (request key and record pointer, or store
document) with the same digest, and that record's abstract must be exactly the
text prepared; otherwise it is recorded unverified. Provenance lands in `reviews.provenance`
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
ocsl-review study works --input study/reviews.jsonl --output study/reviews.works.jsonl
ocsl-review study abstracts --input study/reviews.works.jsonl --output study/reviews.works.abstracts.jsonl \
  --store "$STORE" --provider-cache "$PROVIDER_CACHE"
ocsl-review study prepare --database work/census.sqlite3 --seed "$SEED" --input study/reviews.works.abstracts.jsonl \
  --store "$STORE" --provider-cache "$PROVIDER_CACHE" \
  --require-fresh-enumeration --enumeration study/enumeration.jsonl.receipt.json
ocsl-review study exclude --database work/census.sqlite3 --input protocol/exclusions.jsonl   # if the protocol excludes any
ocsl-review study gates --database work/census.sqlite3 --require-fresh-enumeration
ocsl-review study compare-manifest --database work/census.sqlite3 --stored old/manifest.csv \
  --output results/manifest-comparison.json --label stored
```

The enumeration spec lists groups, each with a journal label, a Europe PMC
query, a default `stratum` and optionally `strata` mapping publication types to
stratum labels, plus a `window` (`from`, `until`, a date field, `FIRST_PDATE` by
default), `pageSize` and `resultType`. A query selects a population, never
reviews one by one (rule `population-fields@1`): every term is `FIELD:value`
with a population-level field (`ISSN`, `JOURNAL`, `PUB_TYPE`, `SRC`, `LANG` and
the date fields `PUB_YEAR`, `FIRST_PDATE`, `FIRST_IDATE`, `P_PDATE`, `E_PDATE`),
joined by `AND`, `OR`, `NOT` and parentheses. Identifier fields (`DOI`,
`EXT_ID`, `PMCID`, `PMID`, ...) and bare terms, which search every field
including identifiers, are refused, so an earlier manifest cannot be replayed
as a fresh population. `study enumerate` pages through Europe PMC search with
cursors, keeps every raw page content-addressed in `<output>.pages/` and writes
the manifest and, last, the receipt `<output>.receipt.json` (spec, query rule,
effective queries, hit counts, page digests and the population digest). A
population that is not exhausted or whose hit count changes while paging is
refused; a review listed twice is kept once and records without a DOI are
listed by provider id. Requests carry identifiers only.

`study acquire` for a fresh study runs with `--live` and a `--route` for every
journal: its byte caches then only stand in for downloads, so which reviews an
earlier study happened to keep cannot decide which reviews are read. `study
works` derives each review's cited works from its own bibliography: a DOI or
PMID in the bibliography markup, else a DOI printed in the entry, one work per
cited work keyed `doi:<doi>` (else `pmid:<pmid>`), bound to the digest of the
document (`workProvenance`, derivation `bibliography-identifiers@1`); entries
without an identifier get no work and stay unresolved. It refuses records that
already carry works. `study abstracts` then fills each derived work's abstract
and metadata from the same Europe PMC record, with their provenance.

`study prepare --require-fresh-enumeration --enumeration RECEIPT` verifies the
receipt (every kept page against its digest, the population re-derived from
the pages, the queries against the rule), pins it (`enumerationSha256` and the
query rule in the run metadata, and the `enumeration` table) and prepares only
records that descend from it: an enumerated journal and DOI, a kept page of
this enumeration, the stratum, year and first-online date the enumeration gave,
a document taken through `study acquire` with the journal's live route on, no
carried `identities`, `country`, `topic` or `documentSha256`, and cited works
exactly as `study works` derives them from the document (derived again and
compared). Cache-hit claims that cannot be re-checked because a cache was not
given are refused, and so are excluded reviews. Once a study is pinned to an
enumeration, every later `study prepare` must name it.

**Exclusions and a complete population.** A study pinned to a fresh enumeration
is complete only when every enumerated review is in the study (prepared,
unavailable or unreadable, each in coverage) or excluded by the protocol with a
recorded reason: `study exclude --input FILE` reads JSON lines of `journal`,
`doi` and `reason` for enumerated reviews not in the study and keeps them in
`exclusions`. Freezing, facts export and the manifest comparison treat a
population with reviews neither in the study nor excluded as incomplete (a
study with no reviews, or only a pilot, is no completed run).

`study gates --require-fresh-enumeration` adds checks that the enumeration is
pinned with its query rule, that no review lies outside it and every enumerated
review is in the study or excluded, that every review has acquisition
provenance and was acquired with its journal's live route, that every cache hit
(review, abstract, full text) was re-checked against the pinned caches, that
cached full texts were taken only while retrieving live, that every abstract
comes from a recorded lookup (a cache hit, a live lookup, a recorded fallback
or a definitive absence), not from text an earlier manifest carried, that no
identity map or country was supplied, and that every work was derived from its
review with its metadata from a recorded lookup. Once a manifest comparison is
recorded, a further check requires the population to be unchanged since.

`study compare-manifest` reads only the DOI, journal and stratum of a stored
manifest (JSON lines, JSON, CSV or TSV with a `doi` column, or one DOI per
line) and counts reviews in both, only in the fresh enumeration and only in the
stored manifest, per journal and stratum (stored-only reviews of journals
outside the enumeration are unattributed), together with the enumerated
reviews in the study and excluded, and the digest of the study population
(every review with its provenance and every exclusion with its reason). It is a
diagnostic, reported and never tuned: it runs only on a study pinned to a fresh
enumeration whose census run and population are complete, unless
`--allow-incomplete`, which the comparison records. The first comparison
freezes the population: `study prepare` and `study exclude` refuse to run after
it. The counts go to `manifest_comparisons`, the per-review lists to the
report, and nothing of the stored manifest enters the study.

## Running a study

Before a census commits to a concurrency, `ocsl-review capacity probe` measures
the entries per second the judge sustains with this instrument's own requests
over synthetic entries, and whether the judge serves other clients
([capacity probe](capacity-probe.md)).

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
and variants); it notes when the server never offers `system_fingerprint`. The
direct transport sends the request body bytes exactly as built and records
their sha256 in the receipt; the key comes from the named environment
variable, never from a file. With a pinned study bundle, `gates` also checks
that every call receipt carries it, and after broker calls that every accepted
verdict comes from a call whose receipt attests the request the instrument
built.

## Running a study through the broker

In the lab the judge is reached through the execution daemon's broker, not
through a URL. The executor holds one handle: the run principal's Unix socket
(`--broker-socket`, or `OCSL_BROKER_SOCKET` when no `--base-url` is given; the
lab launcher binds the principal's socket directory read-only into the
sandbox). The socket is the identity: `GET /configs` over it names the
principal, its run, its granted instruments and one bulk worker per granted
provider, and a socket that names no active run principal (a chat turn socket,
for example) is refused. No bearer token, provider address or key reaches the
executor; `--api-key-env` is refused with the broker transport.

1. Export one bundle per tier and journal and give them to the operator:

   ```bash
   ocsl-review instruments export citation-census@1 --tier abstract --param journal="$JOURNAL_A"
   ocsl-review instruments export citation-census@1 --tier full-text --param journal="$JOURNAL_A" --body > full-text-a.json
   ```

   The output names `digest` (the broker allow-list key), `registryDigest`
   (the instrument's registry digest), the tier, the parameters,
   `bundleSha256` and `bundle`, the exact body of `PUT /v2/instruments/{digest}`
   (`--body` prints only that): the system prompt with the journal
   substituted, byte for byte the builder's; `responseSchema: null` (the
   label parser validates); `maxOutputTokens` `{perItem: 700, minimum: 3000}`;
   temperature 0; seed null; reasoning `none` (abstract) or `low` (full text);
   `itemsPerCall` 40 or 5; `requiredModelFingerprint`, the required served
   model (a different model pauses the job); and `transport.messages:
   verbatim`. The digest is the sha256 of the canonical JSON of
   `{format: ocsl-broker-instrument-v1, instrument, registryDigest, tier,
   parameters, bundleSha256}`, so a changed body never reuses a digest.
2. The operator installs every bundle (`PUT /v2/instruments/{digest}`) and
   grants the digests and the judge provider to the run principal. The
   executor installs nothing.
3. `study prepare ... --study-bundle <file|sha256>` pins the study bundle (an
   `ocsl-study-bundle-v1` record of `instruments.study_bundle_digest`: protocol,
   study prompt, template, instrument digests, dataset versions and package
   tags; a file is checked against its sha256 and its citation-census@1
   digest) before the first call. Every call receipt records its digest, and a
   broker run refuses to start without it. A bare sha256 is recorded unchecked:
   `status` reports `studyBundleVerified: false` and `gates` notes it until the
   bundle file is pinned as well.
4. Run the tiers with `--broker-socket` (or `OCSL_BROKER_SOCKET`) instead of
   `--base-url`: `study judge|full-text|no-truncation|replicate --database ...
   --seed ... --broker-socket "$SOCKET"`. `--broker-worker` selects the worker
   when the principal offers several; the worker's model must be the model the
   census requests (`--model`). Entitled text goes only to a broker provider
   the operator's egress policy lists as on premises: `--on-premises --policy`
   with `onPremBrokerProviders: [{"provider": "<id>", "models": [...]}]`.

Before any call, each command reads the principal's catalog again and refuses
unless the daemon offers verbatim instrument mode (`instrumentVerbatim: 1`),
the worker requests the census model, and every bundle the pending reviews
need is installed and granted and is listed with `transport.messages:
verbatim` (a policy-wrapped install would put the broker's worker policy in
front of the prompt); the error names the digests at fault.

**Calls.** Each paper-batch call (first, repair or full-text call) is one bulk
job with one item whose input is the chat-completions request the instrument
built plus its entry count: `{messages, model, max_tokens, temperature,
stream, reasoning_effort, itemCount}`, the system and user messages unchanged.
The job names the bundle digest. The daemon checks the item against the bundle
installed under it and the worker's model (the system message is the installed
prompt; model, `max_tokens`, temperature, seed, reasoning and `stream` are the
values it sends) and refuses any difference before a provider call with
`INSTRUMENT_MESSAGE_MISMATCH`; otherwise it sends exactly that request upstream,
without a worker policy or assignment text, as canonical JSON. The item id is
the call id, and the job's idempotency key is derived from the call id and the
request digest.
The executor follows the principal's changed-job feed (`GET /jobs?view=summary`)
and reads each settled job's result page; a waiting call also reads its own job
again every 30 seconds, so a settlement the feed missed never waits for the
deadline. A job reported settled while the call's item is still running (a
double launch across a daemon restart) is no answer yet: the call stays open
and meets the job again.

**Fidelity.** Every receipt must report `requestSha256` (the executor reads it
from the item on the job's result page), the sha256 of the canonical JSON of
the body the daemon sent upstream, and it must equal the
sha256 of the canonical JSON of the chat-completions body the executor built
(`model`, `messages`, `max_tokens`, `temperature`, `stream`,
`reasoning_effort`). Canonical JSON sorts object keys by UTF-16 code unit,
has no whitespace and writes strings and numbers as `JSON.stringify` does (an
integral float such as 0.0 is written 0). A missing or different digest makes
the call invalid: it is recorded with error `request_mismatch`, its output is
never parsed, its entries stay open (never invalid outputs) and the run pauses
as `paused:fidelity`. An item the broker refuses before any provider call (no
attempt) stops the run the same way, with the broker's reason in the call's
error: `INSTRUMENT_MESSAGE_MISMATCH` (the messages or settings differ from the
bundle installed under the digest, or from the worker's model),
`PRINCIPAL_REQUIRED` (no run principal authority) or `INVALID_REQUEST` (the
daemon cannot run the instrument verbatim). No repair call follows, since the
same request would be refused again. The next invocation tries again, so fix
the daemon or the installed bundle first.

**What counts as the judge's answer.** Exactly what counts for a direct call: the
item's `servedModel` is the model the judge's response named (a response naming
none, or another model, is model drift, never the requested model taken on
trust), `fingerprint` is the response's own `system_fingerprint`
(`systemFingerprint`; NULL when the server offered none, so the fingerprint
gate fails with the same note on both transports), and the upstream status
decides as it does directly: 400, 413 or 422 is the judge refusing this very
request (an answer without labels), any other failure after the broker's own
attempts (408, 425, 5xx, an unreachable judge, a 2xx without a chat completion
message, `NO_FINAL_MESSAGE`) is a transport failure whose entries stay open, and
an empty completion (asked again by the broker, as a direct call would) is an
answer without labels.

**Receipts.** Each call has a `calls` row (the columns every earlier version
writes) and a `call_transport` row beside it: `transport` (direct or broker),
`study_bundle_sha256`, `request_sha256` and, for broker calls,
`broker_principal_id`, `broker_job_id` (the job that settled it, after any
resume), `broker_item_id` (the call id), `broker_instrument_digest`,
`broker_settings_sha256`, `broker_request_sha256`, `broker_error_code`,
`broker_server_fingerprint` (the daemon's `serverFingerprint`: the server's
`system_fingerprint` when offered, else its own identity of the served model
and the server headers) and `broker_http_status`. Keeping `calls` unchanged
keeps a deploy reversible: an earlier version still opens a database this one
wrote and records its own receipts in it (a call it records counts as a direct
call without the study bundle).

**Pauses and outages.** A provider-limit pause whose `resumeAfter` lies in the
future, an exhausted budget, an expired principal, a withdrawn grant, a
cancelled job or a refused job halt the transport: no further job is
submitted, calls in flight still settle and are recorded, and the run pauses as
`paused:transport` with the broker's reason (`status` shows it as `runPause`,
the command's result carries `resumeAfter`). A call that never settled records
no receipt row, so the next invocation repeats the same call id, meets the same
broker job and resumes it: a provider-limit pause once `resumeAfter` has
passed, a budget pause once the operator has raised the budget. A job resumed
in earlier invocations is followed along its lineage to the tail (each earlier
resume answers with the job it created, changes nothing and does not count as a
resume) and resumed there, after the tail's own `resumeAfter`. Within one
invocation a pause is resumed once per job lineage (a provider pause another
job left behind) and halts the transport if it returns. When the daemon's
worker configuration changed since a call was submitted (a provider's endpoint,
model, credential variable or label), the job under the call's key cannot be
resumed (`WORKER_CONFIG_CHANGED`) and a new submission under it is refused
(`WORKER_KEY_CONFLICT`, naming that job): the executor takes the answer of that
job, or of a resume of it, if one settled the call, and otherwise moves the call
to the next key of a fixed sequence, so every later invocation meets the same
new job. A model drift
pauses as `paused:model_drift`, as with the direct transport. An unreachable
socket, a job feed that keeps failing for a minute or a call without a
settlement before its wait deadline leaves the call open, without a repair call
that would duplicate a call that may still run; the breaker, further passes and
`paused:transport` follow as with the direct transport (only recorded failures
count as transport failures). A socket that is gone is reported as such: the
run principal may have been revoked (or its socket withdrawn) or the daemon is
restarting; calls in flight at a revocation finish unless it cancels them, and
a run continued under a new principal cannot meet the old principal's jobs, so
`gates` notes when calls ran under more than one principal. A fidelity failure
halts the transport too: calls already queued are not sent, and fidelity stops
(`request_mismatch`, broker refusals) are reported apart from transport
failures.

**Never twice.** A call repeated after a crash meets its job through the
idempotency key and takes that job's result without a second provider call;
the unique index on accepted primary verdicts guards the database. A primary
abstract call's accepted verdicts are written with its receipt, so after a
crash a batch resumes with exactly the repair call that was due (the same call
id and request, which meets the repair job that may still run). A full-text
call is recorded in `pending_calls` before it is submitted and removed with its
receipt; the next pass or invocation sends a pending call first, unchanged, and
keeps its entries out of the new chunks, so a lost answer is collected from its
job instead of being judged again by a re-chunked call. (A sensitivity batch is
still re-run whole when it ends incomplete.) `pending_calls` is execution
state: it is never exported and is emptied in a frozen database.

## Scores database

One SQLite file per study: `reviews` (counts per tier and class), `citations`
(one row per printed entry, with identity, in-body flag, loci count, abstract
and full-text verdicts, classes, call ids and positions), `loci`, `works`,
`identity`, `evidence`, `calls` (one receipt per call: instrument digest,
served model, fingerprint, settings digest, system, user, message-list and body
digests, tokens, finish reason and attempts), `call_transport` (beside each
receipt: the transport, the canonical request digest, the pinned study bundle
and, for broker calls, the principal, job, item, bundle digest, the broker's
settings and request digests, its server identity and the upstream status),
`pending_calls` (broker full-text calls submitted and not yet recorded),
`history` (append-only),
`review_scores` and `work_scores` (beta-binomial excess and Benjamini-Hochberg
q-values), `sanitizer_findings`, `input_provenance` (where each review document,
abstract evidence unit and full-text source came from), `enumeration` (a pinned
fresh enumeration), `exclusions` (enumerated reviews the protocol excluded, with
their reasons), `manifest_comparisons` (counts of a stored manifest compared
afterwards, with the population in the study and its digest) and
`data_dictionary`; `reviews.provenance` and `citations.ft_provenance` carry the
provenance class of the review document and of the full text behind each
window, and `works.derivation` and `works.metadata_provenance` how a work record
was derived and where its metadata came from. A working database created before
these columns gains them when a study command opens it. Citing sentences that are
sent, every abstract (once per database) and every full-text window pass the
sanitizer in detect mode: hidden characters and prompt-injection sentences are
recorded by offset, rule and digest, and the text the judge receives stays the
instrument's, byte for byte. `status` counts the inputs that carry chat-template
control tokens (`inputsWithControlTokens`, rules `control-token` and
`control-block`) and `gates` notes them: a judge server that reads special
tokens inside message content may take them as role markers, on either
transport. `study freeze` writes the closed, read-only
copy (rollback journal, evidence text removed unless `--keep-text`). It refuses
a paused run, one with pending reviews, or a fresh-enumeration study whose
population is incomplete, unless `--allow-incomplete`, which is
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
every class including zero, and `.unrecorded` for reviews prepared before
provenance existed), entries by the class of their abstract
(`.provenance.abstracts.<class>`) and of their full-text window
(`.provenance.full_text.<class>`, with `.unrecorded` for full-text outcomes
recorded before provenance), coverage cells by acquisition class
(`.coverage.acquisition.<class>.*`), the fresh enumeration
(`.enumeration.reviews`, `.in_study`, `.outside`, `.excluded`, `.unexplained`,
per stratum) and each recorded manifest comparison
(`.manifest_comparison.<label>.<stratum|all>.<count>` and
`census.manifest_comparison.<label>.<count>` over all journals, including the
enumerated reviews in the study and excluded at comparison time). The pinned
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

- The direct chat-completions transport is for on-premises judges and probes.
  The broker transport needs the execution daemon's verbatim instrument mode
  (`transport.messages: verbatim` in the bundle, the system message checked
  against the installed prompt, the request sent unchanged and its canonical
  digest reported as `requestSha256`); a daemon without it refuses the bundle
  at installation or the calls, and the run stops before any verdict. Calls of
  both transports are receipted in the scores database (`calls`), not in the
  workspace egress ledger.
- `study works` derives cited works from identifiers only (a DOI or PMID in the
  bibliography markup, or a DOI printed in the entry); entries without one stay
  unresolved, and there is no title search. Derived works take their metadata
  from Europe PMC only; a work Europe PMC does not hold has no title, venue or
  year.
- Cached full texts of cited works are read only as Europe PMC fullTextXML and
  only while retrieving live; entitled full text of cited works is acquired
  live, never from a cache.
- The provider cache is read as a quiescent copy (`mode=ro&immutable=1`); a
  cache that another process still writes must be copied first.
- Fresh-enumeration strata come from the provider's publication types through
  the spec's `strata` map; types the provider does not record cannot be
  distinguished.
- Model identity is the served model string and the server fingerprint when
  offered; there is no weights hash.
- Country extraction is new and descriptive only; the topic map is a versioned
  keyword classifier.
