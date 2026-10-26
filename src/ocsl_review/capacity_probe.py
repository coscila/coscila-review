# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review capacity probe``: judge capacity for citation-census@1 before a census run.

The probe drives an OpenAI-compatible chat-completions endpoint with the
instrument's own abstract-tier requests: the system prompt and per-batch user
content from ``citation_census``, temperature 0, reasoning none,
``max_tokens = max(3000, 700 * n)`` and streaming off, built over synthetic
entries generated deterministically from a seed (no corpus data, no entitled
text). A grid of cells (streams x items per call) runs for a fixed time each,
over a keep-alive connection pool sized to the stream count. Every cell records
valid entries per second, tokens, the cached-token share, latency percentiles,
invalid outputs under the instrument's validator, failure classes, server load
from the vLLM Prometheus endpoint (with a shared-server check), the client's
RSS and the served model and fingerprint. Abort rules stop the cell and the
grid when transport failures exceed a share of calls in a trailing window
(the calls in flight are cut off) or the server reports saturation for too
long (the calls in flight finish, and the cell keeps its measurement of the
server at capacity). The report is JSON plus a markdown table with the exit
criterion line.

The probe only measures: it is not part of any instrument digest, sends only
first calls (no repair pass) and never writes a scores database.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import platform
import random
import re
import select
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from . import citation_census as C
from .errors import ValidationError
from .util import atomic_write

FORMAT = "ocsl-capacity-probe@1"
TIER = "abstract"
DEFAULT_STREAMS = (30, 60, 90, 120)
DEFAULT_ITEMS = (1, 5, 10, 40)
ORDERS = ("load", "streams", "items", "as-given", "shuffle")
STREAM_LIMIT = 1024
DEFAULT_JOURNAL = "Journal of Synthetic Reviews"
USER_AGENT = "ocsl-review-capacity-probe"
TOKEN_CHARACTERS = 4          # characters per token, for the dry-run estimates only
SATURATION_STATUS = frozenset({429, 503})
CONNECT_TIMEOUT = 10.0        # seconds to open a connection; reads then wait up to --request-timeout
COVERAGE_MIN = 0.8            # server load must account for this share of our own calls before "not shared" holds
COUNTER_MARGIN = (1000, 0.02)  # generation tokens beyond ours (absolute, share) before the counters show other clients
DETAIL_CHARACTERS = 200       # length of a failure example's text (server error messages; the key is redacted)
REPORT_NAME, MARKDOWN_NAME, CALLS_NAME = "capacity-probe.json", "capacity-probe.md", "calls.jsonl"

# Size distributions of the synthetic entries; recorded in every report.
PROFILE: dict[str, Any] = {
    "reviewEntries": {"distribution": "lognormal", "median": 110, "sigma": 0.6, "min": 8, "max": 600},
    "citingSentences": {"weights": [0.55, 0.25, 0.2], "note": "1, 2 or 3 sentences (never more than the instrument sends)"},
    "sentenceCharacters": {"distribution": "lognormal", "median": 170, "sigma": 0.4, "min": 60, "max": 480},
    "abstractCharacters": {"distribution": "lognormal", "median": 1500, "sigma": 0.45, "min": 200, "max": 4000,
                           "missingShare": 0.05, "structuredShare": 0.3,
                           "note": "the instrument's builder applies its abstract cut"},
    "titleWords": {"min": 6, "max": 24},
    "retractedShare": 0.003,
    "years": {"min": 1985, "max": 2025},
    "printedReference": "authors, title, venue, year, volume, pages and a synthetic DOI; recorded, never sent",
}

_FUNCTION = ("the", "of", "in", "and", "with", "was", "were", "for", "to", "by", "a", "that", "on", "from", "as", "is",
             "are", "than", "after", "between", "among", "within", "not", "both", "only", "these")
_TERMS = ("kinase", "receptor", "signalling", "expression", "tumour", "cohort", "patients", "biomarker", "pathway",
          "inhibitor", "mutation", "survival", "response", "therapy", "trial", "mice", "cells", "protein",
          "transcription", "methylation", "immune", "infiltration", "macrophages", "lymphocytes", "metastasis",
          "proliferation", "apoptosis", "resistance", "sensitivity", "dose", "toxicity", "endpoint", "hazard",
          "progression", "recurrence", "subtype", "sequencing", "variant", "allele", "phenotype", "organoids",
          "xenografts", "plasma", "serum", "imaging", "diagnosis", "screening", "risk", "incidence", "mortality",
          "stroma", "fibroblasts", "angiogenesis", "hypoxia", "metabolism", "glycolysis", "autophagy", "senescence",
          "ligand", "antibody", "vaccine", "checkpoint", "chemotherapy", "radiotherapy", "surgery", "resection",
          "biopsy", "histology", "grade", "stage", "remission", "relapse", "adverse", "events", "efficacy", "safety",
          "placebo", "baseline", "follow-up", "outcome", "association", "model", "analysis", "regression", "cluster",
          "signature", "panel", "assay", "marker", "level", "activity", "function", "loss", "gain", "deletion",
          "amplification", "fusion", "network", "regulation", "enzyme", "secretion", "uptake", "clearance")
_ADJECTIVES = ("higher", "lower", "significant", "independent", "median", "overall", "progression-free", "adjuvant",
               "early", "advanced", "primary", "secondary", "randomised", "prospective", "retrospective",
               "multicentre", "pooled", "consistent", "distinct", "novel", "systemic", "local")
_VERBS = ("increased", "reduced", "was associated with", "predicted", "correlated with", "did not change", "improved",
          "suppressed", "enhanced", "was observed in", "remained stable in", "was higher in", "was lower in",
          "promoted", "inhibited", "restored", "attenuated")
_VOCABULARY = _FUNCTION * 5 + _TERMS + _ADJECTIVES + _VERBS
_SECTIONS = ("Introduction", "Background", "Mechanisms > Signalling", "Mechanisms > Metabolism",
             "Clinical evidence > Trials", "Clinical evidence > Cohorts", "Biomarkers", "Therapeutic implications",
             "Resistance", "Discussion", "Future directions", "Conclusions", "Table 1", "Box 1")
_VENUE_FORMS = ("Annals of", "Archives of", "Bulletin of", "Reports in", "Letters in", "Proceedings in")
_VENUE_FIELDS = ("Cell Signalling", "Molecular Pathology", "Experimental Therapeutics", "Translational Genomics",
                 "Immune Regulation", "Clinical Pharmacology", "Metabolic Research", "Comparative Physiology",
                 "Applied Biostatistics", "Synthetic Biomedicine")
_SURNAMES = ("Abel", "Brandt", "Costa", "Dahl", "Eriksen", "Fischer", "Garcia", "Haddad", "Ito", "Jensen", "Kowalski",
             "Laurent", "Moreau", "Novak", "Okafor", "Petrov", "Quinn", "Rossi", "Sato", "Tanaka", "Ueda", "Varga",
             "Weber", "Xu", "Yilmaz", "Zhang")
_ABSTRACT_HEADINGS = (("Background", 0.2), ("Methods", 0.25), ("Results", 0.35), ("Conclusions", 0.2))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------- settings and grid
@dataclass
class ProbeSettings:
    """Everything a probe run needs except the key, which is read from ``api_key_env`` when the run starts."""

    base_url: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    metrics_url: str | None = None
    proxy_pids: tuple[int, ...] = ()
    streams: tuple[int, ...] = DEFAULT_STREAMS
    items: tuple[int, ...] = DEFAULT_ITEMS
    cells: tuple[tuple[int, int], ...] = ()
    order: str = "load"
    max_streams: int | None = None
    cell_seconds: float = 300.0
    warmup_seconds: float = 30.0
    ramp_seconds: float = 10.0
    drain_seconds: float = 180.0
    pause_seconds: float = 10.0
    request_timeout: float = 300.0
    sample_seconds: float = 5.0
    baseline_samples: int = 3
    abort_failure_share: float = 0.05
    abort_window_seconds: float = 60.0
    abort_min_calls: int = 20
    abort_min_failures: int = 3
    saturation_seconds: float = 60.0
    saturation_waiting: float = 1.0
    saturation_kv: float | None = None
    shared_tolerance: float = 1.0
    shared_min_samples: int = 2
    abandon_grace_seconds: float = 30.0
    allowed_streams: int = 60
    target_rate: float = 40.0
    seed: int = 1
    journal: str = DEFAULT_JOURNAL
    heartbeat_seconds: float = 30.0
    plan_calls: int = 32

    def validate(self, *, live: bool) -> None:
        if live and not self.base_url:
            raise ValidationError("--base-url is required unless --dry-run is given")
        if self.order not in ORDERS:
            raise ValidationError(f"--order must be one of {', '.join(ORDERS)}")
        if self.order == "as-given" and not self.cells:
            raise ValidationError("--order as-given needs --cells")
        for name, value in vars(self).items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValidationError(f"{name.replace('_', '-')} must be a finite number")
        if self.shared_tolerance < 0:
            raise ValidationError("--shared-tolerance must not be negative")
        for name in ("cell_seconds", "sample_seconds", "request_timeout", "abort_window_seconds"):
            if not getattr(self, name) > 0:
                raise ValidationError(f"{name.replace('_', '-')} must be positive")
        for name in ("warmup_seconds", "ramp_seconds", "drain_seconds", "pause_seconds", "saturation_seconds",
                     "abandon_grace_seconds", "heartbeat_seconds"):
            if getattr(self, name) < 0:
                raise ValidationError(f"{name.replace('_', '-')} must not be negative")
        if not self.warmup_seconds < self.cell_seconds:
            raise ValidationError("the warm-up must end before the cell does")
        if self.ramp_seconds > self.warmup_seconds:
            raise ValidationError("the ramp must end before the warm-up does, so the measured window runs at full concurrency")
        if not 0 < self.abort_failure_share <= 1:
            raise ValidationError("--abort-failure-share must be in (0, 1]")
        if (self.abort_min_calls < 1 or self.abort_min_failures < 1 or self.baseline_samples < 0
                or self.shared_min_samples < 1 or self.plan_calls < 1):
            raise ValidationError("--abort-min-calls, --abort-min-failures, --shared-min-samples and the plan sample "
                                  "must be positive")
        if self.max_streams is not None and not 1 <= self.max_streams <= STREAM_LIMIT:
            raise ValidationError(f"--max-streams must be from 1 to {STREAM_LIMIT}")
        if self.saturation_kv is not None and not 0 < self.saturation_kv <= 1:
            raise ValidationError("--saturation-kv is a KV cache usage share in (0, 1]")
        if self.saturation_waiting <= 0:
            raise ValidationError("--saturation-waiting must be positive")
        if self.allowed_streams < 1 or self.target_rate <= 0:
            raise ValidationError("--allowed-streams and --target-rate must be positive")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ValidationError("--seed must be an integer")
        C.system_prompt(self.journal)                 # a one-line journal parameter, as the instrument requires
        for streams, items in self.grid_cells():
            if not 1 <= streams <= STREAM_LIMIT:
                raise ValidationError(f"streams must be from 1 to {STREAM_LIMIT}: {streams}")
            if not 1 <= items <= C.items_per_call(TIER):
                raise ValidationError(f"items per call must be from 1 to the instrument's {C.items_per_call(TIER)}: {items}")
        if self.max_streams is not None and all(streams > self.max_streams for streams, _ in self.grid_cells()):
            raise ValidationError(f"every cell has more streams than --max-streams {self.max_streams}")
        if self.api_key_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValidationError("--api-key-env takes the name of an environment variable")
        for pid in self.proxy_pids:
            if not isinstance(pid, int) or pid < 1:
                raise ValidationError("--proxy-pid takes process ids")

    def grid_cells(self) -> list[tuple[int, int]]:
        if self.cells:
            return list(dict.fromkeys(self.cells))
        return [(s, n) for s in dict.fromkeys(self.streams) for n in dict.fromkeys(self.items)]

    def requested_model(self) -> str:
        return self.model or C.instrument().manifest["requiredModel"]["served"]


