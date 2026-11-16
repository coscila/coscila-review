---
name: ocsl-review-followup-audit
description: Branch a frozen citation census, give every cited work a dated retraction, correction or concern status, and build the follow-up audit's strata, statistics, facts and exports in any chat; a lab chat gives its judging commands no route to a model.
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
`ocsl-review followup` command on the follow-up branch database (`--database`)
and returns JSON. Do not write drivers around the commands and do not edit the
instruments, the branch schema or the protocol after the strata are frozen.

## Commands that run in any chat

These call no model. Run them in this order:

1. `followup init --parent <frozen census scores database> --protocol
   <protocol.json>`: the person running the study supplies the protocol file,
   and the census is attached read-only and never copied. Add `--parent-text`
   for the private copy that keeps entitled citing sentences and
   `--study-bundle` when the study has one. Then `followup verify`. The status
   of cited works needs no protocol: on a branch made without `--protocol`,
   `verify`, `describe`, `pin-bundle`, `status-import`, `status-fetch`,
   `status-resolve`, `gates`, `facts`, `verify-facts` and `export` run. Such a
   branch stops at `hotspots` and `strata` and cannot take a protocol later, so
   the steps after the status need a branch made with `--protocol`.
2. Status: `followup status-import` (a Retraction Watch CSV snapshot, read
   locally), `followup status-fetch`, then `followup status-resolve`, which
   gives every cited work one status row with its source and date. An unknown
   status stays unknown. A fetch paused by a throttling provider resumes with
   `followup status-fetch --continue`.
3. `followup hotspots`, then `followup strata --freeze`.
4. `followup dependency` (a claim is `sole` when its sentence cites no other
   work; `essential` and `redundant` need the full-text support verdicts),
   `followup citedby` (`--widen` for every review with an S1 locus), and
   `followup second-gen` with `--stage sample`, `--stage acquire` and
   `--stage candidates`.
5. `followup lag`, `followup estimands`, `followup gates`, `followup facts`,
   `followup verify-facts` and `followup export`. An estimand that needs model
   judgments reports `not-measured` until they exist.

Egress: `status-fetch` asks Crossref, PubMed and Europe PMC, `citedby` asks
Europe PMC and OpenAlex, and `second-gen --stage acquire` fetches open full text
from Europe PMC. These are HTTPS GET requests that carry identifiers only, to
api.crossref.org, eutils.ncbi.nlm.nih.gov, www.ebi.ac.uk and api.openalex.org,
sent directly or through the chat's public-web proxy. Every other command reads
and writes local files only.

## Commands that judge with a model

`followup support --tier abstract|full-text`, `followup function`, `followup
numeric` and the `judge` stage of `followup second-gen` (part of its default
`--stage all`) judge with citation-support-verbatim@1, citation-function@1,
numeric-check@1 and claim-match@1. A lab chat gives these four commands no route
to a model. Given `--model`, each of them exits 5 without a broker socket with

    validation: give --base-url for a judge called directly, or --broker-socket (or OCSL_BROKER_SOCKET)

and on the chat's own orchestration socket with

    validation: the broker socket names no principal: instruments run only on a run principal's socket (a chat turn socket carries no run authority)

Without `--model`, each exits 5 with `validation: give --model, the served model
the protocol pins`. In a chat, run the commands that need no model and report
that the model judgments were not made. After a run without model judgments,
`followup gates` reports `ok: false`.

## Measurement rules

- Abstract silence and failed or missing downloads never count as unsupported.
  An unassessable unit keeps its reason and goes into the coverage tables, never
  into a numerator.
- Instruments run unchanged, and a judge abstains rather than infer; an
  abstention is recorded with its reason. The function judge never sees the
  cited work's status, the stratum or any support verdict.
- Estimates are weighted by the realised inclusion probabilities and carry
  review-clustered bootstrap intervals with the protocol's seed; never pass
  `--b` or `--bootstrap-seed` for a study's results. Strata sizes and rates come
  from the run's own data; the protocol holds parameters only.

## Confidentiality boundary

Entitled text (licensed review sentences and full text) reaches only judges that
the operator's egress policy lists as on premises. Provider requests carry
identifiers only, through the review companion's scholarly client: the only
contact identity is the operator's `OCSL_REVIEW_CONTACT_EMAIL`, and none is ever
inferred. Keep the branch, its exports and its receipts private within the
selected workspace.

## Role boundary

This skill runs the maintained follow-up commands and reports their results. It
does not change the parent census, re-judge the census instrument, author
analysis code, or present a finding as established; human adjudication, if the
study asks for it, has its own provenance.
