<!-- SPDX-License-Identifier: Apache-2.0 -->
# Census workflow lessons and limits

Methodological lessons for a corpus-scale citation-support census: what lets
such a census work at scale and how instrument decisions are handled. They are
not results, quotas or benchmark targets; every count is measured in the study
itself. Identify supplied inputs by their provenance in the study manifest,
never by an absolute host path.

## What makes it work at scale

1. Use the inputs the study supplies (source documents, provider caches) as
   byte caches. Open originals read-only, hash before and after, and write
   corrections and analysis to new tables/artifacts. Independent audits
   reconstruct inputs and recompute facts.
2. Parse publisher JATS and publisher HTML or PMC manuscripts. Serialize text,
   then each child's text followed by that child's tail, so superscript citation
   markers stay with their actual sentence. Cover table cells, captions, boxes
   and lists and expand numerical citation ranges.
3. Bind sources across manuscript versions by DOI, then checked title, using
   printed labels only when numbering is independently verified. Label-only
   joins misbind entries of renumbered papers; identifier-first mapping avoids
   that, and affected papers are rerun.
4. Use the same abstract provider policy across compared journals, preserve
   retrieved material and metadata, and use a separate full-text evidence field.
5. Execute Qwen judgments in a bounded concurrent pipeline with SQLite state,
   raw responses and request receipts. The Claude controller checks outputs,
   repairs code, coordinates targeted reruns and constructs the final
   statistical narrative.
6. Generate figures and prose from SQL-backed facts and frozen method outputs.

The duration of an inference phase over prepared inputs is not the duration of
a complete fresh acquisition-to-manuscript run, and the throughput of one
instrument geometry does not carry over to another (per-locus tasks, complete
abstracts and changed validation have different costs). Measure on the current
protocol. Record response-side model identity; an endpoint alias such as `chat`
does not identify the underlying model.

## Avoid work amplification

A blanket full-text requirement triggered by claim type turns every specific
claim into an abstention by construction and spends most of the tokens on those
calls. A classifier that treats any bare number as a quantitative claim
(protein names, dates and list labels included) widens that subset further.

The v2 instrument removes that predetermined outcome: explicitly quoted
abstract statements may support abstract alignment. Details absent from the
abstract escalate once to suitable evidence. Only affected invalid items need
format repair; missing evidence does not become repeated inference. Rejecting
responses solely for omitting an echo of hundreds of unit IDs wastes calls; v2
instead checks the exact units actually quoted and records supplied coverage
mechanically, without claiming that an ID list proves attention.

Reasoning tokens dominate the output at high reasoning effort. The v2 template
starts abstract work at `none` and targeted full-text work at `low`, with
small-batch calibration before scaling. Reducing known waste is justified;
preserved quality and a new completion time require measurement. Keep every
judgment and protocol version of a study separately.

## Instrument decisions

- An instrument's evidence policy and geometry are part of the instrument: how
  much of an abstract is sent, how many citing sentences, how many entries per
  call, their order, the reasoning effort and the repair pass. Run a registered
  instrument unchanged, record these per entry and per call, and treat any change
  as a different instrument with its own identifier.
- Measure the effect of such choices with the sensitivities the protocol
  declares before any data (for example re-running sampled calls without a cut,
  or comparing call positions). Report them; never tune the instrument to a
  result.
- A claim-targeted full-text window is excerpt-based examination, not a reading
  of the complete paper. Record the evidence tier of every verdict.
- Abstract silence on a specific claim cannot prove that the full paper fails
  to support it. Keep escalation and inaccessible evidence uncertain; report
  abstract alignment separately from full-text support.
- A parser's own output is not an independent reference. Test complaints about
  sentence shifts, ranges or absent citations against another document format
  or independent extraction before dismissing them.
- Provider policy, identity rules and article strata must be the same across
  comparison groups, or differences must be explicit analysis variables.
- A partial model panel is not human ground truth. A validation specification
  stays as approved unless the study is explicitly amended; do not add a panel
  or claim independence across model families that were never run.

Every reported number comes from this study's own records and facts. A fresh
study starts from its own enumeration, acquisition and judgments; it imports no
verdicts, scores or results from other studies.
