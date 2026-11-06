# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review study replay``: the census's model verdicts re-derived from the stored responses, without model calls.

Every verdict a model call produced is a row of the scores database's append-only ``history``: one that names its call
and its position in the call, or an abstract-tier entry recorded as invalid because it still had no parseable line after
the answered first and repair calls of its batch. Replay reads the item of each such call from the broker that settled
it: over the run principal's socket (``GET /jobs/<jobId>/results``, as the census's broker transport reads it) or from
the JSON lines the lab exports from the daemon's item results (``--responses``). The response the daemon stores for a
settled call is the item's output when the item completed, or the judge server's refusal of the request (HTTP 400, 413
or 422, ``census_runner.judge_refusal``), which the census counts as an answer without labels. Before it parses
anything replay checks the item against the receipts:

- the request digest the item reports (``requestSha256``) equals the broker's request digest in the call's receipt
  (``call_transport.broker_request_sha256``) and the canonical digest of the request the instrument built
  (``call_transport.request_sha256``);
- a completed item: its ``outputDigest`` equals the digest of its output computed as the daemon computes it
  (``output_digest``: a string as its UTF-8 bytes, any other value as ``JSON.stringify`` writes it), and the
  instrument's required model served it;
- a refused item: it stores no output (the receipt's output digest is the digest of no output), and the call's
  receipt records the same refusal (error code and HTTP status).

A response that passes is parsed exactly as the census parsed it (``census_runner.broker_output_text`` and
``census_runner.parse_answer`` at the call's tier: the first and repair calls of an abstract-tier batch, full-text
calls); an empty completion or a refusal yields no verdict. Each re-derived entry is compared with the history row of
that call and position on outcome, label, category, support verdict and escalation, and on the quote unless the
recorded quote was redacted (an entitled review in a frozen copy). A verdict the answer yields that the history lacks is
a mismatch too. An entry recorded as invalid replays when none of the answered calls of its batch that carried it
yields a parseable line for it and an answered repair call carried it (for a sensitivity variant, whose batch runs again
whole until a run settles, the calls of its last run). Entries no answered call decided are outside replay: those
decided without a model call (never cited, no identified work) and a primary batch's entries closed after none of its
calls was answered (``invalid:no-response``).

A call without a stored response (a direct call, a history call id without a receipt, a receipt without a broker job, an
item the broker does not hold, one that did not settle or failed in transport) makes its entries unavailable; a
response that fails a check makes them digest failures. Over the socket, only WORKER_NOT_FOUND means the broker holds
no such job. A socket that cannot serve the run's results (a revoked principal, a socket without the principal's
routes) is invalid input, and one that stops answering or keeps refusing a page is an outage: the replay then ends as
a transient failure and records nothing, so an outage never reads as calls without a stored response.

The results go to ``replay_calls``, ``replay_entries`` and ``replay_meta`` (``scores_db.REPLAY_DDL``): in the census
database itself, or with ``--output`` in a new SQLite file while the census database is only read. They hold no output
text: verdicts are compared field by field and quotes are kept as digests. ``replay_meta`` records the extent of the
history replayed (rows and last id), so a facts export can tell whether the replay still covers the frozen history, and
with ``--output`` the sha256 of the census database read. ``reportSha256`` is the sha256 over the rows of both result
tables in a fixed order (``report_sha256``), so it can be recomputed from them.
"""
from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from . import citation_census as C
from . import scores_db
from .census_broker import SOCKET_ENV, BrokerClient, BrokerRefused, BrokerUnavailable, _js_number, _js_string, check_socket
from .census_runner import (PARSER_STAGES, broker_output_text, call_answered, call_id_for, canonical, code_revision,
                            judge_refusal, parse_answer, refusal_error, utc_now, writer_lock)
from .errors import ReviewError, TransientError, ValidationError
from .util import CommandOutcome

FORMAT = "ocsl-census-replay-v1"
EXIT_REPLAYED, EXIT_INVALID_INPUT, EXIT_NOT_REPLAYED = 0, 2, 3
ENTRY_OUTCOMES = ("replayed", "mismatched", "unavailable", "digest-failure", "outside-replay")
CALL_OUTCOMES = ("replayed", "mismatched", "unavailable", "digest-failure")
CALL_CHUNK = 500            # calls replayed together: their history rows read at once, their items fetched in parallel
FETCH_WORKERS = 8
FETCH_ATTEMPTS = 3
MAX_RESULT_PAGES = 10_000
# The daemon's job ids (UUIDs) as its worker routes accept them; a receipt naming another id names no broker job.
JOB_ID = re.compile(r"[a-f0-9-]+")
# Refusals of a socket that cannot serve this run's results at all: a revoked or expired principal, or a socket without
# the principal's worker routes. Asking again cannot help; the replay needs another socket or exported results.
SOCKET_REFUSALS = frozenset({"PRINCIPAL_REVOKED", "PRINCIPAL_EXPIRED", "UNKNOWN_PRINCIPAL_ROUTE", "UNKNOWN_WORKER_ROUTE"})
CALL_COLUMNS = ("call_id", "job_id", "item_id", "outcome", "output_sha256", "request_sha256", "n_entries", "n_replayed",
                "n_mismatched", "detail_json")
ENTRY_COLUMNS = ("call_id", "position", "label", "outcome", "recorded_json", "replayed_json", "journal", "doi", "tier",
                 "variant", "history_id")
HISTORY_COLUMNS = ("history_id", "journal", "doi", "label", "tier", "variant", "call_id", "position", "outcome",
                   "category", "support_verdict", "escalate", "quote")
# The compared fields of a verdict besides the quote (in the history's column names).
COMPARED = ("outcome", "category", "support_verdict", "escalate")
# The abstract-tier basis of a primary entry left without a parseable line after its batch's answered calls.
NO_PARSEABLE_OUTPUT = "invalid:no-parseable-output"


class ReplayInputError(ReviewError):
    """The replay cannot start from what it was given (exit code 2)."""


def _short(value: Any, limit: int = 200) -> Any:
    """A string from a stored item or the broker, bounded before it is recorded."""
    return value[:limit] if isinstance(value, str) else value


# ----------------------------------------------------------------------------- output digests as the daemon computes them
class _Absent:
    def __repr__(self) -> str:
        return "ABSENT"


ABSENT = _Absent()      # an item result without an output field (JavaScript undefined)
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]{0,9}")
_MAX_ARRAY_INDEX = 2 ** 32 - 2
_SURROGATE = re.compile("[\ud800-\udfff]")


def _array_index(key: str) -> int | None:
    """The array index a property key names in JavaScript (0 to 2**32 - 2, written canonically), or None."""
    if _ARRAY_INDEX.fullmatch(key):
        value = int(key)
        if value <= _MAX_ARRAY_INDEX:
            return value
    return None


def js_json(value: Any) -> str:
    """``JSON.stringify(value)`` for a value JSON parsing produced: no whitespace, strings and numbers as JavaScript
    writes them, and object keys in JavaScript's property order (array-index keys ascending, then the other keys in the
    order the parser met them; a repeated key keeps its first place and its last value, as in both parsers)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _js_number(value)
    if isinstance(value, str):
        return _js_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(js_json(item) for item in value) + "]"
    if isinstance(value, Mapping):
        keys = list(value)
        if not all(isinstance(key, str) for key in keys):
            raise ValidationError("JSON objects have string keys")
        indices = sorted((key for key in keys if _array_index(key) is not None), key=_array_index)
        ordered = indices + [key for key in keys if _array_index(key) is None]
        return "{" + ",".join(_js_string(key) + ":" + js_json(value[key]) for key in ordered) + "}"
    raise ValidationError(f"JSON cannot hold {type(value).__name__}")


