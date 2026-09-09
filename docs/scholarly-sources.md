# Scholarly sources and MCPs

The companion adds two stdio MCP servers. Their names are deliberately scoped
so deployment integration can preserve `paper-search-mcp`, `ocsl_web`, BioMCP,
and every unrelated server already present.

## `ocsl_review_catalog`

The catalog server is local and read-only. It exposes seven bounded tools that
list skills, resolve a skill's runtime, list environments, list pinned source
identities, list official standards, and report a schema's path and SHA-256. It
does not read manuscripts or contact scholarly providers.

Its purpose is discovery: an agent can learn the supported review capabilities
and their declared runtime without guessing paths or importing mutable
deployment state.

## `ocsl_scholar`

The scholarly server exposes thirteen operations:

- `resolve_identifier`
- `resolve_reference`
- `reconcile_reference_metadata`
- `search_crossref`
- `search_pubmed`
- `search_europe_pmc`
- `get_publication_status`
- `get_corrections_retractions`
- `get_related_versions`
- `get_abstract_or_open_sections`
- `get_references`
- `get_citations`
- `resolve_dataset_or_software`

Results identify their provider, retrieval time, access level, and limitations.
Searches return at most 25 records, transport timeouts are bounded, provider
metadata responses are capped at 8 MiB, and only validated public HTTPS
destinations are accepted. Bounded reference/citation previews expose totals
when known and mark truncation or incomplete coverage. They are not the
manuscript bibliography universe. Metadata reconciliation is local once records
have been retrieved; the internal extensive workflow additionally corroborates
provider fields against the printed reference.

Every network operation passes the egress policy described in
[Confidential review and egress](confidentiality.md).

## Public providers

The implementation uses these production endpoints and official documentation:

