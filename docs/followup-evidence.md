# Follow-up audit: evidence commands

<!-- SPDX-License-Identifier: Apache-2.0 -->

These are the evidence commands of `ocsl-review followup`, the audit of claims and citations in a corpus of review
articles described in `docs/followup-audit.md`: status observations, support and function judging, dependency, cited-by
traversal, facts and exports. Every command works on one follow-up branch database (`--database`), returns one JSON
result and, with the global `--result-file`, writes it as `ocsl-review-result-v1`. A failure is a non-zero exit with its
error class (`validation`, `policy`, `usage`, `runtime`). Judging commands refuse a branch whose strata are not frozen
(`followup strata --freeze`); so do `dependency` and `citedby`, which read the strata. A command that can stop before
its work is done (a halted broker, items left open, a throttling provider) says so in a top-level `state` of `paused`
with a `reason` (the census step contract); a finished one reports `state` `completed`.

Sizes, shares and rates are never inputs: they come from the run's own data. Every adverse record reads "check
suggested"; the instruments have no validation set, so their outputs are model measurements with evidence and
coverage.

## External requests

Provider requests (Crossref, PubMed, Europe PMC, OpenAlex) go through the review companion's scholarly client: its
User-Agent, bounded responses, retries for throttling, NCBI's request ceiling and a per-host interval for bulk
traversal. Each request passes the egress gate with identifiers and paging controls only (`doi`, `pmid`, `pmcid`,
`page`, `cursor`, `rows`); `--workspace` names the workspace whose egress ledger records them. The only contact
identity is the operator's `OCSL_REVIEW_CONTACT_EMAIL`; none is ever inferred. Every response is stored once by its
sha256 and receipted in `fu_requests` with contact and key parameters removed; a failed request records a failure
class (`http-<status>`, `timeout`, `malformed-response`, `request-failed`), never response text. A host that answers
HTTP 429 or 503 pushes its next request slot by the server's Retry-After (or a capped exponential backoff), and the
bulk loops (status fetch, cited-by traversal, evidence and second-generation acquisition) stop asking it after
`followup_net.MAX_CONSECUTIVE_THROTTLES` throttles in a row, with a paused result naming what was left.

## Status observations

### `followup status-import --csv FILE --declared-version VERSION`

Reads a Retraction Watch CSV snapshot that the operator stored as a dataset input. Nothing is fetched. The file is
registered as a dataset version (kind `retraction-watch-csv`: sha256, declared version, rows, file name); a declared
version or sha256 other than the protocol's (`status.retractionWatchCsv`) is refused, and so is a protocol that lists
no `retraction-watch-csv` source. Rows are matched to every cited work of the parent by `OriginalPaperDOI` (lower case)
or `OriginalPaperPubMedID`; `RetractionNature`, `RetractionDate`, `RetractionDOI`, `RetractionPubMedID`, `Record ID`
and `Reason` become the observation. A work with a DOI or PMID and no row records `none`; a work with neither records
`unknown`. The snapshot lists corrections only selectively, so its `none` covers retractions, expressions of concern and
reinstatements by default (see `status-resolve`). Importing the same snapshot again adds nothing.

### `followup status-fetch [--source S]... [--limit N] [--snapshot ID | --continue] [--workspace DIR]`

Asks every distinct cited work of the parent census (all cited works, not only those in the frame), with the parent's
identifiers, at each live source; one thread per source keeps each host's interval.

| Source | Requests | Observations | Covers |
|---|---|---|---|
| `crossref` | `/works/{doi}` and `/works?filter=updates:{doi}` | one per notice: `updated-by` on the work and `update-to` of notices naming it, with their origin (`crossref:publisher`, `crossref:retraction-watch`, else `crossref`); the notice type maps to retraction (retraction, partial retraction, withdrawal, removal), expression of concern, correction (correction, erratum, corrigendum) or reinstatement; the date and its precision (day, month, year) from `date-parts`; `none` only when both requests answered without a notice | every kind |
| `pubmed` | efetch of the work's PMID (`CommentsCorrectionsList`); esummary of each notice PMID | `RetractionIn`, `RetractedandRepublishedIn` retracted; `ExpressionOfConcernIn` expression of concern; `ErratumIn`, `CorrectedandRepublishedIn` corrected; the notice date only from the notice's own record, otherwise empty | retraction, expression of concern, correction |
| `europe-pmc` | search `DOI:"..."` (else `EXT_ID:<pmid> AND SRC:MED`), `resultType=core` | `isRetracted` `Y` retracted (no notice, no date), `N` none | retraction |

