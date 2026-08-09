# Clean-room implementation boundary

The local `reference/nature-review-studio` project has no project license. It
is not a source dependency and is not part of this companion's implementation
corpus.

## Permitted distillation

Only capability-level ideas may cross the boundary. Examples include broad
requirements such as “review a manuscript by study type,” “keep review findings
anchored to a location,” or “produce a response-oriented revision ledger.” Such
ideas must be re-derived into this repository's own architecture, terminology,
schemas, tests, and instructions using the user's specification, public
scientific guidance, and the established OC Science Lab extension interfaces.

The bioinformatics companion can inform interface compatibility—sibling
placement, global skill mirroring, task environments, MCP staging, and
source/state separation. It is not a wording template. Review-specific behavior
is independently designed around claim-level evidence records and conservative
verification.

## Material that must not cross

Do not read from the unlicensed reference project for the purpose of reproducing
or adapting any of the following:

- source code, command implementations, algorithms expressed as code, or file
  layouts distinctive to that project;
- README text, prompts, skill instructions, labels, examples, or other prose;
- test code, fixture content, expected outputs, case descriptions, or snapshots;
- category systems, review-lens names, severity scales, field taxonomies, or
  schema names;
- images, templates, assets, configuration fragments, or generated artifacts;
- identifiers and naming patterns that would make this work a translation of
  the reference rather than an independent implementation.

Changing variable names, rearranging paragraphs, translating language, or
rewriting line by line is still reuse and is not acceptable.

## Independent design record

Every review feature should be explainable from one or more allowed inputs:

1. a requirement in the user-provided implementation specification;
2. an OC Science Lab or bio-companion extension contract needed for
   interoperability;
3. an official public provider protocol or a published scientific standard;
4. an independently reasoned safety invariant, documented in this repository.

Where upstream repositories are archived for protocol inspection, their exact
commits and license evidence are recorded in `repos/repositories.lock.json`.
Entries marked `NOASSERTION` remain reference-only: they are not imported,
executed, vendored, or redistributed as project-authored content.

## Contributor procedure

Before adding a feature that resembles something in the unlicensed project:

1. Write the behavioral contract without consulting its expression there.
2. Locate a public, authoritative basis when one exists.
3. Choose names and data structures that follow this companion's claim-audit
   model and OC Science Lab interfaces.
4. Author implementation and tests from the new contract.
5. Review the change for distinctive wording, fixture narratives, category
   lists, and structural fingerprints that lack an allowed provenance.

If independent authorship cannot be established, omit the material. A useful
feature can be designed again; unlicensed expression cannot be assumed safe to
reuse.

## Scope of the boundary

This policy is stricter than a runtime isolation rule. It applies to tracked
source, documentation, skill prose, tests, fixtures, schemas, catalogs, commit
messages, and generated assets. It also survives refactoring: prohibited
material does not become acceptable because it passes through an intermediate
file or model.

The repository's MIT license covers only material lawfully authored for this
repository. It does not grant rights in any adjacent unlicensed project.
