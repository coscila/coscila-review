# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review study replay``: the census's model verdicts re-derived from the stored responses, without model calls.

Every verdict a model call produced is a row of the scores database's append-only ``history`` that names its call and
its position in the call. Replay reads the item of each such call from the broker that settled it: over the run
principal's socket (``GET /jobs/<jobId>/results``, as the census's broker transport reads it) or from the JSON lines
the lab exports from the daemon's item results (``--responses``). Before it parses anything it checks the item against
the receipts:

- the item completed with the instrument's required served model;
- the request digest the item reports (``requestSha256``) equals the broker's request digest in the call's receipt
  (``call_transport.broker_request_sha256``) and the canonical digest of the request the instrument built
  (``call_transport.request_sha256``);
- the item's ``outputDigest`` equals the digest of its output computed as the daemon computes it
  (``output_digest``: a string as its UTF-8 bytes, any other value as ``JSON.stringify`` writes it).

A response that passes is parsed exactly as the census parsed it (``census_runner.broker_output_text`` and
``census_runner.parse_answer`` at the call's tier: the first and repair calls of an abstract-tier batch, full-text
calls), and each re-derived entry is compared with the history row of that call and position on outcome, label,
category, support verdict and escalation, and on the quote unless the recorded quote was redacted (an entitled review
in a frozen copy). A verdict the answer yields that the history lacks is a mismatch too. Entries whose history row
names no call were decided without a model call (never cited, no identified work, no parseable line after the repair
pass) and are outside replay. A call without a stored response (a direct call, a history call id without a receipt, a
receipt without a broker job, an item the broker does not hold or that did not complete) makes its entries
unavailable; a response that fails a check makes them digest failures. A socket that stops answering is an outage,
never a call without a stored response: the replay then ends as a transient failure and records nothing.

The results go to ``replay_calls``, ``replay_entries`` and ``replay_meta`` (``scores_db.REPLAY_DDL``): in the census
database itself, or with ``--output`` in a new SQLite file while the census database is only read. They hold no output
text: verdicts are compared field by field and quotes are kept as digests. ``reportSha256`` is the sha256 over the rows
of both result tables in a fixed order (``report_sha256``), so it can be recomputed from them.
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
from .census_runner import PARSER_STAGES, broker_output_text, canonical, code_revision, parse_answer, utc_now, writer_lock
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
CALL_COLUMNS = ("call_id", "job_id", "item_id", "outcome", "output_sha256", "request_sha256", "n_entries", "n_replayed",
                "n_mismatched", "detail_json")
ENTRY_COLUMNS = ("call_id", "position", "label", "outcome", "recorded_json", "replayed_json", "journal", "doi", "tier",
                 "variant", "history_id")
HISTORY_COLUMNS = ("history_id", "journal", "doi", "label", "tier", "variant", "call_id", "position", "outcome",
                   "category", "support_verdict", "escalate", "quote")
# The compared fields of a verdict besides the quote (in the history's column names).
COMPARED = ("outcome", "category", "support_verdict", "escalate")


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
        """One result page, or why the broker holds none. A socket that stops answering ends the replay as a transient
        failure (the same command can run again), so an outage never reads as calls without a stored response."""
        route = f"/jobs/{urllib.parse.quote(job_id, safe='')}/results?after={after}&limit=100"
        failure = "no answer"
        for attempt in range(self.attempts):
            try:
                return self.client.request("GET", route)
            except BrokerRefused as error:
                if error.code == "WORKER_NOT_FOUND" or error.status == 404:
                    return Fetched(None, _short(f"the broker holds no job {job_id} for this principal ({error.code})"))
                return Fetched(None, _short(f"the broker refused the job's result page ({error.code})"))
            except BrokerUnavailable as error:
                failure = str(error)
                if attempt + 1 < self.attempts:
                    self.sleep(2.0 * (attempt + 1))
        raise TransientError(f"the broker socket stopped answering ({failure[:200]}); run the replay again")


