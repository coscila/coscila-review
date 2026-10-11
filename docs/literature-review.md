# Extensive reference and citation review

The workflow asks two separate questions: whether each existing citation
supports its claim, and whether the evidence selected for the claim is adequate.
It obtains source material and validates recorded judgments; it does not create
scientific verdicts automatically.

For systematic platform-worker measurements with pinned prompts, complete
evidence, resumable jobs, immutable tier-specific observations and SQL-derived
facts, continue with [citation-census.md](citation-census.md) after acquisition.
Its model outputs remain separate from expert audit-graph verdicts.

## Prepare the proposed scope locally

Work inside the selected chat workspace. `OCSL_REVIEW_HOME` points to the
complete source checkout. Deployment must already provide the mapped runtime
and trusted workspace context; do not rebuild environments or change deployment
settings from a review chat. The commands below use the checkout launcher. From
an installed skill, run them through its declared `env run --skill` wrapper.

For a supplied file named `manuscript.pdf`:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" review ingest manuscript.pdf --workspace "$PWD"
"$OCSL_REVIEW_HOME/bin/ocsl-review" review inventory --workspace "$PWD"
"$OCSL_REVIEW_HOME/bin/ocsl-review" review plan --workspace "$PWD"
"$OCSL_REVIEW_HOME/bin/ocsl-review" review literature prepare --workspace "$PWD" --json
```

Inspect `.ocsl-review/manuscript.json` ingestion coverage and citation
diagnostics before accepting its reference universe. `[1-10]` means ten
reference members in one citation occurrence. Repeated citations and duplicate
DOIs retain distinct manuscript loci and bibliography entries. Table reference
cells bind to rows. Ambiguous markers, missing range interiors, duplicate
bibliography labels, and unreadable regions must remain visible.

When supplied, local JATS XML and publisher HTML preserve direct reference
targets, printed labels, article identifiers, text tails and citations in
captions, table cells, boxes, lists and footnotes. Inspect `structuredSource`
anchor ownership and original `sourceIdentity` alongside canonical numbering.
Structural extraction does not itself establish scholarly identity or reading.

Show the user the prepared bibliography and citation counts. Propose reading
every accessible reference and investigating targeted missing or contrary
evidence, explaining that this takes longer than metadata checking. Ask for
confirmation of that extensive scope unless the user already explicitly
requested it. A broad request to “check the citations” alone does not supply
that confirmation. Continue local preparation while awaiting an answer, but
do not start dependent acquisition or fabricate consent.

After an actual acceptance, record its wording and the current digest. This
shell recipe prompts for the real answer; it must not be filled with an
invented affirmative response:

```bash
literature_scope_sha256=$(python3 -c 'import json; print(json.load(open(".ocsl-review/literature-audit.json"))["scopeSha256"])')
read -r -p 'Actual user confirmation: ' literature_confirmation_basis
"$OCSL_REVIEW_HOME/bin/ocsl-review" review literature confirm \
  --workspace "$PWD" --scope-sha256 "$literature_scope_sha256" \
  --accept --basis "$literature_confirmation_basis"
```

Use `--decline` instead for an actual refusal. A changed manuscript or
bibliography invalidates the old scope confirmation. Scope confirmation does
not authorize additional disclosure: every provider request still passes the
independent egress policy.

## Obtain and account for sources

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" review literature fetch --workspace "$PWD" --json
"$OCSL_REVIEW_HOME/bin/ocsl-review" review literature status --workspace "$PWD" --json
```

Fetch requires `LAB_WORKSPACE_ROOT` and a validated scholarly context bound to
this exact workspace, as described in [confidentiality.md](confidentiality.md).
The normal chat runtime supplies these. An operation allowlist must permit
`acquire_full_text`, identifier resolution, and publication-status checks for
the respective requests. Do not switch to an ungated HTTP script after a denial.

The client reconciles printed identity, checks publication notices, tries
Europe PMC complete article XML, and then uses available Unpaywall PDF locations
when `OCSL_REVIEW_CONTACT_EMAIL` is configured. It never infers a contact email
from a manuscript, commit author, or reviewer. Public HTTPS destinations and
redirects are validated. Metadata has an 8 MiB response bound; source documents
have a separate 100 MiB bound. Complete text extraction has no per-section cap.

Every bibliography entry remains in the ledger, including uncited entries,
duplicates, unresolved identities, failures, and inaccessible sources. A shared
identifier may reuse source bytes while retaining separate reference records.
Acquisition checkpoints survive interruption. Ordinary reruns skip accounted
entries; explicitly retry failed or refresh selected entries with:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" review literature fetch --workspace "$PWD" --retry
```

`--reference-id` may be repeated to select IDs from `status`. Retries and source
replacement invalidate judgments tied to changed source identity/content.
An explicit retry refreshes the requested acquisition; duplicate entries can
share the refreshed source within that invocation.
Provider errors are coverage limitations, not empty successful reviews.

For a lawfully available local paper already inside the workspace, select its
actual reference ID from `status`:

```bash
read -r -p 'Reference ID from status: ' literature_reference_id
"$OCSL_REVIEW_HOME/bin/ocsl-review" review literature attach paper.pdf \
  --workspace "$PWD" --reference-id "$literature_reference_id"
