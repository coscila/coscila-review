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

It is an operator command: it refuses to run inside a chat workspace, because
the load it puts on a shared judge is the operator's decision. Run it on the
host that will run the census, against the same endpoint.

## Running it

```bash
# The plan: grid, request sizes and expected load. Nothing is sent and no key is read.
ocsl-review capacity probe --dry-run --base-url "$JUDGE_URL" --metrics-url "$JUDGE_METRICS_URL"

# The full grid: 30, 60, 90 and 120 streams x 1, 5, 10 and 40 items per call, 300 s per cell.
ocsl-review capacity probe --base-url "$JUDGE_URL" --api-key-env JUDGE_KEY_VARIABLE \
  --metrics-url "$JUDGE_METRICS_URL" --proxy-pid "$JUDGE_PROXY_PID" --output probes/judge-grid

# One cell, measured for longer.
ocsl-review capacity probe --base-url "$JUDGE_URL" --api-key-env JUDGE_KEY_VARIABLE \
  --metrics-url "$JUDGE_METRICS_URL" --cells 60x40 --cell-seconds 900 --output probes/judge-60x40
```

`--base-url` is the base the chat-completions path is appended to (for vLLM,
`http://HOST:PORT/v1`); `--metrics-url` is the Prometheus endpoint of the vLLM
server itself (`http://HOST:PORT/metrics`), not of a proxy in front of it. The
full grid takes about 16 x (300 s + 10 s pause), 83 minutes, plus up to
`--drain-seconds` per cell; the dry run prints the estimate.

| Option | Default | Meaning |
|---|---|---|
| `--streams`, `--items` | `30,60,90,120`, `1,5,10,40` | the grid; items per call are at most the instrument's 40 |
| `--cells` | all | run only these cells, `STREAMSxITEMS`, comma-separated or repeated |
| `--order` | `load` | `load`: cells within `--allowed-streams` first, each group by ascending streams x items; `streams`; `items`; `as-given` (the `--cells` order); `shuffle` (seeded) |
| `--max-streams` | none | skip cells with more streams |
| `--cell-seconds` | 300 | fixed duration of a cell |
| `--warmup-seconds`, `--ramp-seconds` | 30, 10 | the measured window starts after the warm-up; stream starts are staggered over the ramp |
| `--drain-seconds`, `--pause-seconds` | 180, 10 | after a cell, calls in flight may finish for this long; then an idle pause |
| `--request-timeout` | 300 | read timeout per call (connections open within 10 s) |
| `--sample-seconds` | 5 | server load sampling interval |
| `--abort-*`, `--saturation-*` | see [abort rules](#abort-rules) | |
| `--shared-tolerance` | 1 | requests beyond ours that a sample tolerates while our calls are in flight |
| `--allowed-streams`, `--target-rate` | 60, 40 | the exit criterion: entries/s at the allowed concurrency |
| `--model` | the instrument's required model | model name to request |
| `--journal` | a synthetic name | the system prompt's journal parameter |
| `--seed` | 1 | seed of the synthetic entries |
| `--proxy-pid` | none | process whose resident memory is sampled with the server load (repeatable) |

## What it sends

Every request is the instrument's first abstract-tier call for one batch,
built by `citation_census`: the system prompt with the journal parameter, the
per-batch user content, temperature 0, reasoning effort `none`,
`max_tokens = max(3000, 700 * items)` and streaming off. The probe sends no
repair pass. The request body bytes are recorded by sha256 per call.

The entries are synthetic and deterministic: every value derives from the
seed, the cell and the entry's position, built from a fixed vocabulary. No
corpus data and no entitled text are involved. Each cell has its own call
sequence, so no cell is served from another cell's prefix cache; the system
prompt is shared by all calls, as it is in a census.

| Property | Synthetic distribution |
|---|---|
| entries per review | lognormal, median 110 (8 to 600); batches never cross reviews, so 40-item cells average fewer than 40 items per call |
| citing sentences per entry | 1, 2 or 3 (weights 0.55, 0.25, 0.2), each with a section trail and a citation marker |
| citing sentence length | lognormal, median 170 characters (60 to 480) |
| abstract length | lognormal, median 1,500 characters (200 to 4,000), 30 % structured, 5 % missing; the builder applies the instrument's abstract cut, so sent abstracts stop at the cut |
| cited work | title of 6 to 24 words, venue, year, rare retraction flag |
| printed reference | authors, title, venue, year, volume, pages and a synthetic DOI; generated and never sent, as the instrument requires |

Transport is direct HTTP/1.1 to `--base-url` + `/chat/completions` over a
keep-alive connection pool sized to the cell's stream count: each stream
reuses one connection, a connection the server closed is reopened before the
next call, and proxies from the environment are not used. The key is read
from the environment variable that `--api-key-env` names; it is sent only as
the `Authorization` header to the judge (never to the metrics endpoint), and
it is never printed, logged or written: the report records the variable name
and whether a key was sent, and any echo of the key in a response, error
message or progress line is replaced by `[redacted]`. A base URL with
credentials, a query or a fragment is refused. Before the first cell, a
`GET /models` checks the endpoint; HTTP 401 or 403 stops the probe before any
cell runs.

## A cell

1. The server load is sampled once (the idle "before" sample).
2. The streams start over `--ramp-seconds`; each sends its next call as soon
   as the previous one ends, until `--cell-seconds` have passed.
3. The measured window runs from `--warmup-seconds` to the end of the cell.
4. Calls still in flight may finish for up to `--drain-seconds`; calls that
   are still open then are abandoned (their connections are closed, which
   lets vLLM cancel them). Abandoned calls are reported apart and are no
   transport failures.
5. After `--pause-seconds`, the idle "after" sample closes the cell.

## Measurements

Per cell, in `cells[]` of the report:

- **Entries per second** (`entries.perSecond`): entries with a line the
  instrument's validator (`citation_census.parse_labels`, as the census
  applies it) accepts, per second of the measured window. Each call's valid
  entries are spread over the call's own duration and counted where it
  overlaps the window, so the rate does not depend on where the window cuts
  long calls. `perSecondCompleted` counts only calls that ended inside the
  window; `meanConcurrency` is the mean number of our calls in flight, and
  `clientLimited` flags a mean below 90 % of the streams.