def _utf8(text: str) -> bytes:
    """The UTF-8 bytes Node writes for a JavaScript string: a surrogate pair is one code point, a lone surrogate
    becomes U+FFFD."""
    if not _SURROGATE.search(text):
        return text.encode("utf-8")
    out, index = [], 0
    while index < len(text):
        ch = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if "\ud800" <= ch <= "\udbff" and "\udc00" <= following <= "\udfff":
            out.append(chr(0x10000 + ((ord(ch) - 0xD800) << 10) + (ord(following) - 0xDC00)))
            index += 2
            continue
        out.append("�" if "\ud800" <= ch <= "\udfff" else ch)
        index += 1
    return "".join(out).encode("utf-8")


def output_text(output: Any = ABSENT) -> str:
    """The text a broker receipt's ``outputDigest`` covers (the daemon's ``outputText``): nothing for an item without
    output, a string as it is, any other value as ``JSON.stringify`` writes it."""
    if output is ABSENT:
        return ""
    return output if isinstance(output, str) else js_json(output)


def output_digest(output: Any = ABSENT) -> tuple[str, int]:
    """(sha256, bytes) of an item output as the daemon digests it (``outputDigest``)."""
    data = _utf8(output_text(output))
    return hashlib.sha256(data).hexdigest(), len(data)


# ----------------------------------------------------------------------------- stored item results
@dataclass
class Fetched:
    """A call's stored item result, or why there is none."""

    item: dict[str, Any] | None
    reason: str | None = None


class SocketItems:
    """Item results over the run principal's socket, read page by page as the census's broker transport reads them."""

    kind = "broker-socket"

    def __init__(self, path: str | None = None, *, client: Any = None, workers: int = FETCH_WORKERS,
                 attempts: int = FETCH_ATTEMPTS, sleep: Callable[[float], None] = time.sleep):
        self.client = client if client is not None else BrokerClient(check_socket(str(path or "")))
        self.workers, self.attempts, self.sleep = max(1, workers), max(1, attempts), sleep

    def fetch(self, keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], Fetched]:
        unique = sorted(set(keys))
        if not unique:
            return {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.workers, len(unique))) as pool:
            return dict(zip(unique, pool.map(lambda key: self._item(*key), unique)))

    def _item(self, job_id: str, item_id: str) -> Fetched:
        if not JOB_ID.fullmatch(job_id):
            return Fetched(None, "the receipt names no job id the broker issues")
        found, after = None, 0
        for _ in range(MAX_RESULT_PAGES):
            page = self._page(job_id, after)
            if isinstance(page, Fetched):
                return page
            for result in page.get("results") or []:
                if isinstance(result, dict) and result.get("id") == item_id:
                    found = result          # a changed-state feed: the latest state of the item wins
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not isinstance(cursor, int) or isinstance(cursor, bool) or cursor <= after:
                break
            after = cursor
        return Fetched(found, None if found is not None else "the broker job holds no item of the call")

    def _page(self, job_id: str, after: int) -> dict[str, Any] | Fetched:
        """One result page, or why the broker holds none. Only WORKER_NOT_FOUND means that the broker holds no such job
        for this principal. A refusal that means the socket cannot serve this run's results is invalid input; a socket
        that stops answering, or keeps refusing the page for any other reason, ends the replay as a transient failure
        (the same command can run again), so an outage never reads as calls without a stored response."""
        route = f"/jobs/{urllib.parse.quote(job_id, safe='')}/results?after={after}&limit=100"
        failure = "no answer"
        for attempt in range(self.attempts):
            try:
                return self.client.request("GET", route)
            except BrokerRefused as error:
                if error.code == "WORKER_NOT_FOUND":
                    return Fetched(None, _short(f"the broker holds no job {job_id} for this principal ({error.code})"))
                if error.code in SOCKET_REFUSALS:
                    raise ReplayInputError(f"the broker socket does not serve this run's results ({error.code}, HTTP "
                                           f"{error.status}): give the run principal's socket, or the item results the "
                                           "lab exported (--responses)") from error
                failure = f"the broker refused the job's result page: {error.code} (HTTP {error.status})"
            except BrokerUnavailable as error:
                failure = str(error)
            if attempt + 1 < self.attempts:
                self.sleep(2.0 * (attempt + 1))
        raise TransientError(f"the broker socket did not serve a result page ({failure[:200]}); run the replay again")


