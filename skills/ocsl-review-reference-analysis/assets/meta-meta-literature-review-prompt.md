<!-- SPDX-License-Identifier: Apache-2.0 -->
# meta meta literature review

Use Claude as the main controller and load the saved advanced-orchestration
preset **meta meta literature review — v2**. The preset stores Qwen worker assignments;
the main model and this study prompt are selected separately in the Lab.

## Controller prompt

Read the installed **ocsl-review-reference-analysis** skill, including its
workflow lessons and database/fork instructions. Conduct the complete
oncology citation-support meta-meta review and produce a reusable literature
database plus the scientific paper. Use the installed review and writing
companions and recorded advanced orchestration. Start by identifying whether
this is a new study, continuation of an existing study, or a new analysis of an
inherited corpus. Record that choice in the database and preserve provenance.

This prompt's scientific scope and manuscript specification apply to the oncology
citation-support study. If the user requests a different question in a fork,
replace those two sections with that question's protocol and deliverables. Use
the inherited database as evidence, configure task-appropriate worker assignments
and record new schemas and analysis identity. Do not rerun the original census
or require its paper format merely because the parent used this template.

Use the skill's workflow lessons and limits as the methodological baseline.
Counts are determined from verified source records, never assumed or set as
completion targets. Supplied earlier judgments are comparators, never new
results or human ground truth.

### Scientific scope

- First online publication from **2020-01-01 through 2026-09-09**, inclusive.
  Cited works are not restricted to that window or to review articles.
- Primary comparison: publisher-labelled **Nature Reviews Clinical Oncology
  Review / Review Article** versus **Frontiers in Oncology Review**.
- Also enumerate and assess Frontiers **Mini Review** and **Systematic Review**
  as separate secondary strata, scheduled after the primary comparison. Retain
  publisher labels separately from deposited article types. Exclude non-review
  material from the main population; record retractions, notices and inaccessible
  eligible reviews explicitly. Determine counts from verified source records.
- Measure all bibliography entries and all attributable citation occurrences.
  Preserve separate review, reference-entry, unique-work and locus denominators.
  Repeated citations share acquired source material, not context-specific verdicts.
- Preserve abstract observations and targeted, matched full-text follow-up,
  source versions, verified quotes, uncertainty and reasons. In v2, an explicit
  numerical or methodological statement can establish alignment with a complete
  abstract. Missing detail requires escalation. Do not require full text merely
  because the classifier sees a number; do not infer absence from the paper from
  abstract silence. Label full-text excerpts honestly and reserve `not-addressed`
  for complete full-text coverage. A full-paper census of every locus is a
  separate scientific scope and cost, not an implicit consequence of pairing.
- Pin eligibility, extraction/identity versions, evidence policy, class mapping,
  aggregation, validation and uncertainty methods before judging. Apply one
  instrument to both cohorts. Use review-clustered uncertainty and report access
  coverage and subtype sensitivities. Do not force a journal ranking or an
  expected numerical result.

### Responsibilities and execution

Claude owns executable workflow code, database transformations, analysis,
scientific decisions, manuscript prose and final figures. Use maintained programs
first. Qwen workers read evidence, extract requested records, judge, replicate and
report defects; they do not write or modify executable code. Discover actual
worker IDs and capabilities from the configured broker. Use bulk inference for
independent assessment tasks and reserve tool-driven agents for tasks that need
them. The three configured roles are not three independent model families.

The configured validation is **Qwen-only**: selected adversarial checks, explicitly
blind replicates, matched tier transitions and a predeclared stratified sample.
Keep extremes separate from population calibration. Do not claim Claude,
OpenAI or human benchmark judgments unless the study is explicitly amended and
those observations actually exist. Claude remains the controller and author.

Load the existing approval/run state before planning again. Continue unchanged
authorized work under the existing lifecycle. Check the actual installed
acquisition, evidence, worker and public-material authorization contracts before
production. Prove a bounded complete path through accepted judgments and exported
facts, then measure workload, tokens and throughput. The pilot is an execution
check inside the full census, not the deliverable. Use the maintained
`ocsl-review corpus` commands: `init`, `import-manifest`, `import-legacy`, `import-snapshot`,
`enumerate`, `acquire`, `prepare`, `calibrate`, `execute`, `run`, `resume`,
`validate`, `status`, `export`, `snapshot`, `verify` and `branch`. Read their installed help
and `docs/corpus-review.md`; use `execute` for the maintained full-workflow loop,
do not reconstruct a bespoke corpus coordinator in the workspace. Retain the
eligible manifest, shared source library and durable per-locus task queue.
Report failed deployment prerequisites precisely rather than inventing commands
or bypassing a denied transport. The template's public-literature permission
applies to approved execution and acquired public sources; private attachments
retain their existing restrictions.

