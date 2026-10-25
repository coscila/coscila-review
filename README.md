# OC Science Lab Review

`oc-science-lab-review` is the evidence-verification companion for OC Science
Lab. It turns a manuscript into a location-addressed review package and, when
the evidence is available, a claim-to-citation audit. Its job is to show how a
conclusion was reached: what was checked, which source passage was used, where
scope diverged, what remained inaccessible, and what a person still needs to
decide.

This repository is a sibling capability layer:

```text
oc-science-lab/          laboratory workflow and product lifecycle
oc-science-lab-bio/      bioinformatics capabilities, when installed
oc-science-lab-review/   manuscript review and literature verification
```

Installation is additive. It mirrors twenty-two `ocsl-review-*` skills into
configured global skill roots and adds only `ocsl_review_catalog` and
`ocsl_scholar` to deployment MCP configuration. It does not require tracked
changes in either sibling repository.

A request to check references or citations now leads to a concrete proposal
for an extensive review: confirm the scope, obtain and read every accessible
reference, verify each citation locus, and investigate targeted evidence gaps.
Missing full text remains visible; complete abstracts can inform appropriate
claims at lower confidence, while full-text questions still abstain. Numeric
ranges such as `[1-10]` retain all ten members. See the runnable
[literature workflow](docs/literature-review.md).

For systematic model measurements, the [citation census](docs/citation-census.md)
preserves independent abstract, full-text and panel runs, complete evidence,
native or API worker jobs, configuration revisions and assessment history. Its
SQL/hash facts export supports reproducible writing without treating model
judgments as scientific ground truth. Local JATS and publisher HTML ingestion
preserve citation targets and display loci before measurement. Registered,
digest-pinned instruments, the entry-level `citation-census@1` study commands,
the scores database, estimands and facts export are described in
[census-instrument.md](docs/census-instrument.md).

For one manuscript, [`ocsl-review screen`](docs/claim-check.md) is research-integrity
tooling for authors, reviewers and study teams: it resolves numeric, structured and
author-year citations against the reference list, records reference integrity findings
(never cited, cited but not listed, DOI registration, metadata agreement, duplicate
identity, dated retraction and correction status), and judges each citation against its
whole sentence with retrieval and model receipts. Hidden text and prompt-injection
payloads never reach a judge prompt, confidential mode lets only identifiers leave, and
`ocsl-review replay` re-derives every verdict without a model call.

For a complete corpus and future research questions, the
[reference-analysis skill](skills/ocsl-review-reference-analysis/SKILL.md)
preserves source evidence, all citation loci and versioned assessments in a
portable literature database. The maintained [`ocsl-review corpus` workflow](docs/corpus-review.md)
acquires shared sources, schedules per-locus v2 assessments, resumes durable
queues and creates verified snapshots and analysis branches ([storage](docs/storage.md):
packs, Merkle-rooted snapshots, delta branches and read-only stores). It keeps legacy
results separate and calibrates small batches before enabling them. The skill
includes census workflow lessons and limitations; deployment authorization and
live throughput still require their own checks.

## What the companion decides

The primary data structure is a claim-citation audit graph, not a free-form
review prompt. Each material claim retains its manuscript locator, origin,
evidence requirement, citation binding, resolved references, access level,
source passage locator, support and scope decisions, completeness assessment,
confidence, abstention reason, and requested human action.

From that graph, `audit-summary.json` answers two different questions:

- **Cited correctly:** does the identified source support the bound clause at
  the same population, species, outcome, direction, magnitude, and causal
  strength, and is the cited publication the right current version?
- **Cited enough:** is the evidence set direct, independent, broad, current,
  balanced, and appropriate for the type of claim without irrelevant padding?

Every answer carries processing denominators. A sample, incomplete ingestion,
ambiguous binding, unresolved reference, inaccessible necessary source,
metadata-only check, or abstract-only check of a full-text question prevents an
unqualified `yes`.

## Safety model

Unpublished manuscripts are confidential by default. Provider requests pass a
local egress gate before network I/O. In confidential mode, identifiers and
short sanitized search concepts may leave the workspace; manuscript prose,
titles, author combinations, figures, captions, tables, paths, and reviewer
notes may not. Logs retain field names and a payload digest rather than the
payload itself, and confidential work cannot opt into a shared provider cache.

Tracked source, operator-managed state, and chat workspaces are separate trust
domains. Promoted Conda templates, exact artifact locks, pinned repository
archives, and standards snapshots become read-only. A review receives a
writable chat-specific environment copied from a promoted template and checked
for shared file inodes.

