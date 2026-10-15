<!-- SPDX-License-Identifier: Apache-2.0 -->
# The earlier Claude audit: working solution and limits

This is an original methodological summary of the user's local research
artifacts, inspected on 2026-10-14. It neither redistributes those artifacts nor
copies their implementation or publisher text. Do not require an absolute host
path to use this skill; identify supplied baseline artifacts by the provenance
below and record their location in the study manifest.

## Which session and which result

Claude Code session `268ee91b-1eae-4810-a7ce-604f17f8275f`, September 11–12,
2026, workspace `fd685429-1efa-489e-b79f-b271b5d0c565`, audited and corrected an
earlier Qwen census. It produced *Citation support in oncology review articles
at scale: a census of two journals with one language-model judge*, an audit,
a defect post-mortem, a facts store and a score database. This was an audit of
existing corpus data and inference, not a new inference of every item by Claude.

The released `scores_db.sqlite3` contains 5,564 reviews and 670,938 reference
entries: 5,338 Frontiers in Oncology reviews / 624,175 entries and 226 readable
Nature Reviews Clinical Oncology reviews / 46,763 entries. Reference entries
are not distinct cited papers or citation occurrences. The original article
inventory included inaccessible reviews and publisher strata which must remain
visible in a new study. Full-text follow-up covered 8,969 entries, not all
670,938. These counts describe the historical artifact, not a future quota.

## What made it work at scale

1. Reuse the existing databases and downloaded documents. Open originals
   read-only, hash before and after, and write corrections and analysis to new
   tables/artifacts. Independent audits reconstruct inputs and recompute facts.
2. Parse Frontiers publisher JATS and NRCO publisher HTML or PMC manuscripts.
   Serialize text, then each child's text followed by that child's tail, so
   superscript citation markers stay with their actual sentence. Cover table
   cells, captions, boxes and lists and expand numerical citation ranges.
3. Bind sources across manuscript versions by DOI, then checked title, using
   printed labels only when numbering is independently verified. An initial
   label-only correction itself misjoined 1,550 entries in 20 renumbered papers;
   identifier-first mapping fixed that mistake and affected papers were rerun.
4. Use the same abstract provider policy across compared journals, preserve
   retrieved material and metadata, and use a separate full-text evidence field.
5. Execute Qwen judgments in a bounded concurrent pipeline with SQLite state,
   raw responses and request receipts. Claude audits, repairs code, coordinates
   targeted reruns and constructs the final statistical narrative.
6. Generate figures and prose from SQL-backed facts and frozen method outputs.
   The historical paper's five figures cover pipeline/cost, distributions,
   extremes, validation and the reusable score database.

The historical run used 40-entry paper batches, temperature zero and no reasoning
for the main judge. The original judge configuration used 160 concurrent calls;
the corrected audit reruns used 60. Its recorded audit reruns processed 36,331
entries in 902 seconds and 69,047 in 1,347 seconds (40.3–51.3 entries/second).
These durations describe inference phases over prepared inputs, not a complete
fresh acquisition-to-manuscript run. These are historical measurements, **not
v2 performance results**. Per-locus tasks, complete abstracts and changed
validation have different costs. Record response-side model identity; an
endpoint alias such as `chat` does not identify the underlying model.

## Lessons from the later slow run

The October 14 investigation found structural work amplification, not evidence
that the earlier corpus was impossible. The inspected production state contained
2,493 dispatchable abstract tasks, including 1,042 (41.8%) whose claim types
triggered a blanket full-text requirement. All 565 accepted observations in that
specific-claim subset were abstentions by construction. Calls to this subset
used 55.7% of the 20.4 million tokens in completed production calls. The classifier
also treated any bare number as a quantitative claim, including protein names,
dates and list labels. These figures describe that stopped run, not a new study.

