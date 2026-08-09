---
name: ocsl-review-reproducibility-data-code
description: Audit data, code, software, and workflow provenance and perform bounded rerun checks.
---

# Reproducibility, data, and code

Use when a manuscript makes materials available or claims that analyses can be reproduced.

## Runtime

Primary environment: `review-core`

```bash
"$OCSL_REVIEW_HOME/bin/ocsl-review" env run \
  --skill ocsl-review-reproducibility-data-code \
  --workspace "$PWD" \
  -- <command>
```

## Audit procedure

1. Inventory data, code, workflows, containers, environments, reference databases, models, instruments, and software with persistent identity, version, license, and access status.
2. Map each major result to required inputs, transformations, parameters, random seeds, and outputs.
3. Compare availability statements with actual access conditions, embargoes, credentials, data-use limits, and repository contents.
4. Inspect code before execution. Use isolated review environments, bounded resources, synthetic or approved inputs, and recorded commands; never run untrusted setup scripts automatically.
5. Distinguish computational repeatability, analytic reproducibility, and independent replication.
6. Record exact failures, missing dependencies, nondeterminism, and which claims they prevent checking.

## Evidence limits

A repository landing page or DOI proves neither completeness nor executability. Metadata-only access cannot validate a result. Abstain when protected data, unavailable code, hardware, or undocumented parameters block the relevant rerun, and request a specific remedy.

## Confidentiality and role

Do not upload manuscript materials or confidential data to public repositories or services. This skill audits supplied artifacts; it neither repairs authors' code silently nor treats inability to reproduce as evidence of misconduct.