class ResponseItems:
    """Item results the lab exported as JSON lines ``{jobId, itemId, state, output, outputDigest, requestSha256,
    servedModel}``. The file is indexed once (a later line for the same job and item replaces an earlier one, as in the
    daemon's changed-state result feed) and each item is read again when its call is replayed, so the outputs are
    never all held in memory."""

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
    """What replay recorded for one call: its replay_calls row and its replay_entries rows."""

    call_row: tuple
    entry_rows: list[tuple] = field(default_factory=list)
    outcome: str = "replayed"


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
    """("verified" | "unavailable" | "digest-failure", detail, the text the census parsed) for one broker call.

    A call is replayed only from an item that completed and passes every check against the receipts: the request
    digests (the item's, the broker receipt's and the census's own), the output digest and the served model. The
    call's instrument digest must be the census's as well."""
    detail: dict[str, Any] = {}
    item = fetched.item if fetched is not None else None
    if item is None:
        detail["reasons"] = [(fetched.reason if fetched is not None else None) or "no stored item for the call"]
        return "unavailable", detail, None
    state = _short(item.get("state"), 40)
    detail.update({"state": state,
                   "errorCode": _short(item.get("errorCode"), 80) if isinstance(item.get("errorCode"), str) else None,
                   "servedModel": _short(item.get("servedModel")) if isinstance(item.get("servedModel"), str) else None})
    if state != "completed":
        detail["reasons"] = [f"the broker item is {state if isinstance(state, str) else 'without a state'}"
                             + (f" ({detail['errorCode']})" if detail["errorCode"] else "") + ": no stored response"]
        return "unavailable", detail, None
    output = item.get("output", ABSENT)
    try:
        computed: str | None = output_digest(output)[0]
    except ValidationError:
        computed = None
    reported = item.get("requestSha256")
    checks = {
        "requestDigest": isinstance(reported, str) and reported == call.get("broker_request_sha256")
        and reported == call.get("request_sha256"),
        "outputDigest": computed is not None and isinstance(item.get("outputDigest"), str)
        and item.get("outputDigest") == computed,
        "servedModel": item.get("servedModel") == required_model,
        "instrumentDigest": call.get("instrument_digest") == instrument_digest,
        "tier": call.get("tier") in PARSER_STAGES,
    }
    detail.update({"checks": checks, "outputSha256": computed,
                   "requestSha256": reported if isinstance(reported, str) else None})
    reasons = {"requestDigest": "the item's request digest differs from the broker receipt's or the census's",
               "outputDigest": "the output differs from the item's output digest" if isinstance(
                   item.get("outputDigest"), str) else "the item carries no output digest",
               "servedModel": f"the item was served by {detail['servedModel'] or 'no named model'}, not {required_model}",
               "instrumentDigest": "the call ran another instrument digest than the census",
               "tier": f"the call's tier {call.get('tier')!r} has no parser stage"}
    failed = [reasons[name] for name, passed in checks.items() if not passed]
    if failed:
        detail["reasons"] = failed
        return "digest-failure", detail, None
    return "verified", detail, broker_output_text(None if output is ABSENT else output)