A source that cannot answer records `unknown` with its reason (`no-doi`, `no-pmid`, `no-identifier`, `not-found`,
`no-record`, `no-flag`, a failure class); it never stops the run. One run is one status snapshot (`status-<UTC time>`),
registered as a dataset version (kind `status-snapshot`, sha256 of the snapshot's canonical observation rows) when every
source finished. A source whose host throttles repeatedly stops; the snapshot then stays open and the result is paused.
`--snapshot` continues a given snapshot and `--continue` the latest one that did not finish (so the same command line
can always pass `--continue`: a new snapshot starts when none is open): pairs of work and source that answered are
skipped, failed ones are asked again. An unexpected failure leaves the snapshot open and is a `runtime` failure, never a
refusal. `--source` defaults to the protocol's live sources; `--limit` asks only the first works (a smoke test).

### `followup status-resolve [--snapshot ID]...`

Gives every cited work of the parent exactly one `fu_work_status` row under `status-resolution@1`, from the
observations of the chosen snapshots (default: the latest finished live snapshot and the latest Retraction Watch
import) and the protocol's sources only:

1. retracted when a retraction notice stands; a reinstatement dated later than every dated retraction notice sets
   `reinstated` and the next kind decides (a reinstatement cannot be shown to be later than an undated retraction);
2. otherwise expression of concern, otherwise corrected;
3. otherwise `none` when every notice kind (retraction, expression of concern, correction) is covered by a source that
   answered for the work, otherwise `unknown` with the kinds no answering source covers (`unknown_kinds_json`). The
   kinds an answer covers come from the protocol (`status.covers`, per source) or the defaults: Crossref every kind,
   PubMed retraction, expression of concern and correction, Europe PMC retraction, the Retraction Watch snapshot
   retraction, expression of concern and reinstatement (it records corrections only selectively), so a correction
   counts as covered through Crossref or PubMed;
4. the deciding notice is the earliest-dated notice of the deciding kind, dates compared at the coarser of their
   precisions (on a tie the finer date wins; undated notices come last); its DOI, date, precision and source are
   recorded. Disagreeing dates stay in the observations.

`sources_json` lists every source with whether it answered, its statuses, the kinds it reports, the kinds counted as
covered and their basis (`protocol` or `default`), its latest retrieval time and its notices. The result reports the statuses, the deciding sources, `citedWorksWithoutStatusRow` (the gate
needs none) and a diagnostic: the parent's retraction flag (what the census judge was shown) against the resolved
status. The flag is never a status source. Resolving again after the freeze is refused.

## Support: `followup support --tier abstract|full-text`

Options: `--store DIR --store-root ROOT`, `--provider-cache FILE`, `--europe-pmc`, `--entitled-documents DIR`,
`--documents DIR`, `--fetch-workers N`, `--retry-failed`, `--workspace DIR`, and the judge options (`--model`,
`--base-url`, `--api-key-env`, `--broker-socket`, `--broker-worker`, `--concurrency`, `--on-premises`, `--policy`,
`--transport-retries`, `--transport-cooldown`, `--variant`, `--scope`, `--public-only`, `--limit`).

The protocol pins the support instrument (`instruments.support`); it runs one item per call on the units of S0, S1,
S2, S3 and the co-cited loci at either tier. citation-support-verbatim@1 carries citation-support@2's prompt, output
schema, validator and class map and declares its request settings per tier (temperature zero, no seed, reasoning off at
the abstract tier and low at the full-text tier, its output-token rule) and the served model it requires: the
protocol's `model.served` must be that model, and a protocol that also predeclares `instruments.supportSettings` is
refused. It runs on a judge called directly or through the broker (`--broker-socket`, one bundle per tier; see the
broker judge below). citation-support@2 runs unchanged on a judge called directly, with the request settings the
protocol predeclares for the tier (`instruments.supportSettings`: the reasoning effort, and optionally the temperature;
the instrument declares none); the command refuses to send anything without them. Every request body carries the
tier's settings, and the result reports them (`requestSettings`: the reasoning effort, temperature, max_tokens and items
per call, with their source, the protocol or the instrument). `--scope pilot` takes the
pilot units and the co-cited loci of pilot anchors; `--variant replicate` takes the replicate units every judging
command draws (`followup_strata.replicate_keys`): within each replicate group (the unit's first stratum in the order
S1, S2, S3, S0, co-cited, and within S1 its work's status), the ceiling of the protocol's replicate share times the
group's number, ranked by the sha256 of the protocol seed, the instrument id, the group and the locus id (the result
records the rule and the groups; replicate verdicts stay in the judgment ledger and never replace a primary row);
`--public-only` sends only public items; `--limit` sends at most that many items. Items already accepted under the
same instrument digest, tier and variant are skipped, so a run resumes where it stopped.