class ResponseItems:
    """Item results the lab exported as JSON lines ``{jobId, itemId, state, output, outputDigest, requestSha256,
    servedModel}``, with ``errorCode``, ``httpStatus`` and ``attempts`` for an item that did not complete. The file is
    indexed once (a later line for the same job and item replaces an earlier one, as in the daemon's changed-state
    result feed) and each item is read again when its call is replayed, so the outputs are never all held in memory."""

    kind = "responses"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.index: dict[tuple[str, str], tuple[int, int, int]] = {}
        try:
            handle = self.path.open("rb")
        except OSError as error:
            raise ReplayInputError(f"--responses cannot be read: {type(error).__name__}") from error
        with handle:
            offset = 0
            for number, raw in enumerate(handle, 1):
                if raw.strip():
                    value = self._parse(raw, number)
                    self.index[(value["jobId"], value["itemId"])] = (offset, len(raw), number)
                offset += len(raw)

    @staticmethod
    def _parse(raw: bytes, number: int) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise ReplayInputError(f"--responses line {number} is not JSON") from error
        if not isinstance(value, dict) or not all(isinstance(value.get(name), str) and value.get(name)
                                                  for name in ("jobId", "itemId")):
            raise ReplayInputError(f"--responses line {number} names no jobId and itemId")
        return value

    def fetch(self, keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], Fetched]:
        out: dict[tuple[str, str], Fetched] = {}
        located = sorted(((self.index[key], key) for key in set(keys) if key in self.index), key=lambda pair: pair[0])
        for key in set(keys) - set(self.index):
            out[key] = Fetched(None, "the responses file holds no item of the call")
        with self.path.open("rb") as handle:
            for (offset, length, number), key in located:
                handle.seek(offset)
                value = self._parse(handle.read(length), number)
                item = {name: entry for name, entry in value.items() if name not in ("jobId", "itemId")}
                out[key] = Fetched({**item, "id": value["itemId"], "jobId": value["jobId"]})
        return out


# ----------------------------------------------------------------------------- one call
@dataclass
class CallReplay:
    """What replay recorded for one call: its replay_calls row and its replay_entries rows, how its stored response
    checked out (``status``: verified, unavailable or digest-failure), the labels it carried and the verdicts its answer
    yields by label."""

    call_row: tuple
    entry_rows: list[tuple] = field(default_factory=list)
    outcome: str = "replayed"
    status: str = "verified"
    labels: list[str] = field(default_factory=list)
    yielded: dict[str, dict[str, Any]] = field(default_factory=dict)


def _quote_view(quote: Any) -> str | None:
    if quote is None:
        return None
    text = str(quote)
    return "" if text == "" else "sha256:" + hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _verdict_view(entry: Mapping[str, Any] | None, label: Any = None) -> dict[str, Any]:
    """A verdict as replay records it: the compared fields, the quote as a digest (never its text)."""
    entry = entry or {}
    return {"outcome": entry.get("outcome"), "label": entry.get("label", label), "category": entry.get("category"),
            "supportVerdict": entry.get("support_verdict"), "escalate": entry.get("escalate"),
            "quote": _quote_view(entry.get("quote"))}


def verify_item(call: Mapping[str, Any], fetched: Fetched | None, *, required_model: str,
                instrument_digest: str) -> tuple[str, dict[str, Any], str | None]:
    """("verified" | "unavailable" | "digest-failure", detail, the answer's text) for one broker call.

    A call replays from the response the broker stores for it: an item that completed, or one the judge server refused
    (``judge_refusal``: HTTP 400, 413 or 422 after a provider call, an answer without labels for the census). The item
    must pass every check against the receipts: the request digests (the item's, the broker receipt's and the census's
    own); for a completed item the output digest and the served model; for a refused one that it stores no output and
    that the call's receipt records the same refusal. The call's instrument digest must be the census's as well. Any
    other item (one that did not settle, failed in transport, paused or was cancelled) holds no response."""
    detail: dict[str, Any] = {}
    item = fetched.item if fetched is not None else None
    if item is None:
        detail["reasons"] = [(fetched.reason if fetched is not None else None) or "no stored item for the call"]
        return "unavailable", detail, None
    state = _short(item.get("state"), 40)
    detail.update({"state": state,
                   "errorCode": _short(item.get("errorCode"), 80) if isinstance(item.get("errorCode"), str) else None,
                   "servedModel": _short(item.get("servedModel")) if isinstance(item.get("servedModel"), str) else None})
    refused = judge_refusal(item) if state == "failed" else None
    if state != "completed" and refused is None:
        detail["reasons"] = [f"the broker item is {state if isinstance(state, str) else 'without a state'}"
                             + (f" ({detail['errorCode']})" if detail["errorCode"] else "") + ": no stored response"]
        return "unavailable", detail, None
    output = item.get("output", ABSENT)
    try:
        computed: str | None = output_digest(output)[0]
    except ValidationError:
        computed = None
    reported = item.get("requestSha256")
    checks = {"requestDigest": isinstance(reported, str) and reported == call.get("broker_request_sha256")
              and reported == call.get("request_sha256")}
    if refused is None:
        checks["outputDigest"] = (computed is not None and isinstance(item.get("outputDigest"), str)
                                  and item.get("outputDigest") == computed)
        checks["servedModel"] = item.get("servedModel") == required_model
    else:
        # A refusal stores no output: the broker receipt's output digest is the digest of no output.
        detail.update({"httpStatus": refused, "attempts": item.get("attempts")})
        checks["outputDigest"] = output is ABSENT and item.get("outputDigest", computed) == computed
        checks["refusal"] = (call.get("error") == refusal_error(refused)
                             and call.get("broker_error_code") == item.get("errorCode")
                             and call.get("broker_http_status") == refused)
    checks["instrumentDigest"] = call.get("instrument_digest") == instrument_digest
    checks["tier"] = call.get("tier") in PARSER_STAGES
    detail.update({"checks": checks, "outputSha256": computed,
                   "requestSha256": reported if isinstance(reported, str) else None})
    reasons = {"requestDigest": "the item's request digest differs from the broker receipt's or the census's",
               "outputDigest": ("the refused item stores an output" if refused is not None else
                                "the output differs from the item's output digest" if isinstance(
                                    item.get("outputDigest"), str) else "the item carries no output digest"),
               "servedModel": f"the item was served by {detail['servedModel'] or 'no named model'}, not {required_model}",
               "refusal": "the call's receipt records no refusal with this error code and HTTP status",
               "instrumentDigest": "the call ran another instrument digest than the census",
               "tier": f"the call's tier {call.get('tier')!r} has no parser stage"}
    failed = [reasons[name] for name, passed in checks.items() if not passed]
    if failed:
        detail["reasons"] = failed
        return "digest-failure", detail, None
    return "verified", detail, ("" if refused is not None else broker_output_text(None if output is ABSENT else output))


