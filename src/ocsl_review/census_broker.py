# SPDX-License-Identifier: Apache-2.0
"""Broker transport for registered instruments: bulk jobs over a run principal's Unix socket.

The executor never holds provider credentials or a bearer token. The run
principal's socket is its identity: ``GET /configs`` over it names the
principal, its run, its granted instruments and one bulk worker per granted
provider. Each paper-batch call becomes one bulk job with one item that names
the instrument's broker digest and carries the request the instrument built:
the messages exactly as built (``transport.messages: verbatim``) and the request
settings, which the broker checks against the installed bundle and the worker's
model before any provider call. The broker reports a
receipt per item (served model, server fingerprint, settings digest and the
digest of the request it sent upstream); the executor compares that request
digest with the canonical JSON of the body it built itself.

The job's idempotency key is derived from the call id and the request digest,
so a call repeated after a crash meets the job the broker already holds (and
its result) instead of starting a second judgment. A paused job (provider
limit, budget, expiry, withdrawn grant) halts the transport unless it may be
resumed: a pause observed during an invocation is resumed once per job lineage
(it may be a provider pause another call left), a job met already paused is
resumed along its lineage, and nothing is resumed while the job's
``resumeAfter`` lies in the future or once the transport has halted. A
cancellation observed during an invocation is never resumed by it.

Nothing here builds prompts or reads verdicts; the census runner maps the
outcomes onto its receipts.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .errors import ReviewError, ValidationError

SOCKET_ENV = "OCSL_BROKER_SOCKET"
ACTIVE_STATES = frozenset({"queued", "running"})
SETTLED_STATES = frozenset({"completed", "failed"})
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_WAIT_SECONDS = 7200.0
DEFAULT_OUTAGE_SECONDS = 60.0
MAX_RESUMES = 16            # resumes followed along one call's job lineage within one invocation


# ----------------------------------------------------------------------------- canonical JSON
def _js_number(value: int | float) -> str:
    """A number as JavaScript's JSON.stringify writes it (an integral float such as 0.0 is written 0)."""
    if isinstance(value, int):
        if abs(value) <= 2 ** 53 - 1:
            return str(value)
        try:
            value = float(value)    # what JSON.parse makes of the same digits
        except OverflowError as error:
            raise ValidationError("a canonical body holds no integer beyond the double range") from error
    if value != value or value in (float("inf"), float("-inf")):
        return "null"
    if value == 0:
        return "0"
    text = repr(value)
    sign = "-" if text.startswith("-") else ""
    mantissa, _, exponent = text.lstrip("-").partition("e")
    whole, _, fraction = mantissa.partition(".")
    run = whole + fraction
    leading = len(run) - len(run.lstrip("0"))
    digits = run.strip("0")
    point = len(whole) + (int(exponent) if exponent else 0) - leading
    count = len(digits)
    if count <= point <= 21:
        rendered = digits + "0" * (point - count)
    elif 0 < point <= 21:
        rendered = digits[:point] + "." + digits[point:]
    elif -6 < point <= 0:
        rendered = "0." + "0" * -point + digits
    else:
        power = point - 1
        rendered = digits[0] + ("." + digits[1:] if count > 1 else "") + "e" + ("+" if power >= 0 else "-") + str(abs(power))
    return sign + rendered


_SURROGATE = re.compile("[\ud800-\udfff]")


def _js_string(value: str) -> str:
    """A string as JSON.stringify writes it: UTF-16 surrogate pairs joined, lone surrogates escaped."""
    if _SURROGATE.search(value):
        out, index = [], 0
        while index < len(value):
            ch = value[index]
            following = value[index + 1] if index + 1 < len(value) else ""
            if "\ud800" <= ch <= "\udbff" and "\udc00" <= following <= "\udfff":
                out.append(chr(0x10000 + ((ord(ch) - 0xD800) << 10) + (ord(following) - 0xDC00)))
                index += 2
                continue
            out.append(ch)
            index += 1
        joined = "".join(out)
        rendered = json.dumps(joined, ensure_ascii=False)
        return "".join("\\u%04x" % ord(ch) if "\ud800" <= ch <= "\udfff" else ch for ch in rendered)
    return json.dumps(value, ensure_ascii=False)


