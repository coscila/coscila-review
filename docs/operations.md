# Installation and operations

This guide is for the operator of an existing OC Science Lab deployment. It
assumes the three repositories are sibling checkouts and that the laboratory's
real configuration-staging command is known.

## CLI shape

The launcher is:

```text
$OCSL_REVIEW_HOME/bin/ocsl-review
```

After `review-core` has been promoted, the launcher resolves that immutable
template through its validated `current` pointer and uses its Python runtime.
Before the first environment build it falls back to `python3` from `PATH`, so
that one-time operator runtime must satisfy the dependencies declared in
`pyproject.toml`. The launcher canonicalizes its own path, so invoking it
through a symlink does not change `OCSL_REVIEW_HOME`.

Do not replace the checkout with a wheel-only installation. The Python modules
are only one part of the companion; the source-tree catalogs, schemas, skills,
environment inputs, and MCP launchers are required at runtime. For this reason
the wheel deliberately exposes no console script.

Global path overrides are `--home`, `--state`, and `--ocsl-repo`; `--json`
selects machine-readable output. The command families are:

```text
catalog validate
bootstrap
doctor
integrate install|status|verify|uninstall
skills list|show|validate|install|uninstall
repos list|path|verify|sync|update
standards list|path|verify|sync
env list|build|ensure|run|fork|describe|verify
mcp list|render|doctor|call
review ingest|inventory|plan|package|render|prepare
audit validate|summarize
calibrate
```

Use each subcommand's `--help` before automating it. Exit status `0` means the
requested operation completed; runtime, usage, trust-boundary, policy, and
validation failures use distinct nonzero statuses.

## Deployment variables

Use absolute, operator-owned paths. A representative configuration is:

```bash
export OCSL_REVIEW_HOME=/srv/oc-science-lab-review
export OCSL_REVIEW_STATE=/var/lib/ocsl-review
export OCSL_REPO=/srv/oc-science-lab

export OCSL_REVIEW_NATIVE_SKILLS_ROOT=/var/lib/ocsl-agent/skills
export OCSL_REVIEW_CODEX_SKILLS_ROOT=/var/lib/codex/skills

export OCSL_REVIEW_MCP_CONFIG=/var/lib/ocsl-agent/.claude.json
export OCSL_REVIEW_STAGED_MCP_CONFIG=/var/lib/ocsl-agent/runtime/codex-mcp.json
export OCSL_REVIEW_STAGE_COMMAND='corepack pnpm puppenclaw:config'
export OCSL_REVIEW_STAGE_CWD=/srv/oc-science-lab
export OCSL_REVIEW_STAGE_TIMEOUT=900

export LAB_CONDA_INSTALL_ROOT=/var/lib/ocsl/conda-bootstrap
export LAB_CONDA_ROOT=/var/lib/ocsl/chat-conda
```

`OCSL_REVIEW_STAGE_COMMAND` must be the deployment's real regeneration or
staging command. A no-op command, unconditional success script, or command that
does not make the MCP values reachable is not acceptable.

`OCSL_REVIEW_STAGE_TIMEOUT` is used when `--stage-timeout` is omitted and
defaults to 900 seconds. An explicit non-positive timeout is rejected.

The sandbox configuration should add these as read-only roots:

```text
$OCSL_REVIEW_HOME
$OCSL_REVIEW_STATE/environment-templates
$OCSL_REVIEW_STATE/environment-locks
$OCSL_REVIEW_STATE/repository-store
$OCSL_REVIEW_STATE/standards-store
```

Do not mount all of `OCSL_REVIEW_STATE` read-only: transaction records,
operator staging, and private state must remain writable. Do not expose that
entire root to review-time tools either.

Optional network configuration includes `OCSL_REVIEW_CONTACT_EMAIL` for public
provider etiquette and `NCBI_API_KEY` for NCBI. The scholarly MCP reads a
review-specific policy through `OCSL_REVIEW_EGRESS_PLAN`. A plan that broadens
the confidential default also requires the operator-bound
`OCSL_REVIEW_EGRESS_PLAN_SHA256`; conservative plans need no digest. The MCP
writes redacted events when `OCSL_REVIEW_WORKSPACE` selects a validated chat or
when its exact process working directory is a validated descendant of
`LAB_WORKSPACE_ROOT`.

## Pre-install preparation

### 1. Validate tracked inputs

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" catalog validate
"$OCSL_REVIEW_HOME/bin/ocsl-review" skills validate
```

These checks should fail on missing skills, environment-map disagreement,
schema drift, launcher drift, malformed locks, or undeclared content. Validation
does not contact providers or build environments.

### 2. Populate read-only reference stores

Repository archives are pinned to exact commits. Synchronization maintains an
operator Git mirror, exports the pinned tree without its `.git` directory,
records a tree digest, and removes write bits:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" repos sync
"$OCSL_REVIEW_HOME/bin/ocsl-review" repos verify --strict
```

`repos update` performs a remote HEAD lookup and writes candidate records under
deployment state. It does not rewrite the tracked lock. Review a candidate's
license, compatibility, and changes before intentionally updating source.

Official standards are fetched from catalogued authority URLs, stored by
content digest, and selected through validated state pointers:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" standards sync
"$OCSL_REVIEW_HOME/bin/ocsl-review" standards verify --strict
```

A current official web page may change. The snapshot digest and retrieval
record make the exact material used by a review identifiable.

When an authority blocks non-browser retrieval, the catalogue may name a raw
Internet Archive capture only together with the expected SHA-256. The official
URL is always attempted first; the archived response is accepted only when its
media type and digest match the tracked contract.

### 3. Build environment templates

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env build --all
```

