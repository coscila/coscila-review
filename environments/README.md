# Review environment lifecycle

The files in this directory are build inputs, not prebuilt Conda prefixes. The
deployment state is the source of truth for completed template builds.

## Reproducibility contract

Each catalog entry names a readable specification and a stricter input lock.
The catalog records the input lock's SHA-256. An operator build verifies that
hash, solves a disposable seed prefix, and asks Conda for a checksummed
`@EXPLICIT` export. That export is normalized and hashed. The template build
identifier binds the input-lock hash, target platform, and exact export hash,
so a changed build input cannot collide with an older immutable template even
when both solves happen to select the same artifacts.

The exact export and promoted template live below `OCSL_REVIEW_STATE`, outside
the repository. Because Conda prefixes can contain absolute paths, the exact
export is installed without solving at its final content-addressed location.
An adjacent in-progress marker hides that path from readers until final-prefix
health checks pass, a receipt is written, and the tree becomes read-only.
Promotion is the atomic update of the validated `current` pointer. A later
build that resolves to the same identifier verifies the existing tree without
editing or replacing it.

After promotion, an environment's `current` symlink is atomically selected by
its relative build identifier. MCP launchers may resolve through that stable
name, while environment operations validate both the symlink target and the
immutable template receipt before use. Historical build directories remain
addressable and are never rewritten by pointer updates.

This split is intentional. The tracked input locks constrain an operator-only
solve, but they are not falsely presented as platform artifact locks. The first
successful build produces that platform-specific artifact lock. Reproducible
rebuilds consume the artifact lock directly.

## Chat materialization

An ordinary ensure operation resolves a promoted template and executes a Conda
clone with copy and offline flags. It then compares every regular-file inode in
the two trees. Any shared inode rejects and removes the incomplete chat prefix.
The same exhaustive check applies to named forks and chat-fork rematerialization.

Chat prefixes are placed below:

```text
$LAB_CONDA_ROOT/envs/ocsl-review/<sanitized-workspace-id>/
```

Their descriptors are stored in the selected workspace under
`envs/<logical-name>/environment.json`. A descriptor is schema-validated before
use and cannot point outside the configured chat Conda root.

Fork mode `clone` copies an existing chat prefix and permits no additions. Fork
mode `reproducible-rebuild` installs the promoted template's exact artifact
lock. Optional Conda additions must be artifact URLs carrying an MD5 or SHA-256
fragment. Optional pip additions must be single, version-pinned requirements
with an inline SHA-256 hash; pip runs with dependency resolution and indexes
disabled. The installed `conda-meta` artifact set and checksums are reconciled
against the exact lock before any overlay is accepted. Thus neither runtime
path submits package names or channels to Conda's solver.

Multi-environment `--all` forks reject overlay flags rather than silently
discarding them. A fork descriptor may be used through the normal runner only
after its schema, promoted-template identity, health checks, and inode isolation
are revalidated. Verification uses transient caches and does not rewrite the
descriptor or change prefix permissions.

Source, deployment-state, workspace, and chat-prefix roots may not overlap.
Existing path components are checked for symlink redirection, and symlinks
inside a candidate template must resolve within that template. Multi-environment
chat-fork rematerialization rolls back every prefix and descriptor it created if
a later materialization fails.

Only failed build staging directories and failed, newly-created chat prefixes
are eligible for automatic cleanup. Promoted templates are never removal
targets of the environment module.
