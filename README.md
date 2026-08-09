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

Installation is additive. It mirrors twenty `ocsl-review-*` skills into
configured global skill roots and adds only `ocsl_review_catalog` and
`ocsl_scholar` to deployment MCP configuration. It does not require tracked
changes in either sibling repository.

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
  -- <command>
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

This repository is licensed under the [MIT License](LICENSE). The adjacent
`reference/nature-review-studio` project has no project license and is excluded
from the implementation corpus: no text, code, prompts, tests, fixtures, or
taxonomies may be taken from it. Only abstract capabilities may be distilled,
then designed and expressed independently against public scientific and
technical standards. The detailed policy is in
[Clean-room implementation boundary](docs/clean-room.md).