| Provider | Runtime base | Role | Official documentation |
| --- | --- | --- | --- |
| Crossref | `https://api.crossref.org` | DOI resolution, bibliographic search, deposited relationships, update and status signals | [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) |
| NCBI E-utilities / PubMed | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils` | Search, PMID summaries, complete structured abstracts, and directed publication notices | [NCBI E-utilities quick start](https://www.ncbi.nlm.nih.gov/books/NBK25500/) |
| Europe PMC | `https://www.ebi.ac.uk/europepmc/webservices/rest` | biomedical metadata, abstracts, licensed open full-text XML, references and citations | [Europe PMC REST service](https://europepmc.org/RestfulWebService) |
| DataCite | `https://api.datacite.org` | DOI records and search for datasets and research software | [DataCite API guide](https://support.datacite.org/docs/api) |
| OpenCitations Index | `https://api.opencitations.net/index/v2` | DOI-based incoming and outgoing citation links | [OpenCitations Index REST API v2](https://opencitations.net/index/api/v2) |
| Unpaywall | `https://api.unpaywall.org/v2` | Open-copy PDF locations and version/license metadata for internal full-source acquisition | [Unpaywall API](https://unpaywall.org/api), [data format](https://unpaywall.org/data-format) |

Operators should supply `OCSL_REVIEW_CONTACT_EMAIL` where provider etiquette
requests contact identification. `NCBI_API_KEY` is passed to NCBI when present.
Credentials belong in the deployment environment, never a tracked egress plan
or review package.

Unpaywall is used only when a contact email is configured; no manuscript,
reviewer, or commit-author identity supplies a fallback. Document downloads
carry generic headers without provider credentials or a contact address, and
redirects receive fresh destination checks and egress authorization. Metadata
requests cannot redirect credentials to another origin. NCBI requests are
spaced by at least 0.35 seconds within each transport, including retries. This
does not coordinate independent processes sharing an IP; operators must
account for aggregate use under the
[NCBI usage policy](https://www.ncbi.nlm.nih.gov/books/NBK25497/).

Provider terms, rate policies, schemas, and coverage can change. Before a
production deployment, consult the linked official pages and run the live MCP
acceptance suite. The table records intended provider contracts; it is not a
service-level guarantee.

## Retractions, corrections, and versions

Status lookup combines Crossref and Europe PMC/PubMed signals and reads PubMed
`CommentsCorrectionsList` from structured EFetch XML. Notice direction is
preserved: an `ErratumIn` link concerns the target article, while an `ErratumFor`
link identifies the notice's subject. A Crossref `update-to` record attached to
a notice does not establish that the notice itself was retracted.

Retraction Watch data is available through Crossref's production REST metadata.
The companion therefore reads production Crossref update records rather than a
separate historical laboratory endpoint. Crossref documents the current feed
and its `update-to` records on its
[Retraction Watch integration page](https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/).

Before reporting `current`, the route must retrieve the target's structured
record and demonstrate notice detection using known-positive controls without
provider failures. Missing controls or a dead route leave surveillance
unproven and the state unknown unless a positive signal was found. Controls
exercise the same EFetch relationship path; they are not static claims of
service health. Their provenance is recorded in `scholarly.py`.

No metadata service is exhaustive. `current` does not prove the absence of an
unindexed publisher notice. For consequential decisions,
inspect the version of record and relevant notice directly. The
[ICMJE recommendations](https://www.icmje.org/recommendations/browse/manuscript-preparation/preparing-for-submission.html)
also place responsibility on authors to check references, support, and
retractions.

Related-version and citation-link data also require interpretation. A relation
field may be missing, a preprint may be linked incompletely, and a citation
index has a coverage boundary. The audit graph should record these limitations
and use an independence group rather than assuming every returned identifier is
a separate line of evidence.

## Access levels are evidence controls

Provider envelopes distinguish:

- `metadata-only`: enough to inspect identity or status fields, not a paper's
  substantive findings;
- `abstract-only`: potentially enough for a claim explicitly classified that
  way, but not for an exact method, quotation, number, subgroup, or other
  full-text question;
- `partial-text`: the MCP open-section preview contains at most twenty sections
  and 4,000 characters per section, with `complete: false`;
- `full-text`: the internal acquisition workflow retained a complete readable
  document, which still requires actual reading and scientific comparison;
- inaccessible: the source cannot support a verdict in this run.

The [extensive CLI workflow](literature-review.md) calls internal
`acquire_full_text` rather than using an MCP preview as complete evidence.
It attempts Europe PMC full article XML and eligible Unpaywall PDF locations,
retaining original bytes, hashes, complete text, and ordered blocks privately
inside the workspace. Documents have a separate 100 MiB limit and bounded
extraction time/output. XML tables, captions, footnotes, and late sections are
included without section caps. External supplements and graphics are
inventoried as remaining access/inspection limitations. PDF extraction needs
`pdftotext`; image-only pages prevent complete-text certification.

Complete abstracts remain available when source acquisition fails. Title and
abstract analysis can support an appropriately classified claim at lower
confidence; full-text-required claims still abstain. Identity, access, reading,
evidence used, and confidence are distinct controls. Neither an access level
nor metadata establishes support. Source storage and excerpts remain subject
to applicable rights and license conditions; a report needs only bounded
evidence excerpts and their verified locators.

## MCP launcher and schema contract

Both launchers require `OCSL_REVIEW_HOME` and `OCSL_REVIEW_STATE`. By default
they resolve Python through the current promoted `mcp-review` template for the
host platform. The tracked schema lock pins the canonical `tools/list` surface
for each server.

Render the two deployment values with:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" mcp render
```

The integration transaction merges exactly those values. Rendering or listing
a server does not demonstrate reachability.

## Required acceptance

For every enabled server, the doctor checks:

1. launcher resolution;
2. protocol initialization;
3. `tools/list`;
4. canonical tool-schema hash;
5. an unknown-tool error path;
6. deliberate timeout behavior;
7. a clean shutdown;
8. when requested, one bounded live tool call.

The same sequence must run from source launchers and from the actual staged OC
Science Lab configuration. Strict acceptance is complete only when both paths
pass and live calls were included:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" mcp doctor \
  --live \
  --staged-config /path/to/staged-config.json \
  --strict
```

Use a staged TOML path when that is the deployment's generated format. A
source-only probe, a probe without `--live`, or a passing unit test must not be
reported as complete MCP acceptance.

## Direct calls and review evidence

`mcp call` is useful for bounded diagnostics, but its output becomes scientific
evidence only after the review workflow records the returned provider and
access information in the audit graph and a reviewer inspects the relevant
material. Do not cite an MCP response itself as though it were the publication.