Evidence per cited work is acquired once and stored in `fu_evidence` and `fu_evidence_units`:

- abstract: the byte caches (a pinned literature-evidence@2 store, a provider response cache), then Europe PMC
  `resultType=core` live; only a complete structured abstract counts;
- full text: the caches (Europe PMC fullTextXML by PMCID), then with `--europe-pmc` Europe PMC `fullTextXML` through
  the scholarly transport's document route (the PMCID from the parent, else from the Europe PMC record), then the
  study's entitled documents. The JATS becomes abstract paragraphs, section paragraphs, tables and figure captions,
  each with its section trail.

Obtained evidence and definitive outcomes are reused; a failed fetch is retried with `--retry-failed`. Entitled
documents are a local directory with `entitled-documents.json`
(`{"format": "ocsl-followup-entitled-v1", "documents": [{"doi": ..., "pmid": ..., "path": ..., "sha256": ...}]}`);
each file must hash to its recorded digest, and its evidence has provenance `entitled`.

Each task is what `corpus_assessment.TaskPlanner` expects: `id` `<locus id>|<tier>`, the citing sentence as `claim`,
the neighbouring sentences as `context` (with `--documents`), the marker and section in `citationLocus`, the cited
work's title and year, the evidence units, completeness and the source digest. No identifier of the cited work is
sent, and neither is its status: the status markers publishers and indexes put into a title (a leading `RETRACTED:`,
`Retracted article:`, `WITHDRAWN:`, `Retraction note:`, `Expression of concern:`, `Correction to:`, `Erratum:` or
`Corrigendum:`, a trailing `[Retracted]` or `(Retracted article. See ...)`) are removed from every cited title, in
every stratum, before the support, function and numeric judges see it (`followup_model.status_blind_title`). The evidence is the whole document when it fits claim-check@1's window, otherwise the claim-targeted window of
`claim_check.select_evidence` over a retrieval index of the work (claim-check@1's k and window size). Every text
passes the sanitizer first. The item's provenance (entitled when the citing sentence comes from the text parent or the
evidence is entitled) travels beside it and is never sent; entitled items reach only a judge the operator's policy
lists as on premises, and the run stops before the first call otherwise.

`fu_support` gets one row per unit and tier: the verdict (`direct-support`, `scope-caveat`, `partial-indirect`,
`contradicted`, `not-addressed`) with its first exact quote, evidence coverage and provenance, or `assessable = 0`
with its reason (see the table at the end). An abstention keeps the verdict `abstain` with reason `abstained`.

