# Follow-up fixtures

Apache-2.0 synthetic fixture; invented identifiers (10.5555 test prefix), no publisher content. Built by hand for the follow-up audit tests; no file holds study data.

- `status/`: Crossref work records (with `updated-by` notices from the publisher and from Retraction Watch) and an
  `updates:` query, PubMed efetch records with CommentsCorrections links, Europe PMC search records with
  `isRetracted`, and a Retraction Watch CSV snapshot in the distributed column layout.
- `citedby/`: two Europe PMC citation pages and OpenAlex work and `cites:` pages.
- `judge/`: one valid request and response per follow-up instrument.
- `tables/`: a synthetic statistics-table source for the numeric comparison.
- `secondgen/`: a later article (JATS) that cites a synthetic review.
