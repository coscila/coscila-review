# Agent guidance for oc-science-lab-review

This repository is an additive OC Science Lab companion. Its source, mutable
deployment state, and chat workspaces are separate security domains.

## Non-negotiable boundaries

- Leave sibling repositories unchanged. Integrate only through global skill
  mirrors, deployment MCP configuration, environment variables, and read-only
  sandbox mounts.
- Never reuse project-authored material from `reference/nature-review-studio`.
  It has no project license. Reimplement useful concepts independently.
- Operator actions must fail inside chat workspaces. Review-time actions may
  write only to the selected workspace and chat-specific environment area.
- Treat unpublished manuscripts as confidential unless an explicit egress plan
  authorizes particular content. Provider requests must pass the egress gate.
- Do not infer evidentiary support from metadata, titles, or abstracts when the
  claim requires full-text examination. Record an abstention instead.
- Never report an enabled MCP or built environment until its required probes
  have passed. Do not add placeholder commands or success-only branches.
- Environment templates and archived source/standards material are read-only
  after promotion. Chat environments must not share mutable inodes with them.

## Development

- Python 3.11 or newer; two-space indentation is not required for Python.
- Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` for the dependency-light suite.
- Run `PYTHONPATH=src python3 -m ocsl_review.cli --help` to verify the CLI surface from a
  clean source checkout, or use `./bin/ocsl-review --help` to exercise the deployment launcher.
- Use imperative, one-line commit messages if committing changes.