Retain the command output, promoted receipts, and exact locks. Then verify
template readiness with `env list` and the deployment doctor. See
[Review environments](environments.md) for the solve/materialization boundary.

### 4. Exercise the MCPs

Render the owned entries, stage them through the laboratory, then perform the
source and staged checks including live bounded calls:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" mcp render
"$OCSL_REVIEW_HOME/bin/ocsl-review" mcp doctor \
  --live \
  --staged-config /path/to/staged-config.json \
  --strict
```

Do not label the MCPs operational until strict acceptance reports
`acceptanceComplete: true` for that deployment.

## Transactional integration

Install only after the prerequisites are ready:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate install \
  --staged-config /var/lib/ocsl-agent/runtime/codex-mcp.json
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate verify
```

Installation performs one transaction:

1. resolve two distinct configured global skill roots;
2. validate all twenty skill source directories and descriptors;
3. snapshot every pre-existing destination, including files, directories,
   symbolic links, empty directories, and modes;
4. copy the namespaced skills and record hashes and ownership metadata;
5. back up the exact deployment MCP file;
6. merge only `ocsl_review_catalog` and `ocsl_scholar`, retaining any prior
   values for those names in private rollback state;
7. run the configured deployment staging command;
8. verify that the tracked state of `oc-science-lab` and, when present,
   `oc-science-lab-bio` is unchanged;
9. write `install-state.json` only after all preceding steps succeed.

Skill roots are rejected if either they or any protected source/state tree
contains the other; an untracked directory inside a sibling checkout is not a
valid installation target. The MCP source must be a regular file (or absent),
never a symbolic link. Its original permission mode is retained during install,
rollback, and uninstall.

The staged path identifies the generated configuration that the running agent
consumes. It must differ from `OCSL_REVIEW_MCP_CONFIG`, which is the mutable
input merged by the transaction. New installations record the staged identity
so later verification cannot relabel the source file as staged. The older
`--acceptance-config` spelling remains an alias for `--staged-config`.

If any step fails, installed skills and the MCP file are restored. When staging
had begun, the staging command is run again against the restored configuration;
a recovery failure is surfaced rather than hidden.

The transaction records required environment variables and read-only roots for
audit, but it does not inject them into a tracked laboratory file. Deployment
configuration remains the operator's responsibility.

Repeating install with the same verified roots, MCP entries, configuration, and
staging command reuses the active installation. A request with different values
is rejected rather than mutating an installation whose ownership record no
longer describes it.

## Status and verification

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate status
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate verify
"$OCSL_REVIEW_HOME/bin/ocsl-review" doctor
```

Integration verification is read-only. It checks the install-state shape,
skill-manifest digest, installed skill files and modes, the two owned MCP values,
rollback backup, and both MCP launch surfaces through the recorded generated
configuration. It does not restage the deployment. Without `--live`, MCP probes
remain local; `--live --strict` additionally requires bounded provider calls.
Legacy installation records that predate the staged-path field must supply
`--staged-config` explicitly. A structural or MCP verification failure exits
nonzero even without `--strict`.

## Removal

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate uninstall
```

Removal verifies ownership before writing anything. It stops if:

- an installed skill differs from its recorded content;
- one of the two installed MCP values was edited;
- an ownership manifest or required backup is absent or changed.

This refusal protects work created after installation. Archive or deliberately
reconcile the changed material; do not delete an ownership marker to force
removal.

On a clean installation, removal restores every previous skill target and the
previous values—or absence—of the two MCP names. Unrelated MCP edits made since
installation are preserved. The recorded staging command runs again, sibling
Git trees are compared, and skill removal follows only after successful
restaging. A failure restores the installed MCP configuration.

Backups remain below `$OCSL_REVIEW_STATE/integration/backups/` for operator
audit. Running uninstall when no installation state exists is an idempotent
no-op.

## Review-time operation

The `review` family keeps the stages explicit:

- `ingest` extracts a supported TXT, Markdown, DOCX, or PDF into stable local
  structures and diagnostics;
- `inventory` creates atomic claims and conservative citation requirements;
- `plan` chooses applicable review passes without treating heuristic study
  classification as a final human decision;
- `package` assembles concerns, revision actions, graph, and summary;
- `render` publishes JSON, Markdown, and DOCX only after package validation;
- `prepare` orchestrates the local preparatory stages without manufacturing
  literature verification that has not occurred.

The `audit validate` and `audit summarize` commands operate on the evidence
graph. `calibrate` evaluates predictions against adversarial gold records. Use
`--help` for exact file and output options; never replace a missing audit graph
with a zero-filled “success.” The conservative empty state is
`not-fully-verifiable`.

## Operational troubleshooting

- **No current template:** run the operator build, inspect health-check output,
  and verify the promoted receipt; do not point a launcher at an arbitrary
  environment.
- **Policy rejection:** reduce the request to an identifier or sanitized
  concept, or obtain and record authority; do not rename a sensitive field.
- **Provider disagreement:** retain both attributed records and require a human
  identity/status decision.
- **Strict MCP failure:** inspect the individual source and staged probe fields;
  a successful initialization does not compensate for schema, call, timeout,
  or shutdown failure.
- **Integration rollback warning:** preserve transaction backups and both
  staging outputs before retrying.
- **Uninstall detects edits:** archive and reconcile them first. The refusal is
  intentional.

For the required evidence at handoff, see
[Acceptance and definition of done](acceptance.md).
