---
name: ocsl-review-followup-audit
description: Branch a frozen citation census and audit which review claims rest on retracted, corrected or concern-flagged cited works, or on works cited systematically without support, and how those claims propagate to later articles.
---

# Compromised-evidence dependency audit

<!-- SPDX-License-Identifier: Apache-2.0 -->

Use for a predeclared follow-up study on a corpus of review articles whose
citation census is complete and frozen. The audit asks which claims rest on
cited works with a dated retraction, expression of concern or correction, or on
works that the census shows to be cited systematically without support, whether
such a work is the sole or an essential support of the claim, whether the review
acknowledges the notice, and whether later articles repeat the claim on the
review's authority. It produces model measurements with evidence, coverage and
uncertainty; it is research-integrity tooling for authors, reviewers and study
teams, and every adverse finding reads "check suggested".

## Runtime

Primary environment: `review-literature`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-followup-audit \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" followup --help
```

Read `$OCSL_REVIEW_HOME/docs/followup-audit.md` first. Every step is one
`ocsl-review followup` command on the follow-up branch database; each returns
JSON, and `--result-file` records it for a Study Run step. Do not write drivers
around the commands and do not edit the instruments, the branch schema or the
protocol after the strata are frozen.

## Order of work

1. `followup init`: branch the frozen census (read-only, never copied) with the
   signed-off protocol and its study bundle (`--study-bundle`, or `followup
   pin-bundle` before the first judge call); `followup verify` must pass. Add
   the private copy that keeps entitled citing sentences (`--parent-text`) only
   when those sentences are judged on premises; it needs to be present only in
   the steps that read citing sentences.
2. Status: `followup status-import` (the Retraction Watch CSV snapshot as a
   dataset input), `followup status-fetch` (Crossref, PubMed, Europe PMC) and
   `followup status-resolve`, which gives every cited work one status row with
   its source and date. An unknown status stays unknown. A fetch paused by a
   throttling provider resumes with `followup status-fetch --continue`.
3. `followup hotspots`, then `followup strata --freeze`: the strata, their sizes,
   the estimands and their denominators enter the protocol digest before any
   follow-up judging; judging commands refuse an unfrozen branch.
4. Judging, smoke test and pilot first (`--limit`, `--scope pilot`): `followup
   support --tier abstract`, `followup support --tier full-text`, `followup
   function`, and `followup numeric` only when the protocol records verified
   table licensing. Then `followup dependency`. Give support and function the
   census's review documents (`--documents`) so the judge sees the sentences
   around each citing sentence. Support runs on a judge called directly, with
   the reasoning effort the protocol predeclares per tier; the other judging
   commands may use the broker socket. The replicate (`--variant replicate`)
   follows the primary run of each judging command. A result whose `state` is
   `paused` names its reason; run the command again to resume.
5. Propagation: `followup citedby`, then `followup second-gen`.
6. `followup lag`, `followup estimands` (and `--scope pilot` for the pilot's
   results), `followup gates`, `followup facts`, `followup verify-facts` and
   `followup export`. Never pass `--b` or `--bootstrap-seed` in a study run.

## Measurement rules

- Abstract silence and failed or missing downloads never count as unsupported.
  An unassessable unit keeps its reason and goes into the coverage tables, never
  into a numerator.
- Instruments run unchanged: citation-support@2 for support, citation-function@1
  for the citation's function, numeric-check@1 for statistic claims and
  claim-match@1 for later restatements. A judge abstains rather than infer; an
  abstention is recorded with its reason.
- The function judge never sees the cited work's status, the stratum or any
  support verdict. Dependency is computed from the full-text verdicts of every
  co-cited work in the same citing sentence; a co-cited work without a decided
  verdict (unsettled support included) makes the dependency undetermined, not
  essential, and support from a co-cited work that carries a notice itself does
  not count.
- Estimates are weighted by the realised inclusion probabilities and carry
  review-clustered bootstrap intervals with the protocol's seed. Strata sizes and
  rates come from the run's own data; the protocol holds parameters only.
- Replicates and agreement between runs of one model family measure stability,
  not accuracy, and no follow-up instrument has a validation set; `followup
  estimands` reports the agreement and `followup gates` checks it.

## Confidentiality boundary

Entitled text (licensed review sentences and full text) reaches only judges that
the operator's egress policy lists as on premises; a run that would send it
elsewhere stops before the first call. Provider requests carry identifiers
only, through the review companion's scholarly client: the only contact identity
is the operator's `OCSL_REVIEW_CONTACT_EMAIL`, and none is ever inferred. Keep
the branch, its exports and its receipts private within the selected workspace.

## Role boundary

This skill runs the maintained follow-up commands and reports their results. It
does not change the parent census, re-judge the census instrument, author
analysis code, or present a finding as established; human adjudication, if the
study asks for it, has its own provenance.
