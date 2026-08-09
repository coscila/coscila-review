# Claim-citation evidence model

A narrative peer review can be useful while still hiding the most important
facts about its own work: how much of the paper it covered, whether it read the
cited source, and which part of a compound sentence the citation supports. The
companion addresses that problem with a structured graph and derives prose from
the graph rather than treating prose as the record of verification.

## Artifact sequence

The review workflow produces related but distinct artifacts:

1. An ingested manuscript records the source digest, extraction coverage,
   paragraphs, sentences, tables, figures, citations, bibliography entries, and
   stable locators.
2. A review plan chooses applicable analysis passes and carries extraction and
   confidentiality state forward.
3. A claim inventory splits material statements into auditable clauses and
   records why each does or does not require outside evidence.
4. A claim-citation audit graph records reference identity, evidence access,
   claim-to-source comparisons, search provenance, and completeness decisions.
5. An audit summary calculates the answer and its denominators from the graph.
6. A review package connects graph evidence to anchored concerns and a revision
   ledger, then renders machine-readable and human-readable outputs.

The schemas under `schemas/` are the authoritative field contracts. They use
closed objects so misspellings and undeclared fields fail validation instead of
silently disappearing.

## Graph entities

### Manuscript and audit scope

The graph binds itself to a manuscript identifier and source SHA-256. It records
whether ingestion completed, how many sections were processed, whether the
audit is exhaustive or sampled, whether citation binding completed, and the
date through which literature searching is current.

### Claims

A claim is an atomic clause with a section/paragraph/sentence/clause locator.
It records:

- origin: external background, manuscript result, method, interpretation,
  reviewer-added statement, or another identified origin;
- type: fact, number, causal assertion, generalization, novelty, method,
  software, dataset, quotation, recommendation, or interpretation;
- whether a citation is needed and a non-empty reason for the decision;
- citation clusters bound to the clause and whether that binding is exact,
  ambiguous, missing, or inapplicable;
- the minimum access needed to decide it: full text, abstract, metadata, or no
  external evidence;
- whether the claim was actually processed.

Original results are not automatically treated as missing citations. Their
validity is examined through methods, statistics, internal consistency, and
reproducibility passes. External provenance is still required when the sentence
uses an outside method, software release, dataset, comparison, or literature
claim.

### Citation clusters and references

A citation cluster retains the exact manuscript location, displayed markers,
resolved reference identifiers, and processing state. A reference retains the
raw entry, identifiers, reconciled identity, resolution state, publication
status and check time, source role, independence group, and metadata problems.

`independenceGroup` prevents multiple reports of the same underlying work from
being counted as independent confirmation. A preprint and journal version,
papers reusing one cohort, or several analyses of one trial can therefore be
represented honestly without discarding their bibliographic identities.

### Verification links

Each claim-reference edge records the actual verification:

- access level: full text, abstract only, metadata only, or inaccessible;
- provider, URI, and check time;
- one or more source locators with an excerpt digest and an optional bounded
  excerpt;
- support verdict and a reason;
- comparison across species, population, intervention or exposure, comparator,
  outcome, direction, magnitude, causal strength, setting, and timeframe;
- quotation or numeric fidelity;
- confidence, abstention reason, and required human action.

The support vocabulary is deliberately more expressive than yes/no:

| Verdict | Meaning |
| --- | --- |
| `direct-support` | The inspected evidence addresses the clause at the material scope and strength claimed |
| `scope-caveat` | There is relevant support, but at least one important qualification must be made explicit |
| `partial-indirect` | The source is related or supports only part of the clause; it cannot carry the statement as written |
| `contradicted` | The inspected evidence conflicts with the clause |
| `not-addressed` | The cited source does not answer the claim |
| `abstain` | Available access or information cannot justify a support decision |

Metadata-only and inaccessible edges must abstain. An abstract-only edge must
also abstain when the claim's evidence requirement is full text. Positive
support requires a source location, and `direct-support` is invalid when any
recorded scope dimension is a mismatch.

