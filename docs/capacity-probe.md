<!-- SPDX-License-Identifier: Apache-2.0 -->
# Capacity probe

`ocsl-review capacity probe` measures how many `citation-census@1` entries per
second a judge sustains, before a census run commits to a concurrency. It
drives an OpenAI-compatible chat-completions endpoint with the instrument's
own abstract-tier requests over synthetic entries, cell by cell over a grid of
stream counts and items per call, and writes a JSON report, a markdown table
and a per-call log. The probe only measures: it is no part of any instrument
digest and never writes a scores database, and its chat requests are the ones
a census sends, except that their entries are synthetic.

`--tier full-text` probes the full-text tier instead, with real inputs: the
census's own full-text calls over the loci of real reviews from the study's
literature-evidence@2 store ([full-text tier](#full-text-tier---tier-full-text)).

It is an operator command: it refuses to run inside a chat workspace, because
the load it puts on a shared judge is the operator's decision. Run it on the
host that will run the census, against the same endpoint.

## Running it

```bash
# The plan: grid, roles, request sizes and expected load. Nothing is sent and no key is read.
ocsl-review capacity probe --dry-run --base-url "$JUDGE_URL" --metrics-url "$JUDGE_METRICS_URL"

# The full grid: 30, 60, 90 and 120 streams x 1, 5, 10 and 40 items per call, 300 s per cell.
ocsl-review capacity probe --base-url "$JUDGE_URL" --api-key-env JUDGE_KEY_VARIABLE \
  --metrics-url "$JUDGE_METRICS_URL" --proxy-pid "$JUDGE_PROXY_PID" --output probes/judge-grid

# The deciding cell alone, measured for longer (for example when the grid's line says its window was too short).
ocsl-review capacity probe --base-url "$JUDGE_URL" --api-key-env JUDGE_KEY_VARIABLE \
  --metrics-url "$JUDGE_METRICS_URL" --cells 60x40 --cell-seconds 900 --output probes/judge-60x40

# The deciding cell with the census's repair pass: invalid outputs before and after it, by items per call.
ocsl-review capacity probe --base-url "$JUDGE_URL" --api-key-env JUDGE_KEY_VARIABLE \
  --cells 60x40 --cell-seconds 900 --repair --output probes/judge-60x40-repair

# The full-text tier: one cell of 20 streams over real loci of one journal's reviews in the store, each call once.
ocsl-review capacity probe --base-url "$JUDGE_URL" --api-key-env JUDGE_KEY_VARIABLE --tier full-text \
  --store "$STORE" --store-root "$STORE_ROOT" --journal "$JOURNAL" --review-doi-prefix "$DOI_PREFIX" \
  --review-route public --cells 20x5 --full-text-entries 2000 --cell-seconds 900 --drain-seconds 600 \
  --full-text-cache probes/full-text-cache --hypothetical-repair --output probes/judge-full-text

# With sizes and expected tokens per entry measured on a study's own public inputs (after its pilot).
ocsl-review capacity profile --database work/census.sqlite3 --output probes/profile.json
ocsl-review capacity probe --base-url "$JUDGE_URL" --api-key-env JUDGE_KEY_VARIABLE \
  --metrics-url "$JUDGE_METRICS_URL" --cells 60x40 --profile probes/profile.json --output probes/judge-check
```

`--base-url` is the base the chat-completions path is appended to (for vLLM,
`http://HOST:PORT/v1`); `--metrics-url` is the Prometheus endpoint of the vLLM
server itself (`http://HOST:PORT/metrics`), not of a proxy in front of it. When
`--metrics-url` is given, it must answer with vLLM's request gauges before any
load is sent, otherwise the probe refuses to start. The full grid takes about
16 x (300 s + 10 s pause), 83 minutes, plus per cell a tail and a drain of up
to `--drain-seconds` each; the dry run prints the estimate.

| Option | Default | Meaning |
|---|---|---|
| `--streams`, `--items` | `30,60,90,120`, `1,5,10,40` | the grid; items per call are at most the instrument's 40 |
| `--cells` | all | run only these cells, `STREAMSxITEMS`, comma-separated or repeated |
| `--order` | `load` | `load`: the instrument's geometry within `--allowed-streams` first, then the other cells within it, then those above it, each group by ascending streams x items; `streams`; `items`; `as-given` (the `--cells` order); `shuffle` (seeded) |
| `--max-streams` | none | skip cells with more streams |
| `--run-above-allowed` | off | run cells above `--allowed-streams` even when the server is shared or cannot be shown unshared |
| `--cell-seconds` | 300 | fixed duration of a cell |
| `--warmup-seconds`, `--ramp-seconds` | 30, 10 | the measured window starts no earlier than the warm-up (and only once the ramp's calls have finished); stream starts are staggered over the ramp |
| `--drain-seconds`, `--pause-seconds` | 180, 10 | the limit of the tail after the window and of the drain after it; then an idle pause |
| `--request-timeout` | 300 | read timeout per call (connections open within 10 s) |
| `--sample-seconds` | 5 | server load sampling interval |
| `--abort-*`, `--saturation-*` | see [abort rules](#abort-rules) | |
| `--shared-tolerance` | 0 | requests beyond ours that a sample tolerates while our calls are in flight |
| `--allowed-streams`, `--target-rate` | 60, 40 | the exit criterion: entries/s with the instrument's geometry at the allowed concurrency (operator settings; a study derives its own capacity gate) |
| `--profile` | built-in | size profile and expected tokens per entry from `capacity profile` |
| `--expect-output-tokens`, `--expect-prompt-tokens` | none (or the profile's) | predeclared tokens per entry of the census's real entries |
| `--expect-tolerance` | 0.2 | departure from the expected tokens per entry that warns and normalises the rate |
| `--model` | the instrument's required model | model name to request |
| `--journal` | a synthetic name | the system prompt's journal parameter |
| `--seed` | 1 | seed of the synthetic entries |
| `--proxy-pid` | none | process whose resident memory is sampled with the server load (repeatable) |
| `--repair` | off | also send the census's one repair call per batch and report invalid outputs before and after the [repair pass](#repair-pass---repair) |
| `--tier` | `abstract` | `full-text`: the census's full-text calls over real loci, one cell (default `20x5`) |
| `--store`, `--store-root` | none | full-text tier: the literature-evidence@2 projection (absolute path) and the Merkle root it must have |
| `--review-doi-prefix` | none | full-text tier: DOI prefix of the reviews to use (repeatable); all are labelled with `--journal` |
| `--review-route` | `entitled` | full-text tier: the journal's route class in the protocol; entitled reviews need `--on-premises --policy` |
| `--full-text-entries` | 2000 | full-text tier: retrieved entries to prepare and send |
| `--fetch-workers` | 4 | full-text tier: Europe PMC fetch threads (as `study full-text`) |
| `--full-text-cache` | `full-text-cache` in `--output` | full-text tier: directory (absolute) that keeps fetched fullTextXML across runs |
| `--hypothetical-repair` | off | full-text tier: also resend every entry left without a valid verdict alone, reported as hypothetical |

## What it sends

Every request is the instrument's first abstract-tier call for one batch,
built by `citation_census`: the system prompt with the journal parameter, the
per-batch user content, temperature 0, reasoning effort `none`,
`max_tokens = max(3000, 700 * items)` and streaming off. Without `--repair`
the probe sends no repair pass. The request body bytes are recorded by sha256
per call.

The entries are synthetic and deterministic: every value derives from the
seed, the cell, the entry's position and the size profile, built from a fixed
vocabulary. No corpus data and no entitled text are involved. Each cell has
its own call sequence, so no cell is served from another cell's prefix cache;
the system prompt is shared by all calls, as it is in a census. Most citing
sentences restate a span of the entry's own abstract, so the judge can support
most entries and the length of its lines (a supported entry needs no quote)
resembles real entries more than unrelated text would; the rest are unrelated
text.

| Property | Built-in synthetic distribution |
|---|---|
| entries per review | lognormal, median 110 (8 to 600); batches never cross reviews, so 40-item cells average fewer than 40 items per call |
| citing sentences per entry | 1, 2 or 3 (weights 0.55, 0.25, 0.2), each with a section trail and a citation marker |
| citing sentence length | lognormal, median 170 characters (60 to 480) |
| citing sentences from the abstract | 80 %: a span of the entry's own abstract; the rest unrelated text |
| abstract length | lognormal, median 1,500 characters (200 to 4,000), 30 % structured, 5 % missing; the builder applies the instrument's abstract cut, so sent abstracts stop at the cut |
| cited work | title of 6 to 24 words, venue, year, rare retraction flag |
| printed reference | authors, title, venue, year, volume, pages and a synthetic DOI; generated and never sent, as the instrument requires |

**Profiles.** `ocsl-review capacity profile --database DB --output FILE` reads
a census scores database (working or frozen, read-only) and writes the sizes of
its public reviews: judged entries per review, citing sentences per entry and
their length, abstract length and the share of missing and structured
abstracts (lognormal fits with the observed range), title words, and the
prompt and output tokens per entry of its answered first calls (for example
from the study's pilot). Only sizes and token counts are read; no text leaves
the database and no verdict is read. `--profile FILE` replaces the built-in
sizes with these and predeclares the expected tokens per entry, unless
`--expect-output-tokens` or `--expect-prompt-tokens` give them explicitly. The
report records the profile in use and the file's digest.

Transport is direct HTTP/1.1 to `--base-url` + `/chat/completions` over a
keep-alive connection pool sized to the cell's stream count: each stream
reuses one connection, a connection the server closed is reopened before the
next call, and proxies from the environment are not used. The key is read
from the environment variable that `--api-key-env` names; it is sent only as
the `Authorization` header to the judge (never to the metrics endpoint), and
it is never printed, logged or written: the report records the variable name
and whether a key was sent. Every string the server supplies (error messages,
model, fingerprint, finish reason, listed models) is cleaned as it arrives:
the key and any run of eight or more of its characters become `[redacted]`
before a message is cut to its 200 characters, and the markdown is rendered
from the cleaned report. A base URL with credentials, a query or a fragment is
refused. Before the first cell, a `GET /models` checks the endpoint; HTTP 401
or 403 stops the probe before any cell runs.

## A cell

1. The server load is sampled once (the idle "before" sample).
2. The streams start over `--ramp-seconds`; each sends its next call as soon
   as the previous one ends.
3. At `--cell-seconds`, the **tail**: the streams keep sending until every
   call that overlaps the end of the window has finished (at most
   `--drain-seconds`), so those calls run under full load.
4. The streams stop; the calls still in flight may finish for up to
   `--drain-seconds`, then they are abandoned (their connections are closed,
   which lets vLLM cancel them). Abandoned calls are reported apart and are no
   transport failures.
5. After `--pause-seconds`, the idle "after" sample closes the cell.

**The steady-state window.** The measured window starts at `--warmup-seconds`,
or later, once every call started during the ramp has finished (those calls
began on a server with fewer of our calls). It ends at `--cell-seconds`, or
earlier at the earliest start of the calls still in flight when sending
stopped: when a stop rule fired (the streams stop at once) or the tail ran out
before the window's last calls finished. Calls that finish on an emptying
server would otherwise read high. Every call inside the window therefore ran
under the cell's full load; the report records the window's start, end, the
time it was cut short by, the tail and whether it completed.

## Measurements

Per cell, in `cells[]` of the report:

- **Entries per second** (`entries.perSecond`): entries with a line the
  instrument's validator (`citation_census.parse_labels`, as the census
  applies it) accepts, per second of the steady-state window. Each call's
  valid entries are spread over the call's own duration and counted where it
  overlaps the window, so the rate does not depend on where the window cuts
  long calls. `perSecondCompleted` counts only calls that ended inside the
  window; `meanConcurrency` is the mean number of our calls in flight, and
  `clientLimited` flags a mean below 90 % of the streams.
- **Calls**: sent, answered, failed, abandoned, mean items per call.
- **Tokens**: prompt and completion tokens from `usage`, per call, per entry
  and per second, and the cached share: `prompt_tokens_details.cached_tokens`
  over prompt tokens when the server reports it, else the server's prefix
  cache hits over queries between the idle samples (`cachedShareSource`
  `server-prefix-cache`; server-wide, shown as "(server)" in the markdown).
- **Latency**: p50, p95, p99, mean and maximum of answered calls that started
  inside the steady-state window.
- **Invalid outputs**: entries of answered calls without a valid line, over
  the entries of answered calls (`invalidOutputShare`); an empty completion
  makes all its entries invalid. Also invalid and truncated calls
  (`finish_reason` `length`), empty completions, lines the validator rejected
  and up to three examples.
- **Failure classes**: `http-<status>`, `timeout`, `reset` (including a body
  cut off mid-response), `refused`, `tls`, `protocol`, `dns`, `connection`,
  `invalid-json` (a 2xx body that is no JSON object) and `invalid-response`
  (JSON that is no chat completion), with `transportFailureShare` over
  answered and failed calls. `failureExamples` keeps up to three server
  messages per class (for example the context-length message behind an
  `http-400`), cut to 200 characters.
- **Served model**: the models and `system_fingerprint` values the server
  returned, calls without a fingerprint, and whether every answer came from
  the instrument's required model.
- **Server load**, every `--sample-seconds` from the vLLM Prometheus
  endpoint: requests running and waiting (summed over engines), KV cache usage
  (`kv_cache_usage_perc`, or `gpu_cache_usage_perc` on older servers), the
  preemption, prompt-token, generation-token, finished-request
  (`request_success_total`) and prefix-cache counters, and our own calls in
  flight at that moment.
- **Client**: the probe's resident memory, the pool size, connections opened
  and reused, and connections the server closed; with `--proxy-pid`, that
  process's resident memory.

Each cell also has a `role`: `decisive` (the instrument's 40 items per call at
`--allowed-streams`), `instrument` (40 items per call at another stream count)
or `exploratory` (1, 5 or 10 items per call, which a census never sends).

`calls.jsonl` holds one line per call: cell, stream, batch, items, body size
and sha256, outcome, failure class, status, start, end, tokens, served model
and fingerprint.

## Abort rules

Every rule stops the cell and the grid; the remaining cells are listed in the
markdown as a `--cells` argument for a later run.

- **Transport failures.** More than `--abort-failure-share` (5 %) of the calls
  that finished in the trailing `--abort-window-seconds` (60 s) failed, once
  that window holds `--abort-min-calls` (20) calls or `--abort-min-failures`
  (3) failures. The calls in flight are cut off, and the cell is `aborted`: it
  never decides. A defect of the probe itself stops the grid the same way.
- **Saturation.** Every server-load sample after the warm-up reports
  saturation for more than `--saturation-seconds` (60 s). A sample is
  saturated when at least `--saturation-waiting` (1) requests wait on the
  server, when the preemption counter rose since the previous sample (the KV
  cache was full), when HTTP 429 or 503 answers arrived since the previous
  sample, or, with `--saturation-kv`, when KV cache usage reaches that share.
  A sample without metrics neither extends nor breaks the streak; only an
  unsaturated sample breaks it. The cell stops sending, its calls in flight
  finish, and the cell is `saturated`: its steady-state window ends at the
  earliest start of the calls the stop cut off, and it may still decide (see
  the exit criterion). Cells with more load could not add throughput and would
  only queue other clients' requests behind ours.
- **Unreadable metrics.** With `--metrics-url`, metrics that cannot be read
  for longer than `--saturation-seconds` while our calls are in flight stop
  the cell like saturation (`metrics-lost`), since saturation can no longer be
  watched.

The default order runs the instrument's geometry within the allowed
concurrency first, so a rule that fires later never costs the cell the exit
criterion is assessed on. To measure a saturated cell for its full duration,
run it alone with a larger `--saturation-seconds`.

## Shared server and the allowed concurrency

The census concurrency stays at `--allowed-streams` (60) unless the report
shows that the server is not shared. The report says `shared: true` on any
evidence of other clients:

- before the first cell (three idle samples): requests on the server beyond
  the tolerance in two samples, or prompt-token, generation-token or
  finished-request counters that advance between the samples;
- in a cell's samples: more requests running and waiting on the server than
  our own calls in flight, beyond `--shared-tolerance` (0: our own count is an
  upper bound, since a call counts from before it is sent until its whole
  response is read) after allowing for our calls abandoned in the last 30 s,
  in two or more samples;
- across a cell, between the idle samples before and after it: more generated
  tokens than our answers carried, more prompt tokens than our answers
  carried, or more finished requests than our answered calls, beyond what our
  unanswered calls that may have run could account for (their `max_tokens`,
  their body bytes plus a chat-template allowance, one request each; also for
  calls abandoned in the 30 s before the cell) plus a margin of 16 tokens and
  one token per call. A refusal with a 4xx status (bad request, context
  length, rate limit) and a connection that was refused reached no model and
  get no allowance. Preemptions in the cell make the token counters
  incomparable (recomputed prefills); the request counter still counts.

It says `shared: false` only when there is no such evidence, the metrics were
read before the first cell and in every cell, and they demonstrably cover our
own calls: in every cell the server's running and waiting requests account
for at least 80 % of our calls in flight (median over the measured window),
and its generated tokens for at least 80 % of ours, each where it can be
computed. Metrics of one replica behind a load balancer, or of a server behind
a proxy that queues our calls, fail this check, and the answer is unknown
(`null`), as it is without `--metrics-url`. The markdown lists the evidence,
or what kept the probe from showing an unshared server.

Cells above `--allowed-streams` run only while the server can still be shown
unshared: before each one, the probe checks the report so far, and skips the
cell with its reason when `--metrics-url` is missing, when the server showed
other clients, or when the baseline or an earlier cell could not be shown
unshared. `--run-above-allowed` runs them anyway. `allowedConcurrency` states
the result: the allowed concurrency stays at `--allowed-streams`, and only when
the server is not shared does the report say that the author may allow more
and name the best cell above it with the instrument's geometry (exploratory
geometries are named apart).

## Repair pass (`--repair`)

The census's limit of 1 % invalid outputs applies after its one repair pass,
which the first calls alone cannot show. With `--repair`, each stream sends,
right after the first call of a batch that left labels without a parseable
line, the census's repair call for that batch: the same selection and builder
the census runner uses (`census_runner.repair_items` and `abstract_call` with
kind `repair`), so the open labels in batch order, the instrument's repair
note as the second line of the user message and `max_tokens` for the repair
call's own size. A first call that failed is repaired with every label, as in
the census; an empty completion leaves every label open for the repair call.
No repair call is sent once an abort rule fired or the cell is cutting its
calls off; such a batch's labels stay open.

Each cell then carries a `repair` block and `invalidOutputShareAfterRepair`:

- `beforeRepair`: labels the answered first calls left without a parseable
  line, over the entries they carried (equal to `invalidOutputShare`);
- `afterRepair`: labels still without one once the batch's repair call was
  answered, over the entries decided, which is how the census gates count
  them; `open` counts the labels of batches whose repair call got no answer or
  was not sent, undecided as in the census and never counted as valid or
  invalid;
- both by the items of the batch's first call, in the census gates' buckets
  `1`, `2-9`, `10-39` and `40` (a 40-item cell carries full batches and each
  review's remainder batch);
- `calls`: repair calls sent, answered, failed, cut off and not sent, their
  items, the labels they recovered and those still without a line, tokens and
  latency; `entriesPerSecondWithRepair` adds the recovered entries to the rate.

Every first-call statistic (rates, latency, tokens, transport failures, the
window) stays the first calls' own, so cells with and without `--repair`
compare; the repair calls still take their stream's time, count for the
concurrency, the server counters, the served model and the abort rules, and
appear in `calls.jsonl` with `kind: repair` (`kind: first` for first calls).
The markdown adds a table of both shares by items per call, and the exit line
reads "invalid outputs X before and Y after the repair pass" and applies the
census limit to Y.

## Full-text tier (`--tier full-text`)

The abstract tier's synthetic entries say nothing about the full-text tier: its
calls carry five claim-targeted windows of real full texts, at reasoning effort
`low`, and citation-census@1 has no repair pass there. `--tier full-text`
therefore sends the census's own full-text calls over real inputs.

**Inputs.** `--store` names a literature-evidence@2 projection, the byte cache a
census reads (`study prepare --store`), for example the sealed version a study
binds. It is opened through the census's own guarded reader, which refuses every
table outside the projection's kept tables and bytes that carry legacy verdict
vocabulary, so no label, verdict, locus or bibliography row can be read; any
other store is refused. The reviews are the store's retained
review captures whose DOI starts with a `--review-doi-prefix` (the store does not
record journals, so the prefix selects the journal `--journal` names, spelled as
the census's enumeration spec spells it: it enters the system prompt), in an
order shuffled with `--seed`. Each review is prepared exactly as a census
prepares it, into a scratch census database in the output directory
(`full-text-inputs.sqlite3`):

1. acquired from the store as `study acquire` acquires it without a live route
   (`ReviewAcquirer`); `--review-route` is the journal's route class in the
   protocol and decides the class of a retained capture (a Europe PMC document
   is always public). Entitled review text reaches only a judge the operator's
   egress policy names on premises, as in the census: `--on-premises --policy`;
2. its cited works derived from its own bibliography (`study works`), with
   title, venue, year and PMCID from the Europe PMC record `study abstracts`
   selects: the store's record first, else the census's live Europe PMC search
   (40 DOIs per query, the census inputs' User-Agent, 0.4 s between requests);
   the dry run stays in the store;
3. prepared by `CensusRun.prepare_review`, so its loci are the census's own.

Every in-body entry with a resolved identity whose cited work has a PMCID is a
candidate. These are not the census's escalated entries: the full-text tier
judges only what its abstract tier escalates, which the probe cannot know and
never reads. Each review's candidates are shuffled with the seed, grouped by
review as the tier groups them, and retrieved and windowed by the tier itself
(`CensusRun.prepare_full_text_group`): the store's Europe PMC fullTextXML first,
then Europe PMC live through the census's own fetcher (`study_cli.europe_pmc_fetcher`:
the census inputs' User-Agent, 0.4 s between a thread's requests, retries with
backoff), with `--fetch-workers` threads, review by review until
`--full-text-entries` entries are retrieved. Europe PMC answers fullTextXML with
HTTP 500 for every PMCID outside its open-access subset; the fetcher checks a
server error against the PMCID's Europe PMC record, so such a PMCID costs two
requests and is a definitive miss, while a server error for a PMCID in the
subset stays transient (`left open by a transient failure`). A PMCID the search
finds no record of is never a miss on one answer: under the census's rule (its
search finds no record over at least 60 s, and then one search for it and a
sentinel, another PMCID the fetcher saw in the index or else PMC2000001, finds
the sentinel's record only; docs/census-instrument.md) it can be one only when
a later candidate of the same probe run cites the same PMCID a minute or more
after the first answer. Otherwise it is left open by a transient failure after
four requests and 7 s of backoff. Every cited work's PMCID came from a Europe
PMC record, so neither should occur. The `Full texts:` line counts misses by the
fetcher's reason (`absentNotOpenAccess`, `absentNoRecord`, `absentNotFound` in
the report's `fullTextInputs.fullTexts`). Fetched full texts are kept in the
cache directory as Europe PMC served them (`PMC<n>.xml`, mode 0600; a
definitive miss as `PMC<n>.absent`, holding the reason), so a later run with the
same `--full-text-cache` fetches nothing it asked for before, except candidates
left transient and a PMCID missed for want of any record, which are never kept
and are asked for again. The calls are the census's: five retrieved
entries of one review per call, each review's remainder in its last call
(`citation_census.full_text_chunks`), built by `census_runner.full_text_call`
(the windows in the FULL TEXT field, reasoning `low`,
`max_tokens = max(3000, 700 * items)`, the journal label in the system prompt).

**The cell.** One cell, `STREAMSx5` (default `20x5`). It sends each prepared
call once, in order, and ends when the last call has been handed out or at
`--cell-seconds`, whichever comes first; the calls in flight then finish within
`--drain-seconds`. The steady-state window and the abort rules are the abstract
tier's; when the calls run out the window ends at the earliest start of the
calls still in flight. Twenty streams is the default because a census's
full-text calls arrive at the pace of its Europe PMC retrieval (four fetch
threads, each spacing its requests 0.4 s apart, and a review's calls only once
all of its entries are retrieved), not at the abstract tier's sixty streams.

**Measurements.** Per cell, in `cells[].fullText`, over answered calls:

- entries `valid` (an accepted line), `missing` (no line at all: the judge
  returned fewer lines than items) and `malformed` (a line the validator
  rejected: a known label with a bad category names its entry; another rejected
  line counts for one entry still without a valid line), with their shares,
  overall and by the call's items (1 to 5);
- calls the judge refused (HTTP 400, 413 or 422): transport failures in the
  probe, while the census counts their entries as missing; `censusInvalidShare`
  adds them, and the exit line applies it;
- per call: calls that were all valid, had a missing or a malformed entry,
  empty completions, truncated calls (`finish_reason` `length`), and calls by
  the number of entries they left invalid;
- tokens per entry (prompt and output) and entries per second;
- `census`: what citation-census@1 does with an entry left without a parseable
  verdict. It has no repair pass at this tier: the census records such an
  entry as missing, never sends it again, and its gate
  `fullTextInvalidOutputsAtMostOnePercent` counts it; only a call without any
  response is sent again (whole, in a further pass). The share the probe
  measures is therefore the census's own share.

The report's `fullTextInputs` records the store version, the reviews and
entries considered, prepared and retrieved, where each full text came from
(store, Europe PMC, the cache directory), the calls by items and the window
sizes. The exit line reads "Full-text tier (census gate
fullTextInvalidOutputsAtMostOnePercent ...): PASS|FAIL" and applies the census
limit of 1 % to the entries without a parseable verdict; entries per second are
reported, never a criterion.

**Hypothetical single-item resend (`--hypothetical-repair`).** So that a
predeclared repair can be decided with numbers, every entry an answered call
left without a valid verdict is sent again alone, by the same stream right
after the call, as the census's own full-text call of one item
(`census_runner.full_text_call([entry], ...)`: the same system prompt, citing
sentences and window, reasoning `low`, `max_tokens` 3000); one resend per entry
and nothing further. A call without any response is not resent. citation-census@1
sends no such call, so `fullText.hypotheticalRepair` is labelled hypothetical: the
share after it by items per call, the resend calls (recovered, still invalid,
tokens, latency) and the entries per second with them. Adopting it would change
the instrument. The exit line keeps the census's own share and names the
hypothetical one beside it.

**Dry run.** `--dry-run --tier full-text` opens the store, counts its reviews for
the prefixes and prepares the first five offline: nothing is sent, fetched or
looked up, so cited works get metadata only from the store's Europe PMC records
and only the full texts the store or an existing cache directory hold are used. It prints the inputs, the request sizes of those calls and an estimate
of the reviews and fetch time the target needs.

## Exit criterion

Only one cell decides: the instrument's own geometry (40 items per call, the
only one a census sends) at `--allowed-streams`, the census's concurrency. It
decides when

- it measured the server: it completed, or was stopped by the saturation or
  unreadable-metrics rule with its calls finished (a cell stopped by transport
  failures, interrupted or skipped never decides);
- every answer came from the instrument's required model (a census accepts no
  label from another model and pauses on model drift);
- its steady-state window is at least two p95 call latencies long, or its
  whole planned window when that is shorter.

It passes when its entries per second reach `--target-rate`. When tokens per
entry are predeclared (`--expect-output-tokens`, `--expect-prompt-tokens` or
`--profile`) and the cell's output tokens per entry depart from them by more
than `--expect-tolerance`, the line warns and also gives a token-normalised
rate (the rate scaled by measured over expected output tokens per entry,
reading throughput as output-token bound); the lower of the two decides.
Otherwise the line reads "not assessed" with the reason (for a window that is
too short, with the indicative rate and the command to rerun the cell alone
for longer). Cells with 1, 5 or 10 items per call at or below the allowed
concurrency are reported as exploratory and never pass the criterion. One
line, wrapped here:

```text
Exit criterion (≥ <target-rate> entries/s with the instrument's 40 items per call at the allowed concurrency of <allowed-streams> streams):
PASS - 60x40: <rate> entries/s over a 236 s steady-state window; invalid outputs 0.10 %, transport failures 0.00 %
(within the census limits of 1 % and 0.5 %); tokens per entry: prompt 512.3, output 41.6 against the expected
output 43 (-3 %); served model `nvidia/Qwen3.8-Flash-Next-NVFP4`. Exploratory geometries at ≤ 60 streams (not the
instrument's, never deciding): best 60x10 at <rate> entries/s.
```

The census gates for invalid outputs and transport failures are reported, not
part of the pass. A failing line ends with "the author decides".

## Output

`--output` must name a new or empty directory. It receives:

- `capacity-probe.json` (`format: ocsl-capacity-probe@2`), rewritten after
  every cell: the code revision, the instrument id and digest, the request
  settings, the endpoint without credentials, the synthetic profile and its
  source, the expectation, the grid and its timing, the abort rules, the
  decision rules, the preflight, the baseline, one summary per cell, the
  shared-server verdict, the exit criterion and the allowed concurrency;
- `capacity-probe.md`: the exit criterion and allowed-concurrency lines, the
  shared-server evidence, a throughput table and a token, load and client
  table;
- `calls.jsonl`: the call log.

With `--tier full-text` it also receives `full-text-inputs.sqlite3`, the
scratch census database of the prepared reviews (their loci, works and
windows; nothing in it was judged), and `full-text-cache/` unless
`--full-text-cache` names another directory.

The report's `state` is `completed`, `aborted` (a rule stopped the grid;
`abort` names the rule and the cell), `interrupted` or `failed`; each cell's
`state` is `completed`, `saturated`, `metrics-lost`, `aborted`, `interrupted`
or `skipped` (with its reason).

The markdown also goes to standard output (the report with `--json`); progress
goes to standard error as JSON lines (`baseline`, `cell-start`,
`cell-skipped`, `heartbeat`, `cell-end`, `abort`, `done`). The exit status is
0 when the probe ran, also when a rule stopped the grid early; 5 when settings
are refused, the endpoint cannot be reached or refuses the key, or the metrics
URL cannot be read; 130 when interrupted, after the report was written with
state `interrupted`.

## Dry run

`--dry-run` prints, without contacting any endpoint or reading a key: the grid
in run order with each cell's role, the skipped cells and the cells above the
allowed concurrency that run only if the server can be shown unshared; the
deciding cell; the profile and the expected tokens per entry; request sizes
from the first 32 calls of each geometry (user characters, body bytes, prompt
tokens estimated at four characters per token, in total and per entry,
`max_tokens`, the share of cut abstracts); the expected load per cell (items in
flight, prompt tokens in flight, a KV ceiling of streams x (p95 prompt tokens +
`max_tokens`), the latency budget, that is the mean call latency at which the
cell reaches the target, streams x items / target); the abort rules; and the
duration estimate. `--json` prints the plan as JSON.

## Differences from a census run

- First calls only, unless `--repair`: the census repairs labels without a
  parseable line once, so its invalid share is at most the probe's first-call
  share; `--repair` measures it after the census's own repair call. A repair
  call the judge refuses (HTTP 400, 413 or 422) leaves the probe's labels open,
  where the census counts them invalid.
- One attempt per call: the census makes up to three attempts with backoff, so
  the probe's transport failures per call bound the census's from above.
- The census counts an empty completion, and a refusal of the request itself
  (HTTP 400, 413 or 422), as an answered call whose entries are invalid; the
  probe classes refusals as transport failures with the server's message and
  counts empty completions as answered calls with all entries invalid.
- The census transport opens a connection per request; on a local network
  that costs little next to call latencies of seconds, but a census rate below
  the probe's would show it.
- Synthetic entries match the size distributions of the profile, not the
  wording of real reviews and abstracts; output lengths follow the model's
  answers to synthetic text, which is why the exit line compares tokens per
  entry with a predeclared expectation.
- The abstract tier is probed over synthetic entries; the full-text tier over
  real loci that are not the census's escalated entries (the probe never reads a
  verdict).

## Tests

`tests/test_capacity_probe_full_text.py` builds a literature-evidence@2
projection with retained reviews and Europe PMC records, and checks that the
full-text tier's calls are byte for byte those the census's own full-text pass
sends for the same review, the store-first retrieval and the cache directory,
the valid, missing and malformed shares per call and overall, the hypothetical
resend, the refusal of any other store and of entitled reviews without an
on-premises judge, and a dry run that sends and fetches nothing.

`tests/test_capacity_probe.py` runs the probe against a threaded mock
OpenAI-compatible server with configurable latency, failures, saturation,
fingerprint, another tenant and a metrics endpoint: request fidelity to the
instrument's builders, connection reuse, rates, the steady-state window
(checked against a processor-sharing simulation, where stopping at the window's
end reads high and the probe's window does not), abort rules, invalid outputs,
the shared-server checks, cells above the allowed concurrency, the decision
rules, profiles from a census database, the repair pass (`RepairTests`: the
repair calls' bodies equal the census runner's, shares before and after by
items per call, failed and unanswered repairs), the report shape and that the key
never reaches the report, the call log or the output streams, in whole or in
part.