- **Calls**: sent, answered, failed, abandoned, mean items per call.
- **Tokens**: prompt and completion tokens from `usage`, per call, per entry
  and per second, and the cached share (`prompt_tokens_details.cached_tokens`
  over prompt tokens) when the server reports it.
- **Latency**: p50, p95, p99, mean and maximum of answered calls that started
  after the warm-up.
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
  preemption, prompt-token, generation-token and prefix-cache counters, and
  our own calls in flight at that moment.
- **Client**: the probe's resident memory, the pool size, connections opened
  and reused, and connections the server closed; with `--proxy-pid`, that
  process's resident memory.

`calls.jsonl` holds one line per call: cell, stream, batch, items, body size
and sha256, outcome, failure class, status, start, end, tokens, served model
and fingerprint.

## Abort rules

Both rules stop the cell and the grid; the remaining cells are listed in the
markdown as a `--cells` argument for a later run.

- **Transport failures.** More than `--abort-failure-share` (5 %) of the calls
  that finished in the trailing `--abort-window-seconds` (60 s) failed, once
  that window holds `--abort-min-calls` (20) calls or `--abort-min-failures`
  (3) failures. The calls in flight are cut off, and the cell is `aborted`: it
  is never the best cell. A defect of the probe itself stops the grid the same
  way.
- **Saturation.** Every server-load sample after the warm-up reports
  saturation for more than `--saturation-seconds` (60 s). A sample is
  saturated when at least `--saturation-waiting` (1) requests wait on the
  server, when the preemption counter rose since the previous sample (the KV
  cache was full), when HTTP 429 or 503 answers arrived since the previous
  sample, or, with `--saturation-kv`, when KV cache usage reaches that share.
  A sample without metrics breaks the streak. The cell stops sending, its
  calls in flight finish, and the cell is `saturated`: its window measured the
  server at capacity, so it stays eligible for the exit criterion. Cells with
  more load could not add throughput and would only queue other clients'
  requests behind ours.

The default order runs the cells within the allowed concurrency first, so a
rule that fires above it never costs the cells the exit criterion is assessed
on. To measure a saturated cell for its full duration, run it alone with a
larger `--saturation-seconds`.

## Shared server and the allowed concurrency

The census concurrency stays at `--allowed-streams` (60) unless the report
shows that the server is not shared. The report says `shared: true` on any
evidence of other clients:

- before the first cell (three idle samples): requests on the server beyond
  the tolerance in two samples, or token counters that advance between the
  samples;
- in a cell's samples: more requests running and waiting on the server than
  our own calls in flight, beyond `--shared-tolerance` after allowing for our
  calls abandoned in the last 30 s, in two or more samples;
- across a cell: more tokens generated between the idle samples before and
  after it than our answers carried, beyond what our unanswered calls could
  have generated (their `max_tokens`, also for calls abandoned in the 30 s
  before the cell) plus a margin, the larger of 1,000 tokens and 2 % of ours.

