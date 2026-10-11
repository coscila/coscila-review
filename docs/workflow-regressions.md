# Workflow regression applicability

This register maps the externally reported W1–W46 issue identifiers to this
repository's independently implemented controls. It is a traceability map,
not a claim that the external implementation or its data are present here.
Only synthetic repository fixtures are used. An automated test listed below
covers the named invariant; it does not prove scientific accuracy on every
paper or successful live provider/deployment operation.

Paths in the implementation column are relative to `src/ocsl_review/`; test
paths are relative to `tests/`. “Guidance” means a reviewer obligation, not an
automatically enforced scientific decision. “Outside scope” identifies a
component this companion does not implement.

| ID | Failure to prevent | Applicability and implementation | Regression evidence or remaining boundary |
| --- | --- | --- | --- |
| W1 | PDF word geometry corrupts printed references | Applicable: `ingest.py` retains text and geometry-derived citation structure; identity uses printed strings without guessed repairs | `test_citations.py` real PDF geometry cases; `test_ingest_render.py` extraction limits |
| W2 | Numeric ranges retain only endpoints | Applicable: `citations.py` scans then expands complete groups, including `[1-10]` | `test_citations.py::test_ten_references_are_all_bound_and_missing_members_block_exact`; mixed-list/dash cases |
| W3 | Bibliography splitting discards continuation text | Applicable: `ingest.py` retains numbered-entry continuations | `test_citations.py::test_numbered_bibliography_continuations_preserve_all_paragraphs` |
| W4 | Exponents and notes become spurious citations | Applicable: `citations.py` validates superscript context and bibliography evidence | `test_citations.py` negative controls and real PDF superscript cases |
| W5 | Citation marker conversion defeats footer handling | Applicable principle: `ingest.py` preserves original geometry/text and recognizes markers without destructive marker tokens | `test_citations.py::test_real_pdf_superscript_range_two_columns_and_negative_controls`; ambiguous layout remains reported |
| W6 | Table citation cells lose row-local claims | Applicable: `ingest.py` and `claim_inventory.py` retain table row/cell loci | `test_citations.py` table reference column, geometry, and wrapped-row cases |
| W7 | A truncated abstract suppresses a better available source | Applicable: `scholarly.py` retains complete provider abstract candidates and independent full-source attempts | `test_fulltext.py` long structured abstract and failed-source fallback cases |
| W8 | Longer text is mislabeled as stronger evidence | Applicable: access and content completion are distinct from evidence requirements in `scholarly.py`, `literature.py`, and `audit.py` | `test_fulltext.py::test_preview_is_explicitly_partial`; `test_evidence_coverage.py` access/evidence-tier cases |
| W9 | Obtained, read, and used sources are conflated | Applicable: `literature.py` tracks acquisition, expected/read units, and source examinations; `audit.py` counts evidence used | `test_literature.py::test_record_reading_rejects_invented_units_and_does_not_judge`; `test_evidence_coverage.py::test_downloaded_and_abstained_does_not_mean_checked` |
| W10 | Access availability biases the reported evidence direction | Applicable control: all entries and access gaps persist; adverse and favorable verdicts use the same examination gates | `test_literature.py` every-reference and outage cases; `test_evidence_coverage.py::test_unread_and_partial_sources_require_abstention_in_both_directions`; scientific selection bias still needs review |
| W11 | Evidence-tier assumptions silently determine a score | No aggregate literature quality score is implemented; `audit.py` exposes categorical decisions and denominators | `test_evidence_coverage.py` separate correctness/completeness and confidence-ceiling cases; weighting a new score would need separate validation |
| W12 | Positive support exceeds the evidence actually used | Applicable: `audit.py` validates requirement, acquisition, reading, identity, and tier together | `test_evidence_coverage.py::test_full_text_requirement_applies_to_evidence_used` |
| W13 | Title-only matching certifies identity | Applicable: `scholarly.py` requires distinct providers and multiple fields, including printed-entry corroboration | `test_fulltext.py::test_identity_requires_distinct_providers_and_complete_shared_fields` |
| W14 | Same author/year silently repairs to the wrong paper | Applicable: original printed identity remains binding; mismatched titles or venue fields prevent strong agreement | `test_fulltext.py` identity and DOI-punctuation tests; `test_evidence_coverage.py::test_weak_identity_needs_abstention` |
| W15 | PMID-keyed storage drops duplicate bibliography entries | Applicable: `literature.py` stores entries by manuscript reference ID and deduplicates source artifacts separately | `test_literature.py::test_every_reference_survives_shared_identifier_and_resume` |
| W16 | A dead publication-notice route appears reassuring | Applicable: `scholarly.py` checks structured PubMed relationships against known-positive controls; failed surveillance remains unknown | `test_fulltext.py::test_known_positive_route_must_find_actual_notices`; live probes remain deployment acceptance work |
| W17 | One metadata provider yields a content verdict | Applicable: identity agreement is distinct from source reading; `record_assessments` never synthesizes judgments | `test_fulltext.py` identity gates; `test_literature.py` reading-without-judgment case |
| W18 | Recalled identifiers substitute for verified records | Applicable: identifiers are validated, provider DOI identity is checked, and unresolved references remain present | `test_scholarly.py::test_normalize_doi`; `test_fulltext.py` wrong/invalid delivered DOI cases; reference-integrity skill prohibits recalled repairs |
| W19 | Severity agreement hides changes of direction | Applicable to calibration: `calibration.py` preserves initial and repaired decisions and direction | `test_calibration.py::test_repair_retains_both_readings_and_direction` |
| W20 | Classification/severity choices hide sensitivity | Guidance: calibration reports disaggregate error types and disclose denominator/weight choices; no weighted paper ranking is supplied | `test_calibration.py` false-support and false-adverse cases; label-sensitivity studies require a separately specified benchmark |
| W21 | Abstentions disappear from denominators | Applicable: `audit.py` and `calibration.py` retain unresolved, inaccessible, and abstained counts | `test_evidence_coverage.py` abstention and empty-assessment cases; `test_calibration.py` empty and error cases |
| W22 | An enriched stress sample is presented as population prevalence | Guidance: calibration skill distinguishes adversarial fixtures from population estimates | No population sampling estimator is implemented; fixture pass rates must retain their stated population |
| W23 | Noisy differences become a paper ranking | Outside scope: no cross-paper ranking is implemented; peer-report skill prohibits unsupported rankings | A future ranking method needs its own uncertainty analysis and validation |
| W24 | Narrative numbers disagree with tables or categories | Applicable: summaries and rendered evidence appendices derive from the same validated graph | `test_evidence_rendering.py::test_every_locus_and_enum_survives_rendering`; stale summary rejection |
| W25 | Low-confidence access supports an adverse accusation | Applicable: `audit.py` applies reading and identity gates in both verdict directions | `test_evidence_coverage.py::test_unread_and_partial_sources_require_abstention_in_both_directions` |
| W26 | An uncited bibliography entry becomes an automatic support error | Applicable distinction: bibliography accounting is separate from bound citation loci and citation-required claims | `test_evidence_coverage.py::test_uncited_required_claim_does_not_hide_behind_other_support`; reference-integrity skill requires complete binding before declaring an entry uncited |
| W27 | Identity repairs change rates without showing old judgments | Applicable: source/identity changes invalidate dependent evidence; calibration retains pre/post repair decisions | `test_evidence_coverage.py::test_scope_or_identity_edits_invalidate_recorded_evidence`; `test_calibration.py` repair case |
| W28 | Empty output passes vacuously | Applicable: `audit.py`, `literature.py`, and `calibration.py` reject affirmative empty coverage | `test_evidence_coverage.py::test_empty_and_missing_assessments_never_pass`; `test_calibration.py::test_empty_case_set_is_blind` |
| W29 | Stale files are reported as fresh success | Applicable: managed graph/source digests, package guards, and render manifests | `test_literature.py::test_source_tampering_and_stale_graph_prevent_publication`; `test_evidence_rendering.py` fresh manifest and stale-summary cases |
| W30 | Slicing category labels changes their meaning | Applicable: closed schema enums and full-locus rendering preserve labels | `test_evidence_rendering.py::test_every_locus_and_enum_survives_rendering` |
| W31 | Provider HTML entities damage displayed metadata | Applicable: `scholarly.py` decodes display text and retains raw provider metadata | `test_fulltext.py::test_title_decoding_retains_raw_provider_strings` |
| W32 | Figure typography rules conflict or replace visual inspection | Outside scope for a new figure generator; results-display skill requires inspection of the real display | Existing DOCX/Markdown reports retain data and rows; no claim of publication-figure QA is made |
| W33 | Figure geometry collisions survive generation | Outside scope: this change adds no scientific plotting/layout engine | Visual inspection remains required for supplied figures; captions alone do not prove inspection |
| W34 | PDF floats or overflow hide content | Outside scope for PDF report production; applicable preservation principle for existing DOCX/Markdown output | `test_evidence_rendering.py` complete-row rendering; `test_ingest_render.py` valid DOCX and atomic bundle rollback |
| W35 | Missing wrappers lead to an unreported degraded workflow | Applicable: runtime skill snippets invoke real help; launcher/runtime failures remain failures | `test_launcher.py`; `test_environments.py::test_cmd_run_prefers_passthrough_argv_over_subcommand_name`; no fallback success branch |
| W36 | A blocked tool is bypassed to send confidential content | Applicable: validated scholarly context and egress checks precede all provider I/O | `test_scholar_context.py` invalid/digest-bound context cases; `test_fulltext.py::test_acquisition_policy_gate_precedes_transport` |
| W37 | Generic page fetches stand in for structured records | Applicable: provider-specific JSON/XML parsers, directed notice relationships, and complete document checks | `test_fulltext.py` malformed/empty source and structured notice cases; remote access messages cannot be full text |
| W38 | Missing binaries or unwritable caches masquerade as success | Applicable: bounded local extractor fails explicitly; chat environments must pass probes and remain isolated | `test_ingest_render.py` no-silent-fallback and timed extractor cases; `test_fulltext.py` bounded child case; `test_environments.py` failed-probe cleanup |
| W39 | Shell networking bypasses a tool's policy limits | Applicable: acquisition goes through `ScholarlyClient` authorization and validated direct HTTPS, not shell download scripts | `test_fulltext.py` egress, redirect, DNS, and credential-isolation cases; literature workflow forbids ungated retries |
| W40 | Managed skill mirrors are edited as source | Applicable: repository skills are the source; deployment installation remains an operator transaction | `test_skills_integration.py` modified-mirror protection and rollback; this change does not modify installed mirrors |
| W41 | Partial credit hides false adverse verdicts | Applicable calibration distinction: false-adverse and false-support errors are separately visible | `test_calibration.py::test_false_adverse_verdict_is_measured`; no partial-credit paper score is introduced |
| W42 | Truncation changes populations across report sections | Applicable: complete source extraction and full-locus rendering avoid silent row/section caps | `test_fulltext.py::test_complete_article_contains_late_long_passages_tables_captions_and_supplements`; `test_evidence_rendering.py` every-locus case |
| W43 | An artifact has no reproducible generator provenance | Applicable: `rendering.py` manifests include generator/code hashes and source dependencies | `test_evidence_rendering.py::test_manifest_hashes_fresh_outputs_and_uses_private_modes` |
| W44 | Skills promise filenames the workflow never emits | Applicable: affected descriptors/catalog now use canonical `.ocsl-review/` artifacts; runnable commands are documented | `test_skills_integration.py::test_catalog_and_all_runtime_contracts_validate`; `test_cli_workflow.py` prepare/render case |
| W45 | Citation occurrences, expanded members, and bibliography entries share a denominator | Applicable: `literature_status` and audit coverage expose separate counters | `test_citations.py` ten-member binding; `test_literature.py` duplicate-entry accounting |
| W46 | A scanner accepts a token the expansion parser cannot interpret | Applicable: `citations.py` owns whole-group scanning and expansion together, including conjunction/list variants | `test_citations.py::test_range_and_list_variants_reach_the_scanner`; malformed groups never partially parse |

