# Follow-up audit: evidence commands

<!-- SPDX-License-Identifier: Apache-2.0 -->

These are the evidence commands of `ocsl-review followup`, the audit of claims and citations in a corpus of review
articles described in `docs/followup-audit.md`: status observations, support and function judging, dependency,
cited-by traversal, facts and exports. Every command works on one follow-up branch database (`--database`), returns
one JSON result and, with the global `--result-file`, writes it as `ocsl-review-result-v1` for a Study Run step. A
failure is a non-zero exit with its error class (`validation`, `policy`, `usage`). Judging commands refuse a branch
whose strata are not frozen (`followup strata --freeze`); so do `dependency` and `citedby`, which read the strata.

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
class (`http-<status>`, `timeout`, `malformed-response`, `request-failed`), never response text.

## Status observations

### `followup status-import --csv FILE --declared-version VERSION`

Reads a Retraction Watch CSV snapshot that the operator stored as a dataset input. Nothing is fetched. The file is
registered as a dataset version (kind `retraction-watch-csv`: sha256, declared version, rows, file name); a declared
version or sha256 other than the protocol's (`status.retractionWatchCsv`) is refused, and so is a protocol that lists
no `retraction-watch-csv` source. Rows are matched to every cited work of the parent by `OriginalPaperDOI` (lower case)
or `OriginalPaperPubMedID`; `RetractionNature`, `RetractionDate`, `RetractionDOI`, `RetractionPubMedID`, `Record ID`
and `Reason` become the observation. A work with a DOI or PMID and no row records `none` (the snapshot covers every
notice kind); a work with neither records `unknown`. Importing the same snapshot again adds nothing.

### `followup status-fetch [--source S]... [--limit N] [--snapshot ID] [--workspace DIR]`

Asks every distinct cited work of the parent census (all cited works, not only those in the frame), with the parent's
identifiers, at each live source; one thread per source keeps each host's interval.

| Source | Requests | Observations | Covers |
|---|---|---|---|
| `crossref` | `/works/{doi}` and `/works?filter=updates:{doi}` | one per notice: `updated-by` on the work and `update-to` of notices naming it, with their origin (`crossref:publisher`, `crossref:retraction-watch`, else `crossref`); the notice type maps to retraction (retraction, partial retraction, withdrawal, removal), expression of concern, correction (correction, erratum, corrigendum) or reinstatement; the date and its precision (day, month, year) from `date-parts`; `none` only when both requests answered without a notice | every kind |
| `pubmed` | efetch of the work's PMID (`CommentsCorrectionsList`); esummary of each notice PMID | `RetractionIn`, `RetractedandRepublishedIn` retracted; `ExpressionOfConcernIn` expression of concern; `ErratumIn`, `CorrectedandRepublishedIn` corrected; the notice date only from the notice's own record, otherwise empty | retraction, expression of concern, correction |
| `europe-pmc` | search `DOI:"..."` (else `EXT_ID:<pmid> AND SRC:MED`), `resultType=core` | `isRetracted` `Y` retracted (no notice, no date), `N` none | retraction |

A source that cannot answer records `unknown` with its reason (`no-doi`, `no-pmid`, `no-identifier`, `not-found`,
`no-record`, `no-flag`, a failure class); it never stops the run. One run is one status snapshot
(`status-<UTC time>`), registered as a dataset version (kind `status-snapshot`, sha256 of the snapshot's canonical
observation rows). `--snapshot` continues a snapshot: pairs of work and source that answered are skipped, failed ones
are asked again. `--source` defaults to the protocol's live sources; `--limit` asks only the first works (a smoke
test).

### `followup status-resolve [--snapshot ID]...`

Gives every cited work of the parent exactly one `fu_work_status` row under `status-resolution@1`, from the
observations of the chosen snapshots (default: the latest live snapshot and the latest Retraction Watch import) and
the protocol's sources only:

1. retracted when a retraction notice stands; a reinstatement dated later than every dated retraction notice sets
   `reinstated` and the next kind decides (a reinstatement cannot be shown to be later than an undated retraction);
2. otherwise expression of concern, otherwise corrected;
3. otherwise `none` when every notice kind (retraction, expression of concern, correction) is covered by a source that
   answered for the work, otherwise `unknown` with the kinds no answering source covers (`unknown_kinds_json`);
4. the deciding notice is the earliest-dated notice of the deciding kind, dates compared at the coarser of their
   precisions (on a tie the finer date wins; undated notices come last); its DOI, date, precision and source are
   recorded. Disagreeing dates stay in the observations.

