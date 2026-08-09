---
name: ocsl-review-environments
description: Inspect and materialize review environments without mutating shared templates.
---

# Review environment management

Use to select, inspect, create, verify, fork, or diagnose a task environment. Operator template construction remains outside the chat trust boundary.

## Runtime

Primary environment: `review-core`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-environments \
  --workspace "$PWD" \
  -- <command>
```

## Procedure

Resolve the environment through the skill catalog. Confirm the promoted template build ID and complete lock digest before materialization. Create a writable chat-specific instance without solving dependencies during ordinary use, then verify prefix identity, package health, descriptor schema, and absence of mutable inode sharing with the template or another chat.

Use clone forks for an exact writable continuation and reproducible rebuild forks for typed dependency overlays. Record parent build, requested overlay, resolved lock, full build identity, creation time, and fork lineage. A chat fork must rematerialize under the new chat identity rather than reuse its parent's writable prefix.

Never install into the bootstrap base, edit a promoted template, copy a conda prefix with an unsafe generic file copy, or claim an environment exists because a directory is present. Failed builds and incomplete clones remain unavailable.

## Review boundary

Environment management does not inspect confidential manuscript content and may not send it to package services. If an environment required for full-text or statistical verification is unavailable, downstream review records must abstain rather than fall back silently. Operator approval is required for shared template promotion or dependency acquisition.