def parse_cells(values: Sequence[str] | None) -> tuple[tuple[int, int], ...]:
    """``60x40,30x1`` (repeatable) to ((60, 40), (30, 1))."""
    cells = []
    for value in values or ():
        for token in str(value).split(","):
            token = token.strip().lower()
            if not token:
                continue
            match = re.fullmatch(r"(\d+)\s*[x:]\s*(\d+)", token)
            if not match:
                raise ValidationError(f"a cell is STREAMSxITEMS, for example 60x40: {token!r}")
            cells.append((int(match.group(1)), int(match.group(2))))
    return tuple(cells)


def parse_counts(value: str | Sequence[int], what: str) -> tuple[int, ...]:
    if isinstance(value, str):
        try:
            numbers = tuple(int(part) for part in value.split(",") if part.strip())
        except ValueError as error:
            raise ValidationError(f"{what} takes comma-separated integers") from error
    else:
        numbers = tuple(int(part) for part in value)
    if not numbers:
        raise ValidationError(f"{what} needs at least one value")
    return numbers


def label(streams: int, items: int) -> str:
    return f"{streams}x{items}"


def ordered_grid(settings: ProbeSettings) -> list[dict[str, Any]]:
    """The cells in run order, each with the reason it is skipped (``None`` when it runs).

    ``load`` (the default) runs the cells within the allowed concurrency first, each group by ascending load
    (streams x items, then streams): an abort rule that fires above the allowed concurrency then never costs the
    cells the exit criterion is assessed on.
    """
    cells = settings.grid_cells()
    if settings.order == "load":
        cells.sort(key=lambda cell: (cell[0] > settings.allowed_streams, cell[0] * cell[1], cell[0], cell[1]))
    elif settings.order == "streams":
        cells.sort()
    elif settings.order == "items":
        cells.sort(key=lambda cell: (cell[1], cell[0]))
    elif settings.order == "shuffle":
        random.Random(settings.seed).shuffle(cells)
    grid = []
    for position, (streams, items) in enumerate(cells, 1):
        skip = None
        if settings.max_streams is not None and streams > settings.max_streams:
            skip = f"above --max-streams {settings.max_streams}"
        grid.append({"position": position, "cell": label(streams, items), "streams": streams, "itemsPerCall": items,
                     "skip": skip})
    return grid


# ----------------------------------------------------------------------------- synthetic entries
@dataclass(frozen=True)
class Batch:
    index: int          # position in the cell's call sequence
    review: int         # synthetic review it belongs to
    number: int         # paper-batch number within the review
    title: str
    items: list[dict[str, Any]] = field(repr=False)


def _draw(rng: random.Random, spec: dict[str, Any]) -> int:
    value = rng.lognormvariate(math.log(spec["median"]), spec["sigma"])
    return int(min(spec["max"], max(spec["min"], round(value))))


def _quantity(rng: random.Random) -> str:
    kind = rng.randrange(6)
    if kind == 0:
        return "(n = %d)" % rng.randint(12, 4000)
    if kind == 1:
        ratio = rng.uniform(0.3, 0.9)
        return "(HR %.2f, 95%% CI %.2f-%.2f)" % (ratio, ratio * 0.8, min(0.99, ratio * 1.2))
    if kind == 2:
        return "(p = %.3f)" % rng.uniform(0.0005, 0.2)
    if kind == 3:
        return "%d%%" % rng.randint(2, 98)
    if kind == 4:
        return "%.1f-fold" % rng.uniform(1.2, 9.0)
    return "%d months" % rng.randint(3, 120)


def _sentence(rng: random.Random, words: int) -> str:
    parts = rng.choices(_VOCABULARY, k=max(3, words))
    if rng.random() < 0.3:
        parts.insert(rng.randrange(len(parts) + 1), _quantity(rng))
    text = " ".join(parts)
    return text[0].upper() + text[1:] + "."


def _text(rng: random.Random, characters: int) -> str:
    sentences, size = [], 0
    while size < characters:
        sentence = _sentence(rng, rng.randint(12, 28))
        sentences.append(sentence)
        size += len(sentence) + 1
    text = " ".join(sentences)
    if len(text) > characters:
        cut = text.rfind(" ", 0, characters)
        text = text[:cut if cut > 0 else characters].rstrip(" ,;.") + "."
    return text


class SyntheticCorpus:
    """Deterministic synthetic reviews: entries with citing sentences, cited-work metadata, abstracts and printed references.

    Every value depends only on (seed, scope, review, position), so a cell's call sequence is reproducible and two
    cells (different scopes) never send the same prompt. Batches follow the instrument's geometry
    (``citation_census.paper_batches``): one review's entries in bibliography order, cut into calls of ``n``.
    """

    def __init__(self, seed: int, scope: str):
        self.seed, self.scope = seed, scope
        self.max_sentences = C.max_citing_sentences()

    def _rng(self, *parts: Any) -> random.Random:
        material = ":".join(str(part) for part in (FORMAT, self.seed, self.scope, *parts)).encode()
        return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:16], "big"))

    def review_size(self, review: int) -> int:
        return _draw(self._rng("size", review), PROFILE["reviewEntries"])

    def review_title(self, review: int) -> str:
        rng = self._rng("title", review)
        return "%s %s and %s in %s: a synthetic review" % (
            rng.choice(_ADJECTIVES).capitalize(), rng.choice(_TERMS), rng.choice(_TERMS), rng.choice(_TERMS))

    def _title(self, rng: random.Random) -> str:
        spec = PROFILE["titleWords"]
        words = [rng.choice(_TERMS if i % 2 else _ADJECTIVES + _TERMS) for i in range(rng.randint(spec["min"], spec["max"]))]
        text = " ".join(words)
        return text[0].upper() + text[1:]

    def _citing_sentence(self, rng: random.Random, label_text: str) -> str:
        characters = _draw(rng, PROFILE["sentenceCharacters"])
        sentence = _sentence(rng, max(6, round(characters / 7)))[:-1]
        if len(sentence) > characters:
            cut = sentence.rfind(" ", 0, characters)
            sentence = sentence[:cut if cut > 0 else characters].rstrip(" ,;")
        return "%s ⟨B%s⟩." % (sentence, label_text)

    def _abstract(self, rng: random.Random) -> str:
        spec = PROFILE["abstractCharacters"]
        characters = _draw(rng, spec)
        if rng.random() >= spec["structuredShare"]:
            return _text(rng, characters)
        return " ".join("%s: %s" % (heading, _text(rng, max(60, int(characters * share))))
                        for heading, share in _ABSTRACT_HEADINGS)

    def item(self, review: int, position: int) -> dict[str, Any]:
        """Entry ``position`` (0-based) of a review; ``printed`` is recorded but never sent, as the instrument requires."""
        rng = self._rng("item", review, position)
        number = str(position + 1)
        weights = PROFILE["citingSentences"]["weights"][:self.max_sentences]
        count = rng.choices(range(1, len(weights) + 1), weights=weights)[0]
        passages = [{"sentence": self._citing_sentence(rng, number), "section": rng.choice(_SECTIONS)}
                    for _ in range(count)]
        years = PROFILE["years"]
        year = max(years["min"], years["max"] - int(rng.expovariate(1 / 8)))
        title = self._title(rng)
        venue = "%s %s" % (rng.choice(_VENUE_FORMS), rng.choice(_VENUE_FIELDS))
        retracted = rng.random() < PROFILE["retractedShare"]
        abstract = "" if rng.random() < PROFILE["abstractCharacters"]["missingShare"] else self._abstract(rng)
        authors = ", ".join("%s %s" % (rng.choice(_SURNAMES), "".join(rng.choice("ABCDEFGHJKLMNPRST")
                                                                       for _ in range(rng.randint(1, 2))))
                            for _ in range(rng.randint(1, 6)))
        first = rng.randint(1, 2400)
        printed = "%s%s. %s. %s. %d;%d(%d):%d-%d. doi:10.5555/capacity-probe.%d.%d.%d" % (
            authors, ", et al" if rng.random() < 0.3 else "", title, venue, year, rng.randint(1, 80),
            rng.randint(1, 12), first, first + rng.randint(2, 18), self.seed, review, position + 1)
        return {"label": number, "citing_passages": passages,
                "cited_work": {"title": title, "venue": venue, "year": year, "retracted": retracted},
                "abstract": abstract, "printed": printed}

    def review(self, review: int) -> tuple[str, list[dict[str, Any]]]:
        return self.review_title(review), [self.item(review, k) for k in range(self.review_size(review))]

    def batches(self, items_per_call: int) -> Iterator[Batch]:
        index, review = 0, 0
        while True:
            title, entries = self.review(review)
            for number, chunk in enumerate(C.paper_batches(entries, items_per_call)):
                yield Batch(index, review, number, title, chunk)
                index += 1
            review += 1

    def batch(self, items_per_call: int, index: int) -> Batch:
        """Batch ``index`` of the call sequence (random access, for checks)."""
        for batch in self.batches(items_per_call):
            if batch.index == index:
                return batch
        raise AssertionError("unreachable")


def cell_corpus(settings: ProbeSettings, streams: int, items: int) -> SyntheticCorpus:
    return SyntheticCorpus(settings.seed, label(streams, items))


def build_request(batch: Batch, *, journal: str, model: str) -> tuple[dict[str, Any], bytes, list[str]]:
    """The instrument's first call for one batch: its messages, settings and exact body bytes."""
    messages = C.build_abstract_messages(batch.items, batch.title, journal)
    payload = C.request_payload(messages, model=model, items=len(batch.items), tier=TIER)
    return payload, C.request_body(payload), [item["label"] for item in batch.items]


class _BatchSource:
    """A cell's call sequence, shared by its streams; the first ``prefill`` batches are built before the clock starts."""

    def __init__(self, corpus: SyntheticCorpus, items: int, *, journal: str, model: str, prefill: int):
        self._iterator = corpus.batches(items)
        self._lock = threading.Lock()
        self._journal, self._model = journal, model
        self._ready: deque = deque(self._build(next(self._iterator)) for _ in range(prefill))

    def _build(self, batch: Batch) -> tuple[Batch, bytes, list[str]]:
        _, body, labels = build_request(batch, journal=self._journal, model=self._model)
        return batch, body, labels

    def next(self) -> tuple[Batch, bytes, list[str]]:
        with self._lock:
            if self._ready:
                return self._ready.popleft()
            batch = next(self._iterator)
        return self._build(batch)


# ----------------------------------------------------------------------------- endpoints and connections
@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str
    port: int
    path: str

    @classmethod
    def parse(cls, url: str, what: str) -> "Endpoint":
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValidationError(f"the {what} must be an http(s) URL with a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValidationError(f"the {what} carries no credentials, query or fragment (a key comes from --api-key-env)")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as error:
            raise ValidationError(f"the {what} has an invalid port") from error
        return cls(parsed.scheme, parsed.hostname, port, parsed.path.rstrip("/"))

    def url(self, suffix: str = "") -> str:
        host = "[%s]" % self.host if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}{self.path}{suffix}"

    def connection(self, timeout: float, on_open: Callable[[], None] | None = None) -> http.client.HTTPConnection:
        """A connection that opens its socket on first use: ``CONNECT_TIMEOUT`` to connect, then ``timeout`` per read."""
        kind = _PooledHTTPSConnection if self.scheme == "https" else _PooledHTTPConnection
        connection = kind(self.host, self.port, timeout=min(CONNECT_TIMEOUT, timeout))
        connection.io_timeout = timeout
        connection.on_open = on_open
        return connection


def _opened(connection: Any) -> None:
    """After connect: the read timeout, no Nagle delay, and the owner's check (which may refuse to send)."""
    try:
        connection.sock.settimeout(connection.io_timeout)
        connection.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, AttributeError):
        pass
    if connection.on_open:
        connection.on_open()


class _PooledHTTPConnection(http.client.HTTPConnection):
    on_open: Callable[[], None] | None = None
    io_timeout: float | None = None

    def connect(self) -> None:
        super().connect()
        _opened(self)