### Completeness assessments

Support and sufficiency are separate. A source can directly support a narrow
claim while the manuscript's broader evidence selection remains inadequate.
Each citation-required claim therefore receives one completeness verdict:

- sufficient;
- missing citation;
- insufficient directness, scope, breadth, currency, or balance;
- not assessable.

The decision can cite dated search runs. Novelty claims marked sufficient must
be backed by searches from at least two providers. Source count alone is never
a sufficiency rule: roles, independence groups, claim breadth, contrary or null
results, replication, and date coverage all matter.

### Search runs and review assertions

A search run records provider, execution time, database names, date range,
result count, independent groups found, disclosure class, and a query digest.
Full confidential queries are not stored in the graph.

The review report is audited too. Factual statements introduced by a reviewer
must be evidence-backed or explicitly abstained; they cannot be relabeled as
opinion to escape verification. Methodological judgments and editorial
opinions remain identifiable as judgments rather than invented literature
facts.

## Denominator safety

`audit-summary.json` exposes totals and processed counts for citation clusters,
bibliography entries, and citation-required claims, plus the number of unique
sources checked at full-text, abstract-only, or inaccessible levels. These
counts let a reader distinguish “all 84 citations were examined” from “five
examples were sampled.”

The summary separately aggregates:

- direct support, scope caveats, indirect support, contradiction, lack of
  coverage, publication-status problems, and metadata problems;
- sufficient claims and insufficiency by missing citation, directness, scope,
  breadth, currency, and balance.

The status vocabulary is `yes`, `no`, `partly`, and
`not-fully-verifiable`. A `yes` is impossible while any mandatory verification
limit is present. Current blockers are:

- incomplete manuscript ingestion;
- a sampled rather than exhaustive audit;
- incomplete or ambiguous citation binding;
- an unprocessed citation cluster or citation-required claim;
- an unresolved bibliography entry;
- an inaccessible necessary source;
- a metadata-only source check;
- an abstract-only check for a full-text question.

This is more than display logic. Semantic graph validation rejects evidence
edges that could otherwise allow an unsupported positive conclusion, and the
summary builder checks again that no affirmative status escaped a blocker.

## Correctness is not sufficiency

Consider a statement that a treatment “improves survival in all adults.” A
single randomized trial in one narrow clinical subgroup might be cited
accurately and contain a real survival difference. The edge can record relevant
support while flagging population and breadth limitations. The completeness
record can then require broader or more qualified evidence. Neither decision is
lost in one citation-density score.

Likewise, a review article may accurately summarize a primary result but still
be an indirect source for a precise number. The graph preserves both the review
and primary article, their roles, and the exact passage examined instead of
letting a secondary citation obscure the original evidence.

## Reporting and methodological validity

Reporting-guideline checks ask whether readers received the information a
study type should disclose. Methodological appraisal asks whether the design,
analysis, and inference are credible. These are separate review passes and
separate findings. A complete checklist does not make a biased design valid,
and a sound study can still be reported too poorly to verify.

This separation follows the purpose of reporting guidance described by the
[EQUATOR Network's peer-review resources](https://www.equator-network.org/toolkits/peer-reviewing-research/).
Reference identity, direct support, and retraction checks also reflect the
[ICMJE manuscript-preparation recommendations](https://www.icmje.org/recommendations/browse/manuscript-preparation/preparing-for-submission.html),
while the companion's schema and verdict rules are its own implementation.

## Human responsibility

The graph improves traceability; it does not replace expert judgment. Human
action remains required for ambiguous manuscript structure, access obtained
under private subscriptions, scientific context not represented in provider
metadata, conflicts of interest, ethics decisions, journal-specific policy,
and final editorial disposition. Every abstention and material caveat must name
the next action rather than ending with an unexplained low confidence score.