It says `shared: false` only when there is no such evidence, the metrics were
read before the first cell and in every cell, and they demonstrably cover our
own calls: in every cell the server's running and waiting requests account
for at least 80 % of our calls in flight (median over the measured window),
and its generated tokens for at least 80 % of ours, each where it can be
computed. Metrics of one replica behind a load balancer, or of a server behind
a proxy that queues our calls, fail this check, and the answer is unknown
(`null`), as it is without `--metrics-url`. The markdown lists the evidence,
or what kept the probe from showing an unshared server.

`allowedConcurrency` states the result: the allowed concurrency stays at
`--allowed-streams`, and only when the server is not shared does the report
say that the author may allow more and name the best cell above it. Cells
above the allowed concurrency load every other user of a shared server; when
the server is known to be shared, add `--max-streams 60`.

## Exit criterion

The exit criterion line names the best cell with at most `--allowed-streams`
streams among the completed and saturated cells, and passes when its entries
per second reach `--target-rate` (one line; wrapped here):

```text
Exit criterion (≥ 40 entries/s at the allowed concurrency of 60 streams): PASS - best cell at ≤ 60 streams:
60 streams x 40 items per call, 44.5 entries/s; invalid outputs 0.10 %, transport failures 0.00 %
(within the census limits of 1 % and 0.5 %).
```

The line also says when the best cell was stopped by the saturation rule and
after how many seconds of measurement, and whether the cell met the census
gates for invalid outputs and transport failures; these are reported, not part
of the pass. A failing line ends with "the author decides".

## Output

`--output` must name a new or empty directory. It receives:

- `capacity-probe.json` (`format: ocsl-capacity-probe@1`), rewritten after
  every cell: the code revision, the instrument id and digest, the request
  settings, the endpoint without credentials, the synthetic profile, the grid
  and its timing, the abort rules, the preflight, the baseline, one summary
  per cell, the shared-server verdict, the exit criterion and the allowed
  concurrency;
- `capacity-probe.md`: the exit criterion and allowed-concurrency lines, the
  shared-server evidence, a throughput table and a token, load and client
  table;
- `calls.jsonl`: the call log.

The report's `state` is `completed`, `aborted` (a rule stopped the grid;
`abort` names the rule and the cell), `interrupted` or `failed`; each cell's
`state` is `completed`, `saturated`, `aborted`, `interrupted` or `skipped`
(with its reason).

The markdown also goes to standard output (the report with `--json`); progress
goes to standard error as JSON lines (`baseline`, `cell-start`, `heartbeat`,
`cell-end`, `abort`, `done`). The exit status is 0 when the probe ran, also
when a rule stopped the grid early; 5 when settings are refused, the endpoint
cannot be reached or refuses the key; 130 when interrupted, after the report
was written with state `interrupted`.

## Dry run

`--dry-run` prints, without contacting any endpoint or reading a key: the grid
in run order with skipped cells; request sizes from the first 32 calls of each
geometry (user characters, body bytes, prompt tokens estimated at four
characters per token, `max_tokens`, the share of cut abstracts); the expected
load per cell (items in flight, prompt tokens in flight, a KV ceiling of
streams x (p95 prompt tokens + `max_tokens`), the latency budget, that is the
mean call latency at which the cell reaches the target, streams x items /
target); the abort rules; and the duration estimate. `--json` prints the plan
as JSON.

## Differences from a census run

- First calls only: the census repairs labels without a parseable line once,
  so its invalid share is at most the probe's.
- One attempt per call: the census makes up to three attempts with backoff, so
  the probe's transport failures per call bound the census's from above.
- The census counts an empty completion, and a refusal of the request itself
  (HTTP 400, 413 or 422), as an answered call whose entries are invalid; the
  probe classes refusals as transport failures with the server's message and
  counts empty completions as answered calls with all entries invalid.
- The census transport opens a connection per request; on a local network
  that costs little next to call latencies of seconds.
- Synthetic entries match the size distributions above, not the wording of
  real reviews and abstracts; output lengths follow the model's answers to
  synthetic text.
- Only the abstract tier is probed.

## Tests

`tests/test_capacity_probe.py` runs the probe against a threaded mock
OpenAI-compatible server with configurable latency, failures, saturation,
fingerprint and a metrics endpoint: request fidelity to the instrument's
builders, connection reuse, rates, abort rules, invalid outputs, the
shared-server checks, the report shape and that the key never reaches the
report, the call log or the output streams.
