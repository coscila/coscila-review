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
