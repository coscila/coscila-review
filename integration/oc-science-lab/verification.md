# Integration verification and recovery

`integrate verify` checks the private installation-state contract, the skill-manifest digest, every installed skill file and mode, both owned MCP values, the MCP rollback backup, and both MCP launch surfaces through the generated deployment configuration. Verification is read-only and does not restage the deployment. It exits nonzero when a structural or MCP probe fails; `--live --strict` also requires bounded live calls.

New installations record the generated configuration passed with `--staged-config`. That path must not be the mutable MCP source file. For an installation record created before this field existed, pass the generated path to each verification explicitly:

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" integrate verify \
  --staged-config /path/to/generated/codex-mcp.json
```

The installer records independent backups using an installation identifier plus a digest of each destination root. This avoids collisions when both global roots end in the same directory name. Backups include files, empty directories, modes, and symbolic-link targets.

An ordinary uninstall deliberately refuses three unsafe cases:

- an installed skill differs from its recorded snapshot;
- either owned MCP value was edited after installation;
- a required rollback backup is absent or changed.

Preserve the changed material, decide whether it should become companion source or remain local, and restore the recorded installed form before retrying. Do not delete the ownership manifest to bypass the check.

If deployment staging fails during installation, the integration source is restored and the staging command is invoked once more against the restored MCP configuration. Any failure of that recovery command is reported explicitly. Inspect the deployment configuration and the transaction-specific backups before another attempt.

Environment-variable and read-only-root requirements are recorded but not silently injected into tracked laboratory configuration. Operators manage them in the deployment secret/environment layer and can compare that layer with `install-state.json` during audits.

## Core6 update — 2026-09-09

Updated the installed companion from upstream `9fc7634`, preserving existing
deployment tooling in merge `da40aa1`. The documents environment retains its
deployed Poppler 26.05.0 pin; duplicate/conflicting merge entries were removed.
Commit `90e5f83` fixes operator promotion across changed input-lock and health
contracts without weakening review-time resolution or modifying older builds.

- All 248 tests passed with zero skips, including real PDF extraction and
  immutable-template upgrade regressions.
- Rebuilt and promoted `review-literature` as
  `0db337b1e2bd5a1c53d876c2b0b1914985c380f91c4a270a27df797d8c88c553`;
  all six current environments verified ready. Prior templates remain intact.
- Refreshed twenty skills in each existing native/Codex mirror through guarded
  uninstall/install. All eighty installed Markdown/descriptor files matched
  current source. New installation ID: `f715a76c27554b51b4013d577c1c8036`.
- Strict live integration verification and the full strict doctor passed.
  Source and staged `ocsl_review_catalog` (7 tools) and `ocsl_scholar` (13 tools)
  passed initialization, schema, bounded-call, error, timeout and shutdown checks.
- Pre-update mirrors, ownership records, MCP configuration, runtime environment
  and template pointer were preserved under the private backup directory
  `/local/science_lab_workspace/runtime/backups/review-package-update-20260909`.
  Unrelated MCP/authentication configuration and the runtime environment were
  verified unchanged; sibling tracked repositories remained unchanged.

No web rebuild or service restart was needed. Refreshed skills and Python code
are available to new agent/MCP processes; active chats were left running and
may retain their earlier loaded context until a fresh session is created.