```

XML, readable PDFs, and explicit local UTF-8 text/Markdown are supported. An
abstract attachment must use `--access-level abstract-only`. Local inputs are
copied into private managed storage; shared inodes and paths escaping the
workspace are rejected. Attachment establishes access, not bibliographic
identity or support; a conflicting identifier explicitly present in XML is
rejected. Image-only PDF pages need OCR before complete text can be
certified. XML captions do not establish that graphic content was inspected;
linked media and supplements remain recorded limitations until separately
obtained and examined where relevant.

## Read the source and record the judgment

`status` returns each source's `documentPath`, `artifactPath`,
`expectedUnitIds`, and `readUnitIds`. Open the JSON at `documentPath`; it stores
the complete text and ordered blocks with IDs and locators. Read all blocks,
including late results, tables, captions, footnotes, and relevant supplements.
For long sources, inspect successive bounded chunks until every block has been
read. Split an oversized block for display and record that block as read only
after all its text was inspected. Tool-output truncation is not reading.

Write assessment JSON locally and submit it with:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" review literature record \
  .ocsl-review/assessment.json --workspace "$PWD"
```

The input may contain `referenceId`, `readUnitIds`, `verificationLinks`,
`completenessAssessments`, `searchRuns`, `reviewAssertions`, `referenceRecords`,
`identifiers`, and `claimRequirements`. Use only IDs from the current graph and
source artifacts. A reading-only record can contain `referenceId` and the unit
IDs actually inspected; it creates no verdict. Never populate `readUnitIds` by
blindly copying `expectedUnitIds`.

Each verification link needs a `claimId`, `citationClusterId`, `supportVerdict`,
`supportReason`, all `scopeComparison` dimensions, `quotationOrNumberCheck`,
numeric `confidence`, explicit `confidenceLabel`, `abstentionReason`,
`humanAction`, and `evidenceLocations`. Locations name actual `unitId` values
and optionally exact excerpts of at most 500 characters. The recorder verifies
excerpts against stored text and supplies source access, stable link IDs,
locators, and hashes. It rejects invented passages and stale bindings.

Use the closed contracts in
[`claim-citation-audit.schema.json`](../schemas/claim-citation-audit.schema.json)
and [`literature-audit.schema.json`](../schemas/literature-audit.schema.json).
`tests/test_literature.py` contains synthetic examples of record construction;
its scientific judgments are test data and must not be reused for real papers.

| Evidence decision | Required treatment |
| --- | --- |
| Complete relevant full text read, strong identity, material scope examined | May justify `very-confident`; the reviewer must still select the verdict and confidence |
| Complete abstract read, strong identity, claim explicitly `abstract-sufficient` | At most `confident`; title and abstract must actually address the claim |
| Metadata, incomplete text, uncertain identity, or unexamined source | `not-confident` and abstention on substantive support |
| Full-text-required claim with abstract-only access | `abstain`, identify missing evidence, and continue independent work |

The machine labels are `very-confident`, `confident`, and `not-confident`; the
report displays “very confident”, “confident”, and “not confident”. Availability
sets a confidence ceiling rather than a default favorable judgment. Recording
defaults an omitted label to `not-confident`. Negative judgments such as
`contradicted` and `not-addressed` require the same examination standard as
positive support.

If a qualitative claim can be settled from a complete abstract, explicitly
record a `claimRequirements` item with `claimId`,
`requiredEvidenceLevel: "abstract-sufficient"`, and a scientific `reason`.
Numeric, causal, quotation, methods/resource, and novelty claim types retain
their full-text requirement. Reclassifying a claim invalidates its earlier
assessments. A lower access level never automatically changes that requirement.

## Investigate adequacy separately

Use minimized concepts to search relevant providers for missing, broader,
newer, contrary, or independent evidence. Record dated `searchRuns`, including
query digest, disclosure class, database, result count, and bounds. Search hits
are candidates, not findings. A targeted update is not a systematic review.

Acquire a candidate DOI associated with a current claim as follows:

```bash
read -r -p 'Candidate DOI from the search: ' literature_gap_doi
read -r -p 'Affected claim ID: ' literature_claim_id
"$OCSL_REVIEW_HOME/bin/ocsl-review" review literature fetch --workspace "$PWD" \
  --additional-doi "$literature_gap_doi" --claim-id "$literature_claim_id"
```

These candidates enter `additionalReferences`, with their own identity and
reading requirements. They do not change bibliography totals and cannot
replace an existing citation's verification link. Record actual reading first,
then refer to their `sourceExaminationIds` in `completenessAssessments`. Explain
directness, scope, independence, breadth, currency, and balance using the
examined evidence. Both a sufficient verdict and an adverse omission finding
need evidence; unresolved access may require `not-assessable`.

## Validate and render current records

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" audit validate --workspace "$PWD"
"$OCSL_REVIEW_HOME/bin/ocsl-review" audit summarize --workspace "$PWD"
"$OCSL_REVIEW_HOME/bin/ocsl-review" review package --workspace "$PWD"
"$OCSL_REVIEW_HOME/bin/ocsl-review" review render --workspace "$PWD"
```

The graph is a projection of the managed ledger and content-addressed graph
snapshots. Use the record command to update assessments. Publication guards
reject stale graph edits, changed source bytes, mismatched summaries, and
unmanaged assertions of complete reading.

Canonical artifacts are `.ocsl-review/manuscript.json`, `review-plan.json`,
`literature-audit.json`, `audit-graph.json`, `audit-summary.json`, and
`review-package.json`, all under `.ocsl-review/`. The report stem is
`.ocsl-review/review-report`; rendered outputs include JSON, Markdown, DOCX, and a
manifest of fresh output hashes. Inspect the actual rendered output.

Report citation occurrences, expanded members, bibliography entries,
additional sources, documents obtained, documents read, and evidence used as
distinct counts. `acquisitionAccountedFor` means every entry has an acquisition
outcome; it does not mean every source was read or every claim assessed.
`citedCorrectly` and `citedEnough` retain separate conclusions and blockers.
Keep missing sources and abstentions visible, and never describe a partial run
as a fully verified literature review.