def _unit_order(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def canonical_json(value: Any) -> str:
    """Canonical JSON as the broker computes it: object keys sorted by UTF-16 code unit, no whitespace,
    strings and numbers as JSON.stringify writes them."""
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
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValidationError("canonical JSON objects have string keys")
        return "{" + ",".join(_js_string(key) + ":" + canonical_json(value[key])
                              for key in sorted(value, key=_unit_order)) + "}"
    raise ValidationError(f"canonical JSON cannot hold {type(value).__name__}")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def request_sha256(payload: Mapping[str, Any]) -> str:
    """Digest of the request body the executor built; the broker's receipt must report the same."""
    return canonical_sha256(payload)


def idempotency_key(call_id: str, request_digest: str) -> str:
    """One broker job per call and request: a repeated call meets its job, a changed request starts a new one."""
    return "census-" + hashlib.sha256(f"{call_id}\n{request_digest}".encode("utf-8")).hexdigest()[:48]


# ----------------------------------------------------------------------------- socket client
class BrokerUnavailable(ReviewError):
    """The principal socket could not be reached or answered without a readable body."""


class BrokerRefused(ReviewError):
    """The broker answered and refused the request (an error status with a closed code)."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{code} (HTTP {status})" + (f": {message[:200]}" if message else ""))
        self.status, self.code = status, code


def check_socket(path: str) -> str:
    if not path or not os.path.isabs(path):
        raise ValidationError("the broker socket must be an absolute path (--broker-socket or OCSL_BROKER_SOCKET)")
    try:
        mode = os.stat(path).st_mode
    except OSError as error:
        raise ValidationError(f"the broker socket is not reachable: {type(error).__name__}") from error
    if not stat.S_ISSOCK(mode):
        raise ValidationError("the broker socket path is not a Unix socket")
    return path


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class BrokerClient:
    """JSON over the principal's Unix socket; no credential or identity header is ever sent."""

    def __init__(self, path: str, *, timeout: float = 60.0):
        self.path, self.timeout = path, timeout

    def request(self, method: str, route: str, payload: Any = None) -> dict[str, Any]:
        connection = _UnixConnection(self.path, self.timeout)
        try:
            body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            connection.request(method, route, body, {"Content-Type": "application/json"} if body is not None else {})
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
        except (OSError, http.client.HTTPException) as error:
            raise BrokerUnavailable(f"broker socket: {type(error).__name__}") from error
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BrokerUnavailable("broker response exceeds the bounded page")
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise BrokerUnavailable(f"broker answered HTTP {status} without JSON") from error
        if status != 200:
            detail = value if isinstance(value, dict) else {}
            raise BrokerRefused(status, str(detail.get("code") or "HTTP_%d" % status), str(detail.get("error") or ""))
        if not isinstance(value, dict):
            raise BrokerUnavailable("broker answer is not a JSON object")
        return value


# ----------------------------------------------------------------------------- transport
@dataclass
class BrokerOutcome:
    """One call through the broker.

    ``kind``: ``settled`` (the item completed or failed; ``item`` holds its result), ``drift`` (the broker paused
    on a different served model), ``paused`` (provider limit, budget, expiry, withdrawn grant or cancellation;
    the transport is halted), ``halted`` (not submitted: the transport had halted), ``refused`` (the broker
    refused the job itself), ``unavailable`` (the socket failed) or ``timeout`` (no settlement in time).
    """

    kind: str
    job: dict[str, Any] | None = None
    item: dict[str, Any] | None = None
    error: str | None = None
    code: str | None = None
    seconds: float = 0.0


@dataclass
class _Waiter:
    event: threading.Event = field(default_factory=threading.Event)
    job: dict[str, Any] | None = None
    unavailable: str | None = None


def _job(value: Mapping[str, Any]) -> dict[str, Any]:
    """The fields of a job summary or a full job record that the transport reads."""
    job_id = value.get("jobId")
    if not isinstance(job_id, str) or not job_id:
        raise BrokerUnavailable("broker answer names no job")
    return {"jobId": job_id, "state": value.get("state"), "error": value.get("error"),
            "resumeAfter": value.get("resumeAfter"), "instrumentDigest": value.get("instrumentDigest"),
            "revision": value.get("revision"), "resumedFromJobId": value.get("resumedFromJobId")}


def _instant(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class BrokerTransport:
    """Bulk jobs over a run principal's socket, one item per paper-batch call (``kind = "broker"``)."""

    kind = "broker"

    def __init__(self, socket_path: str, *, worker: str | None = None, on_premises: bool = False,
                 client: Any = None, poll_interval: float = 0.5, wait_seconds: float = DEFAULT_WAIT_SECONDS,
                 outage_seconds: float = DEFAULT_OUTAGE_SECONDS, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.socket_path = check_socket(socket_path) if client is None else socket_path
        self.client = client or BrokerClient(self.socket_path)
        self.requested_worker, self.on_premises = worker, on_premises
        self.poll_interval, self.wait_seconds, self.outage_seconds = poll_interval, wait_seconds, outage_seconds
        self.clock, self.wall = clock, wall
        self.catalog: dict[str, Any] | None = None
        self.principal_id = self.run_id = self.worker_id = self.provider_id = self.worker_model = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._waiters: dict[str, _Waiter] = {}
        self._cursor: int | None = None
        self._resumed: set[str] = set()
        self._halted: dict[str, Any] | None = None
        self._closed = False
        self._poller: threading.Thread | None = None
        self.counters = {"jobs": 0, "replayed": 0, "resumed": 0, "polls": 0}

    # ------------------------------------------------------------------ identity and grants
    def open(self, *, refresh: bool = False) -> dict[str, Any]:
        """Read the principal's catalog (once, unless ``refresh``): the socket must belong to an active run principal."""
        if self.catalog is not None and not refresh:
            return self.catalog
        catalog = self.client.request("GET", "/configs")
        authorization = catalog.get("authorization") if isinstance(catalog.get("authorization"), dict) else {}
        principal = authorization.get("principalId")
        if not isinstance(principal, str) or not principal:
            raise ValidationError("the broker socket names no principal: the census runs only on a run principal's "
                                  "socket (a chat turn socket carries no run authority)")
        if authorization.get("kind") not in (None, "run"):
            raise ValidationError(f"the broker principal is a {authorization.get('kind')} principal, not a run principal")
        if authorization.get("state") not in (None, "active"):
            raise ValidationError(f"the broker principal is {authorization.get('state')}")
        if catalog.get("instrumentMode") != 1:
            raise ValidationError("the broker does not offer instrument mode")
        workers = [w for w in catalog.get("workers") or [] if isinstance(w, dict) and w.get("bulkAvailable")]
        if self.requested_worker is not None:
            chosen = [w for w in workers if w.get("id") == self.requested_worker]
            if not chosen:
                raise ValidationError(f"the broker principal has no bulk worker {self.requested_worker!r}; it offers "
                                      + (", ".join(sorted(str(w.get('id')) for w in workers)) or "none"))
        elif len(workers) == 1:
            chosen = workers
        else:
            raise ValidationError("select the broker worker with --broker-worker; the principal offers "
                                  + (", ".join(sorted(str(w.get('id')) for w in workers)) or "no bulk worker"))
        worker = chosen[0]
        if self.principal_id is not None and principal != self.principal_id:
            raise ValidationError("the broker socket now names another principal than at the start of this run")
        self.catalog, self.principal_id, self.run_id = catalog, principal, authorization.get("runId")
        self.worker_id, self.worker_model = worker.get("id"), worker.get("model")
        self.provider_id = worker.get("providerId") or self.worker_id
        return catalog

    def require(self, digests: Mapping[str, str], *, model: str) -> None:
        """Refuse before any call unless the worker serves ``model`` and every digest is installed and granted
        (the catalog is read again: grants and installed bundles may have changed since the last invocation)."""
        try:
            catalog = self.open(refresh=True)
        except BrokerUnavailable:
            if self.catalog is None:
                raise
            catalog = self.catalog      # an outage after the start: the calls meet it and the run pauses as an outage
        if model != self.worker_model:
            raise ValidationError(f"the broker worker {self.worker_id!r} requests model {self.worker_model!r}, the census "
                                  f"requests {model!r}: the request digests could never agree (pass --model to match)")
        available = {item.get("digest") for item in catalog.get("instruments") or [] if isinstance(item, dict)}
        granted = set((catalog.get("authorization") or {}).get("instruments") or [])
        missing = {label: digest for label, digest in digests.items() if digest not in available}
        if missing:
            detail = "; ".join(f"{label}: {digest} ({'granted, not installed' if digest in granted else 'not granted'})"
                               for label, digest in sorted(missing.items()))
            raise ValidationError("the run principal lacks instrument bundles: " + detail + ". Export each with "
                                  "'ocsl-review instruments export', install it with PUT /v2/instruments/{digest} and "
                                  "grant the digest to the run principal")

    def describe(self) -> dict[str, Any]:
        with self._lock:
            halted = dict(self._halted) if self._halted else None
            counters = dict(self.counters)
        return {"kind": self.kind, "principalId": self.principal_id, "runId": self.run_id, "worker": self.worker_id,
                "model": self.worker_model, "onPremises": self.on_premises, "halted": halted, **counters}

    def _count(self, name: str) -> None:
        with self._lock:
            self.counters[name] += 1

    # ------------------------------------------------------------------ halting
    def begin(self) -> None:
        """A new invocation: no halt and no job resumed yet (a paused job may be resumed once again)."""
        with self._lock:
            self._halted = None
            self._resumed = set()

    @property
    def halted(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._halted) if self._halted else None

    def halt(self, reason: str, *, code: str | None = None, resume_after: str | None = None) -> None:
        """Stop submitting (the first reason wins); calls already in flight still settle and are recorded."""
        with self._lock:
            if self._halted is None:
                self._halted = {"reason": str(reason)[:300], "code": code, "resumeAfter": resume_after}

    # ------------------------------------------------------------------ calls
    def call(self, *, item_id: str, messages: list[dict[str, Any]], item_count: int, digest: str, key: str,
             settings: Mapping[str, Any] | None = None, wait_seconds: float | None = None) -> BrokerOutcome:
        """Submit one instrument call as a one-item job and follow its job lineage until the item settles.

        The item carries the request the instrument built: ``messages`` unchanged, ``item_count`` and ``settings``,
        the request's other fields (model, max_tokens, temperature, stream, reasoning_effort). The broker checks every
        setting against the installed bundle and the worker's model and refuses the item before any provider call when
        one differs (INSTRUMENT_MESSAGE_MISMATCH), so a request other than the instrument's never reaches a judge.
        """
        started = self.clock()
        deadline = started + (wait_seconds if wait_seconds is not None else self.wait_seconds)
        outcome = self._call(item_id, messages, item_count, digest, key, dict(settings or {}), deadline)
        outcome.seconds = round(self.clock() - started, 3)
        return outcome

    def _call(self, item_id, messages, item_count, digest, key, settings, deadline) -> BrokerOutcome:
        halted = self.halted
        if halted:
            return BrokerOutcome("halted", error="transport halted: " + halted["reason"])
        item = {**{name: value for name, value in settings.items() if name not in ("messages", "itemCount")},
                "messages": messages, "itemCount": item_count}
        request = {"configId": self.worker_id, "idempotencyKey": key, "concurrency": 1, "instrumentDigest": digest,
                   "items": [{"id": item_id, "input": item}]}
        try:
            job = _job(self.client.request("POST", "/bulk/submit?view=summary", request))
        except BrokerUnavailable as error:
            return BrokerOutcome("unavailable", error=str(error))
        except BrokerRefused as error:
            self.halt(f"the broker refused the job: {error}", code=error.code)
            return BrokerOutcome("refused", error=f"broker refused the job: {error.code}", code=error.code)
        if job.get("instrumentDigest") not in (None, digest):
            self.halt("the broker job runs another instrument digest", code="INSTRUMENT_MISMATCH")
            return BrokerOutcome("refused", job=job, error="broker job runs another instrument", code="INSTRUMENT_MISMATCH")
        self._count("jobs")
        if job.get("state") not in ("queued", None):
            self._count("replayed")
        # ``observed``: this invocation saw the current job leave queued/running. A job met already paused (a
        # repeated call, or a resume answered with the lineage's existing job) paused before this invocation.
        root, observed, hops = job["jobId"], False, 0
        while True:
            state = job.get("state")
            if state in ACTIVE_STATES or state is None:
                current = self._await(job["jobId"], deadline)
                if isinstance(current, BrokerOutcome):
                    return current
                job, observed = current, True
                continue
            try:
                item = self._item(job["jobId"], item_id)
            except BrokerUnavailable as error:
                return BrokerOutcome("unavailable", job=job, error=str(error))
            if state in SETTLED_STATES:
                if item is None:
                    return BrokerOutcome("unavailable", job=job, error="the broker job holds no result for the call")
                return BrokerOutcome("settled", job=job, item=item)
            if item is not None and item.get("errorCode") == "MODEL_DRIFT":
                # The broker keeps the provider paused; resuming other jobs would only meet the drift again.
                self.halt(f"broker job paused: {job.get('error') or 'MODEL_DRIFT'}", code="MODEL_DRIFT")
                return BrokerOutcome("drift", job=job, item=item, code="MODEL_DRIFT")
            code = (item or {}).get("errorCode") or ("CANCELLED" if state == "cancelled" else state)
            reason = f"broker job {state}: {job.get('error') or code}"
            resume_after = job.get("resumeAfter")
            until = _instant(resume_after)
            with self._lock:
                halted = self._halted is not None
                # A pause observed now is resumed once per lineage and invocation (it may be a provider pause left
                # from earlier); a cancellation observed now is the operator's and is never resumed.
                again = observed and (root in self._resumed or state == "cancelled")
                if observed:
                    self._resumed.add(root)
            if halted or again or (until is not None and until > self.wall()) or hops >= MAX_RESUMES:
                self.halt(reason, code=code, resume_after=resume_after)
                return BrokerOutcome("paused", job=job, item=item, error=reason, code=code)
            hops += 1
            try:
                job = _job(self.client.request("POST", f"/jobs/{job['jobId']}/resume"))
            except BrokerUnavailable as error:
                return BrokerOutcome("unavailable", error=str(error))
            except BrokerRefused as error:
                self.halt(f"{reason}; resume refused: {error.code}", code=error.code, resume_after=resume_after)
                return BrokerOutcome("paused", item=item, error=f"broker resume refused: {error.code}", code=error.code)
            self._count("resumed")
            observed = False

    def _item(self, job_id: str, item_id: str) -> dict[str, Any] | None:
        after = 0
        while True:
            page = self.client.request("GET", f"/jobs/{job_id}/results?after={after}&limit=100")
            for result in page.get("results") or []:
                if isinstance(result, dict) and result.get("id") == item_id:
                    return result
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not isinstance(cursor, int) or cursor <= after:
                return None
            after = cursor

    def _await(self, job_id: str, deadline: float) -> dict[str, Any] | BrokerOutcome:
        """The job once it leaves queued/running: registered first, then checked, then left to the poller."""
        waiter = _Waiter()
        with self._cond:
            if self._closed:
                return BrokerOutcome("unavailable", error="transport closed")
            self._waiters[job_id] = waiter
            self._start_poller()
        try:
            try:
                current = _job(self.client.request("GET", f"/jobs/{job_id}?view=summary"))
            except BrokerUnavailable as error:
                return BrokerOutcome("unavailable", error=str(error))
            except BrokerRefused as error:
                return BrokerOutcome("unavailable", error=f"broker job lookup refused: {error.code}", code=error.code)
            revision = current.get("revision")
            with self._cond:
                if self._cursor is None and isinstance(revision, int) and not isinstance(revision, bool):
                    self._cursor = max(0, revision - 1)
                    self._cond.notify_all()
            if current.get("state") not in ACTIVE_STATES:
                return current
            while not waiter.event.wait(timeout=max(0.0, min(1.0, deadline - self.clock()))):
                if self.clock() >= deadline:
                    return BrokerOutcome("timeout", job=current, error="no broker settlement before the wait deadline")
            if waiter.unavailable is not None:
                return BrokerOutcome("unavailable", job=current, error=waiter.unavailable)
            return waiter.job
        finally:
            with self._cond:
                self._waiters.pop(job_id, None)

    # ------------------------------------------------------------------ poller
    def _start_poller(self) -> None:
        if self._poller is None or not self._poller.is_alive():
            self._poller = threading.Thread(target=self._poll, name="ocsl-broker-poller", daemon=True)
            self._poller.start()
        self._cond.notify_all()

    def _poll(self) -> None:
        """Follow the principal's changed-job feed and hand each settled or paused job to its waiter."""
        failing_since = None
        while True:
            with self._cond:
                while not self._closed and (not self._waiters or self._cursor is None):
                    self._cond.wait()
                if self._closed:
                    return
                cursor = self._cursor
            try:
                page = self.client.request("GET", f"/jobs?view=summary&after={cursor}&limit=1000")
            except (BrokerUnavailable, BrokerRefused) as error:
                failing_since = failing_since or self.clock()
                if self.clock() - failing_since >= self.outage_seconds:
                    with self._cond:
                        for waiter in self._waiters.values():
                            waiter.unavailable = f"broker feed unavailable: {error}"
                            waiter.event.set()
                with self._cond:
                    self._cond.wait(timeout=self.poll_interval)
                continue
            failing_since = None
            self._count("polls")
            with self._cond:
                for entry in page.get("jobs") or []:
                    if not isinstance(entry, dict):
                        continue
                    waiter = self._waiters.get(entry.get("jobId"))
                    if waiter is not None and entry.get("state") not in ACTIVE_STATES:
                        waiter.job = _job(entry)
                        waiter.event.set()
                following = page.get("nextCursor")
                if isinstance(following, int) and not isinstance(following, bool) and following > (self._cursor or 0):
                    self._cursor = following
                if not page.get("hasMore"):
                    self._cond.wait(timeout=self.poll_interval)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            for waiter in self._waiters.values():
                waiter.unavailable = "transport closed"
                waiter.event.set()
            self._cond.notify_all()
        if self._poller is not None:
            self._poller.join(timeout=5)