The v2 instrument removes that predetermined outcome: explicitly quoted
abstract statements may support abstract alignment. Details absent from the
abstract escalate once to suitable evidence. Only affected invalid items need
format repair; missing evidence does not become repeated inference. The full-text
pilot also rejected responses solely for omitting the echo of hundreds of unit
IDs. V2 instead checks the exact units actually quoted and records supplied
coverage mechanically, without claiming that an ID list proves attention.

In the inspected completed production receipts, about 95% of output tokens
were reasoning tokens under `xhigh`. The v2 template starts abstract work at
`none` and targeted full-text work at `low`, with small-batch calibration before
scaling. There is no paired historical-versus-v2 accuracy result yet. Reducing
known waste is justified; preserved quality and a new completion time require
measurement. Retain all original v1 judgments and protocol versions separately.

## What not to reproduce

The final paper is a design baseline, not a certification that every limitation
was removed. Distinguish deployed code, audit corrections and post-mortem
recommendations:

- Some historical judge paths still capped abstracts at 2,200 characters and
  provided only the first three citing sentences. The current protocol keeps
  full abstracts and every locus; do not inherit those caps from old scripts.
- Historical full-text follow-up used targeted 11,000-character windows. This
  is excerpt-based examination, not demonstrated reading of a complete paper.
  Retain such runs under their actual evidence tier when importing history.
- Forty-item calls showed substantial position/batch effects. Keep one locus
  per scientific task and start one task per model call. V2 tests four-item
  calls against single-item and reordered controls before enabling that batch
  size. Preserve every item identity and position; transport grouping must not
  merge repeated occurrences or their judgments.
- Abstract silence on a specific claim cannot prove that the full paper fails
  to support it. Keep escalation and inaccessible evidence uncertain; report
  abstract alignment separately from full-text support.
- A parser's own output is not an independent reference. Test complaints about
  sentence shifts, ranges or absent citations against another document format
  or independent extraction before dismissing them.
- Provider policy, identity rules and article strata must be the same across
  comparison groups, or differences must be explicit analysis variables.
- The historical Claude panel was partial and is not human ground truth. A
  Qwen-only validation specification must stay Qwen-only unless the study is
  explicitly amended; do not silently restore the historical panel or claim
  independence across model families that were never run.

## Artifact identifiers

Relative locations below are inside the historical workspace. SHA-256 values
identify the inspected artifacts; scripts and source evidence beyond the score
database are necessary to reconstruct the earlier analysis fully.

| Artifact | Relative location | SHA-256 |
|---|---|---|
| Final paper | `results/audit/paper_nature.pdf` | `70408ecedaf8be997f643f97c983d93872513b663a477c9fd84dc5f74a740771` |
| Defect post-mortem | `results/audit/workflow_postmortem.pdf` | `94001d9fff11dc94af523ff5b023202861859914aeed7e41246fd5801e891eb2` |
| Released scores | `results/audit/scores_db.sqlite3` | `82aeccc4f5f835b65c0e7218de0856d13f98d84c002d6177e9c63f021570efec` |
| Audit provenance | `results/audit/PROVENANCE_audit.md` | `3ce0fa96697ce8bc49cc5cd11f499c3ce3a8b28234a42d7921199b17a56c9856` |

Supporting sources are `workflow_postmortem.tex` (defects D01–D33 and proposed
workflow), `paper_nature.tex` (Methods, five figures and limitations),
`defects_catalogue.tsv`, `audit_facts.json` and the original immutable database
manifest. Use them as evidence for historical assertions, not as instructions
that override the current approved study.

The timing evidence is in `work/audit/logs/n4_nrco_v2.log` and
`work/audit/logs/n4_frontiers_gain.log`; original batch geometry is in
`work/reviews/tools/judge_llm.py`. The slow-run figures come from workspace
`08d43c92-2286-4f4c-aeb8-a8a6872a8c6a`, its per-review census databases under
`work/corpus/ws/*/.ocsl-review/census/`, the pinned v1 assessments and broker
receipts. These private research artifacts are provenance identifiers, not
runtime dependencies or redistributed package inputs.
