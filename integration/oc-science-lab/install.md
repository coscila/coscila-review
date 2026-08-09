# Deployment integration

The review companion joins an existing OC Science Lab deployment through global skill roots, two namespaced MCP entries, environment variables, and read-only mounts. It does not write tracked files in the laboratory or bioinformatics repositories.

## Required configuration

Set absolute paths for the companion source and mutable state. Configure separate native-agent and Codex skill roots, the deployment MCP JSON file, and the command that regenerates the laboratory's staged agent configuration.

```bash
export OCSL_REVIEW_HOME=/srv/oc-science-lab-review
export OCSL_REVIEW_STATE=/var/lib/ocsl-review
export OCSL_REVIEW_NATIVE_SKILLS_ROOT=/var/lib/ocsl-agent/skills
export OCSL_REVIEW_CODEX_SKILLS_ROOT=/var/lib/codex/skills
export OCSL_REVIEW_MCP_CONFIG=/var/lib/ocsl-agent/.claude.json
export OCSL_REVIEW_STAGED_MCP_CONFIG=/var/lib/ocsl-agent/runtime/codex-mcp.json
export OCSL_REVIEW_STAGE_COMMAND='corepack pnpm puppenclaw:config'
export OCSL_REVIEW_STAGE_CWD=/srv/oc-science-lab
```

Append the companion checkout, promoted environment templates, exact environment locks, pinned repository store, and standards store to the deployment's read-only sandbox roots. The state directory itself must not be mounted read-only because integration manifests, private caches, and operator build state live there.

## Install and verify

Before installation, build and verify all six environment templates and complete both MCP acceptance suites. Produce the owned MCP entries through the companion's MCP render operation, then run:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate install \
  --staged-config /var/lib/ocsl-agent/runtime/codex-mcp.json
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate verify
```

The staged path is the generated file consumed by the agent after the staging
command runs, not `OCSL_REVIEW_MCP_CONFIG`, which is the transaction's mutable
input. The two paths must be distinct. The generated identity is retained in
private install state for later read-only verification.

Installation validates the complete skill set, backs up every pre-existing target independently, installs both skill mirrors, and merges only `ocsl_review_catalog` and `ocsl_scholar`. Existing values under those names are retained in the private rollback record. Every unrelated MCP entry remains untouched.

The configured staging command must return success. The installer then compares the tracked Git state of the laboratory and bioinformatics siblings with its pre-install snapshot. A staging failure, validation failure, or tracked-tree change triggers restoration of the MCP file and skill targets. Integration state is written only after all checks pass.

## Removal

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate uninstall
```

Removal first verifies that installed skill files, rollback backups, and owned MCP values are unchanged. It preserves unrelated MCP edits made after installation and restores the exact prior values for the two owned names. If a user edited an installed skill or owned MCP entry, removal stops without overwriting that work; reconcile or archive the edit before retrying.

Backups remain under `$OCSL_REVIEW_STATE/integration/backups/` for operator audit. Repeating install or removal is safe: a verified active installation is reused, and removal with no active state is a no-op.
