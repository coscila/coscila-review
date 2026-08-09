# Review environments

The companion keeps document parsing, literature access, local language
analysis, statistics, and MCP serving in separate Conda templates. A skill maps
to exactly one primary template, and every skill tells the agent to enter it
through the same launcher contract.

## Environment catalog

| Environment | Primary responsibility | Skills |
| --- | --- | --- |
| `review-core` | orchestration, structured records, validation, ethics and reporting coordination | router, reporting guidelines, reproducibility/data/code, ethics/transparency, environments |
| `review-documents` | local manuscript ingestion and review-document production | manuscript ingest, peer report, revision response |
| `review-literature` | bibliographic reconciliation, scholarly lookup, and evidence-set analysis | reference integrity, claim/source verification, citation completeness, literature update, novelty/priority, evidence balance, journal policy |
| `review-nlp` | local clause segmentation and claim triage | claim inventory |
| `review-statistics` | numerical checks, figures/tables, systematic-review computation, and calibration | methods/statistics, results/figures/tables, systematic review, calibration |
| `mcp-review` | isolated runtime for the two local MCP processes | no direct skill mapping |

The canonical IDs and complete mappings live in
`environments/catalog.toml` and `skills/catalog.toml`. Validation requires the
two catalogs to agree; this table is explanatory rather than a third source of
truth.

## Two-phase dependency lifecycle

### Operator build

The tracked YAML specification describes intent. Its companion input lock pins
the requested packages and is bound into the catalog by SHA-256. It is still a
solve input, not a platform artifact lock.

A trusted operator build performs the one permitted solve:

1. verify the input-lock digest and catalog structure;
2. solve a disposable seed prefix for a supported platform;
3. export and normalize a checksummed Conda `@EXPLICIT` artifact list;
4. derive a build ID from input-lock hash, platform, and exact-lock hash;
5. install that exact list without solving at the final content-addressed path;
6. run every declared health check at that final path;
7. add a receipt and remove write bits while an adjacent build marker hides it;
8. remove the marker and atomically select the validated build with `current`.

The final-path materialization is intentional. Renaming a Conda prefix can
break absolute links or embedded prefix strings. Readers use only a validated
`current` target and ignore any hash directory with an in-progress marker.

Build all templates with:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env build --all
```

The command requires a Conda-compatible executable below
`LAB_CONDA_INSTALL_ROOT`. At present, the catalog supports `linux-64` and
`linux-aarch64`. A returned build record and subsequent verification are
evidence of a build; the YAML files alone are not.

Promoted paths have this form:

```text
$OCSL_REVIEW_STATE/environment-templates/<platform>/<environment>/<build-id>/
$OCSL_REVIEW_STATE/environment-locks/<platform>/<environment>/<lock-sha>.explicit.txt
```

Existing builds are verified rather than edited or replaced. Historical builds
remain addressable when `current` advances.

`env list` reports both `currentBuildId` and `currentExactLockSha256` for the
promoted template selected by `current`. A ready environment must expose both
complete digests; the tracked input-lock digest is not a substitute for the
platform-specific exact artifact-lock digest.

### Review-time materialization

An ordinary review never solves dependencies. `env ensure` resolves the skill's
promoted template, creates an offline Conda clone with copy semantics, makes the
new prefix writable, runs its health checks, and compares every regular-file
inode in source and destination. Any shared inode invalidates and removes the
new prefix.

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env ensure \
  --skill ocsl-review-claim-inventory \
  --workspace "$PWD"
```

The writable prefix lives below:

```text
$LAB_CONDA_ROOT/envs/ocsl-review/<workspace-id>/
```

Its schema-validated descriptor is placed in:

```text
<workspace>/envs/<logical-name>/environment.json
```

The descriptor binds the instance to its template build, exact lock, platform,
workspace identity, skill, materialization mode, and any allowed overlay.
Reusing an instance requires that identity to match and the health checks to
pass again.

## Skill command contract

Every namespaced skill contains its own ID in this exact invocation shape:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill <ocsl-review-skill-id> \
  --workspace "$PWD" \
  -- <command>
```

For example:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-methods-statistics \
  --workspace "$PWD" \
  -- python verify_analysis.py review-package.json
```

`env run` ensures or reuses the mapped instance, executes through the
Conda-compatible runner, and propagates a nonzero child result. A skill cannot
select a second primary environment opportunistically. Cross-cutting MCP access
is declared separately in the skill catalog. When the selected logical name is
a `clone-fork` or `rebuild-fork`, the runner validates its descriptor, immutable
template identity, prefix health, and inode isolation before executing it.

## Forking environments

Chat forks must rematerialize their environments; copying a workspace
descriptor does not transfer a writable prefix safely.

Two modes are supported:

- `clone` copies the source chat prefix without additions and audits inode
  isolation from both the source instance and promoted template.
- `reproducible-rebuild` installs directly from the promoted template's exact
  artifact lock. Optional additions are allowed only as hash-pinned artifacts.

Conda additions must be artifact URLs with an MD5 or SHA-256 fragment. Pip
additions must be single version-pinned requirements with an inline SHA-256;
indexes and dependency resolution remain disabled. Package names, version
ranges, and unpinned URLs are rejected because they would turn a fork into an
unrecorded solve.

The `env fork` command can rematerialize one named environment or every review
descriptor when a chat is forked. Source and target workspaces must be distinct,
and both must remain outside source and deployment state. Overlay flags are
rejected with `--all`; use a named `reproducible-rebuild` fork when one
environment needs additions.

## Verification and repair

Useful read-only checks include:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env list
"$OCSL_REVIEW_HOME/bin/ocsl-review" env describe \
  review-literature \
  --workspace "$PWD"
"$OCSL_REVIEW_HOME/bin/ocsl-review" env verify \
  review-literature \
  --workspace "$PWD"
```

A promoted template is not repaired in place. If its receipt, exact lock,
permissions, prefix metadata, or health checks fail, preserve diagnostics and
run an operator build from the tracked inputs. Cleanup is limited to failed
staging directories and newly created failed chat prefixes; promoted templates
are not automatic deletion targets.

`env verify` is observational. It validates the descriptor and template
identity, probes package health with transient external caches, and audits inode
isolation without changing prefix permissions or rewriting `verifiedAt`.

The MCP launchers resolve the promoted `mcp-review/current` template directly.
They fail if that pointer or its Python executable is absent. Setting
`OCSL_REVIEW_MCP_PYTHON` is an explicit testing/development override, not proof
that the production template is ready.

## What the repository proves

The repository provides catalog validation, input-lock hashing, build and
materialization logic, fake-Conda tests, descriptor checks, health-check
contracts, and inode-isolation tests. It does not ship promoted prefixes. Each
deployment must retain and verify its own build receipts and exact locks before
claiming these environments are available.
