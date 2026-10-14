<!-- SPDX-License-Identifier: Apache-2.0 -->
# Corpus protocol and acceptance criteria

## Preserve the population and units

Freeze inclusion rules and provider queries with dates, pagination, article
types and excluded/unavailable outcomes. Keep article versions distinct and
resolve their relationships. Preserve raw source documents, raw provider
responses, normalized identities, reference slots and every citation locus.

A printed reference slot belongs to one citing article version. A canonical
cited work may be shared by many slots. A locus binds a claim and its location
to a slot; numerical ranges create one locus per member. A judgment belongs to
a locus, evidence version, analysis and protocol. Counts of these entities must
never substitute for each other in progress or reporting.

## Extraction and identity before inference

Prefer structured publisher HTML or JATS; retain source hashes, parser version,
anchors, numbering, location and original text. Count all bibliography anchors
including display material and assert ownership by exactly one extracted block.
Validate tail order, nested inline elements, ranges and version-renumbered
references against an independent source. Use a validated fallback for a
PDF-only paper and label its limitations; do not silently exclude it.

Resolve DOI/PMID/PMCID candidates before fetching their evidence. Identifier
agreement must concern the same work: a citing page's PubMed link can itself be
wrong. Preserve candidates and resolution evidence, never let an unverified
PMCID choose another paper's full text or publication-status record. Accept
provider-declared journal aliases and corroborated ISSNs as identity evidence,
not guessed venue expansions. Preserve genuine unresolved conflicts.

Deduplicate acquired work versions, not citation slots or occurrences. Cache
successes and explicit failures with source/provider, retrieval timestamp,
status, licence/access metadata, byte count and SHA-256. Use one recorded
provider priority policy across comparison groups; distinguish access refusal,
rate limits, transient failure and a missing record. Retry only retryable
outcomes with bounded backoff.

## Evidence and assessment

Keep full acquired abstracts and full text. Record completeness and extraction
limitations in machine-readable fields, with the actual source passed to the
worker. A selected excerpt remains an excerpt even if its parent PDF is stored.
For a claim requiring methods, numbers, causality, priority or exact quotations,
obtain adequate full text or abstain. Absence from an abstract is not evidence
of absence from the paper. Never merge separate evidence tiers into one score
without a declared aggregation rule.

Pin the protocol, prompt hash, schema, provider/model, reasoning and sampling
settings, item order where relevant, evidence hashes, task ID and attempt ID.
Qwen judgment workers receive evidence and return schema-constrained records,
including quotes and uncertainty. Validate quote substrings and identity links;
preserve rejected responses and diagnostics. Reading declarations are validation
signals, not proof of model attention. Keep independent validations blind to
earlier judgments when the study calls for that design.

## One controller, durable execution

The controller owns one study specification and maintains progress from stored
state. Store stage and task outcomes before announcing completion. Use one
configured aggregate provider budget, bounded chunk submission and incremental
result imports. Group documents for acquisition efficiency while retaining
per-locus judging identities. Do not launch a separate free-form research agent
for every reference or hold the corpus in the controller's conversation.

Before the full run, exercise representative papers through **accepted real
judgments and facts export**, including an unavailable source, a retry and a
checkpoint/resume. A successful download or mocked dispatch is insufficient.
Benchmark the current task geometry, then continue the approved full manifest;
the canary is not an alternative deliverable. If the maintained coordinator or
authorization handoff is absent, record the exact blocker rather than claiming
the skill itself supplies that capability.

Define stop conditions by effect: isolate a missing source and continue other
work; stop an affected stage on systematic wrong identity/extraction, changed
inputs or authorization denial. Explicit user Stop checkpoints and cancels
managed work. On reconnection, query durable state and import finished results
before retrying incomplete items. Chat text and live broker IDs are not portable
scientific checkpoints.

## Facts and completion

Report population counts, acquisition by tier/provider/year/stratum, accepted
assessments, pending/rejected/blocked outcomes and uncertainty. Predeclare each
estimand and denominator; an uncited entry is an integrity observation, not an
unsupported citation. Account for clustering within reviews and reuse of cited
works. Report matched-locus tier transitions, validation uncertainty and actual
token/throughput receipts rather than extrapolated promises.

Every numerical finding must trace to a snapshot hash plus SQL or versioned
analysis code, parameters and random seed where relevant. Tables, figures,
prose and provenance must use the same fact definitions. Check rendered report
numbers against that store. Completion means all eligible units have explicit
outcomes and all requested deliverables exist; scientific uncertainty may remain
and must be quantified rather than removed from the denominator.