class _PooledHTTPSConnection(http.client.HTTPSConnection):
    on_open: Callable[[], None] | None = None
    io_timeout: float | None = None

    def connect(self) -> None:
        super().connect()
        _opened(self)


def _dropped(sock: Any) -> bool:
    """An idle keep-alive socket that is readable has been closed by the server (or holds stray bytes)."""
    try:
        poller = select.poll()
        poller.register(sock, select.POLLIN | select.POLLPRI | select.POLLERR | select.POLLHUP)
        return bool(poller.poll(0))
    except (AttributeError, OSError, ValueError):
        try:
            readable, _, _ = select.select([sock], [], [], 0)
            return bool(readable)
        except (OSError, ValueError):
            return True


class PoolClosed(Exception):
    """The cell has ended; a stream that asks for a connection now stops."""


class ConnectionPool:
    """Keep-alive HTTP connections, at most ``size`` (the cell's stream count); never one connection per request.

    A connection the server closed (``Connection: close``, an idle timeout) reconnects on its next request; every TCP
    connect is counted, so the report shows whether the server kept connections alive.
    """

    def __init__(self, endpoint: Endpoint, size: int, timeout: float):
        self.endpoint, self.size, self.timeout = endpoint, size, timeout
        self._idle: list[http.client.HTTPConnection] = []
        self._busy: set[http.client.HTTPConnection] = set()
        self._lock = threading.Lock()
        self._created = 0
        self._closed = False
        self.counts = Counter()

    def _opened(self) -> None:
        """Count a TCP connect; a call that connects only after the cell ended is refused before it sends."""
        with self._lock:
            self.counts["opened"] += 1
            if self._closed:
                raise PoolClosed()

    def acquire(self) -> tuple[http.client.HTTPConnection, bool]:
        """A connection and whether its socket is already open (a reuse)."""
        with self._lock:
            if self._closed:
                raise PoolClosed()
            if self._idle:
                connection = self._idle.pop()
            elif self._created < self.size:
                connection = self.endpoint.connection(self.timeout, self._opened)
                self._created += 1
            else:
                raise RuntimeError("connection pool exhausted: more callers than streams")
            if connection.sock is not None and _dropped(connection.sock):
                connection.close()
                self.counts["staleDiscarded"] += 1
            reused = connection.sock is not None
            self.counts["reused"] += reused
            self._busy.add(connection)
            return connection, reused

    def release(self, connection: http.client.HTTPConnection, *, broken: bool, server_closed: bool) -> None:
        with self._lock:
            self._busy.discard(connection)
            self.counts["serverClosed"] += server_closed
            if broken or self._closed:
                connection.close()
            if not self._closed:
                self._idle.append(connection)

    def abandon_busy(self) -> int:
        """Shut down the sockets of calls in flight, so their threads return at once (drain cut-off or abort)."""
        with self._lock:
            busy = list(self._busy)
        for connection in busy:
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        return len(busy)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            connections, self._idle = self._idle + list(self._busy), []
        for connection in connections:
            connection.close()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"poolSize": self.size, "connectionsCreated": self._created, "connectionsOpened": self.counts["opened"],
                    "connectionReuses": self.counts["reused"], "serverClosedConnections": self.counts["serverClosed"],
                    "staleConnectionsDiscarded": self.counts["staleDiscarded"]}


def failure_class(error: BaseException) -> str:
    """A fixed failure class for a transport exception; exception text is never recorded."""
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionRefusedError):
        return "refused"
    if isinstance(error, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, http.client.IncompleteRead)):
        return "reset"
    if isinstance(error, ssl.SSLError):
        return "tls"
    if isinstance(error, http.client.HTTPException):
        return "protocol"
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, OSError):
        return "connection"
    return "error:" + type(error).__name__