def rederive(call: Mapping[str, Any], text: str) -> tuple[dict[int, dict[str, Any]], dict[str, int]]:
    """The entries the census derives from a call's answer, by position in the call, and parse counts.

    As the census records them: an abstract-tier call (first or repair, any variant) records an accepted verdict for
    each label with a parseable line and nothing for the others (they go to the repair call or become invalid); a
    full-text call records an accepted verdict or ``missing`` at every position. An answer without text (an empty
    completion, or a request the judge server refused) yields no verdict."""
    labels = json.loads(call["labels_json"])
    records: dict[str, Any] = {}
    problems = 0
    if text.strip():
        records, found, _ = parse_answer(text, labels, tier=call["tier"])
        problems = len(found)
    entries: dict[int, dict[str, Any]] = {}
    for position, label in enumerate(labels, 1):
        record = records.get(label)
        if record is not None:
            entries[position] = {"outcome": "accepted", "label": label, "category": record["category"],
                                 "support_verdict": record["support_verdict"], "escalate": record["escalate"],
                                 "quote": record["quote"]}
        elif call["tier"] == "full-text":
            entries[position] = {"outcome": "missing", "label": label, "category": None, "support_verdict": None,
                                 "escalate": None, "quote": None}
    return entries, {"labels": len(labels), "parsed": len(records), "parserProblems": problems}


def compare(row: Mapping[str, Any], derived: Mapping[str, Any] | None, call: Mapping[str, Any],
            labels: Sequence[str]) -> tuple[list[str], bool]:
    """(the fields in which a history row and the entry re-derived at its position differ, whether the quote was
    compared). The quote is compared unless the recorded one was redacted (NULL)."""
    position = row["position"]
    at = labels[position - 1] if isinstance(position, int) and 1 <= position <= len(labels) else None
    differs = []
    if row["label"] != at or (derived is not None and derived["label"] != row["label"]):
        differs.append("label")
    for name in ("journal", "doi", "tier", "variant"):
        if row[name] != call.get(name):
            differs.append(name)
    for name in COMPARED:
        if row[name] != (derived or {}).get(name):
            differs.append(name)
    quoted = row["quote"] is not None
    if quoted and row["quote"] != (derived or {}).get("quote"):
        differs.append("quote")
    return differs, quoted


def replay_call(call_id: str, call: Mapping[str, Any] | None, rows: Sequence[Mapping[str, Any]],
                fetched: Mapping[tuple[str, str], Fetched], *, required_model: str, instrument_digest: str,
                now: str, invalid: frozenset[str] | set[str] = frozenset()) -> CallReplay:
    """Replay one call: verify its stored response, re-derive its entries and compare them with the call's history
    rows (none for a call that decided only entries recorded as invalid). ``invalid`` names the entries recorded as
    invalid that the call decided: a verdict its answer yields for one of them is compared with that entry's history
    row (``_invalid_entry``), not counted as a verdict the history lacks."""
    def entry(row: Mapping[str, Any], outcome: str, replayed: Mapping[str, Any] | None = None) -> tuple:
        return (call_id, row["position"], row["label"], outcome, canonical(_verdict_view(row)),
                None if replayed is None else canonical(replayed), row["journal"], row["doi"], row["tier"],
                row["variant"], row["history_id"])

    job_id = item_id = None
    labels: list[str] = []
    if call is None:
        status, detail, text = "unavailable", {"reasons": ["the history names a call without a receipt"]}, None
    else:
        labels = json.loads(call["labels_json"])
        job_id, item_id = call.get("broker_job_id"), call.get("broker_item_id")
        if (call.get("transport") or "direct") != "broker":
            status, detail, text = "unavailable", {"reasons": ["a direct call: no stored response"]}, None
        elif not job_id or not item_id:
            status, detail, text = "unavailable", {"reasons": ["the receipt names no broker job or item"]}, None
        else:
            status, detail, text = verify_item(call, fetched.get((job_id, item_id)), required_model=required_model,
                                               instrument_digest=instrument_digest)
        detail.update({"tier": call.get("tier"), "kind": call.get("kind"), "variant": call.get("variant"),
                       "censusError": _short(call.get("error"))})
    output_sha, request_sha = detail.pop("outputSha256", None), detail.pop("requestSha256", None)
    entries: list[tuple] = []
    yielded: dict[str, dict[str, Any]] = {}
    replayed = mismatched = 0
    if status != "verified":
        entries = [entry(row, status) for row in rows]
        outcome = status
    else:
        derived, counts = rederive(call, text or "")
        yielded = {found["label"]: found for found in derived.values() if found["outcome"] == "accepted"}
        detail.update(counts)
        recorded_positions = set()
        for row in rows:
            recorded_positions.add(row["position"])
            found = derived.get(row["position"]) if isinstance(row["position"], int) else None
            differs, quoted = compare(row, found, call, labels)
            view = {**_verdict_view(found, labels[row["position"] - 1] if found is None and isinstance(
                row["position"], int) and 1 <= row["position"] <= len(labels) else None),
                    "quoteCompared": quoted, "differs": differs}
            entries.append(entry(row, "mismatched" if differs else "replayed", view))
            mismatched += bool(differs)
            replayed += not differs
        # A verdict the answer yields at a position the history has no row for: the record lacks it.
        unrecorded = [position for position in sorted(derived)
                      if position not in recorded_positions and derived[position]["label"] not in invalid]
        for position in unrecorded:
            found = derived[position]
            entries.append((call_id, position, found["label"], "mismatched", None,
                            canonical({**_verdict_view(found), "quoteCompared": False, "differs": ["unrecorded"]}),
                            call["journal"], call["doi"], call["tier"], call["variant"], None))
        mismatched += len(unrecorded)
        detail["unrecorded"] = len(unrecorded)
        outcome = "mismatched" if mismatched else "replayed"
    call_row = (call_id, job_id, item_id, outcome, output_sha, request_sha, len(entries), replayed, mismatched,
                canonical(detail), now)
    return CallReplay(call_row, entries, outcome, status, labels, yielded)