Additional regression coverage checks complete range interiors, including
superscript/run-split markers and incomplete bibliography ranges; every
claim–citation occurrence–reference member needs its own assessment. Freshly
acquired source bytes still require complete reading and a separate scientific
judgment. Targeted gap sources keep their own source examinations and do not
inflate the manuscript bibliography denominator.

Structured JATS/publisher HTML extraction adds direct target binding, separate
printed labels, text-tail preservation and single ownership of citations in
paragraphs, captions, table cells, lists, boxes and footnotes. Synthetic cases
in `test_structured.py` cover range interiors, repeated loci beyond three
sentences, renumbered HTML identities, unresolved targets, unsafe XML entities
and non-article pages. `test_census.py` exercises the original
`fixtures/citation_census_canary.xml` through ingestion, independent source
identity, every bound member, immutable observations and SQL/hash facts export.

Census regression cases additionally cover complete evidence beyond old text
caps, exact Unicode quotations, missing reading units, independent abstract and
full-text histories, task/schema/source tampering, worker revision changes,
native jobs, reconnection to replacement jobs, bounded wait files, retries that
exclude accepted loci, confidentiality gates and unknown token counts. These
tests establish mechanics using explicitly synthetic providers/models; they
cannot establish live-model accuracy, population prevalence or scientific gold.

Run the dependency-light suite from a source checkout with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m ocsl_review.cli catalog validate
PYTHONPATH=src python3 -m ocsl_review.cli --help
```

These commands do not constitute live MCP or deployment acceptance. See
[acceptance.md](acceptance.md) for those separately scoped checks.
