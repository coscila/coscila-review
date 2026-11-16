# SPDX-License-Identifier: Apache-2.0
"""Network base of the follow-up's provider adapters (status sources, cited-by traversal, document acquisition).

Every request goes through ``scholarly.ScholarlyClient``: its transport keeps the tool User-Agent, the response and
time bounds, the retry policy for 429 and 503 and NCBI's request ceiling, and document requests keep only the caller's
Accept header and a User-Agent without any contact address (``fulltext.open_public_https``). ``client`` adds a
per-host minimum interval for bulk traversal and never passes a contact identity: the only contact the client knows is
the operator's ``OCSL_REVIEW_CONTACT_EMAIL``, read by ScholarlyClient itself, and nothing here infers one.

Bulk traversal backs off when a provider throttles: a request that ends in HTTP 429 or 503 (after the scholarly
transport's own bounded retries) pushes that host's next request slot forward by the server's Retry-After, or by a
capped exponential backoff without one, and counts a consecutive throttle for the host. The bulk loops (status fetch,
cited-by traversal, evidence and second-generation acquisition) stop asking a host after ``MAX_CONSECUTIVE_THROTTLES``
throttles in a row and report the works left; running the command again continues where it stopped.

``record_request`` keeps a receipt of each provider response in the branch with contact and key parameters removed
from the recorded parameters, so receipts never carry an identity.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
from typing import Any, Callable, Mapping

from .errors import ProviderError
from .followup_branch import now, store_artifact
from .followup_model import canonical, digest

# Seconds between two requests to one host. The census reads Europe PMC at the same spacing per thread; NCBI's own
# ceiling is enforced again by the scholarly transport.
DEFAULT_SPACING = {
    "api.crossref.org": 0.5,
    "www.ebi.ac.uk": 0.4,
    "eutils.ncbi.nlm.nih.gov": 0.35,
    "api.openalex.org": 0.2,
}
FALLBACK_SPACING = 0.5
# Throttling: HTTP statuses that mean "slow down", the backoff without a Retry-After (base seconds, doubling per
# consecutive throttle), the most a host's next slot is pushed, and the run of throttles after which a loop stops.
THROTTLE_STATUSES = frozenset({429, 503})
BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 600.0
MAX_CONSECUTIVE_THROTTLES = 5
HOSTS = {"crossref": "api.crossref.org", "pubmed": "eutils.ncbi.nlm.nih.gov", "europe-pmc": "www.ebi.ac.uk",
         "openalex": "api.openalex.org"}
# Query parameters that identify a person or carry a credential; they are sent only as the scholarly client decides
# and never recorded.
PRIVATE_PARAMS = frozenset({"mailto", "email", "api_key", "apikey", "key", "token", "access_token"})


class SpacedTransport:
    """Wrap a scholarly transport: at most one request per host per interval, across threads."""

    def __init__(self, inner: Any, spacing: Mapping[str, float] | None = None, *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.inner = inner
        self.spacing = {**DEFAULT_SPACING, **dict(spacing or {})}
        self.clock, self.sleep = clock, sleep
        self._next: dict[str, float] = {}
        self._throttles: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _host(url: str) -> str:
        return (urllib.parse.urlsplit(url).hostname or "").lower()

    def consecutive_throttles(self, host: str) -> int:
        with self._lock:
            return self._throttles.get(host.lower(), 0)

    def _outcome(self, url: str, error: BaseException | None) -> None:
        """After a request: a throttle pushes the host's next slot forward and counts; any other outcome resets."""
        host = self._host(url)
        match = re.search(r"provider HTTP (\d{3})", str(error)) if error is not None else None
        if match is None or int(match.group(1)) not in THROTTLE_STATUSES:
            with self._lock:
                self._throttles.pop(host, None)
            return
        headers = getattr(error.__cause__, "headers", None)
        requested = None
        if headers is not None:
            from .scholarly import HttpTransport
            try:
                requested = HttpTransport._retry_after_seconds(headers)
            except (AttributeError, TypeError, ValueError):
                requested = None
        with self._lock:
            count = self._throttles.get(host, 0) + 1
            self._throttles[host] = count
            delay = requested if requested is not None else BACKOFF_SECONDS * 2 ** (count - 1)
            delay = min(max(float(delay), 0.0), MAX_BACKOFF_SECONDS)
            self._next[host] = max(self._next.get(host, 0.0), self.clock() + delay)

    def _reserve(self, url: str) -> None:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        interval = float(self.spacing.get(host, FALLBACK_SPACING))
        with self._lock:
            start = max(self.clock(), self._next.get(host, 0.0))
            self._next[host] = start + interval
        delay = start - self.clock()
        if delay > 0:
            self.sleep(delay)

    def _send(self, url: str, send: Callable[[], Any]) -> Any:
        self._reserve(url)
        try:
            response = send()
        except ProviderError as error:
            self._outcome(url, error)
            raise
        self._outcome(url, None)
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._send(url, lambda: self.inner.get(url, **kwargs))

    def get_document(self, url: str, **kwargs: Any) -> Any:
        getter = getattr(self.inner, "get_document", None)
        return self._send(url, lambda: getter(url, **kwargs) if getter else self.inner.get(url, **kwargs))

    @property
    def user_agent(self) -> str | None:
        return getattr(self.inner, "user_agent", None)


def client(*, workspace: Any = None, plan: Any = None, transport: Any = None,
           spacing: Mapping[str, float] | None = None, sleep: Callable[[float], None] = time.sleep) -> Any:
    """A ScholarlyClient for follow-up traversal. No contact identity is passed: only OCSL_REVIEW_CONTACT_EMAIL counts.

    The default egress plan lets identifiers (DOI, PMID, PMCID) and paging controls leave, nothing else.
    """
    from .scholarly import ScholarlyClient
    scholarly = ScholarlyClient(plan=plan, workspace=workspace, transport=transport)
    scholarly.transport = SpacedTransport(scholarly.transport, spacing, sleep=sleep)
    return scholarly


def throttled(scholarly: Any, host: str) -> bool:
    """A bulk loop stops asking ``host`` (a host name or a source name of ``HOSTS``) once its last
    MAX_CONSECUTIVE_THROTTLES requests were all throttled."""
    counter = getattr(getattr(scholarly, "transport", None), "consecutive_throttles", None)
    if not callable(counter):
        return False
    return counter(HOSTS.get(host, host)) >= MAX_CONSECUTIVE_THROTTLES


def public_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Query parameters as recorded: contact and credential fields removed."""
    return {key: value for key, value in (params or {}).items()
            if key.lower() not in PRIVATE_PARAMS and value is not None}


def bare_url(url: str) -> str:
    """An address without its query string or fragment (parameters are recorded separately, filtered)."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def record_request(connection: Any, *, provider: str, operation: str, url: str, params: Mapping[str, Any] | None,
                   outcome: str, http_status: int | None = None, body: bytes | None = None,
                   error: str | None = None, retrieved_at: str | None = None) -> str:
    """Store a provider response by sha256 and write its receipt; returns the request id."""
    retrieved = retrieved_at or now()
    recorded = public_params(params)
    query = urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)
    recorded.update({key: value for key, value in query if key.lower() not in PRIVATE_PARAMS})
    response_sha = store_artifact(connection, body, "provider-response", "application/octet-stream") if body else None
    request_id = "request-" + digest(provider, operation, bare_url(url), recorded, retrieved)[:32]
    connection.execute("INSERT OR IGNORE INTO fu_requests VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (request_id, provider, operation, bare_url(url), canonical(recorded), http_status, outcome,
                        response_sha, (error or None) and str(error)[:300], retrieved))
    return request_id