See [Architecture and trust boundaries](docs/architecture.md) and
[Confidential review](docs/confidentiality.md) for the complete model.

## Quick start

The checkout contains build inputs, catalogs, schemas, launchers, and tests. It
does **not** contain solved Conda prefixes or proof that public services and a
particular OC Science Lab deployment are reachable.

The supported deployment artifact is the complete source checkout, not a
standalone Python wheel: skills, schemas, catalogs, environment inputs, and MCP
launchers are repository-level runtime assets. `pyproject.toml` declares the
Python package and dependencies for development and build tooling, but it does
not install an `ocsl-review` console script. Invoke `bin/ocsl-review` from the
checkout so those assets retain one verified identity.

Set explicit source and state roots first:

```bash
export OCSL_REVIEW_HOME=/srv/oc-science-lab-review
export OCSL_REVIEW_STATE=/var/lib/ocsl-review
export OCSL_REPO=/srv/oc-science-lab
export LAB_CONDA_INSTALL_ROOT=/opt/ocsl-conda
export LAB_CONDA_ROOT=/var/lib/ocsl-chat-conda
```

Validate tracked contracts, then let an operator build and verify the six
environment templates:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" catalog validate
"$OCSL_REVIEW_HOME/bin/ocsl-review" env build --all
"$OCSL_REVIEW_HOME/bin/ocsl-review" doctor
```

Skills invoke tools through their mapped environment with one uniform form:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-claim-source-verification \
  --workspace "$PWD" \
  -- "$OCSL_REVIEW_HOME/bin/ocsl-review" review literature --help
```

For deployment integration, configure distinct native-agent and Codex skill
roots, the deployment MCP JSON file, and its real staging command. Then install
and verify:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate install \
  --staged-config /path/to/generated/codex-mcp.json
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate verify --live --strict
```

Removal is guarded and reversible:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate uninstall
```

The installer backs up pre-existing skill destinations and same-name MCP
values, preserves all unrelated MCP entries, restages the deployment, checks
that sibling Git trees did not change, and rolls back on failure. Uninstall
refuses to overwrite post-install edits.

Read [Installation and operations](docs/operations.md) before using these
commands on a deployment.

## Documentation

- [Architecture and trust boundaries](docs/architecture.md)
- [Clean-room implementation boundary](docs/clean-room.md)
- [Claim-citation evidence model](docs/evidence-model.md)
- [Extensive literature workflow](docs/literature-review.md)
- [Resumable citation-support census](docs/citation-census.md)
- [Reference and claim screen](docs/claim-check.md)
- [Workflow regression applicability](docs/workflow-regressions.md)
- [Confidential review and egress](docs/confidentiality.md)
- [Review environments](docs/environments.md)
- [Scholarly sources and MCPs](docs/scholarly-sources.md)
- [Installation and operations](docs/operations.md)
- [Acceptance and definition of done](docs/acceptance.md)

## Verification status

Catalog files, source modules, schemas, adversarial fixtures, and the
dependency-light test suite are included. Environment specifications are inputs
to operator builds, not evidence that those builds happened. Likewise, MCP
catalog entries and launchers are not evidence of successful live calls or
deployment staging. Treat a capability as accepted only when the checks in
[Acceptance and definition of done](docs/acceptance.md) produce current,
deployment-specific results.

Run the local suite with Python 3.11 or newer:

```bash
PYTHONPATH="$OCSL_REVIEW_HOME/src" \
  python3 -m unittest discover -s tests -v
```

## License and provenance

The existing package is licensed under the [MIT License](LICENSE). The new
`structured.py` and `census.py` modules, census prompt and assessment schema,
structured/census tests and fixtures, census skill, and census guide are
independently implemented Apache-2.0 originals, as are the `author_year.py`,
`sanitize.py`, `retrieval.py`, `integrity.py`, `claim_check.py` and `screen.py`
modules, the `schemas/claim-check.sql` extension, their tests and fixtures, and the
claim-screen guide, with the
[Apache license text](LICENSES/Apache-2.0.txt) included. Existing MIT assets
remain under MIT. The adjacent
`reference/nature-review-studio` project has no project license and is excluded
from the implementation corpus: no text, code, prompts, tests, fixtures, or
taxonomies may be taken from it. Only abstract capabilities may be distilled,
then designed and expressed independently against public scientific and
technical standards. The detailed policy is in
[Clean-room implementation boundary](docs/clean-room.md).