The result of every judging command (support, function, and numeric and second-gen in the analysis set) carries the
census step contract at the top level: `state` (`completed`, or `paused` when the run stopped or items are still
open), a `reason` with the census prefixes (`model_drift: served ..., required ...`, `fidelity: ...`, `transport:
broker halted: ...`, `transport: N items still open after P further passes`), `resumeAfter` and `transport` (the broker
transport's description with its halt record) when there are any, and the totals of the invocation: `accepted`,
`invalid` (items rejected after the repair pass), `calls` (first and repair calls) and `reviews` (the units decided:
loci or candidate sentences, under the census field name). `run` holds the runner's summary and `judge` names the
judge. Evidence acquisition stopped by a throttling provider also pauses the support result.

## Function: `followup function [--documents DIR]` with the judge options

citation-function@1 classifies the units of S0, S1, S2 and S3 (co-cited loci are not classified) as `relies`,
`background`, `critical` or `notes-retraction`, with an exact quote from the field it names, or abstains with a
reason. The judge sees the named work's marker, the citing sentence, the sentences before and after it, the section
and the cited work's title (without status markers) and year; never its status, the stratum, support verdicts,
identifiers or the journal, so S0 and S1 loci are judged alike.

The marker is the census's rendering of the work's bibliography target (`⟨rid⟩`, or the publisher-HTML target
without its `ref-` prefix); a sentence with one marker names that one; when the sentence's targets match none of its
markers and as many markers are left as unmatched loci, they are paired in bibliography order; otherwise the unit is
not sent. The neighbouring sentences come from `--documents`: the census's review documents as it stored them
(`<sha256>.xml` or `<sha256>.html`, the digest checked), re-extracted with the census's own extraction; the citing unit
whose sentence digest matches gives its neighbours, and without a document they are empty strings, as the
instrument's evidence policy says. Ten items go in a call, with one repair pass. `fu_function` gets one row per unit:
the class with its quote and field, or the reason.

## Dependency: `followup dependency [--scope pilot|all]`

`dependency-aggregation@1` for every S1 and S2 anchor, from the parent's loci of the same citing sentence (so a work
the frame leaves out can only make the class undetermined): the co-cited works are the distinct other works of the
sentence (a work cited twice counts once; a locus with an unresolved identity counts as one unassessable
co-citation). A co-cited locus is decided when its full-text verdict is in citation-support@2's class map `supported`
or `adverse` classes; a verdict of the `uncertain` class (`partial-indirect`; an abstention is never assessable) is
unsettled support and leaves it unassessable, as abstract silence and failed downloads do. A co-cited work supports
the claim when one of its decided loci is `supported` (`direct-support`, `scope-caveat`), and it is assessable when
one of its loci is decided. A co-cited work whose own resolved status is one of the protocol's S1 statuses never
counts as support: a claim resting only on compromised works is not redundant. The class is `sole` (no other work),
`redundant` (another work without a notice of its own supports), `essential` (every other work assessable, none
without a notice supports) or `undetermined` (otherwise). `fu_dependency` records the counts, the anchor's own
full-text verdict and, in `detail_json`, every co-cited work with its status and its loci with their verdicts, the
support of compromised works (`compromisedSupporting`) and the class each other reading gives (`readings`:
unsettled support counted as decided non-support; support of compromised works counted), for a predeclared
sensitivity. Recomputing gives the same rows; `--scope pilot` (re)computes only the pilot anchors' rows.

## Cited-by: `followup citedby [--source S]... [--limit N] [--widen] [--max-pages N] [--workspace DIR]`

Targets are the reviews with an S1 locus whose dependency is sole or essential; `--widen` takes every review with an
S1 locus. `--source` defaults to the protocol's second-generation sources.

- Europe PMC: the review's record (search `DOI:"..."`), then `/{source}/{id}/citations?page=N&pageSize=1000` until
  its hit count is read. States: `complete`, `truncated` at `--max-pages`, `no-identifier` without a Europe PMC
  record, `error` when a request fails.
- OpenAlex: `/works/doi:{doi}`, then `/works?filter=cites:{id}` with cursor paging. Any failure is the state
  `unavailable` with its reason and never stops the traversal. An operator key in `OCSL_REVIEW_OPENALEX_API_KEY` is
  sent as OpenAlex expects and never recorded; `mailto` only with the operator's contact address.