`sources_json` lists every source with whether it answered, its statuses, the kinds it covers, its latest retrieval
time and its notices. The result reports the statuses, the deciding sources, `citedWorksWithoutStatusRow` (the gate
needs none) and a diagnostic: the parent's retraction flag (what the census judge was shown) against the resolved
status. The flag is never a status source. Resolving again after the freeze is refused.

## Support: `followup support --tier abstract|full-text`

Options: `--store DIR --store-root ROOT`, `--provider-cache FILE`, `--europe-pmc`, `--entitled-documents DIR`,
`--documents DIR`, `--fetch-workers N`, `--retry-failed`, `--workspace DIR`, and the judge options (`--model`,
`--base-url`, `--api-key-env`, `--broker-socket`, `--broker-worker`, `--concurrency`, `--on-premises`, `--policy`,
`--transport-retries`, `--transport-cooldown`, `--variant`, `--scope`, `--public-only`, `--limit`).

citation-support@2 runs unchanged, one item per call, on the units of S0, S1, S2, S3 and the co-cited loci at either
tier. `--scope pilot` takes the pilot units and the co-cited loci of pilot anchors; `--variant replicate` takes the
units whose `sha256('<protocol seed>:replicate:<locus id>')`, read as a fraction, lies below the protocol's replicate
share (the result records the rule; replicate verdicts stay in the judgment ledger and never replace a primary row);
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
sent. The evidence is the whole document when it fits claim-check@1's window, otherwise the claim-targeted window of
`claim_check.select_evidence` over a retrieval index of the work (claim-check@1's k and window size). Every text
passes the sanitizer first. The item's provenance (entitled when the citing sentence comes from the text parent or the
evidence is entitled) travels beside it and is never sent; entitled items reach only a judge the operator's policy
lists as on premises, and the run stops before the first call otherwise.

`fu_support` gets one row per unit and tier: the verdict (`direct-support`, `scope-caveat`, `partial-indirect`,
`contradicted`, `not-addressed`) with its first exact quote, evidence coverage and provenance, or `assessable = 0`
with its reason (see the table at the end). An abstention keeps the verdict `abstain` with reason `abstained`.

## Function: `followup function [--documents DIR]` with the judge options

citation-function@1 classifies the units of S0, S1, S2 and S3 (co-cited loci are not classified) as `relies`,
`background`, `critical` or `notes-retraction`, with an exact quote from the field it names, or abstains with a
reason. The judge sees the named work's marker, the citing sentence, the sentences before and after it, the section
and the cited work's title and year; never its status, the stratum, support verdicts, identifiers or the journal, so
S0 and S1 loci are judged alike.

The marker is the census's rendering of the work's bibliography target (`⟨rid⟩`, or the publisher-HTML target
without its `ref-` prefix); a sentence with one marker names that one; when the sentence's targets match none of its
markers and as many markers are left as unmatched loci, they are paired in bibliography order; otherwise the unit is
not sent. The neighbouring sentences come from `--documents`: the census's review documents as it stored them
(`<sha256>.xml` or `<sha256>.html`, the digest checked), re-extracted with the census's own extraction; the citing unit
whose sentence digest matches gives its neighbours, and without a document they are empty strings, as the
instrument's evidence policy says. Ten items go in a call, with one repair pass. `fu_function` gets one row per unit:
the class with its quote and field, or the reason.

## Dependency: `followup dependency`

`dependency-aggregation@1` for every S1 and S2 anchor, from the parent's loci of the same citing sentence (so a work
the frame leaves out can only make the class undetermined): the co-cited works are the distinct other works of the
sentence (a work cited twice counts once; a locus with an unresolved identity counts as one unassessable
co-citation). A co-cited work supports the claim when one of its loci has a full-text verdict of `direct-support` or
`scope-caveat`; it is unassessable without an assessable full-text verdict. The class is `sole` (no other work),
`redundant` (another work supports), `essential` (every other work assessable, none supports) or `undetermined`
(otherwise: abstract silence and failed downloads never count as not supporting). `fu_dependency` records the counts,
the anchor's own full-text verdict and every co-cited locus with its verdict; recomputing gives the same rows.

## Cited-by: `followup citedby [--source S]... [--limit N] [--widen] [--max-pages N] [--workspace DIR]`

Targets are the reviews with an S1 locus whose dependency is sole or essential; `--widen` takes every review with an
S1 locus. `--source` defaults to the protocol's second-generation sources.

- Europe PMC: the review's record (search `DOI:"..."`), then `/{source}/{id}/citations?page=N&pageSize=1000` until
  its hit count is read. States: `complete`, `truncated` at `--max-pages`, `no-identifier` without a Europe PMC
  record, `error` when a request fails.