def _integer(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _detail(body: bytes) -> str:
    """A short, single-line description of a refused or malformed response, for the cell's failure examples."""
    text = body.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        message = (error.get("message") if isinstance(error, dict) else error) or payload.get("message") \
            or payload.get("detail")
        if isinstance(message, str) and message.strip():
            text = message
    return re.sub(r"\s+", " ", text).strip()[:DETAIL_CHARACTERS]


def classify_response(status: int, body: bytes, labels: Sequence[str]) -> dict[str, Any]:
    """One HTTP response: a transport failure, or an answered call validated by the instrument's own label parser.

    Transport failures are a non-2xx status (``http-<status>``), a body that is not a JSON object (``invalid-json``)
    and a JSON object that is no chat completion (``invalid-response``); each carries a short ``detail`` from the
    body. An answered call counts its entries with a valid line; an empty completion is an answer whose entries
    are all invalid outputs.
    """
    if not 200 <= status < 300:
        return {"outcome": "failed", "failure": f"http-{status}", "detail": _detail(body)}
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {"outcome": "failed", "failure": "invalid-json", "detail": _detail(body)}
    if not isinstance(payload, dict):
        return {"outcome": "failed", "failure": "invalid-json", "detail": _detail(body)}
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {"outcome": "failed", "failure": "invalid-response", "detail": "no choices"}
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(message, dict) or (content is not None and not isinstance(content, str)):
        return {"outcome": "failed", "failure": "invalid-response", "detail": "no message text"}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    records, problems, missing = C.parse_labels(content or "", list(labels), stage=TIER)
    model, fingerprint = payload.get("model"), payload.get("system_fingerprint")
    return {"outcome": "answered", "failure": None, "valid": len(records), "missing": len(missing),
            "problems": len(problems), "empty": not content, "finishReason": choices[0].get("finish_reason"),
            "model": model if isinstance(model, str) else None,
            "fingerprint": fingerprint if isinstance(fingerprint, str) else None,
            "promptTokens": _integer(usage.get("prompt_tokens")),
            "completionTokens": _integer(usage.get("completion_tokens")),
            "cachedTokens": _integer(details.get("cached_tokens")),
            "examples": {"missing": missing[:5], "problems": problems[:3]} if missing or problems else None}


# ----------------------------------------------------------------------------- server metrics and client memory
_SAMPLE_LINE = re.compile(r'^([A-Za-z_:][A-Za-z0-9_:]*)(\{(?:[^"}]|"(?:[^"\\]|\\.)*")*\})?\s+(\S+)(?:\s+-?\d+)?\s*$')


def parse_prometheus(text: str) -> dict[str, list[float]]:
    """Sample values by metric name from the Prometheus text format (comments and unparseable lines skipped)."""
    series: dict[str, list[float]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_LINE.match(line)
        if not match:
            continue
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        if math.isnan(value):
            continue
        series.setdefault(match.group(1), []).append(value)
    return series


def _first(series: dict[str, list[float]], names: Sequence[str], combine: Callable[[list[float]], float]) -> float | None:
    for name in names:
        if series.get(name):
            return combine(series[name])
    return None


def server_load(series: dict[str, list[float]]) -> dict[str, Any]:
    """vLLM load gauges and counters, summed over engines (the KV cache usage is the fullest engine's)."""
    load = {
        "running": _first(series, ("vllm:num_requests_running",), sum),
        "waiting": _first(series, ("vllm:num_requests_waiting",), sum),
        "kvCacheUsage": _first(series, ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"), max),
        "preemptions": _first(series, ("vllm:num_preemptions_total", "vllm:num_preemptions"), sum),
        "promptTokens": _first(series, ("vllm:prompt_tokens_total",), sum),
        "generationTokens": _first(series, ("vllm:generation_tokens_total",), sum),
        "prefixCacheQueries": _first(series, ("vllm:prefix_cache_queries_total", "vllm:gpu_prefix_cache_queries_total"), sum),
        "prefixCacheHits": _first(series, ("vllm:prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits_total"), sum),
    }
    load["recognized"] = load["running"] is not None
    return load


class MetricsClient:
    """GET the Prometheus endpoint over one keep-alive connection; it never carries the judge key."""

    def __init__(self, endpoint: Endpoint, timeout: float):
        self.endpoint, self.timeout = endpoint, timeout
        self._connection: http.client.HTTPConnection | None = None
        self._lock = threading.Lock()

    def fetch(self) -> dict[str, Any]:
        with self._lock:
            try:
                if self._connection is None:
                    self._connection = self.endpoint.connection(self.timeout)
                self._connection.request("GET", self.endpoint.path or "/",
                                         headers={"Accept": "text/plain", "User-Agent": USER_AGENT})
                response = self._connection.getresponse()
                data = response.read()
            except Exception as error:  # noqa: BLE001 - any failure is a missing sample, never a probe failure
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
                return {"ok": False, "error": failure_class(error)}
        if response.status != 200:
            return {"ok": False, "error": f"http-{response.status}"}
        return {"ok": True, **server_load(parse_prometheus(data.decode("utf-8", "replace")))}

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


def rss_mb(pid: int | str = "self") -> float | None:
    """Resident set size from /proc/<pid>/status (Linux); None when unavailable."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    if pid == "self":
        try:
            import resource
            return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
        except (ImportError, OSError, ValueError):
            return None
    return None


# ----------------------------------------------------------------------------- abort rules
class AbortMonitor:
    """A cell's abort rules: the transport-failure share over a trailing window, and sustained saturation.

    The failure share counts once the window holds ``abort_min_calls`` calls or ``abort_min_failures`` failures, so
    neither one early failure nor a slow cell whose window never fills decides alone.
    """

    def __init__(self, settings: ProbeSettings, on_abort: Callable[[dict[str, Any]], None]):
        self.share, self.window = settings.abort_failure_share, settings.abort_window_seconds
        self.min_calls, self.saturation_seconds = settings.abort_min_calls, settings.saturation_seconds
        self.min_failures = settings.abort_min_failures
        self._on_abort = on_abort
        self._calls: deque = deque()
        self._failures = 0
        self._saturated_since: float | None = None
        self._lock = threading.Lock()
        self.fired: dict[str, Any] | None = None

    def _fire(self, detail: dict[str, Any]) -> None:
        if self.fired is None:
            self.fired = detail
            self._on_abort(detail)

    def fire(self, detail: dict[str, Any]) -> None:
        with self._lock:
            self._fire(detail)

    def call_finished(self, t: float, failed: bool) -> None:
        with self._lock:
            self._calls.append((t, failed))
            self._failures += failed
            while self._calls and self._calls[0][0] < t - self.window:
                self._failures -= self._calls.popleft()[1]
            calls = len(self._calls)
            if (self._failures / calls > self.share
                    and (calls >= self.min_calls or self._failures >= self.min_failures)):
                self._fire({"rule": "transport-failures", "at": round(t, 3), "windowSeconds": self.window,
                            "calls": calls, "failures": self._failures, "share": round(self._failures / calls, 4),
                            "limit": self.share})

    def sample(self, t: float, saturated: bool | None, signals: Sequence[str]) -> None:
        with self._lock:
            if not saturated:
                self._saturated_since = None
                return
            if self._saturated_since is None:
                self._saturated_since = t
            elif t - self._saturated_since > self.saturation_seconds:
                self._fire({"rule": "saturation", "at": round(t, 3), "since": round(self._saturated_since, 3),
                            "seconds": round(t - self._saturated_since, 3), "limitSeconds": self.saturation_seconds,
                            "signals": list(signals)})


# ----------------------------------------------------------------------------- measurement arithmetic
def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in [0, 1])."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _overlap(record: dict[str, Any], start: float, end: float) -> float:
    return max(0.0, min(end, record["end"]) - max(start, record["start"]))


def time_weighted_rate(records: Sequence[dict[str, Any]], start: float, end: float,
                       value: Callable[[dict[str, Any]], float]) -> float | None:
    """Per second over [start, end]: each call's value spread evenly over its own duration, counted where it overlaps.

    In steady state this equals the completion rate and does not depend on where the window cuts the calls, so a
    window shorter than a few call latencies (long calls at 40 items) is not biased by synchronised completions.
    """
    if end <= start:
        return None
    total = 0.0
    for record in records:
        amount = value(record)
        if not amount:
            continue
        duration = record["end"] - record["start"]
        if duration <= 0:
            total += amount if start <= record["end"] <= end else 0.0
        else:
            total += amount * _overlap(record, start, end) / duration
    return total / (end - start)


def completed_rate(records: Sequence[dict[str, Any]], start: float, end: float,
                   value: Callable[[dict[str, Any]], float]) -> float | None:
    """Per second over [start, end], counting only calls that ended inside the window."""
    if end <= start:
        return None
    return sum(value(r) for r in records if start <= r["end"] <= end) / (end - start)


def mean_concurrency(records: Sequence[dict[str, Any]], start: float, end: float) -> float | None:
    if end <= start:
        return None
    return sum(_overlap(r, start, end) for r in records) / (end - start)


def _stats(values: Sequence[float], digits: int = 3) -> dict[str, Any]:
    values = [v for v in values if v is not None]
    if not values:
        return {"max": None, "mean": None}
    return {"max": round(max(values), digits), "mean": round(sum(values) / len(values), digits)}


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


# ----------------------------------------------------------------------------- one cell
class _Inflight:
    """Our own calls in flight, with the peak since the last mark (for the shared-server comparison)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.value = 0
        self._peak = 0

    def up(self) -> None:
        with self._lock:
            self.value += 1
            self._peak = max(self._peak, self.value)

    def down(self) -> None:
        with self._lock:
            self.value -= 1

    def mark(self) -> int:
        with self._lock:
            self._peak = self.value
            return self.value

    def peak(self) -> int:
        with self._lock:
            return self._peak


class _CellRun:
    def __init__(self, probe: "CapacityProbe", entry: dict[str, Any]):
        self.probe, self.settings = probe, probe.settings
        self.entry = entry
        self.streams, self.items, self.label = entry["streams"], entry["itemsPerCall"], entry["cell"]
        self.records: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.aborted = threading.Event()        # an abort rule fired: no new calls
        self.abandon_now = threading.Event()    # ... and the calls in flight are cut off (all rules but saturation)
        self.abandoning = threading.Event()
        self.gauge = _Inflight()
        self.monitor = AbortMonitor(self.settings, self._on_abort)
        self.saturation_responses = 0
        self.errors: list[str] = []
        self.invalid_examples: list[dict[str, Any]] = []
        self.failure_examples: dict[str, list[dict[str, Any]]] = {}
        self.last_preemptions: float | None = None
        self.carried_tokens = 0         # max_tokens of earlier cells' abandoned calls the server may still run
        self.t0 = 0.0

    def _on_abort(self, detail: dict[str, Any]) -> None:
        """Saturation stops new calls and lets those in flight finish (they measure the saturated server);
        failures and probe errors also cut off the calls in flight."""
        self.aborted.set()
        if detail.get("rule") != "saturation":
            self.abandon_now.set()

    # -------------------------------------------------------------- calls
    def _call(self, pool: ConnectionPool, body: bytes, labels: list[str]) -> dict[str, Any]:
        connection, reused = pool.acquire()
        status, data, failure, server_closed = None, b"", None, False
        self.gauge.up()
        started = time.monotonic()
        try:
            connection.request("POST", self.probe.chat_path, body=body, headers=self.probe.headers)
            response = connection.getresponse()
            data = response.read()
            status, server_closed = response.status, bool(response.will_close)
        except Exception as error:  # noqa: BLE001 - every transport exception becomes a failure class
            failure = failure_class(error)
        finally:
            ended = time.monotonic()
            self.gauge.down()
            pool.release(connection, broken=failure is not None, server_closed=server_closed)
        if failure is not None:
            result = {"outcome": "abandoned" if self.abandoning.is_set() else "failed", "failure": failure}
        else:
            result = classify_response(status, data, labels)
        return {**result, "status": status, "reused": reused, "start": round(started - self.t0, 4),
                "end": round(ended - self.t0, 4), "seconds": round(ended - started, 4)}

    def _worker(self, index: int, pool: ConnectionPool, source: _BatchSource, end_at: float) -> None:
        if self.stop.wait(max(0.0, self.t0 + index * self.settings.ramp_seconds / self.streams - time.monotonic())):
            return
        seq = 0
        try:
            while not self.stop.is_set() and time.monotonic() < end_at:
                batch, body, labels = source.next()
                if self.stop.is_set():
                    break
                try:
                    outcome = self._call(pool, body, labels)
                except PoolClosed:
                    break
                examples, detail = outcome.pop("examples", None), outcome.pop("detail", None)
                record = {"cell": self.label, "stream": index, "seq": seq, "batch": batch.index, "review": batch.review,
                          "items": len(labels), "bodyBytes": len(body), "bodySha256": hashlib.sha256(body).hexdigest(),
                          **outcome}
                self._record(record, examples, detail)
                seq += 1
        except Exception as error:  # noqa: BLE001 - a probe defect stops the grid and is reported
            with self.lock:
                self.errors.append(f"{type(error).__name__}: {str(error)[:200]}")
            self.monitor.fire({"rule": "internal-error", "at": round(time.monotonic() - self.t0, 3)})

    def _record(self, record: dict[str, Any], examples: dict[str, Any] | None, detail: str | None = None) -> None:
        with self.lock:
            self.records.append(record)
            if record.get("failure") in ("http-429", "http-503"):
                self.saturation_responses += 1
            if examples and len(self.invalid_examples) < 3:
                self.invalid_examples.append({"batch": record["batch"], **examples})
            if detail is not None:
                kept = self.failure_examples.setdefault(record["failure"], [])
                if len(kept) < 3:
                    kept.append({"batch": record["batch"], "status": record.get("status"), "detail": detail})
        if record["outcome"] == "abandoned" or record.get("failure") == "timeout":
            self.probe.abandoned(self.t0 + record["end"], C.max_tokens(record["items"], TIER))
        if record["outcome"] != "abandoned":
            self.monitor.call_finished(record["end"], record["outcome"] == "failed")

    # -------------------------------------------------------------- samples
    def _sample(self, phase: str) -> dict[str, Any]:
        settings, t = self.settings, time.monotonic() - self.t0
        before = self.gauge.mark()
        load = self.probe.metrics.fetch() if self.probe.metrics else None
        ours = max(before, self.gauge.peak())
        with self.lock:
            responses, self.saturation_responses = self.saturation_responses, 0
        allowance = self.probe.abandoned_recently(settings.abandon_grace_seconds)
        sample: dict[str, Any] = {"t": round(t, 3), "phase": phase, "ours": ours, "abandonedAllowance": allowance,
                                  "saturationResponses": responses, "clientRssMB": rss_mb(),
                                  "proxyRssMB": {str(pid): rss_mb(pid) for pid in settings.proxy_pids} or None}
        recognized = bool(load and load.get("ok") and load.get("recognized"))
        if load is not None:
            sample.update({"ok": bool(load.get("ok")), "recognized": recognized, "error": load.get("error")})
            for key in ("running", "waiting", "kvCacheUsage", "preemptions", "promptTokens", "generationTokens",
                        "prefixCacheQueries", "prefixCacheHits"):
                sample[key] = load.get(key)
        signals = []
        if recognized:
            server = (sample["running"] or 0) + (sample["waiting"] or 0)
            sample["excess"] = round(server - ours - allowance, 3)
            sample["foreign"] = sample["excess"] > settings.shared_tolerance
            # The share of our own calls in flight that the server's load accounts for (below 1 when the metrics
            # come from another replica, or a proxy queues our calls before they reach the server).
            sample["coverage"] = round(server / ours, 3) if ours else None
            if sample["waiting"] is not None and sample["waiting"] >= settings.saturation_waiting:
                signals.append("waiting")
            if (settings.saturation_kv is not None and sample["kvCacheUsage"] is not None
                    and sample["kvCacheUsage"] >= settings.saturation_kv):
                signals.append("kv-cache")
            preempted = sample["preemptions"]
            if preempted is not None:
                if self.last_preemptions is not None and preempted > self.last_preemptions:
                    signals.append("preemptions")       # the server evicted running requests: its KV cache is full
                self.last_preemptions = preempted
        if responses:
            signals.append("http-429-503")
        sample["saturationSignals"] = signals
        sample["saturated"] = True if signals else (None if self.probe.metrics and not recognized else False)
        with self.lock:
            self.samples.append(sample)
        if phase == "run" and t >= settings.warmup_seconds:
            self.monitor.sample(t, sample["saturated"], signals)   # the ramp's queue is no saturation
        return sample

    def _sampler(self, until: threading.Event) -> None:
        interval = self.settings.sample_seconds
        next_at, beat = self.t0 + interval, time.monotonic()
        while not until.wait(max(0.0, next_at - time.monotonic())):
            self._sample("run")
            next_at += interval
            if self.settings.heartbeat_seconds and time.monotonic() - beat >= self.settings.heartbeat_seconds:
                beat = time.monotonic()
                with self.lock:
                    counts = Counter(r["outcome"] for r in self.records)
                self.probe.emit({"event": "heartbeat", "cell": self.label,
                                 "elapsed": round(time.monotonic() - self.t0, 1), "answered": counts["answered"],
                                 "failed": counts["failed"], "inFlight": self.gauge.value})

    # -------------------------------------------------------------- run
    def run(self) -> dict[str, Any]:
        settings, probe = self.settings, self.probe
        corpus = cell_corpus(settings, self.streams, self.items)
        source = _BatchSource(corpus, self.items, journal=settings.journal, model=probe.model, prefill=self.streams)
        pool = ConnectionPool(probe.endpoint, self.streams, settings.request_timeout)
        started_at = utc_now()
        self.t0 = time.monotonic()
        self.carried_tokens = probe.abandoned_tokens(settings.abandon_grace_seconds)
        self._sample("before")      # idle: our requests in flight are 0; the token counters start here
        self.t0 = time.monotonic()
        end_at = self.t0 + settings.cell_seconds
        sampler_stop = threading.Event()
        sampler = threading.Thread(target=self._sampler, args=(sampler_stop,), name=f"probe-{self.label}-sampler",
                                   daemon=True)
        workers = [threading.Thread(target=self._worker, args=(k, pool, source, end_at), name=f"probe-{self.label}-{k}",
                                    daemon=True) for k in range(self.streams)]
        sampler.start()
        for worker in workers:
            worker.start()
        abandoned_in_flight, interrupted = 0, False
        window_end = settings.cell_seconds
        try:
            while not self.aborted.is_set() and time.monotonic() < end_at:
                self.aborted.wait(max(0.0, min(1.0, end_at - time.monotonic())))
            stopped = min(time.monotonic(), end_at)
            window_end = stopped - self.t0
            self.stop.set()
            # Calls in flight finish (and count where they overlap the window) unless an abort cuts them off.
            deadline = stopped + settings.drain_seconds
            while (not self.abandon_now.is_set() and time.monotonic() < deadline
                   and any(worker.is_alive() for worker in workers)):
                self.abandon_now.wait(max(0.0, min(0.05, deadline - time.monotonic())))
        except KeyboardInterrupt:
            interrupted = True          # the calls so far are kept; the probe reports and re-raises
            window_end = min(time.monotonic(), end_at) - self.t0
        finally:
            self.stop.set()
            abandoned_in_flight = self._abandon(pool, workers)
            drained_at = time.monotonic()
            sampler_stop.set()
            sampler.join(10.0)
            pool.close()
        if not interrupted:
            try:
                if settings.pause_seconds:
                    time.sleep(settings.pause_seconds)
                self._sample("after")
            except KeyboardInterrupt:
                interrupted = True
        summary = self.summarize(started_at=started_at, window_end=window_end, drained=drained_at - self.t0,
                                 abandoned_in_flight=abandoned_in_flight, pool=pool.stats(),
                                 unjoined=sum(worker.is_alive() for worker in workers))
        if interrupted:
            summary["state"] = "interrupted"
        return summary

    def _abandon(self, pool: ConnectionPool, workers: list[threading.Thread]) -> int:
        """Abandon the calls still in flight (drain cut-off or abort) and wait for every stream thread to return."""
        if not any(worker.is_alive() for worker in workers):
            return 0
        self.abandoning.set()
        abandoned, deadline = pool.abandon_busy(), time.monotonic() + 10.0
        while any(worker.is_alive() for worker in workers) and time.monotonic() < deadline:
            for worker in workers:
                worker.join(0.1)
            pool.abandon_busy()     # a call that connected after the first pass is shut down as well
        return abandoned

    # -------------------------------------------------------------- summary
    def summarize(self, *, started_at: str, window_end: float, drained: float, abandoned_in_flight: int,
                  pool: dict[str, int], unjoined: int) -> dict[str, Any]:
        settings, records = self.settings, list(self.records)
        abort = self.monitor.fired
        window_start = settings.warmup_seconds
        measured = window_end - window_start if window_end > window_start else 0.0
        answered = [r for r in records if r["outcome"] == "answered"]
        failed = [r for r in records if r["outcome"] == "failed"]
        abandoned = [r for r in records if r["outcome"] == "abandoned"]
        valid = lambda r: r.get("valid") or 0
        rate = time_weighted_rate(records, window_start, window_end, valid) if measured else None
        answered_items = sum(r["items"] for r in answered)
        missing = sum(r.get("missing") or 0 for r in answered)
        late = [r for r in answered if r["start"] >= window_start]
        latency_scope = "calls started after the warm-up" if late else "all answered calls"
        latencies = [r["seconds"] for r in (late or answered)]
        cached_calls = [r for r in answered if r.get("cachedTokens") is not None and r.get("promptTokens")]
        prompt = sum(r.get("promptTokens") or 0 for r in answered)
        completion = sum(r.get("completionTokens") or 0 for r in answered)
        concurrency = mean_concurrency(records, window_start, window_end) if measured else None
        served_models = Counter(r.get("model") or "(none)" for r in answered)
        fingerprints = Counter(r.get("fingerprint") for r in answered if r.get("fingerprint"))
        required = C.instrument().manifest["requiredModel"]["served"]
        # A saturated cell stopped dispatching and let its calls finish: its window measured the server at capacity.
        state = "completed" if abort is None else ("saturated" if abort["rule"] == "saturation" else "aborted")
        summary = {
            "position": self.entry["position"], "cell": self.label, "streams": self.streams, "itemsPerCall": self.items,
            "state": state, "startedAt": started_at, "abort": abort,
            "timing": {"cellSeconds": settings.cell_seconds, "warmupSeconds": window_start,
                       "rampSeconds": settings.ramp_seconds, "measuredSeconds": round(measured, 3),
                       "drainSeconds": round(max(0.0, drained - window_end), 3), "wallSeconds": round(drained, 3)},
            "calls": {"sent": len(records), "answered": len(answered), "failed": len(failed),
                      "abandoned": len(abandoned), "abandonedInFlight": abandoned_in_flight,
                      "meanItems": _round(sum(r["items"] for r in records) / len(records), 2) if records else None},
            "entries": {"requested": sum(r["items"] for r in records), "answered": answered_items,
                        "valid": sum(valid(r) for r in answered), "invalid": missing,
                        "perSecond": _round(rate, 2),
                        "perSecondCompleted": _round(completed_rate(records, window_start, window_end, valid), 2)
                        if measured else None,
                        "meanConcurrency": _round(concurrency, 2),
                        "clientLimited": bool(concurrency is not None and concurrency < 0.9 * self.streams)},
            "tokens": {"prompt": prompt, "completion": completion,
                       "cached": sum(r["cachedTokens"] for r in cached_calls),
                       "cachedReportedCalls": len(cached_calls),
                       "cachedShare": _round(sum(r["cachedTokens"] for r in cached_calls)
                                             / sum(r["promptTokens"] for r in cached_calls), 4) if cached_calls else None,
                       "promptPerCall": _round(prompt / len(answered), 1) if answered else None,
                       "completionPerEntry": _round(completion / answered_items, 1) if answered_items else None,
                       "promptPerSecond": _round(time_weighted_rate(
                           records, window_start, window_end, lambda r: r.get("promptTokens") or 0), 1) if measured else None,
                       "completionPerSecond": _round(time_weighted_rate(
                           records, window_start, window_end, lambda r: r.get("completionTokens") or 0), 1) if measured else None},
            "latencySeconds": {"scope": latency_scope, "count": len(latencies),
                               "p50": _round(percentile(latencies, 0.5)), "p95": _round(percentile(latencies, 0.95)),
                               "p99": _round(percentile(latencies, 0.99)),
                               "mean": _round(sum(latencies) / len(latencies)) if latencies else None,
                               "max": _round(max(latencies)) if latencies else None},
            "invalidOutputShare": _round(missing / answered_items, 5) if answered_items else None,
            "invalidCalls": sum(1 for r in answered if r.get("missing")),
            "emptyCompletions": sum(1 for r in answered if r.get("empty")),
            "truncatedCalls": sum(1 for r in answered if r.get("finishReason") == "length"),
            "rejectedLines": sum(r.get("problems") or 0 for r in answered),
            "invalidExamples": list(self.invalid_examples),
            "transportFailureShare": _round(len(failed) / (len(failed) + len(answered)), 5) if failed or answered else None,
            "failures": dict(sorted(Counter(r["failure"] for r in failed).items())),
            "failureExamples": {k: list(v) for k, v in sorted(self.failure_examples.items())},
            "served": {"models": dict(served_models), "fingerprints": dict(fingerprints),
                       "withoutFingerprint": sum(1 for r in answered if not r.get("fingerprint")),
                       "matchesInstrument": bool(answered) and set(served_models) == {required}},
            "server": self._server_summary(records, failed, abandoned, window_end),
            "client": {**pool, "rssMaxMB": _stats([s.get("clientRssMB") for s in self.samples], 1)["max"],
                       "rssEndMB": self.samples[-1].get("clientRssMB") if self.samples else None},
            "proxies": self._proxy_summary(),
            "internalErrors": list(self.errors) + ([f"{unjoined} stream threads did not stop"] if unjoined else []),
        }
        return summary

    def _proxy_summary(self) -> dict[str, Any] | None:
        if not self.settings.proxy_pids:
            return None
        result = {}
        for pid in self.settings.proxy_pids:
            values = [(s.get("proxyRssMB") or {}).get(str(pid)) for s in self.samples]
            result[str(pid)] = {"rssMaxMB": _stats(values, 1)["max"],
                                "rssEndMB": values[-1] if values else None}
        return result

    def _server_summary(self, records: list[dict[str, Any]], failed: list, abandoned: list,
                        window_end: float) -> dict[str, Any]:
        """Server load over the cell, and whether it shows other clients (``shared``).

        ``shared`` is True on evidence of other clients: at least ``shared_min_samples`` samples with more requests
        on the server than ours (beyond the tolerance), or more generated tokens between the idle samples before and
        after the cell than our answers carried plus what our unanswered calls could have generated. It is False
        only when there is no such evidence and the metrics demonstrably cover our own calls (in-flight load and
        generated tokens both at least ``COVERAGE_MIN`` of ours): metrics of one replica behind a balancer, or a
        proxy that queues our calls, cannot show that a server is not shared. Otherwise it is None.
        """
        settings = self.settings
        samples = list(self.samples)
        compact = [{k: s.get(k) for k in ("t", "phase", "ok", "running", "waiting", "kvCacheUsage", "ours",
                                          "abandonedAllowance", "excess", "foreign", "coverage", "saturated",
                                          "saturationSignals", "clientRssMB")} for s in samples]
        if not self.probe.metrics:
            return {"metrics": "not-configured", "samples": compact,
                    "saturatedSamples": sum(1 for s in samples if s.get("saturated")), "shared": None}
        run = [s for s in samples if s["phase"] == "run" and s.get("recognized")]
        known = [s for s in samples if s.get("recognized")]
        state = "recognized" if known else ("unrecognized" if any(s.get("ok") for s in samples) else "unavailable")
        foreign = [s for s in known if s.get("foreign")]
        window = [s["coverage"] for s in run
                  if settings.warmup_seconds <= s["t"] <= window_end and s.get("coverage") is not None]
        before = next((s for s in samples if s["phase"] == "before" and s.get("recognized")), None)
        after = next((s for s in samples if s["phase"] == "after" and s.get("recognized")), None)
        counters = None
        if before and after:
            delta = lambda key: (after[key] - before[key]) if after.get(key) is not None and before.get(key) is not None else None
            ours = sum(r.get("completionTokens") or 0 for r in records if r["outcome"] == "answered")
            generation = delta("generationTokens")
            # A call without an answer may still have generated up to its max_tokens on the server, as may an
            # earlier cell's abandoned call that was still running when this cell began.
            unanswered = sum(C.max_tokens(r["items"], TIER) for r in (*failed, *abandoned)) + self.carried_tokens
            margin = max(COUNTER_MARGIN[0], COUNTER_MARGIN[1] * ours)
            excess = generation - ours if generation is not None else None
            counters = {"generationTokens": generation, "promptTokens": delta("promptTokens"),
                        "preemptions": delta("preemptions"), "prefixCacheQueries": delta("prefixCacheQueries"),
                        "prefixCacheHits": delta("prefixCacheHits"), "ourCompletionTokens": ours,
                        "generationExcess": excess, "unansweredTokenBound": unanswered,
                        "carriedTokenBound": self.carried_tokens, "margin": round(margin),
                        "comparable": generation is not None,
                        "coverage": round(generation / ours, 3) if generation is not None and ours else None,
                        "foreign": bool(excess is not None and excess > unanswered + margin)}
        coverage = {"inFlight": _round(percentile(window, 0.5)), "inFlightSamples": len(window),
                    "tokens": (counters or {}).get("coverage")}
        measured = [value for value in (coverage["inFlight"], coverage["tokens"]) if value is not None]
        coverage["covered"] = bool(measured) and all(value >= COVERAGE_MIN for value in measured)
        shared = None
        if known:
            gauge_foreign = len(foreign) >= settings.shared_min_samples
            counter_foreign = bool(counters and counters["foreign"])
            shared = True if (gauge_foreign or counter_foreign) else (False if run and coverage["covered"] else None)
        return {"metrics": state, "samples": compact, "sampleCount": len(run),
                "metricsErrors": sum(1 for s in samples if s.get("ok") is False),
                "running": _stats([s.get("running") for s in run]), "waiting": _stats([s.get("waiting") for s in run]),
                "kvCacheUsage": _stats([s.get("kvCacheUsage") for s in run], 4),
                "foreignSamples": len(foreign), "maxExcess": max((s["excess"] for s in known), default=None),
                "saturatedSamples": sum(1 for s in samples if s.get("saturated")),
                "coverage": coverage, "counters": counters, "shared": shared}


# ----------------------------------------------------------------------------- the probe
class CapacityProbe:
    def __init__(self, settings: ProbeSettings, *, progress: Callable[[str], None] | None = None,
                 environ: dict[str, str] | None = None):
        self.settings = settings
        self.progress = progress
        self._environ = os.environ if environ is None else environ
        self._key: str | None = None
        self.endpoint: Endpoint | None = None
        self.metrics: MetricsClient | None = None
        self.model = settings.requested_model()
        self.chat_path = ""
        self.headers: dict[str, str] = {}
        self._abandoned: list[tuple[float, int]] = []
        self._abandoned_lock = threading.Lock()

    def abandoned(self, at: float, max_tokens: int) -> None:
        """Record a call that was abandoned or timed out (monotonic seconds): the server may still be running it,
        so later samples allow for it and later token counters for up to its ``max_tokens``."""
        with self._abandoned_lock:
            self._abandoned.append((at, max_tokens))

    def _recent(self, grace: float) -> list[tuple[float, int]]:
        horizon = time.monotonic() - grace
        with self._abandoned_lock:
            self._abandoned = [entry for entry in self._abandoned if entry[0] >= horizon]
            return list(self._abandoned)

    def abandoned_recently(self, grace: float) -> int:
        return len(self._recent(grace))

    def abandoned_tokens(self, grace: float) -> int:
        return sum(tokens for _, tokens in self._recent(grace))

    # -------------------------------------------------------------- secrets
    def scrub(self, text: str) -> str:
        """Remove the key from any text the probe writes or prints (a server could echo it)."""
        if not self._key:
            return text
        for form in {self._key, json.dumps(self._key)[1:-1], json.dumps(self._key, ensure_ascii=False)[1:-1]}:
            if form:
                text = text.replace(form, "[redacted]")
        return text

    def emit(self, event: dict[str, Any]) -> None:
        if self.progress:
            self.progress(self.scrub(json.dumps(event, sort_keys=True, default=str)))

    def _prepare_transport(self) -> None:
        settings = self.settings
        self.endpoint = Endpoint.parse(settings.base_url or "", "base URL")
        self.chat_path = self.endpoint.path + "/chat/completions"
        self.headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
        if settings.api_key_env:
            key = self._environ.get(settings.api_key_env, "")
            if not key:
                raise ValidationError(f"the environment variable {settings.api_key_env} named by --api-key-env is unset or empty")
            if any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
                raise ValidationError(f"the key in {settings.api_key_env} holds characters an HTTP header cannot carry")
            self._key = key
            self.headers["Authorization"] = "Bearer " + key
        if settings.metrics_url:
            self.metrics = MetricsClient(Endpoint.parse(settings.metrics_url, "metrics URL"),
                                         timeout=max(1.0, min(5.0, settings.sample_seconds)))

    # -------------------------------------------------------------- plan
    def plan(self) -> dict[str, Any]:
        """The grid, request sizes and expected load; sends nothing."""
        settings = self.settings
        settings.validate(live=False)
        grid = ordered_grid(settings)
        runnable = [entry for entry in grid if not entry["skip"]]
        sizes: dict[int, dict[str, Any]] = {}
        system = C.system_prompt(settings.journal)
        for entry in runnable:
            items = entry["itemsPerCall"]
            if items in sizes:
                continue
            corpus = cell_corpus(settings, entry["streams"], items)
            user, body, counts, cut = [], [], [], 0
            for batch in corpus.batches(items):
                if batch.index >= settings.plan_calls:
                    break
                payload, data, labels = build_request(batch, journal=settings.journal, model=self.model)
                user.append(len(payload["messages"][1]["content"]))
                body.append(len(data))
                counts.append(len(labels))
                cut += sum(1 for item in batch.items if C.abstract_evidence(item)["cut"])
            tokens = [(len(system) + u) / TOKEN_CHARACTERS for u in user]
            sizes[items] = {"itemsPerCall": items, "callsSampled": len(user), "meanItems": round(sum(counts) / len(counts), 2),
                            "systemCharacters": len(system), "userCharacters": {"p50": percentile(user, 0.5),
                                                                                 "max": max(user)},
                            "bodyBytes": {"p50": percentile(body, 0.5), "max": max(body)},
                            "promptTokensEstimate": {"p50": round(percentile(tokens, 0.5)),
                                                     "p95": round(percentile(tokens, 0.95)), "max": round(max(tokens))},
                            "maxTokens": C.max_tokens(items, TIER),
                            "cutAbstractShare": round(cut / sum(counts), 3)}
        measured = settings.cell_seconds - settings.warmup_seconds
        load = []
        for entry in grid:
            size = sizes.get(entry["itemsPerCall"])
            row = {**entry}
            if size:
                streams, mean_items = entry["streams"], size["meanItems"]
                row.update({"itemsInFlight": round(streams * mean_items),
                            "promptTokensInFlightEstimate": streams * size["promptTokensEstimate"]["p50"],
                            "kvCeilingTokensEstimate": streams * (size["promptTokensEstimate"]["p95"] + size["maxTokens"]),
                            "latencyBudgetSeconds": round(streams * mean_items / settings.target_rate, 2),
                            "entriesForTarget": round(settings.target_rate * measured),
                            "callsForTarget": math.ceil(settings.target_rate * measured / mean_items)})
            load.append(row)
        per_cell = settings.cell_seconds + settings.pause_seconds
        minimum = len(runnable) * per_cell + settings.baseline_samples * settings.sample_seconds
        return {"format": FORMAT, "dryRun": True, "instrument": self._instrument_record(),
                "request": self._request_record(), "synthetic": self._synthetic_record(),
                "endpoint": {"baseUrl": self._safe_url(settings.base_url, "base URL"),
                             "metricsUrl": self._safe_url(settings.metrics_url, "metrics URL"), "contacted": False},
                "grid": self._grid_record(grid), "requestSizes": [sizes[k] for k in sorted(sizes)], "load": load,
                "abortRules": self._abort_record(),
                "estimatedSeconds": {"min": round(minimum), "max": round(minimum + len(runnable) * settings.drain_seconds)}}

    def _safe_url(self, url: str | None, what: str) -> str | None:
        return Endpoint.parse(url, what).url() if url else None

    def _instrument_record(self) -> dict[str, Any]:
        instrument = C.instrument()
        return {"id": instrument.id, "digest": instrument.digest, "tier": TIER,
                "requiredModel": instrument.manifest["requiredModel"]["served"],
                "validator": instrument.manifest["validator"]["id"],
                "systemPromptSha256": hashlib.sha256(C.system_prompt(self.settings.journal).encode("utf-8")).hexdigest()}

    def _request_record(self) -> dict[str, Any]:
        request = C.settings(TIER)["request"]
        rule = request["maxOutputTokens"]
        return {"model": self.model, "journal": self.settings.journal, "temperature": float(request["temperature"]),
                "reasoningEffort": request.get("reasoningEffort"), "stream": False,
                "maxTokensRule": f"max({rule['floor']}, {rule['perItem']} * items)",
                "maxTokens": {str(n): C.max_tokens(n, TIER) for n in sorted({c[1] for c in self.settings.grid_cells()})},
                "requestTimeoutSeconds": self.settings.request_timeout,
                "instrumentTimeoutSeconds": request.get("timeoutSeconds"), "repairPass": False,
                "abstractCut": C.abstract_cut()}

    def _synthetic_record(self) -> dict[str, Any]:
        return {"seed": self.settings.seed, "scope": "one call sequence per cell (seed and cell label)",
                "profile": PROFILE, "source": "generated from a fixed vocabulary; no corpus data or entitled text"}

    def _grid_record(self, grid: list[dict[str, Any]]) -> dict[str, Any]:
        settings = self.settings
        return {"order": settings.order, "cells": grid, "maxStreams": settings.max_streams,
                "cellSeconds": settings.cell_seconds, "warmupSeconds": settings.warmup_seconds,
                "rampSeconds": settings.ramp_seconds, "drainSeconds": settings.drain_seconds,
                "pauseSeconds": settings.pause_seconds, "sampleSeconds": settings.sample_seconds}

    def _abort_record(self) -> dict[str, Any]:
        settings = self.settings
        return {"transportFailureShare": settings.abort_failure_share, "windowSeconds": settings.abort_window_seconds,
                "minCalls": settings.abort_min_calls, "minFailures": settings.abort_min_failures,
                "saturationSeconds": settings.saturation_seconds,
                "saturationSignals": {"waitingAtLeast": settings.saturation_waiting, "preemptions": "any increase",
                                      "kvCacheUsageAtLeast": settings.saturation_kv, "httpStatus": sorted(SATURATION_STATUS)},
                "saturationClock": "samples after the warm-up",
                "scope": "either rule stops the cell and the grid; transport failures cut off the calls in flight, "
                         "saturation lets them finish and keeps the cell's measurement"}

    # -------------------------------------------------------------- run
    def _preflight(self) -> dict[str, Any]:
        connection = self.endpoint.connection(min(30.0, self.settings.request_timeout))
        headers = {k: v for k, v in self.headers.items() if k != "Content-Type"}
        try:
            connection.request("GET", self.endpoint.path + "/models", headers=headers)
            response = connection.getresponse()
            data = response.read()
        except Exception as error:  # noqa: BLE001
            raise ValidationError(f"cannot reach the base URL ({failure_class(error)})") from None
        finally:
            connection.close()
        if response.status in (401, 403):
            raise ValidationError(f"the server refused the request (HTTP {response.status}); check --api-key-env")
        record: dict[str, Any] = {"status": response.status, "models": None, "requestedModelListed": None}
        if response.status == 200:
            try:
                listed = [m.get("id") for m in json.loads(data).get("data", []) if isinstance(m, dict)]
                record["models"] = [m for m in listed if isinstance(m, str)][:20]
                record["requestedModelListed"] = self.model in record["models"]
            except (ValueError, AttributeError, TypeError):
                pass
        return record

    def _baseline(self) -> dict[str, Any]:
        """Server load before the first cell, while none of our calls exist.

        Other clients show as requests on the server (beyond the tolerance, in ``shared_min_samples`` samples) or as
        token counters that advance between the samples, which also catches short requests that start and finish
        between two samples.
        """
        settings = self.settings
        samples = []
        if not self.metrics:
            return {"metrics": "not-configured", "samples": [], "foreign": None}
        for index in range(settings.baseline_samples):
            if index:
                time.sleep(settings.sample_seconds)
            load = self.metrics.fetch()
            sample = {k: load.get(k) for k in ("ok", "recognized", "running", "waiting", "kvCacheUsage",
                                               "promptTokens", "generationTokens", "error")}
            if load.get("ok") and load.get("recognized"):
                sample["excess"] = (load.get("running") or 0) + (load.get("waiting") or 0)
                sample["foreign"] = sample["excess"] > settings.shared_tolerance
            samples.append(sample)
        known = [s for s in samples if s.get("recognized")]
        foreign = sum(1 for s in known if s.get("foreign"))
        advanced = {}
        for key in ("promptTokens", "generationTokens"):
            values = [s[key] for s in known if s.get(key) is not None]
            advanced[key] = values[-1] - values[0] if len(values) > 1 else None
        active = any(value for value in advanced.values())
        return {"metrics": "recognized" if known else "unavailable", "samples": samples,
                "foreignSamples": foreign, "countersAdvanced": advanced,
                "foreign": (active or foreign >= min(settings.shared_min_samples, len(known))) if known else None}

    def run(self, output: Path) -> dict[str, Any]:
        settings = self.settings
        settings.validate(live=True)
        self._prepare_transport()
        output = Path(output)
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValidationError(f"the output directory must be new or empty: {output}")
        grid = ordered_grid(settings)
        C.instrument()
        report: dict[str, Any] = {
            "format": FORMAT, "state": "running", "startedAt": utc_now(), "finishedAt": None,
            "code": {"revision": _code_revision(), "python": platform.python_version()},
            "client": {"host": socket.gethostname(), "platform": platform.platform(terse=True)},
            "instrument": self._instrument_record(), "request": self._request_record(),
            "endpoint": {"baseUrl": self.endpoint.url(), "chatPath": self.chat_path,
                         "metricsUrl": self._safe_url(settings.metrics_url, "metrics URL"),
                         "apiKeyEnv": settings.api_key_env, "keySent": bool(self._key),
                         "connection": "direct HTTP/1.1 keep-alive; proxies from the environment are not used"},
            "synthetic": self._synthetic_record(), "grid": self._grid_record(grid), "abortRules": self._abort_record(),
            "preflight": None, "baseline": None, "cells": [], "abort": None,
        }
        report["preflight"] = self._preflight()
        output.mkdir(parents=True, exist_ok=True)
        files = {"json": output / REPORT_NAME, "markdown": output / MARKDOWN_NAME, "calls": output / CALLS_NAME}
        report["files"] = {k: str(v) for k, v in files.items()}
        atomic_write(files["calls"], "")
        try:
            report["baseline"] = self._baseline()
            self.emit({"event": "baseline", **{k: report["baseline"].get(k) for k in ("metrics", "foreign")}})
            self._write(report, files, final=False)
            runnable = [entry for entry in grid if not entry["skip"]]
            for entry in grid:
                if entry["skip"]:
                    report["cells"].append({**self._skipped(entry), "reason": entry["skip"]})
                    continue
                if report["abort"]:
                    report["cells"].append({**self._skipped(entry), "reason": "grid stopped at %s (%s)" % (
                        report["abort"]["cell"], report["abort"]["rule"])})
                    continue
                self.emit({"event": "cell-start", "cell": entry["cell"], "position": entry["position"],
                           "of": len(runnable)})
                cell = _CellRun(self, entry)
                summary = cell.run()
                with files["calls"].open("a", encoding="utf-8") as handle:
                    for record in cell.records:
                        handle.write(self.scrub(json.dumps(record, sort_keys=True)) + "\n")
                report["cells"].append(summary)
                self.emit({"event": "cell-end", "cell": entry["cell"], "state": summary["state"],
                           "entriesPerSecond": summary["entries"]["perSecond"],
                           "invalidOutputShare": summary["invalidOutputShare"],
                           "transportFailureShare": summary["transportFailureShare"],
                           "p95": summary["latencySeconds"]["p95"]})
                if summary["state"] == "interrupted":
                    raise KeyboardInterrupt
                if summary["abort"]:
                    report["abort"] = {"cell": entry["cell"], **summary["abort"]}
                    self.emit({"event": "abort", **report["abort"]})
                self._write(report, files, final=False)
            report["state"] = "aborted" if report["abort"] else "completed"
        except KeyboardInterrupt:
            report["state"] = "interrupted"
            self._write(report, files, final=True)
            raise
        except Exception as error:
            report["state"], report["error"] = "failed", type(error).__name__
            self._write(report, files, final=True)
            raise
        finally:
            if self.metrics:
                self.metrics.close()
        self._write(report, files, final=True)
        self.emit({"event": "done", "state": report["state"], "report": str(files["json"])})
        return json.loads(self.scrub(json.dumps(report)))

    def _skipped(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {"position": entry["position"], "cell": entry["cell"], "streams": entry["streams"],
                "itemsPerCall": entry["itemsPerCall"], "state": "skipped"}

    def _write(self, report: dict[str, Any], files: dict[str, Path], *, final: bool) -> None:
        if final:
            report["finishedAt"] = utc_now()
        report["server"] = server_verdict(report)
        report["exitCriterion"] = exit_criterion(report, self.settings)
        report["allowedConcurrency"] = allowed_concurrency(report, self.settings)
        text = self.scrub(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)) + "\n"
        atomic_write(files["json"], text.encode("utf-8"))
        atomic_write(files["markdown"], self.scrub(render_markdown(report)).encode("utf-8"))


def _code_revision() -> str:
    try:
        from .census_runner import code_revision
        return code_revision()
    except Exception:  # noqa: BLE001 - the revision is informative only
        from . import __version__
        return f"ocsl-review {__version__}"


# ----------------------------------------------------------------------------- verdicts
MEASURED_STATES = ("completed", "saturated")      # cells whose window measured the server (eligible as best cell)


def server_verdict(report: dict[str, Any]) -> dict[str, Any]:
    """Shared server: True on any evidence of other clients; False only when the baseline and every cell that ran
    were measured without such evidence by metrics that cover our own calls; None otherwise."""
    baseline = report.get("baseline") or {}
    ran = [c for c in report.get("cells", []) if c.get("state") != "skipped"]
    evidence, gaps = [], []
    if baseline.get("foreign"):
        evidence.append("baseline: other clients' requests or generated tokens on the server before the first cell")
    elif baseline.get("metrics") != "recognized":
        gaps.append("no server load before the first cell")
    for cell in ran:
        server = cell.get("server") or {}
        if server.get("foreignSamples"):
            evidence.append(f"{cell['cell']}: {server['foreignSamples']} samples with more requests than ours "
                            f"(max excess {server.get('maxExcess')})")
        counters = server.get("counters") or {}
        if counters.get("foreign"):
            evidence.append(f"{cell['cell']}: the server generated {counters.get('generationExcess')} more tokens than "
                            f"our answers carried (allowance {counters.get('unansweredTokenBound', 0)} for unanswered "
                            f"calls and a margin of {counters.get('margin')})")
        if server.get("shared") is None:
            coverage = server.get("coverage") or {}
            if server.get("metrics") != "recognized":
                gaps.append(f"{cell['cell']}: server load {server.get('metrics', 'unavailable')}")
            elif not coverage.get("covered"):
                gaps.append(f"{cell['cell']}: the metrics account for only part of our calls (in flight "
                            f"{coverage.get('inFlight')}, tokens {coverage.get('tokens')})")
    shared_cells = [c["cell"] for c in ran if (c.get("server") or {}).get("shared")]
    measured = bool(ran) and baseline.get("metrics") == "recognized" and all(
        (c.get("server") or {}).get("shared") is not None for c in ran)
    shared = True if (baseline.get("foreign") or shared_cells) else (False if measured else None)
    configured = bool((report.get("endpoint") or {}).get("metricsUrl"))
    if not configured:
        gaps = ["no metrics endpoint (--metrics-url)"]
    elif not ran:
        gaps.append("no cell ran")
    return {"metricsConfigured": configured, "shared": shared, "sharedCells": shared_cells, "evidence": evidence,
            "gaps": gaps if shared is None else []}


def _best(cells: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [c for c in cells if c.get("state") in MEASURED_STATES and c["entries"].get("perSecond") is not None]
    if not eligible:
        return None
    best = max(eligible, key=lambda c: (c["entries"]["perSecond"], -c["streams"]))
    return {"cell": best["cell"], "streams": best["streams"], "itemsPerCall": best["itemsPerCall"],
            "state": best["state"], "measuredSeconds": (best.get("timing") or {}).get("measuredSeconds"),
            "entriesPerSecond": best["entries"]["perSecond"], "invalidOutputShare": best["invalidOutputShare"],
            "transportFailureShare": best["transportFailureShare"], "p95Seconds": best["latencySeconds"]["p95"]}


def _percent(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f} %"


def exit_criterion(report: dict[str, Any], settings: ProbeSettings) -> dict[str, Any]:
    """The best measured cell at no more than the allowed streams, and whether it reaches the target rate.

    Completed cells and cells stopped by the saturation rule (their calls finished, so their window measured the
    server at capacity) are eligible; cells stopped by transport failures or interrupted are not.
    """
    cells = [c for c in report.get("cells", []) if c.get("streams", 0) <= settings.allowed_streams]
    best = _best(cells)
    target, allowed = settings.target_rate, settings.allowed_streams
    head = f"Exit criterion (\u2265 {target:g} entries/s at the allowed concurrency of {allowed} streams):"
    if best is None:
        return {"targetEntriesPerSecond": target, "allowedStreams": allowed, "bestCell": None, "passes": None,
                "line": f"{head} not assessed - no completed or saturated cell at \u2264 {allowed} streams."}
    passes = best["entriesPerSecond"] >= target
    quality = (best["invalidOutputShare"] is not None and best["invalidOutputShare"] <= 0.01
               and (best["transportFailureShare"] or 0) <= 0.005)
    stopped = (f" (stopped by the saturation rule after {best['measuredSeconds']:g} s of measurement)"
               if best["state"] == "saturated" and best["measuredSeconds"] is not None else "")
    line = (f"{head} {'PASS' if passes else 'FAIL'} - best cell at \u2264 {allowed} streams: {best['streams']} streams x "
            f"{best['itemsPerCall']} items per call, {best['entriesPerSecond']:.1f} entries/s{stopped}; invalid outputs "
            f"{_percent(best['invalidOutputShare'], 2)}, transport failures {_percent(best['transportFailureShare'], 2)}"
            f" ({'within' if quality else 'outside'} the census limits of 1 % and 0.5 %)"
            + ("." if passes else "; the author decides."))
    return {"targetEntriesPerSecond": target, "allowedStreams": allowed, "bestCell": best, "passes": passes,
            "withinQualityLimits": quality, "line": line}


def allowed_concurrency(report: dict[str, Any], settings: ProbeSettings) -> dict[str, Any]:
    """The allowed concurrency stays at ``allowed_streams`` unless the report shows the server is not shared."""
    shared = (report.get("server") or {}).get("shared")
    allowed = settings.allowed_streams
    above = _best([c for c in report.get("cells", []) if c.get("streams", 0) > allowed])
    if shared is True:
        basis = "the server load showed other clients (shared server)"
    elif shared is False:
        basis = "no other clients in any sample or token counter: the server is not shared, so the author may allow more streams"
    elif not settings.metrics_url:
        basis = "no metrics endpoint (--metrics-url), so the report cannot show that the server is not shared"
    else:
        basis = ("the server load was not measured before and in every cell by metrics that account for our own "
                 "calls, so the report cannot show that the server is not shared")
    line = f"Allowed concurrency: {allowed} streams - {basis}."
    if shared is False and above:
        line += (f" Best cell above {allowed} streams: {above['streams']} streams x {above['itemsPerCall']} items per call, "
                 f"{above['entriesPerSecond']:.1f} entries/s.")
    return {"streams": allowed, "serverShared": shared, "mayExceed": shared is False, "bestAbove": above, "line": line}


# ----------------------------------------------------------------------------- markdown
def _num(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Capacity probe: {report['instrument']['id']} ({report['instrument']['tier']} tier)", ""]
    endpoint, request, grid = report["endpoint"], report["request"], report["grid"]
    lines += [f"- State: **{report['state']}**; started {report['startedAt']}, finished {report.get('finishedAt') or '-'}",
              f"- Endpoint: {endpoint['baseUrl']} (metrics: {endpoint.get('metricsUrl') or 'not configured'}); "
              f"requested model `{request['model']}`",
              f"- Requests: temperature {request['temperature']:g}, reasoning {request['reasoningEffort']}, "
              f"stream off, max_tokens = {request['maxTokensRule']}, first calls only (no repair pass)",
              f"- Synthetic entries: seed {report['synthetic']['seed']}, instrument digest "
              f"`{report['instrument']['digest'][:16]}`",
              f"- Grid: order {grid['order']}; each cell {grid['cellSeconds']:g} s with a {grid['warmupSeconds']:g} s "
              f"warm-up, drain up to {grid['drainSeconds']:g} s", ""]
    exit_line = (report.get("exitCriterion") or {}).get("line")
    concurrency = (report.get("allowedConcurrency") or {}).get("line")
    if exit_line:
        lines += [f"**{exit_line}**", ""]
    if concurrency:
        lines += [concurrency, ""]
    server = report.get("server") or {}
    notes = [f"- Shared-server evidence: {item}" for item in server.get("evidence") or []]
    notes += [f"- Not shown unshared: {item}" for item in server.get("gaps") or []]
    if notes:
        lines += notes + [""]
    lines += ["| Cell | State | Calls answered | Items/call | Entries/s | Entries/s (completed) | Concurrency | "
              "p50 s | p95 s | p99 s | Invalid | Transport failures | Failure classes |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for cell in report.get("cells", []):
        if cell["state"] == "skipped":
            lines.append(f"| {cell['cell']} | skipped: {_cell(cell.get('reason'))} |  |  |  |  |  |  |  |  |  |  |  |")
            continue
        failures = ", ".join(f"{k} {v}" for k, v in cell["failures"].items()) or "-"
        latency = cell["latencySeconds"]
        lines.append(
            f"| {cell['cell']} | {cell['state']} | {cell['calls']['answered']} | {_num(cell['calls']['meanItems'], 1)} | "
            f"{_num(cell['entries']['perSecond'], 2)} | {_num(cell['entries']['perSecondCompleted'], 2)} | "
            f"{_num(cell['entries']['meanConcurrency'], 1)} | {_num(latency['p50'], 2)} | {_num(latency['p95'], 2)} | "
            f"{_num(latency['p99'], 2)} | {_percent(cell['invalidOutputShare'], 2)} | "
            f"{_percent(cell['transportFailureShare'], 2)} | {failures} |")
    lines += ["", "| Cell | Prompt tokens/call | Output tokens/entry | Cached share | Server running max (mean) | "
              "Waiting max | KV cache max | Shared | Client RSS MB | Connections opened | Served model / fingerprints |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for cell in report.get("cells", []):
        if cell["state"] == "skipped":
            continue
        tokens, srv, client = cell["tokens"], cell["server"], cell["client"]
        running = srv.get("running") or {}
        served = cell["served"]
        models = ", ".join(served["models"]) or "-"
        fingerprints = ", ".join(served["fingerprints"]) or "none offered"
        shared = {True: "yes", False: "no", None: "unknown"}[srv.get("shared")]
        lines.append(
            f"| {cell['cell']} | {_num(tokens['promptPerCall'], 0)} | {_num(tokens['completionPerEntry'], 1)} | "
            f"{_percent(tokens['cachedShare'])} | {_num(running.get('max'), 0)} ({_num(running.get('mean'), 1)}) | "
            f"{_num((srv.get('waiting') or {}).get('max'), 0)} | {_percent((srv.get('kvCacheUsage') or {}).get('max'))} | "
            f"{shared} | {_num(client.get('rssMaxMB'), 0)} | {client.get('connectionsOpened')} | "
            f"{_cell(models)} / {_cell(fingerprints)} |")
    examples = [f"- {cell['cell']} {failure}: {_cell(kept[0]['detail']) or '(empty body)'}"
                for cell in report.get("cells", []) if cell["state"] != "skipped"
                for failure, kept in (cell.get("failureExamples") or {}).items() if kept]
    if examples:
        lines += ["", "Failure examples (first per class and cell):"] + examples
    abort = report.get("abort")
    if abort:
        lines += ["", f"Grid stopped at {abort['cell']}: rule `{abort['rule']}` "
                      f"({', '.join(f'{k} {v}' for k, v in abort.items() if k not in ('cell', 'rule'))})."]
        rest = [c["cell"] for c in report.get("cells", []) if c["state"] == "skipped" and
                str(c.get("reason", "")).startswith("grid stopped")]
        if rest:
            lines.append(f"Remaining cells: `--cells {','.join(rest)}`.")
    lines += ["", f"Files: {', '.join(f'{k} {v}' for k, v in (report.get('files') or {}).items())}", ""]
    return "\n".join(lines)


def _cell(value: Any) -> str:
    """Text for a markdown table cell or list item: one line, pipes escaped."""
    return re.sub(r"\s+", " ", str(value if value is not None else "")).replace("|", "\\|").strip()


def render_plan(plan: dict[str, Any]) -> str:
    request, grid = plan["request"], plan["grid"]
    lines = ["Capacity probe plan (dry run: nothing is sent)",
             f"instrument {plan['instrument']['id']} {plan['instrument']['digest'][:16]}, {plan['instrument']['tier']} tier; "
             f"model {request['model']}",
             f"requests: temperature {request['temperature']:g}, reasoning {request['reasoningEffort']}, stream off, "
             f"max_tokens = {request['maxTokensRule']}; first calls only",
             f"endpoint: {plan['endpoint']['baseUrl'] or '(no --base-url)'} (not contacted); metrics: "
             f"{plan['endpoint']['metricsUrl'] or 'not configured'}",
             f"synthetic entries: seed {plan['synthetic']['seed']}; abstracts cut by the instrument at "
             f"{request['abstractCut']} characters", "",
             f"Request sizes (first {plan['requestSizes'][0]['callsSampled'] if plan['requestSizes'] else 0} calls per "
             f"geometry; tokens estimated at {TOKEN_CHARACTERS} characters per token):",
             "| items/call | mean items | user chars p50 | user chars max | body bytes p50 | body bytes max | "
             "~prompt tokens p50 | ~prompt tokens p95 | max_tokens | cut abstracts |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for size in plan["requestSizes"]:
        lines.append(f"| {size['itemsPerCall']} | {size['meanItems']} | {size['userCharacters']['p50']:.0f} | "
                     f"{size['userCharacters']['max']} | {size['bodyBytes']['p50']:.0f} | {size['bodyBytes']['max']} | "
                     f"{size['promptTokensEstimate']['p50']} | {size['promptTokensEstimate']['p95']} | "
                     f"{size['maxTokens']} | {_percent(size['cutAbstractShare'])} |")
    lines += ["", f"Grid (order {grid['order']}; cell {grid['cellSeconds']:g} s incl. {grid['warmupSeconds']:g} s "
                  f"warm-up; drain up to {grid['drainSeconds']:g} s; pause {grid['pauseSeconds']:g} s):",
              "| # | cell | state | items in flight | ~prompt tokens in flight | ~KV ceiling tokens | "
              "latency budget s | entries for target |",
              "|---|---|---|---|---|---|---|---|"]
    for row in plan["load"]:
        if row["skip"]:
            lines.append(f"| {row['position']} | {row['cell']} | skipped: {row['skip']} |  |  |  |  |  |")
            continue
        lines.append(f"| {row['position']} | {row['cell']} | runs | {row['itemsInFlight']} | "
                     f"{row['promptTokensInFlightEstimate']} | {row['kvCeilingTokensEstimate']} | "
                     f"{row['latencyBudgetSeconds']} | {row['entriesForTarget']} |")
    rules, seconds = plan["abortRules"], plan["estimatedSeconds"]
    lines += ["", "latency budget: the mean call latency at which a cell reaches the target rate (streams x items / target).",
              f"abort: transport failures above {_percent(rules['transportFailureShare'], 0)} of calls in any "
              f"{rules['windowSeconds']:g} s window (once it holds {rules['minCalls']} calls or {rules['minFailures']} "
              f"failures), or saturation (requests waiting, preemptions, HTTP 429/503) in every sample for more than "
              f"{rules['saturationSeconds']:g} s after the warm-up; either stops the cell and the grid.",
              f"estimated duration: {_duration(seconds['min'])} to {_duration(seconds['max'])}.", ""]
    return "\n".join(lines)


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return (f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s")


# ----------------------------------------------------------------------------- command line
def add_commands(families: Any, register: Callable[..., None]) -> None:
    family = families.add_parser("capacity", help="measure judge capacity for a registered instrument")
    commands = family.add_subparsers(dest="capacity_command", required=True)
    probe = commands.add_parser(
        "probe", help="run the citation-census@1 capacity grid against an OpenAI-compatible judge (operator)",
        description="Streams x items-per-call grid of real citation-census@1 abstract-tier requests over synthetic "
                    "entries. See docs/capacity-probe.md.")
    target = probe.add_argument_group("target")
    target.add_argument("--base-url", help="chat-completions base URL, for example http://127.0.0.1:8000/v1")
    target.add_argument("--api-key-env", help="name of the environment variable holding the judge key (never printed)")
    target.add_argument("--model", help="model to request (default: the instrument's required served model)")
    target.add_argument("--metrics-url", help="vLLM Prometheus endpoint, for example http://127.0.0.1:8000/metrics")
    target.add_argument("--proxy-pid", type=int, action="append", default=[],
                        help="process id whose RSS is sampled with the server load (for example the judge proxy)")
    grid = probe.add_argument_group("grid")
    grid.add_argument("--streams", default=",".join(map(str, DEFAULT_STREAMS)), help="stream counts (default 30,60,90,120)")
    grid.add_argument("--items", default=",".join(map(str, DEFAULT_ITEMS)), help="items per call (default 1,5,10,40)")
    grid.add_argument("--cells", action="append", help="run only these cells, STREAMSxITEMS, comma-separated (repeatable)")
    grid.add_argument("--order", choices=ORDERS, default="load",
                      help="load (default: cells within --allowed-streams first, each group by ascending streams x "
                           "items), streams, items, as-given or shuffle (seeded)")
    grid.add_argument("--max-streams", type=int, help="skip cells with more streams than this")
    timing = probe.add_argument_group("timing")
    timing.add_argument("--cell-seconds", type=float, default=300.0, help="fixed duration of each cell (default 300)")
    timing.add_argument("--warmup-seconds", type=float, default=30.0, help="start of the measured window (default 30)")
    timing.add_argument("--ramp-seconds", type=float, default=10.0, help="stagger of the stream starts (default 10)")
    timing.add_argument("--drain-seconds", type=float, default=180.0,
                        help="wait for calls in flight after a cell before abandoning them (default 180)")
    timing.add_argument("--pause-seconds", type=float, default=10.0, help="idle time after each cell (default 10)")
    timing.add_argument("--request-timeout", type=float, default=300.0,
                        help="read timeout per call (default 300; a connection opens within 10 s)")
    timing.add_argument("--sample-seconds", type=float, default=5.0, help="server load sampling interval (default 5)")
    rules = probe.add_argument_group("abort rules and verdict")
    rules.add_argument("--abort-failure-share", type=float, default=0.05,
                       help="stop when transport failures exceed this share of calls in a window (default 0.05)")
    rules.add_argument("--abort-window-seconds", type=float, default=60.0, help="trailing failure window (default 60)")
    rules.add_argument("--abort-min-calls", type=int, default=20,
                       help="the failure share counts once the window holds this many calls (default 20) ...")
    rules.add_argument("--abort-min-failures", type=int, default=3, help="... or this many failures (default 3)")
    rules.add_argument("--saturation-seconds", type=float, default=60.0,
                       help="stop when every sample after the warm-up shows saturation for longer than this (default 60)")
    rules.add_argument("--saturation-waiting", type=float, default=1.0,
                       help="requests waiting on the server that count as saturation (default 1)")
    rules.add_argument("--saturation-kv", type=float,
                       help="also count KV cache usage at or above this share as saturation (off by default)")
    rules.add_argument("--shared-tolerance", type=float, default=1.0,
                       help="requests beyond ours a sample tolerates before it counts as another client's (default 1)")
    rules.add_argument("--allowed-streams", type=int, default=60,
                       help="allowed concurrency the exit criterion is assessed at (default 60)")
    rules.add_argument("--target-rate", type=float, default=40.0, help="exit criterion in entries/s (default 40)")
    probe.add_argument("--seed", type=int, default=1, help="seed of the synthetic entries (default 1)")
    probe.add_argument("--journal", default=DEFAULT_JOURNAL, help="journal parameter of the system prompt")
    probe.add_argument("--output", help="new or empty directory for the report, markdown table and call log")
    probe.add_argument("--dry-run", action="store_true", help="print the grid, request sizes and expected load; send nothing")
    register(probe, "capacity_probe", "command", "operator", state_required=False)


def settings_from_args(args: Any) -> ProbeSettings:
    return ProbeSettings(
        base_url=args.base_url, model=args.model, api_key_env=args.api_key_env, metrics_url=args.metrics_url,
        proxy_pids=tuple(args.proxy_pid or ()), streams=parse_counts(args.streams, "--streams"),
        items=parse_counts(args.items, "--items"), cells=parse_cells(args.cells), order=args.order,
        max_streams=args.max_streams, cell_seconds=args.cell_seconds, warmup_seconds=args.warmup_seconds,
        ramp_seconds=args.ramp_seconds, drain_seconds=args.drain_seconds, pause_seconds=args.pause_seconds,
        request_timeout=args.request_timeout, sample_seconds=args.sample_seconds,
        abort_failure_share=args.abort_failure_share, abort_window_seconds=args.abort_window_seconds,
        abort_min_calls=args.abort_min_calls, abort_min_failures=args.abort_min_failures,
        saturation_seconds=args.saturation_seconds,
        saturation_waiting=args.saturation_waiting, saturation_kv=args.saturation_kv,
        shared_tolerance=args.shared_tolerance, allowed_streams=args.allowed_streams, target_rate=args.target_rate,
        seed=args.seed, journal=args.journal)


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def command(args: Any) -> Any:
    settings = settings_from_args(args)
    as_json = bool(getattr(args, "json", False))
    probe = CapacityProbe(settings, progress=_stderr)
    if args.dry_run:
        plan = probe.plan()
        return plan if as_json else render_plan(plan)
    if not args.output:
        raise ValidationError("--output is required unless --dry-run is given")
    report = probe.run(Path(args.output))
    return report if as_json else probe.scrub(render_markdown(report))


__all__ = ["CapacityProbe", "ProbeSettings", "SyntheticCorpus", "ConnectionPool", "AbortMonitor", "classify_response",
           "parse_prometheus", "server_load", "time_weighted_rate", "completed_rate", "percentile", "ordered_grid",
           "parse_cells", "build_request", "render_markdown", "render_plan", "exit_criterion", "allowed_concurrency",
           "server_verdict"]