A source whose host throttles repeatedly is asked nothing more in that run, while the other source goes on; the result
is then paused and names the targets each throttled source left (`targetsLeft`). The paused run leaves its traversal
open (`fu_meta` key `citedby.unfinished`: the options and the start), and the next run with the same options resumes
it: a target and source covered since that start with a final state (`complete`, `truncated`, `no-identifier`) are not
asked again, while `error` and `unavailable` are. A run that finishes closes the traversal, so the run after it
traverses every target again. Every result counts the targets and sources it wrote (`traversed`). Edges of a target are united across sources: records that share any identifier carry one citing key, the DOI, else
the PMID, else the PMCID, else the OpenAlex id (`doi:`, `pmid:`, `pmcid:`, `openalex:`). `fu_citedby_edges` keeps one
row per target, source and key; `fu_citedby_coverage` one per target and source (state, hit count, edges, reason).
Every run registers the graph as a dataset version (kind `cited-by-graph`, sha256 of the canonical edge rows).

## The broker judge

With `--broker-socket` (or `OCSL_BROKER_SOCKET`) a judging command reaches its judge through the run principal's
broker. Each call is one one-item job naming the instrument's broker digest (`ocsl-review instruments export`,
installed with `PUT /v2/instruments/{digest}` and granted to the run principal) and carrying the request the
instrument built. The job's idempotency key comes from the call's identity (instrument digest, tier, variant, call
kind, item keys, the branch and the number of earlier calls of that identity that settled) and the request digest: a
call that never settled (a paused or queued job, an unreachable broker) is sent again under the same key and meets the
job the broker already holds, while a replicate, a repair call and a call after a settled one each start a job of their
own and reach the model. The served model and the fingerprint are the judge response's own; another served model stops the run as model drift; a request digest other than the one
built, or a refusal before any provider call, halts the transport (code `FIDELITY`); any other failure leaves the
call's items open for the further passes. Once the transport has halted, for any reason (a fidelity stop, the run's
budget, a provider limit with its `resumeAfter`, a revoked grant, a cancellation), the runner sends nothing more and
skips its further passes, and the result is paused with the halt record. A broker run records the pinned study bundle
in every receipt and refuses to start without one. The instruments that export broker bundles run this way:
citation-function@1, numeric-check@1, claim-match@1 and citation-support-verbatim@1. The two tiers of
citation-support-verbatim@1 share one prompt and export one bundle each (`ocsl-review instruments export
citation-support-verbatim@1 --tier abstract`, and `--tier full-text`); a call goes out under the bundle whose settings
are the call's, so the run principal needs the bundle of every tier it judges installed and granted. The result names
the bundles the judge required (`judge.bundles`: `<instrument> <tier>` to broker digest). citation-support@2 has no
verbatim bundle: with `--broker-socket`, support under a protocol that pins it is refused before any call. The broker
judge is on premises only with `--on-premises` and a `--policy` that lists the broker provider (and model) in
`onPremBrokerProviders`.

## Facts: `followup facts --output DIR` and `followup verify-facts --facts DIR`

