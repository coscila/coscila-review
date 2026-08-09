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
responses are capped at 8 MiB, and only HTTPS endpoints are accepted. Metadata
reconciliation is local once records have been retrieved.

Every network operation passes the egress policy described in
[Confidential review and egress](confidentiality.md).

## Public providers

The implementation uses these production endpoints and official documentation:

| Provider | Runtime base | Role | Official documentation |
| --- | --- | --- | --- |
| Crossref | `https://api.crossref.org` | DOI resolution, bibliographic search, deposited relationships, update and status signals | [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) |
| NCBI E-utilities / PubMed | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils` | PubMed search and PMID summaries | [NCBI E-utilities quick start](https://www.ncbi.nlm.nih.gov/books/NBK25500/) |
| Europe PMC | `https://www.ebi.ac.uk/europepmc/webservices/rest` | biomedical metadata, abstracts, licensed open full-text XML, references and citations | [Europe PMC REST service](https://europepmc.org/RestfulWebService) |
| DataCite | `https://api.datacite.org` | DOI records and search for datasets and research software | [DataCite API guide](https://support.datacite.org/docs/api) |
| OpenCitations Index | `https://api.opencitations.net/index/v2` | DOI-based incoming and outgoing citation links | [OpenCitations Index REST API v2](https://opencitations.net/index/api/v2) |

Operators should supply `OCSL_REVIEW_CONTACT_EMAIL` where provider etiquette
requests contact identification. `NCBI_API_KEY` is passed to NCBI when present.
Credentials belong in the deployment environment, never a tracked egress plan
or review package.

Provider terms, rate policies, schemas, and coverage can change. Before a
production deployment, consult the linked official pages and run the live MCP
acceptance suite. The table records intended provider contracts; it is not a
service-level guarantee.

## Retractions, corrections, and versions

Status lookup combines current Crossref and Europe PMC/PubMed signals. It
examines Crossref update relationships, Europe PMC retraction flags, and PubMed
publication types for retractions, errata, expressions of concern, corrections,
and preprints.

Retraction Watch data is available through Crossref's production REST metadata.
The companion therefore reads production Crossref update records rather than a
separate historical laboratory endpoint. Crossref documents the current feed
and its `update-to` records on its
[Retraction Watch integration page](https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/).

No metadata service is exhaustive. A returned state of `current` means no
configured provider supplied a contrary signal at that check time; it does not
prove the absence of an unindexed publisher notice. For consequential decisions,
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
- `full-text`: open sections were retrieved and still require an exact evidence
  locator and scope comparison;
- inaccessible: the source cannot support a verdict in this run.

An open full-text response is bounded to a limited number and length of
sections. It is an access mechanism, not an automatic support decision.
Copyright and license conditions still govern any source text stored in the
workspace; the graph supports a locator plus excerpt digest so lengthy copying
is unnecessary.

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
