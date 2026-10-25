# Reference and claim screen (`claim-check@1`)

`ocsl-review screen` is research-integrity tooling for authors, reviewers and study teams. It
screens one manuscript: it reconciles the citations with the reference list, identifies every cited
work, records reference integrity findings, and judges each citation against the sentence that
carries it. Every step leaves a receipt, and `ocsl-review replay` re-derives the result without a
model call.

Typical uses:

- an author's self-check of a draft before submission (confidential mode, the default);
- a reviewer's dossier for spot checks;
- a study team re-checking a revised manuscript with `screen --diff`;
- corpus studies, which reuse the same modules and literature-library schema.

## Running a screen

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" screen draft.docx --workspace "$PWD" \
  --evidence store.jsonl \
  --judge-url http://127.0.0.1:8000/v1/chat/completions --judge-model <served-model> \
  --retraction-watch retraction-watch.csv --retraction-watch-version <release>
```

| Option | Meaning |
| --- | --- |
| `--evidence` / `--library` | evidence store: JSON or JSON Lines of works with units, or a literature-library database |
| `--policy` | operator-managed egress policy (JSON); its digest is recorded with the run |
| `--public` | the manuscript is public (`public-literature` mode) |
| `--judge-url`, `--judge-model`, `--judge-location` | OpenAI-compatible judge; without a judge, citations are recorded as "not judged" |
| `--offline` | no provider requests; only local integrity classes |
| `--retraction-watch`, `--retraction-watch-version` | offline Retraction Watch copy, pinned by sha256 and declared release |
| `--diff` | an earlier `report.json` to compare against |

Inputs: Markdown, text, DOCX, PDF, JATS XML and publisher HTML. Each run writes a new directory
`.ocsl-review/screen/screen-<UTC>-<sha8>/` with `report.json`, `report.md`, `dossier.md`,
`egress.jsonl`, the run database `screen.sqlite3` and the evidence index `evidence.sqlite3`. All files
are mode 0600.

An evidence-store work looks like this (one per line in JSON Lines):

```json
{"workId": "w1", "identifiers": {"doi": "10.5555/x"}, "title": "…", "year": 2020,
 "units": [{"unitId": "w1#abstract", "kind": "abstract", "text": "…", "complete": true},
           {"unitId": "w1#p1", "kind": "full-text", "text": "…"}]}
```

## Pipeline

1. **Sanitise the document** (`sanitize.py`). Hidden content is removed before ingestion: DOCX
   runs marked vanish, hidden by a style, white or 1-point text; HTML/XML elements hidden by style
   or attribute, templates and comments; HTML comments and hidden spans in Markdown; invisible
   Unicode (zero-width, bidirectional and tag characters). Removals are recorded with kind,
   position, length and sha256, never the text. PDF hidden text is a declared limitation.
2. **Ingest** the sanitised copy. Author-year citations are detected in parenthetical and narrative
   form and resolved against the reference list at ingest (`author_year.py`, below).
3. **Identify** every reference in the evidence store: printed identifier first (DOI, PMID, PMCID),
   then exact title containment (`resolved_unverified`); conflicts and misses stay unresolved.
4. **Integrity** (`integrity.py`): local classes always; with network, the DOI registration check
   and dated publication status through the egress policy.
5. **Claims** (`claim_check.py`): one claim per prose sentence (and per cited table cell) with its
   0..n citations. Uncited sentences are flagged only when the claim inventory says they need a
   citation; they get a store-wide retrieval receipt with candidate works and no attribution.
6. **Evidence and verdicts**: for each (sentence, cited work) the cited work's units are ranked by
   BM25 (`retrieval.py`); full text is preferred (a claim-targeted window of at most 11,000
   characters), otherwise the complete abstract. Claim, context and evidence are sanitised again
   at text level (prompt-injection payloads are removed), then judged with citation-support@2
   semantics: one verdict per citation, exact quotes, explicit abstention, one repair pass,
   batches of at most 40 items per evidence tier, temperature 0.
7. **Report**: a deterministic `report.json` (digest `reportSha256`), `report.md` and `dossier.md`.

## Attribution rules

The judged unit is the sentence that carries the marker.

- Two sentences citing different sources: each citation is judged against its own sentence only.
- A clause supported by Y inside a sentence supported by X, cited at the end: both citations are
  checked separately against the whole sentence; there is no clause parsing.
- Uncited sentences between two citations of X get no attribution.
- Context sentences are supplied as context, with their own markers masked: a citation never
  carries over.

## Author-year citations

`author_year.py` detects `(Smith et al., 2020a; Lee & Park 2019)`, `Smith and Jones (2019)`,
`Lee's (2018a, b)`, `(e.g., García-López, 2015, 2016)`, `(WHO, 2019)`, `(Jones, n.d.)` and similar
forms, and parses author-year, numbered, inverted, compact, natural-order and organisation
reference formats. An item binds only when the surname(s), year and suffix match exactly one
entry; diacritics, particles ("van der") and hyphenation are normalised. Several equally good
matches stay ambiguous, and no match stays unresolved ("cited but not listed"), with hints such as
a one-year difference or a near spelling. A bare `Name (2020)` that matches no entry is treated as
prose, not as a citation.

## Integrity classes