`facts` verifies the branch (the parent digest unchanged), then writes a closed `facts.sqlite3` of aggregates only, so
it stays small at census scale and a fact (which hashes the file) stays cheap to verify: per-family summaries of the
branch (`fs_work_status`, `fs_status_observations`, `fs_totals`, `fs_strata`, `fs_support`, `fs_support_strata`,
`fs_function`, `fs_function_strata`, `fs_dependency`, `fs_dependency_strata`, `fs_numeric`, `fs_citedby`,
`fs_citedby_coverage`, `fs_secondgen`, `fs_calls`, `fs_judgments`, `fs_requests`: each a GROUP BY of the branch table
it names), the estimates and coverage (`fu_estimates`, `fu_coverage`), the stratum sizes, the protocol freeze, the
dataset versions, `fu_census_counts` (per journal, year and status, the parent's in-body identity-resolved loci: the
denominators of the compromised share), `fu_census_flags` (the parent's retraction flag against the resolved status)
and `fact_inputs` (parent, branch and protocol digests, the freeze, instrument digests, the study bundle, dataset
versions, code revision, the selection seed and the bootstrap replicates and seed the estimates used). No per-locus
table and no text of a review or a cited work enters it. `facts.json` (schemaVersion 1) holds one scalar SQL query per
fact with the file's sha256 and the code revision (a summary fact names its branch query in its derivation); the inputs
are recorded once, in the provenance. Keys: `followup.status.*`, `followup.census.*`, `followup.strata.*`,
`followup.support.*`, `followup.function.*`, `followup.dependency.*`, `followup.numeric.*`, `followup.citedby.*`,
`followup.secondgen.*`, `followup.calls.*`, `followup.requests.*`, `followup.estimates.*` (every `fu_estimates` row
with its derivation: the estimate key `followup.<estimand>...` becomes `followup.estimates.<estimand>...`, and a share
that is not measured keeps the value `not-measured`) and
`followup.coverage.<estimand>.<journal>.<year>.<stratum>.<state>.<reason>.units` and `.weighted` (from `fu_coverage`;
`*` reads `all` and the reason `-` of assessable units reads `none`). The estimates must share one bootstrap setting:
an export over estimates computed with different replicates or seeds is refused, and the provenance says whether the
setting is the protocol's. `verify-facts` re-runs every query read-only against the snapshot
(`corpus_facts.verify_export`); the writing companion's `facts` verifier accepts the export too.

## Export: `followup export --output DIR [--allow-entitled-text]`

Writes into a new directory:

- `compromised-claims.sqlite3` (closed): `claims` (one row per S1 or S2 locus: the citing sentence, the cited work,
  its status with source and date, the support verdicts and quotes, the function, the dependency and the judge
  receipts), `observations` (the status observations of the cited works), `receipts` (the judge calls behind the
  verdicts), `alerts` (the alert records below), `adjudications` (empty until a study adjudicates), `meta` and
  `data_dictionary`;
- `alerts.jsonl`: one record per affected claim and cited work, also in the claim database's `alerts` table: the claim,
  the cited work,
  its status, the evidence (support, function, dependency, timing against the notice, receipts), the confidence
  "check suggested" and a suggested action for the authors of the citing review and for a study team. A claim
  resting on a work with a retraction, expression of concern or correction is affected unless the review notes the
  notice itself; an S2 claim is affected when its full-text verdict is contradicted or not-addressed;
- `propagation-graph.json`: compromised works, the claims citing them, and the later articles whose sentences restate a
  claim on the review's authority (confirmed second-generation matches), with the edges between them;
- `manifest.json`: the sha256, size and rows of every file, the branch, parent and protocol digests, the freeze and
  the redaction applied.

A public export drops entitled text: entitled citing sentences become `sha256:<hex>`, their section trails are removed,
and quotes from entitled sentences or evidence are dropped; it needs no text parent. `--allow-entitled-text` makes a
private export that keeps them (it needs the text parent mounted); never share it. An export writes nothing into the
branch, so the branch digest a facts export recorded still holds afterwards. An alert of an essential claim whose
other cited works support it only where they carry a notice themselves says so.

## Reasons a unit has no verdict

| Reason | Meaning |
|---|---|
| `identity-unresolved` | the cited work's identity is unresolved |
| `text-redacted` | the parent keeps the entitled citing sentence only as a digest and no text parent supplies it |
| `no-citing-text` | the parent holds no citing sentence for the locus |
| `abstract-incomplete` | no complete structured abstract (none, cut, or no identifier to look one up) |
| `fetch-failed` | a request for the evidence failed; `--retry-failed` asks again |
| `no-open-full-text` | no route the run had holds the full text (no PMCID, Europe PMC answered not found, no entitled document) |
| `abstained` | the judge abstained, with its reason |
| `invalid-output` | the output failed validation after the repair pass |
| `not-judged` | not sent yet (scope, limit, public only, a call without a response), or no task could be built |
| `no-matching-table`, `table-unavailable` | numeric checks: the cited work is no report of the table source, or its edition has no cells |
| `no-matching-cell` | numeric checks: no cell of the table source holds the claimed condition, metric, geography and year |
| `unsupported-value-form` | numeric checks: the stated number has a form the rule does not read (a range, a ratio, a second number) |

Abstract silence and failed or missing downloads never count as unsupported: an unassessable unit keeps its reason
and goes into the coverage tables, never into a numerator.