The template's primary role uses Qwen reasoning `none`; targeted full-text
escalation uses `low`; selected blind replication uses `none`. Discover the
actual worker IDs and pin their revisions. Start with one item per call and an
aggregate capacity of 60 shared across all roles. These are launch settings,
not a claim about measured capacity or throughput.

Run the bounded calibration on up to 300 unique loci using none/one-item,
none/four-item, reordered none/four-item, and low/one-item variants: at most
1,200 item judgments before separately recorded bounded format repair. Enable
four-item primary calls only with at least 99% valid individual items and 95%
exact paired verdict agreement against the single-item baseline and the
reordered position check. Otherwise keep one-item calls. After the primary
population is available, use `corpus validate` for the frozen blind sample
under the separate 1,000-unique-locus cap. Preserve its stratified and targeted
selection labels and compare them separately. Do not automatically expand that
sample on later invocations. Report actual task, call and token budgets.
Agreement is stability, not accuracy.
`calibrate` and `validate` without `--execute` only prepare/report; use
`--execute --until-complete` for the maintained polling loop with durable
recovery and a provider-pause stop. Do not
claim the live calibration has passed until those measurements exist.

Use bounded submissions and the runner's normal continuation. Persist
accepted observations, retries, provider/model receipts and phase checkpoints.
Advance the complete approved manifest while recording item-local failures.
Stop the affected stage for systematic extraction or identity errors. Follow the
broker's tracked-work lifecycle, joining or cancelling jobs as required; a
detached heartbeat is not a managed workflow. Show measured progress separately
for reviews, entries, loci, verified sources, evidence tiers and accepted
judgments. Do not extrapolate the old 40-item-batch throughput to a new per-locus
or complete-paper instrument without measurement.

Validate each returned item independently, including exact quotations from the
named supplied units. Preserve accepted peers when another item fails; retry
only bounded format failures, never a missing-evidence condition by resending
the same source. Do not require confidence scores or a complete unit-ID echo.
Keep old v1 and historical scores under their original protocols; they are
comparators and reusable provenance, not completed v2 judgments.

### Reusable database and future forks

Maintain a stable study identity and portable database containing the literature
inventory, identity/provenance records, immutable source artifacts and text,
sections/citation locations, all observations with prompt/model/source versions,
processing states and reproducible analysis outputs. Preserve raw inputs and
alternative observations. New questions use new analysis namespaces and declared
eligibility; they do not overwrite the original census.

For a fresh study, acquire inputs and generate results for that study. For an
explicit continuation, reuse verified same-run work. For a downstream fork, verify
the inherited snapshot and reuse its source library with provenance; create a
new analysis and retrieve only genuinely missing evidence. Do not represent
inherited judgments as newly generated, or assume citation-support labels answer
another scientific question. Database availability does not establish full-text
coverage or source redistribution rights.

Use `corpus snapshot` and `corpus verify` for the SQLite/database-and-source
handoff, data
dictionary, protocol, software/prompt versions and checksums under `results/`.
Use the Lab's normal chat fork and `corpus branch` to support a
different research question. Keep credentials, broker job ownership and runtime
permissions out of the portable scientific bundle. A fork receives fresh runtime
accounting and must establish its own authorized execution.

### Scientific deliverables

Personally write the complete manuscript and generate its figures from verified
facts. Organize it in five figure-led chapters: **pipeline/cost
and scaling; journal/year/stratum/topic/affiliation-country distributions;
extremes and anchored examples; instrument validation and paired evidence-tier
changes; reusable database and coverage**. Describe the actual Qwen-only
validation without suggesting an independent model-family benchmark.

Provide the literature database and source manifest, complete coverage accounting,
assessment/provenance history, reproducible SQL and analysis code, facts and
source-data tables, editable manuscript, inspected PDF, five figures and example
tables. Target the presentation to Nature Reviews Clinical Oncology without
adding an editorial-fit gate or contacting the journal. Never manufacture author
details or missing evidence. An unavailable source receives an explicit outcome;
an unfinished queue remains unfinished. Verify completion against the entire
manifest and every required deliverable.