| Class | Meaning |
| --- | --- |
| `listed-not-cited` | a reference-list entry no citation points to (never cited) |
| `cited-not-listed` | a marker that matches no entry |
| `ambiguous-citation` | a marker that matches several entries |
| `identity-unresolved` | the cited work cannot be identified; its support stays unsure, never "unsupported" |
| `doi-not-registered` | the handle service, the doi.org resolver and Crossref all answered 404 |
| `doi-check-inconclusive` | access denied (401/403), rate limited (429), server or network failure; never read as missing |
| `doi-endpoint-artefact` | one endpoint refused a DOI another resolves (informational) |
| `metadata-disagreement` | different work behind the identifier, first author absent or spelled differently, year, volume or first page |
| `impossible-year` | printed year later than the manuscript's own year |
| `duplicate-identity` | three or more entries point to one work |
| `repeated-entry` | two entries point to one work (informational) |
| `retracted`, `expression-of-concern`, `corrected`, `reinstated` | dated notices with source and dataset version |
| `uncited-claim` | a sentence that needs a citation and has none |
| `hidden-text`, `prompt-injection-payload` | content removed from the manuscript or from every judge prompt |

Publication status combines Crossref update relations, the scholarly status route and an offline
Retraction Watch copy. The scholarly result is stored as a content-addressed artifact next to the
dataset version it was read with. With no successful source the status stays `unknown`; with
sources but no notice it is `no-notice-found`, listing the sources, which is not a guarantee.

Findings are mechanical statements; none speaks about intent. No calibration set exists, so
every adverse finding and verdict reads "check suggested".

## Confidential mode and the egress ledger

`ConfidentialPolicy` (`confidential-manuscript`, the default) lets only identifiers (DOI, PMID,
PMCID) reach external providers, refuses free-text lookups before any request, and sends manuscript
and cited text only to an on-prem judge: `--judge-location on-prem` on a host the policy allows
(loopback, private addresses, or the policy's `onPremHosts`). Every decision, allowed or refused,
is appended to the run's `egress.jsonl`: destination, destination class, operation, argument names,
payload sha256 and policy digest, chained by hash. `ocsl-review replay` verifies the chain.

```json
{"policy": "claim-check-egress", "version": 1, "mode": "confidential-manuscript",
 "onPremHosts": ["gpu-node.internal"], "allowPrivateNetworks": true}
```

## Receipts and replay

- Retrieval receipt: query terms, index digest, scope (cited work or whole store), k, ranked unit
  ids and BM25 scores; replayable against the same index.
- Model receipt: request and response bytes stored by sha256, served model, settings,
  instrument digest, finish reason, usage.
- `ocsl-review replay --run <dir>` re-validates every stored response, compares the re-derived
  verdicts with the stored ones, replays every retrieval receipt and rebuilds the report to the
  same digest. It makes no model call.

## Revisions: `screen --diff`

`--diff <earlier report.json>` re-runs the pinned instrument and lists added, removed and changed
references, new and resolved findings (finding ids are stable across runs), changed verdicts for
the same sentence and reference, and status changes such as a retraction since the earlier screen.

## Storage

The run database is a literature library (literature schema) plus the additive extension in
`schemas/claim-check.sql`: `claims` with 0..n `claim_citations`, `retrieval_receipts`,
`integrity_findings`, `integrity_datasets`, `registry_checks`, `work_status_observations` and
`sanitization_records`, all append-only. `claim_loci`, `model_receipts`, `assessments` and
`assessment_evidence` hold the judged loci. The extension applies idempotently to an existing
library (`claim_check.ensure_schema`).

## Acceptance tests

| Plan item | Test |
| --- | --- |
| three attribution cases (Markdown, DOCX, JATS) | `tests/test_claim_check.py::AttributionCaseTests` |
| author-year precision ≥ 0.98 and recall ≥ 0.95 on ≥ 200 citations | `tests/test_author_year.py::LabelledCorpusAcceptanceTests` |
| a 300-locus report within 5 minutes (mocked judges) | `tests/test_screen.py::ScaleAcceptanceTests` |
| no manuscript text leaves in confidential mode | `tests/test_screen.py::ConfidentialEgressAcceptanceTests` |

The author-year fixture is generated deterministically as open JATS: xref targets are the gold
labels, the markup is stripped, and the Markdown rendering is ingested and scored link by link. It
contains author-side inconsistencies fixed before the first evaluation (year typos, a dropped or
superfluous "et al.", folded diacritics, particle variants, omitted suffixes and wrapped entries),
so a resolver that refuses a garbled marker loses recall and one that guesses loses precision. The
measured values are printed by the test assertion messages; compute them with:

```bash
PYTHONPATH=src:tests python3 -c "import tempfile, pathlib, author_year_fixture as F; \
  r = F.evaluate(range(1, 25), pathlib.Path(tempfile.mkdtemp())); \
  print({k: r[k] for k in ('gold', 'predicted', 'correct', 'precision', 'recall')})"
```

## Limits

- Retrieval is lexical (FTS5/BM25); a re-rank hook is available and recorded, but no embedding
  endpoint is verified.
- Claim extraction is sentence-level; model-based claim decomposition is out of scope.
- Hidden text in PDFs (white, tiny or invisible text) is not detectable from extracted text.
- Fresh judge runs are not deterministic; replay re-derives stored verdicts, it does not re-judge.
