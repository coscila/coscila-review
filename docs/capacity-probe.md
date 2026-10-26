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
# The plan: grid, roles, request sizes and expected load. Nothing is sent and no key is read.
ocsl-review capacity probe --dry-run --base-url "$JUDGE_URL" --metrics-url "$JUDGE_METRICS_URL"

# The full grid: 30, 60, 90 and 120 streams x 1, 5, 10 and 40 items per call, 300 s per cell.
ocsl-review capacity probe --base-url "$JUDGE_URL" --api-key-env JUDGE_KEY_VARIABLE \
  --metrics-url "$JUDGE_METRICS_URL" --proxy-pid "$JUDGE_PROXY_PID" --output probes/judge-grid

# The deciding cell alone, measured for longer (for example when the grid's line says its window was too short).
ocsl-review capacity probe --base-url "$JUDGE_URL" --api-key-env JUDGE_KEY_VARIABLE \
  --metrics-url "$JUDGE_METRICS_URL" --cells 60x40 --cell-seconds 900 --output probes/judge-60x40

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
| `--allowed-streams`, `--target-rate` | 60, 40 | the exit criterion: entries/s with the instrument's geometry at the allowed concurrency |
| `--profile` | built-in | size profile and expected tokens per entry from `capacity profile` |
| `--expect-output-tokens`, `--expect-prompt-tokens` | none (or the profile's) | predeclared tokens per entry of the census's real entries |
| `--expect-tolerance` | 0.2 | departure from the expected tokens per entry that warns and normalises the rate |
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
Exit criterion (≥ 40 entries/s with the instrument's 40 items per call at the allowed concurrency of 60 streams):
PASS - 60x40: 44.5 entries/s over a 236 s steady-state window; invalid outputs 0.10 %, transport failures 0.00 %
(within the census limits of 1 % and 0.5 %); tokens per entry: prompt 512.3, output 41.6 against the expected
output 43 (-3 %); served model `nvidia/Qwen3.8-Flash-Next-NVFP4`. Exploratory geometries at ≤ 60 streams (not the
instrument's, never deciding): best 60x10 at 52.0 entries/s.
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

- First calls only: the census repairs labels without a parseable line once,
  so its invalid share is at most the probe's.
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
- Only the abstract tier is probed.

## Tests

`tests/test_capacity_probe.py` runs the probe against a threaded mock
OpenAI-compatible server with configurable latency, failures, saturation,
fingerprint, another tenant and a metrics endpoint: request fidelity to the
instrument's builders, connection reuse, rates, the steady-state window
(checked against a processor-sharing simulation, where stopping at the window's
end reads high and the probe's window does not), abort rules, invalid outputs,
the shared-server checks, cells above the allowed concurrency, the decision
rules, profiles from a census database, the report shape and that the key
never reaches the report, the call log or the output streams, in whole or in
part.