# ----------------------------------------------------------------------------- entries recorded as invalid
@dataclass
class Decided:
    """The answered calls that decided an entry recorded as invalid, first calls before repair calls, each in the
    census's order, and the last answered repair call that carried it (None when none did)."""

    calls: list[str]
    closing: str | None


@dataclass
class Checked:
    """What a deciding call's stored response gave for the entries recorded as invalid that it decided: its status,
    their positions in the call and the verdicts it yields for them (as replay records them, quotes as digests)."""

    status: str
    positions: dict[str, int]
    yielded: dict[str, dict[str, Any]]


def decided_by_calls(row: Mapping[str, Any], basis: str | None) -> bool:
    """Whether answered calls decided a history row that names no call: an abstract-tier entry recorded as invalid after
    its batch's answered calls left it without a parseable line. A primary entry the census closed because no call of
    its batch was answered (``invalid:no-response``) and every outcome decided without a model call are outside
    replay."""
    if row["tier"] != "abstract" or row["outcome"] != "invalid":
        return False
    return row["variant"] != "primary" or basis == NO_PARSEABLE_OUTPUT


def _order_calls(calls: Sequence[dict[str, Any]], journal: str, doi: str, variant: str) -> None:
    """Each call's place among the recorded calls of its batch and kind: the census numbers them in the order it
    recorded them, and the call id names that number (``call_id_for``). A call whose id names none sorts after them, by
    time and id."""
    groups: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = {}
    for call in calls:
        groups.setdefault((call["run_id"], call["batch"], call["kind"]), []).append(call)
    for (run_id, batch, kind), members in groups.items():
        places = {call_id_for(run_id, journal, doi, "abstract", variant, batch, kind, nonce): nonce
                  for nonce in range(len(members))}
        for call in members:
            place = places.get(call["call_id"])
            call["order"] = (0, place, "", "") if place is not None else (1, 0, call["created_at"] or "", call["call_id"])


def _decided(row: Mapping[str, Any], calls: Sequence[dict[str, Any]]) -> Decided:
    """The answered calls of an invalid entry's batch that carried it. A primary batch writes each answered call's
    verdicts with its receipt, so every answered call that carried the entry decided it. A sensitivity batch that did
    not settle runs again whole and only its last run is recorded: its last first call and its last repair call."""
    label = row["label"]
    carrying = [call for call in calls if label in call["labels"]]
    if row["variant"] != "primary":
        batches = {call["batch"] for call in carrying}
        last: dict[tuple[Any, str], dict[str, Any]] = {}
        for call in calls:
            key = (call["batch"], call["kind"])
            if call["batch"] in batches and (key not in last or call["order"] > last[key]["order"]):
                last[key] = call
        carrying = [call for call in last.values() if label in call["labels"]]
    answered = sorted((call for call in carrying if call_answered(call["error"])),
                      key=lambda call: (call["kind"] != "first", call["batch"], call["order"]))
    repairs = [call for call in answered if call["kind"] == "repair"]
    return Decided([call["call_id"] for call in answered], repairs[-1]["call_id"] if repairs else None)


def _deciding(reader: sqlite3.Connection, invalid: Sequence[Mapping[str, Any]]) -> dict[int, Decided]:
    """The deciding calls of every entry recorded as invalid, by history row (the calls of each review and variant read
    once)."""
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in invalid:
        groups.setdefault((row["journal"], row["doi"], row["variant"]), []).append(row)
    columns = ("call_id", "run_id", "batch", "kind", "labels_json", "error", "created_at")
    decided: dict[int, Decided] = {}
    for (journal, doi, variant), rows in groups.items():
        calls = [dict(zip(columns, values)) for values in reader.execute(
            f"SELECT {', '.join(columns)} FROM calls WHERE journal=? AND doi=? AND tier='abstract' AND variant=? "
            "AND kind IN ('first', 'repair')", (journal, doi, variant))]
        for call in calls:
            try:
                labels = json.loads(call["labels_json"])
            except ValueError:
                labels = []
            call["labels"] = set(labels) if isinstance(labels, list) else set()
        _order_calls(calls, journal, doi, variant)
        for row in rows:
            decided[row["history_id"]] = _decided(row, calls)
    return decided


def _checked(result: CallReplay, labels: set[str]) -> Checked:
    """Only what the invalid entries ``labels`` need: a deciding call's other verdicts were compared already."""
    return Checked(result.status,
                   {label: position for position, label in enumerate(result.labels, 1) if label in labels},
                   {label: {**_verdict_view(entry), "_compared": {name: entry.get(name) for name in COMPARED}}
                    for label, entry in result.yielded.items() if label in labels})