def rederive(call: Mapping[str, Any], text: str) -> tuple[dict[int, dict[str, Any]], dict[str, int]]:
    """The entries the census derives from a call's answer, by position in the call, and parse counts.

    As the census records them: an abstract-tier call (first or repair, any variant) records an accepted verdict for
    each label with a parseable line and nothing for the others (they go to the repair call or become invalid without a
    call); a full-text call records an accepted verdict or ``missing`` at every position. A call the census recorded
    with an error (an empty completion) or an answer without text yields no verdict."""
    labels = json.loads(call["labels_json"])
    records: dict[str, Any] = {}
    problems = 0
    if call.get("error") is None and text.strip():
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
                now: str) -> CallReplay:
    """Replay one call the verdict history names: verify its stored response, re-derive its entries and compare them
    with the call's history rows."""
    def entry(row: Mapping[str, Any], outcome: str, replayed: Mapping[str, Any] | None = None) -> tuple:
        return (call_id, row["position"], row["label"], outcome, canonical(_verdict_view(row)),
                None if replayed is None else canonical(replayed), row["journal"], row["doi"], row["tier"],
                row["variant"], row["history_id"])

    job_id = item_id = None
    if call is None:
        status, detail, text = "unavailable", {"reasons": ["the history names a call without a receipt"]}, None
    else:
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
    replayed = mismatched = 0
    if status != "verified":
        entries = [entry(row, status) for row in rows]
        outcome = status
    else:
        labels = json.loads(call["labels_json"])
        derived, counts = rederive(call, text or "")
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
        unrecorded = [position for position in sorted(derived) if position not in recorded_positions]
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
    return CallReplay(call_row, entries, outcome)


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
    transport = ("transport", "request_sha256", "broker_job_id", "broker_item_id", "broker_request_sha256")
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
    entries = dict.fromkeys(ENTRY_OUTCOMES, 0)
    calls = dict.fromkeys(CALL_OUTCOMES, 0)
    # Entries decided without a model call.
    outside = reader.execute(f"SELECT {', '.join(HISTORY_COLUMNS)} FROM history WHERE call_id IS NULL ORDER BY history_id")
    while True:
        batch = outside.fetchmany(5000)
        if not batch:
            break
        writer.entries([(None, row[7], row[3], "outside-replay", canonical(_verdict_view(dict(zip(HISTORY_COLUMNS, row)))),
                         None, row[1], row[2], row[4], row[5], row[0]) for row in batch])
        entries["outside-replay"] += len(batch)
    # The history rows of each call (ids only: the rows are read per chunk of calls).
    by_call: dict[str, list[int]] = {}
    for call_id, history_id in reader.execute("SELECT call_id, history_id FROM history WHERE call_id IS NOT NULL"):
        by_call.setdefault(call_id, []).append(history_id)
    call_ids = sorted(by_call)
    for start in range(0, len(call_ids), CALL_CHUNK):
        chunk = call_ids[start:start + CALL_CHUNK]
        receipts = _calls(reader, chunk)
        rows: dict[str, list[dict[str, Any]]] = {}
        for row in _history_rows(reader, [history_id for call_id in chunk for history_id in by_call[call_id]]):
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
                                 now=writer.now)
            call_rows.append(result.call_row)
            entry_rows.extend(result.entry_rows)
            calls[result.outcome] += 1
            for row in result.entry_rows:
                entries[row[3]] += 1
        writer.calls(call_rows)
        writer.entries(entry_rows)
    return {"replayed": entries["replayed"], "mismatched": entries["mismatched"],
            "outsideReplay": entries["outside-replay"], "unavailable": entries["unavailable"],
            "digestFailures": entries["digest-failure"], "calls": len(call_ids), "entries": sum(entries.values()),
            "callOutcomes": {name.replace("digest-failure", "digestFailure"): count for name, count in calls.items()}}


@contextmanager
def _census_lock(census: Path) -> Iterator[None]:
    try:
        descriptor = writer_lock(census)
    except ValidationError as error:
        raise ReplayInputError(str(error)) from error
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _record(context: Mapping[str, Any], summary: Mapping[str, Any], source: str, report: str, now: str,
            revision: str | None) -> dict[str, Any]:
    return {"format": FORMAT, "censusRunId": context.get("runId") or "", "instrumentDigest": context["instrumentDigest"],
            "requiredModel": context["requiredModel"], "studyBundleSha256": context.get("studyBundleSha256") or "",
            "source": source, "summary": summary, "reportSha256": report, "replayedAt": now,
            "codeRevision": revision or code_revision()}


def replay(census: Path, items: Any, *, output: Path | None = None, revision: str | None = None) -> dict[str, Any]:
    """Replay the census database's verdicts against ``items`` (``SocketItems`` or ``ResponseItems``) and return the
    summary. Without ``output`` the results go to new tables of the census database, under its writer lock and in one
    transaction (a replay that stops leaves the earlier results, if any, as they were); with ``output`` the census
    database is opened read-only and the results go to a new SQLite file, written beside it and renamed into place once
    complete."""
    census = Path(census)
    if not census.is_file():
        raise ReplayInputError(f"census database is missing: {census}")
    now = utc_now()
    if output is None:
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
                    summary["reportSha256"] = report_sha256(connection)
                    writer.finish(_record(context, summary, items.kind, summary["reportSha256"], now, revision))
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        return {**summary, "database": str(census), "source": items.kind}
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ReplayInputError("the replay output already exists; write it to a new path")
    try:
        reader = scores_db.open_readonly(census)
    except ValidationError as error:
        raise ReplayInputError(str(error)) from error
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
                summary["reportSha256"] = report_sha256(connection)
                writer.finish(_record(context, summary, items.kind, summary["reportSha256"], now, revision))
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
    digest failure, 2 when the input is invalid (``ReplayInputError``); a broker socket that stops answering ends it as
    a transient failure (``TransientError``, exit 75) without results."""
    try:
        summary = replay(Path(args.census), _items(args), output=Path(args.output) if getattr(args, "output", None) else None)
    except ValidationError as error:
        raise ReplayInputError(str(error)) from error
    failed = summary["mismatched"] or summary["unavailable"] or summary["digestFailures"]
    return CommandOutcome(EXIT_NOT_REPLAYED if failed else EXIT_REPLAYED, summary)