- OpenAlex: `/works/doi:{doi}`, then `/works?filter=cites:{id}` with cursor paging. Any failure is the state
  `unavailable` with its reason and never stops the traversal. An operator key in `OCSL_REVIEW_OPENALEX_API_KEY` is
  sent as OpenAlex expects and never recorded; `mailto` only with the operator's contact address.

Edges of a target are united across sources: records that share any identifier carry one citing key, the DOI, else
the PMID, else the PMCID, else the OpenAlex id (`doi:`, `pmid:`, `pmcid:`, `openalex:`). `fu_citedby_edges` keeps one
row per target, source and key; `fu_citedby_coverage` one per target and source (state, hit count, edges, reason).
Every run registers the graph as a dataset version (kind `cited-by-graph`, sha256 of the canonical edge rows).

## The broker judge

With `--broker-socket` (or `OCSL_BROKER_SOCKET`) a judging command reaches its judge through the run principal's
broker. Each call is one one-item job naming the instrument's broker digest (`ocsl-review instruments export`,
installed with `pnpm ops broker instruments install` and granted to the run principal) and carrying the request the
instrument built; the job's idempotency key comes from the request digest. The served model and the fingerprint are
the judge response's own; another served model stops the run as model drift; a request digest other than the one
built, or a refusal before any provider call, halts the transport; any other failure leaves the call's items open for
the further passes. Only the verbatim follow-up instruments run this way: citation-support@2 has no verbatim bundle
and runs on a judge called directly. The broker judge is on premises only with `--on-premises` and a `--policy` that
lists the broker provider (and model) in `onPremBrokerProviders`.

## Facts: `followup facts --output DIR` and `followup verify-facts --facts DIR`

`facts` verifies the branch (the parent digest unchanged), then writes a closed `facts.sqlite3`: the follow-up tables
copied with `VACUUM INTO`, without stored request and response bodies, evidence units, quotes, parsed verdicts,
section trails or sentence text, plus `fu_census_counts` (per journal, year and status, the parent's in-body
identity-resolved loci: the denominators of the compromised share), `fu_census_flags` (the parent's retraction flag
against the resolved status) and `fact_inputs` (parent, branch and protocol digests, the freeze, instrument digests,
dataset versions, code revision, the protocol's bootstrap replicates and seeds). `facts.json` (schemaVersion 1) holds
one scalar SQL query per fact with the file's sha256: `followup.status.*`, `followup.census.*`, `followup.strata.*`,
`followup.support.*`, `followup.function.*`, `followup.dependency.*`, `followup.numeric.*`, `followup.citedby.*`,
`followup.secondgen.*`, `followup.calls.*`, `followup.requests.*`, `followup.estimates.*` (from `fu_estimates`, with
their derivation) and `followup.coverage.*` (from `fu_coverage`). `verify-facts` re-runs every query read-only against
the snapshot (`corpus_facts.verify_export`).

## Export: `followup export --output DIR [--allow-entitled-text]`

Writes into a new directory:

- `compromised-claims.sqlite3` (closed): `claims` (one row per S1 or S2 locus: the citing sentence, the cited work,
  its status with source and date, the support verdicts and quotes, the function, the dependency and the judge
  receipts), `observations` (the status observations of the cited works), `receipts` (the judge calls behind the
  verdicts), `adjudications` (empty until a study adjudicates), `meta` and `data_dictionary`;
- `alerts.jsonl`: one record per affected claim and cited work, also kept in `fu_alerts`: the claim, the cited work,
  its status, the evidence (support, function, dependency, timing against the notice, receipts), the confidence
  "check suggested" and a suggested action for the authors of the citing review and for a study team. A claim
  resting on a work with a retraction, expression of concern or correction is affected unless the review notes the
  notice itself; an S2 claim is affected when its full-text verdict is contradicted or not-addressed;
- `propagation-graph.json`: compromised works, the claims citing them, and the later articles whose sentences restate a
  claim on the review's authority (confirmed second-generation matches), with the edges between them;
- `manifest.json`: the sha256, size and rows of every file, the branch, parent and protocol digests, the freeze and
  the redaction applied.

A public export drops entitled text: entitled citing sentences become `sha256:<hex>`, their section trails are removed,
and quotes from entitled sentences or evidence are dropped. `--allow-entitled-text` makes a private export that keeps
them; never share it.

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

Abstract silence and failed or missing downloads never count as unsupported: an unassessable unit keeps its reason
and goes into the coverage tables, never into a numerator.