def _invalid_entry(row: Mapping[str, Any], decided: Decided, checked: Mapping[str, Checked]) -> tuple[str, str | None,
                                                                                                         dict[str, Any] | None]:
    """(outcome, the call it is counted with, the re-derived verdict) of an entry recorded as invalid: a deciding call
    without a usable stored response decides the outcome (digest failure before unavailable); a deciding call whose
    answer yields a parseable line for the entry is a mismatch; so is an entry no answered repair call carried (the
    census would have left it open); otherwise it is invalid again, counted with the repair call that closed it."""
    calls = [(call_id, checked[call_id]) for call_id in decided.calls]
    for status in ("digest-failure", "unavailable"):
        failed = next((call_id for call_id, result in calls if result.status == status), None)
        if failed is not None:
            return status, failed, None
    quoted = row["quote"] is not None
    for call_id, result in calls:
        found = result.yielded.get(row["label"])
        if found is not None:
            view = {name: value for name, value in found.items() if name != "_compared"}
            differs = [name for name in COMPARED if row[name] != found["_compared"][name]]
            if quoted and _quote_view(row["quote"]) != view["quote"]:
                differs.append("quote")
            return "mismatched", call_id, {**view, "quoteCompared": quoted, "differs": differs, "calls": decided.calls}
    derived = {"outcome": "invalid", "category": None, "support_verdict": None, "escalate": None}
    view = {**_verdict_view(derived, row["label"]), "quoteCompared": quoted, "calls": decided.calls}
    if decided.closing is None:
        return "mismatched", (decided.calls[-1] if decided.calls else None), {
            **view, "outcome": None, "differs": ["outcome"], "reason": "no answered repair call carried the entry"}
    differs = [name for name in COMPARED if row[name] != derived[name]] + (["quote"] if quoted else [])
    return ("mismatched" if differs else "replayed"), decided.closing, {**view, "differs": differs}


# ----------------------------------------------------------------------------- the replay
def _history_rows(connection: sqlite3.Connection, ids: Sequence[int]) -> list[dict[str, Any]]:
    rows = []
    for start in range(0, len(ids), 5000):
        chunk = ids[start:start + 5000]
        rows.extend(dict(zip(HISTORY_COLUMNS, row)) for row in connection.execute(
            f"SELECT {', '.join(HISTORY_COLUMNS)} FROM history WHERE history_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(chunk)),)))
    return rows


def _calls(connection: sqlite3.Connection, call_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Receipts of the given calls with their transport row (a call recorded without one is a direct call)."""
    columns = ("call_id", "journal", "doi", "tier", "variant", "batch", "kind", "labels_json", "instrument_digest",
               "served_model", "error")
    transport = ("transport", "request_sha256", "broker_job_id", "broker_item_id", "broker_request_sha256",
                 "broker_error_code", "broker_http_status")
    joined = scores_db.has_table(connection, "call_transport")
    select = ", ".join([f"k.{name}" for name in columns] + [f"t.{name}" if joined else f"NULL AS {name}"
                                                            for name in transport])
    source = "calls k LEFT JOIN call_transport t ON t.call_id=k.call_id" if joined else "calls k"
    return {row[0]: dict(zip(columns + transport, row)) for row in connection.execute(
        f"SELECT {select} FROM {source} WHERE k.call_id IN (SELECT value FROM json_each(?))", (json.dumps(list(call_ids)),))}


class _Writer:
    """The replay's tables in one transaction of ``connection`` (the census database, or a new output file)."""

    def __init__(self, connection: sqlite3.Connection, now: str):
        self.connection, self.now = connection, now

    def begin(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        # One statement at a time: executescript would commit the transaction first.
        for statement in scores_db.REPLAY_DDL.split(";"):
            if statement.strip():
                self.connection.execute(statement)
        for table in ("replay_entries", "replay_calls", "replay_meta"):
            self.connection.execute(f"DELETE FROM {table}")
        scores_db.write_dictionary(self.connection, scores_db.REPLAY_TABLES)

    def calls(self, rows: Sequence[tuple]) -> None:
        self.connection.executemany(f"INSERT INTO replay_calls({', '.join(CALL_COLUMNS)}, created_at) "
                                    f"VALUES({', '.join('?' * (len(CALL_COLUMNS) + 1))})", rows)

    def entries(self, rows: Sequence[tuple]) -> None:
        self.connection.executemany(f"INSERT INTO replay_entries({', '.join(ENTRY_COLUMNS)}) "
                                    f"VALUES({', '.join('?' * len(ENTRY_COLUMNS))})", rows)

    def count(self, added: Mapping[str, Sequence[int]]) -> None:
        """Entries counted with calls already written: (entries, replayed, mismatched) added per call; a replayed call
        with a mismatched entry becomes mismatched."""
        self.connection.executemany(
            "UPDATE replay_calls SET n_entries=n_entries+?, n_replayed=n_replayed+?, n_mismatched=n_mismatched+?, "
            "outcome=CASE WHEN outcome='replayed' AND ?>0 THEN 'mismatched' ELSE outcome END WHERE call_id=?",
            [(entries, replayed, mismatched, mismatched, call_id)
             for call_id, (entries, replayed, mismatched) in sorted(added.items())])

    def finish(self, record: Mapping[str, Any]) -> None:
        self.connection.executemany("INSERT INTO replay_meta VALUES(?,?)", [
            (key, value if isinstance(value, str) else canonical(value)) for key, value in sorted(record.items())])


def _line(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def report_sha256(connection: sqlite3.Connection) -> str:
    """sha256 of a replay's results: the format line, then every ``replay_calls`` row (by call id) and every
    ``replay_entries`` row (by call id, position and history row), each as one line of JSON; ``created_at`` is left
    out, so replaying the same records against the same responses gives the same digest."""
    digest = hashlib.sha256(_line(FORMAT))
    for row in connection.execute(f"SELECT {', '.join(CALL_COLUMNS)} FROM replay_calls ORDER BY call_id"):
        digest.update(_line(["call", *row]))
    for row in connection.execute(f"SELECT {', '.join(ENTRY_COLUMNS)} FROM replay_entries "
                                  "ORDER BY call_id, position, history_id, label"):
        digest.update(_line(["entry", *row]))
    return digest.hexdigest()


def _context(meta: Mapping[str, str]) -> dict[str, Any]:
    """The census's instrument and required served model; a census of another instrument digest is refused, since its
    verdicts came from another parser."""
    instrument = C.instrument()
    pinned = meta.get("instrumentDigest")
    if pinned != instrument.digest:
        raise ReplayInputError(f"the census database is pinned to instrument digest {pinned or '(none)'}, this release "
                               f"carries {instrument.digest} for {instrument.id}: replay parses with the instrument the "
                               "census used")
    return {"instrumentDigest": pinned,
            "requiredModel": meta.get("requiredModel") or instrument.manifest["requiredModel"]["served"],
            "runId": meta.get("runId"), "studyBundleSha256": meta.get("studyBundleSha256")}


def _replay(reader: sqlite3.Connection, writer: _Writer, items: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    history_rows, last_id = reader.execute("SELECT COUNT(*), COALESCE(MAX(history_id), 0) FROM history").fetchone()
    # History rows that name no call: outside replay, unless answered calls decided them (an invalid output).
    invalid: list[dict[str, Any]] = []
    unnamed = reader.execute(f"SELECT {', '.join('h.' + name for name in HISTORY_COLUMNS)}, c.abs_basis FROM history h "
                             "LEFT JOIN citations c ON c.journal=h.journal AND c.doi=h.doi AND c.label=h.label "
                             "WHERE h.call_id IS NULL ORDER BY h.history_id")
    while True:
        batch = unnamed.fetchmany(5000)
        if not batch:
            break
        outside = []
        for values in batch:
            row = dict(zip(HISTORY_COLUMNS, values))
            if decided_by_calls(row, values[len(HISTORY_COLUMNS)]):
                invalid.append(row)
            else:
                outside.append((None, row["position"], row["label"], "outside-replay", canonical(_verdict_view(row)),
                                None, row["journal"], row["doi"], row["tier"], row["variant"], row["history_id"]))
        writer.entries(outside)
    decided = _deciding(reader, invalid)
    deciding: dict[str, set[str]] = {}
    for row in invalid:
        for call_id in decided[row["history_id"]].calls:
            deciding.setdefault(call_id, set()).add(row["label"])
    # The history rows of each call (ids only: the rows are read per chunk of calls), and the calls that decided
    # invalid entries.
    by_call: dict[str, list[int]] = {}
    for call_id, history_id in reader.execute("SELECT call_id, history_id FROM history WHERE call_id IS NOT NULL"):
        by_call.setdefault(call_id, []).append(history_id)
    call_ids = sorted(set(by_call) | set(deciding))
    checked: dict[str, Checked] = {}
    for start in range(0, len(call_ids), CALL_CHUNK):
        chunk = call_ids[start:start + CALL_CHUNK]
        receipts = _calls(reader, chunk)
        rows: dict[str, list[dict[str, Any]]] = {}
        for row in _history_rows(reader, [history_id for call_id in chunk for history_id in by_call.get(call_id, [])]):
            rows.setdefault(row["call_id"], []).append(row)
        keys = [(call["broker_job_id"], call["broker_item_id"]) for call in receipts.values()
                if call.get("transport") == "broker" and call.get("broker_job_id") and call.get("broker_item_id")]
        fetched = items.fetch(keys)
        call_rows, entry_rows = [], []
        for call_id in chunk:
            ordered = sorted(rows.get(call_id, []), key=lambda row: (row["position"] is None, row["position"] or 0,
                                                                      row["history_id"]))
            result = replay_call(call_id, receipts.get(call_id), ordered, fetched,
                                 required_model=context["requiredModel"], instrument_digest=context["instrumentDigest"],
                                 now=writer.now, invalid=deciding.get(call_id, frozenset()))
            call_rows.append(result.call_row)
            entry_rows.extend(result.entry_rows)
            if call_id in deciding:
                checked[call_id] = _checked(result, deciding[call_id])
        writer.calls(call_rows)
        writer.entries(entry_rows)
    # Entries recorded as invalid: each counted with the call whose response decided its outcome.
    rows_out, added = [], {}
    for row in invalid:
        outcome, call_id, view = _invalid_entry(row, decided[row["history_id"]], checked)
        position = checked[call_id].positions.get(row["label"], row["position"]) if call_id is not None else row["position"]
        rows_out.append((call_id, position, row["label"], outcome, canonical(_verdict_view(row)),
                         None if view is None else canonical(view), row["journal"], row["doi"], row["tier"],
                         row["variant"], row["history_id"]))
        if call_id is not None:
            counts = added.setdefault(call_id, [0, 0, 0])
            counts[0] += 1
            counts[1] += outcome == "replayed"
            counts[2] += outcome == "mismatched"
    writer.entries(rows_out)
    writer.count(added)
    entries = dict.fromkeys(ENTRY_OUTCOMES, 0)
    entries.update(dict(writer.connection.execute("SELECT outcome, COUNT(*) FROM replay_entries GROUP BY outcome")))
    calls = dict.fromkeys(CALL_OUTCOMES, 0)
    calls.update(dict(writer.connection.execute("SELECT outcome, COUNT(*) FROM replay_calls GROUP BY outcome")))
    return {"replayed": entries["replayed"], "mismatched": entries["mismatched"],
            "outsideReplay": entries["outside-replay"], "unavailable": entries["unavailable"],
            "digestFailures": entries["digest-failure"], "calls": sum(calls.values()), "entries": sum(entries.values()),
            "callOutcomes": {name.replace("digest-failure", "digestFailure"): count for name, count in calls.items()},
            "_extent": {"historyRows": history_rows, "maxHistoryId": last_id}}


@contextmanager
def _census_lock(census: Path) -> Iterator[None]:
    try:
        descriptor = writer_lock(census)
    except ValidationError as error:
        raise ReplayInputError(str(error)) from error
    except OSError as error:
        raise ReplayInputError(f"the census database's writer lock cannot be taken ({type(error).__name__}): replay it "
                               "into a separate file with --output") from error
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _record(context: Mapping[str, Any], summary: Mapping[str, Any], extent: Mapping[str, Any], source: str, report: str,
            now: str, revision: str | None, census_sha256: str | None = None) -> dict[str, Any]:
    record = {"format": FORMAT, "censusRunId": context.get("runId") or "", "instrumentDigest": context["instrumentDigest"],
              "requiredModel": context["requiredModel"], "studyBundleSha256": context.get("studyBundleSha256") or "",
              "source": source, "summary": summary, "reportSha256": report, "replayedAt": now,
              "codeRevision": revision or code_revision(), "historyRows": extent["historyRows"],
              "maxHistoryId": extent["maxHistoryId"]}
    if census_sha256 is not None:
        record["censusSha256"] = census_sha256
    return record


def _writable(census: Path) -> bool:
    """Whether an in-place replay can write the census database: the file, and its directory for the rollback journal
    and the writer lock."""
    return os.access(census, os.W_OK) and os.access(census.parent, os.W_OK | os.X_OK)


def _file_state(path: Path) -> tuple:
    """What changes when a database file is written: inode, size and times of the file and of a write-ahead log."""
    state = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            info = candidate.stat()
        except FileNotFoundError:
            state.append(None)
            continue
        state.append((info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return tuple(state)


def replay(census: Path, items: Any, *, output: Path | None = None, revision: str | None = None) -> dict[str, Any]:
    """Replay the census database's verdicts against ``items`` (``SocketItems`` or ``ResponseItems``) and return the
    summary. Without ``output`` the results go to new tables of the census database, which must be writable and not
    frozen (checked before its writer lock is taken), under its writer lock and in one transaction (a replay that stops
    leaves the earlier results, if any, as they were); with ``output`` the census database is opened read-only and the
    results go to a new SQLite file, written beside it and renamed into place once complete, with the sha256 of the
    census database it read (a census database that changes meanwhile ends the replay as a transient failure)."""
    census = Path(census)
    if not census.is_file():
        raise ReplayInputError(f"census database is missing: {census}")
    now = utc_now()
    if output is None:
        try:
            probe = scores_db.open_readonly(census)
        except (ValidationError, sqlite3.DatabaseError) as error:
            raise ReplayInputError(f"not a census scores database: {error}") from error
        with closing(probe):
            meta = scores_db.meta(probe)
        if meta.get("frozen") == "1":
            raise ReplayInputError("a frozen scores database is closed: replay it into a separate file with --output")
        _context(meta)
        if not _writable(census):
            raise ReplayInputError("the census database cannot be written here (a read-only file or directory): replay "
                                   "it into a separate file with --output")
        with _census_lock(census):
            try:
                connection = scores_db.connect(census)
            except (ValidationError, sqlite3.DatabaseError) as error:
                raise ReplayInputError(f"not a census scores database: {error}") from error
            with closing(connection):
                meta = scores_db.meta(connection)
                if meta.get("frozen") == "1":
                    raise ReplayInputError("a frozen scores database is closed: replay it into a separate file with "
                                           "--output")
                context = _context(meta)
                writer = _Writer(connection, now)
                writer.begin()
                try:
                    summary = _replay(connection, writer, items, context)
                    extent = summary.pop("_extent")
                    summary["reportSha256"] = report_sha256(connection)
                    writer.finish(_record(context, summary, extent, items.kind, summary["reportSha256"], now, revision))
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        return {**summary, "database": str(census), "source": items.kind}
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ReplayInputError("the replay output already exists; write it to a new path")
    before = _file_state(census)
    try:
        census_sha256 = scores_db.file_sha256(census)
        reader = scores_db.open_readonly(census)
    except (ValidationError, sqlite3.DatabaseError, OSError) as error:
        raise ReplayInputError(f"not a readable census scores database: {error}") from error
    with closing(reader):
        context = _context(scores_db.meta(reader))
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name("." + output.name + ".incoming")
        for leftover in (temporary, Path(str(temporary) + "-journal")):
            leftover.unlink(missing_ok=True)        # a file an earlier, stopped replay left unfinished
        try:
            with closing(sqlite3.connect(temporary)) as connection:
                connection.execute("PRAGMA journal_mode=DELETE")
                writer = _Writer(connection, now)
                writer.begin()
                summary = _replay(reader, writer, items, context)
                extent = summary.pop("_extent")
                summary["reportSha256"] = report_sha256(connection)
                writer.finish(_record(context, summary, extent, items.kind, summary["reportSha256"], now, revision,
                                      census_sha256))
                if _file_state(census) != before:
                    raise TransientError("the census database changed while it was replayed: replay a frozen copy, or "
                                         "run again once no census process writes it")
                connection.commit()
            os.chmod(temporary, 0o444)
            os.replace(temporary, output)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return {**summary, "database": str(output), "source": items.kind}


def _items(args: Any) -> Any:
    if getattr(args, "responses", None):
        return ResponseItems(Path(args.responses))
    socket_path = getattr(args, "broker_socket", None) or os.environ.get(SOCKET_ENV) or None
    if not socket_path:
        raise ReplayInputError(f"give --broker-socket (or {SOCKET_ENV}) for the run principal's socket, or --responses "
                               "for exported item results")
    return SocketItems(socket_path)


def command(args: Any) -> CommandOutcome:
    """``study replay``: exit 0 when every entry in scope replays, 3 when any entry is mismatched, unavailable or a
    digest failure, 2 when the input is invalid (``ReplayInputError``: also a census database an in-place replay cannot
    write, or a broker socket that cannot serve the run's results); a broker socket that stops answering or keeps
    refusing a result page, or a census database that changes while it is replayed into ``--output``, ends it as a
    transient failure (``TransientError``, exit 75) without results."""
    try:
        summary = replay(Path(args.census), _items(args), output=Path(args.output) if getattr(args, "output", None) else None)
    except ValidationError as error:
        raise ReplayInputError(str(error)) from error
    failed = summary["mismatched"] or summary["unavailable"] or summary["digestFailures"]
    return CommandOutcome(EXIT_NOT_REPLAYED if failed else EXIT_REPLAYED, summary)
