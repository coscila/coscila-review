# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review capacity probe``: judge capacity for citation-census@1 before a census run.

The probe drives an OpenAI-compatible chat-completions endpoint with the
instrument's own abstract-tier requests: the system prompt and per-batch user
content from ``citation_census``, temperature 0, reasoning none,
``max_tokens = max(3000, 700 * n)`` and streaming off, built over synthetic
entries generated deterministically from a seed (no corpus data, no entitled
text; sizes from a built-in profile or from a profile measured on a study's own
public inputs). A grid of cells (streams x items per call) runs for a fixed time
each, over a keep-alive connection pool sized to the stream count. The measured
window is a steady state: it starts once the calls of the ramp have finished
and the streams keep sending past its end until the calls that overlap it have
finished. Every cell records valid entries per second, tokens, the cached-token
share, latency percentiles, invalid outputs under the instrument's validator,
failure classes, server load from the vLLM Prometheus endpoint (with a
shared-server check over request gauges and token and request counters), the
client's RSS and the served model and fingerprint. Abort rules stop the cell and
the grid when transport failures exceed a share of calls in a trailing window
(the calls in flight are cut off), or when the server reports saturation, or its
metrics stay unreadable, for too long (the calls in flight finish).

The exit criterion is assessed on one cell, the instrument's own geometry (its
items per call) at the allowed concurrency, and only when that cell was answered
by the instrument's required model and measured long enough; the other
geometries are exploratory. Cells above the allowed concurrency run only while
the server can still be shown not to be shared. The report is JSON plus a
markdown table with the exit criterion line.

The probe only measures: it is not part of any instrument digest and never
writes a scores database. It sends first calls only, unless ``--repair`` adds
the census's one repair pass: for every batch whose first call left labels
without a parseable line (every label when the first call failed), the same
stream sends the repair call the census runner builds (``census_runner``'s
``repair_items`` and ``abstract_call``, with the instrument's repair note), and
each cell reports its invalid outputs before and after the repair pass, overall
and by the items of the batch's first call (1, 2-9, 10-39, 40), as the census
gates count them.

``--tier full-text`` probes the full-text tier with real inputs instead: the
reviews of one journal in the operator's literature-evidence@2 projection (the
store the census reads as a byte cache, opened through the census's own guarded
reader, so no label or verdict can be read) are prepared exactly as a census
prepares them (``ReviewAcquirer`` from the store, ``derive_works``, metadata from
the store's Europe PMC records, ``CensusRun.prepare_review``), every entry whose
cited work has a PMCID is retrieved and windowed by the tier's own code
(``CensusRun.prepare_full_text_group``: the store first, then Europe PMC through
the census's fetcher, with its headers and spacing; fetched full texts are kept
in a cache directory for later runs), and the calls are the census's
(``census_runner.full_text_call``: five entries of one review per call, the
claim-targeted windows in the FULL TEXT field, reasoning low, ``max_tokens`` for
the call's size, the journal label in the system prompt). One cell sends each
prepared call once and reports valid, missing and malformed entries per call
and overall. citation-census@1 has no repair pass at that tier, which the report
states; ``--hypothetical-repair`` adds a predeclared single-item resend of every
entry left without a valid verdict, reported as hypothetical.
"""
from __future__ import annotations

import hashlib
import http.client
import itertools
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
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from . import citation_census as C
from .errors import ValidationError
from .util import atomic_write

FORMAT = "ocsl-capacity-probe@2"
PROFILE_FORMAT = "ocsl-capacity-profile@1"
TIER = "abstract"
FULL_TEXT = "full-text"
TIERS = (TIER, FULL_TEXT)
FULL_TEXT_STAGE = "fulltext"              # the validator's stage at the full-text tier (census_runner._judge_full_text)
FULL_TEXT_STREAMS = 20                    # the full-text cell's default streams (Stage B is paced by its retrieval)
FULL_TEXT_ENTRIES = 2000                  # retrieved entries the full-text tier prepares by default
FULL_TEXT_INPUTS_NAME = "full-text-inputs.sqlite3"
FULL_TEXT_CACHE_NAME = "full-text-cache"
FETCH_WORKERS = 4                         # study full-text --fetch-workers' default
FETCH_WORKERS_LIMIT = 16
PLAN_REVIEWS = 5                          # reviews the dry run prepares (offline) to describe the full-text inputs
HYPOTHETICAL_REPAIR = "hypothetical-repair"
REFUSED_FAILURES = ("http-400", "http-413", "http-422")   # refusals the census counts as answered without labels
PMCID = re.compile(r"PMC[0-9]{1,12}")
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
COUNTER_MARGIN = (16, 1)      # tokens beyond ours the counters tolerate: absolute, and per call that may have run
PROMPT_OVERHEAD = 64          # prompt tokens a chat template may add to a request beyond its body bytes
DECISION_CYCLES = 2           # a cell decides only after this many p95 call latencies of steady state (or its full window)
DETAIL_CHARACTERS = 200       # length of a failure example's text (server error messages; the key is redacted)
KEY_FRAGMENT = 8              # a run of this many characters of the key in server text is redacted too
REPORT_NAME, MARKDOWN_NAME, CALLS_NAME = "capacity-probe.json", "capacity-probe.md", "calls.jsonl"
MEASURED_STATES = ("completed", "saturated", "metrics-lost")   # cells whose window measured the server
STOP_RULES = {"saturation": "saturated", "metrics-unavailable": "metrics-lost"}   # rules that let calls in flight finish

# Size distributions of the synthetic entries; recorded in every report. ``--profile`` replaces them with the sizes of
# a study's own public inputs (``capacity profile``).
PROFILE: dict[str, Any] = {
    "reviewEntries": {"distribution": "lognormal", "median": 110, "sigma": 0.6, "min": 8, "max": 600},
    "citingSentences": {"weights": [0.55, 0.25, 0.2], "note": "1, 2 or 3 sentences (never more than the instrument sends)"},
    "sentenceCharacters": {"distribution": "lognormal", "median": 170, "sigma": 0.4, "min": 60, "max": 480},
    "citingFromAbstract": {"share": 0.8, "note": "citing sentences built from a span of the entry's own abstract, so the "
                                                 "judge can support most entries; the rest are unrelated text"},
    "abstractCharacters": {"distribution": "lognormal", "median": 1500, "sigma": 0.45, "min": 200, "max": 4000,
                           "missingShare": 0.05, "structuredShare": 0.3,
                           "note": "the instrument's builder applies its abstract cut"},
    "titleWords": {"min": 6, "max": 24},
    "retractedShare": 0.003,
    "years": {"min": 1985, "max": 2025},
    "printedReference": "authors, title, venue, year, volume, pages and a synthetic DOI; recorded, never sent",
}
_LOGNORMAL_KEYS = ("reviewEntries", "sentenceCharacters", "abstractCharacters")

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
_STRUCTURED = re.compile(r"\b(?:Background|Methods|Results|Conclusions?|Objectives?|Purpose|Findings)\s*:")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------- profiles
def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_profile(profile: Any) -> dict[str, Any]:
    """A complete synthetic size profile: the built-in one with the given keys replaced, each checked."""
    if not isinstance(profile, dict):
        raise ValidationError("a capacity profile is a JSON object")
    unknown = set(profile) - set(PROFILE)
    if unknown:
        raise ValidationError(f"unknown capacity profile keys: {', '.join(sorted(unknown))}")
    merged = json.loads(json.dumps(PROFILE))
    for key, value in profile.items():
        if key == "printedReference":
            continue
        if key in _LOGNORMAL_KEYS:
            if not isinstance(value, dict) or value.get("distribution", "lognormal") != "lognormal":
                raise ValidationError(f"profile {key} is a lognormal distribution")
            spec = {**merged[key], **value}
            if not (all(_number(spec.get(k)) for k in ("median", "sigma", "min", "max")) and spec["median"] > 0
                    and spec["sigma"] >= 0 and 1 <= spec["min"] <= spec["max"]):
                raise ValidationError(f"profile {key} needs median > 0, sigma >= 0 and 1 <= min <= max")
            for share in ("missingShare", "structuredShare"):
                if share in spec and not (_number(spec[share]) and 0 <= spec[share] <= 1):
                    raise ValidationError(f"profile {key}.{share} is a share in [0, 1]")
            merged[key] = spec
        elif key == "citingSentences":
            weights = value.get("weights") if isinstance(value, dict) else None
            if (not isinstance(weights, list) or not 1 <= len(weights) <= 3 or not all(_number(w) and w >= 0 for w in weights)
                    or not sum(weights) > 0):
                raise ValidationError("profile citingSentences.weights lists one to three non-negative weights")
            merged[key] = {**merged[key], **value}
        elif key == "citingFromAbstract":
            share = value.get("share") if isinstance(value, dict) else None
            if not (_number(share) and 0 <= share <= 1):
                raise ValidationError("profile citingFromAbstract.share is a share in [0, 1]")
            merged[key] = {**merged[key], **value}
        elif key in ("titleWords", "years"):
            if not isinstance(value, dict) or not all(isinstance(value.get(k), int) for k in ("min", "max")) \
                    or not 1 <= value["min"] <= value["max"]:
                raise ValidationError(f"profile {key} needs integer min and max with 1 <= min <= max")
            merged[key] = {**merged[key], **value}
        elif key == "retractedShare":
            if not (_number(value) and 0 <= value <= 1):
                raise ValidationError("profile retractedShare is a share in [0, 1]")
            merged[key] = value
    return merged


def load_profile(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """(profile, expected tokens per entry or None, source record) from a ``capacity profile`` file."""
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ValidationError("the capacity profile is not JSON") from error
    if not isinstance(value, dict) or value.get("format") != PROFILE_FORMAT:
        raise ValidationError(f"not a capacity profile ({PROFILE_FORMAT})")
    profile = validate_profile(value.get("profile") or {})
    expected = value.get("expected")
    if expected is not None:
        if not isinstance(expected, dict):
            raise ValidationError("the profile's expected tokens are an object")
        expected = {k: expected[k] for k in ("promptTokensPerEntry", "outputTokensPerEntry")
                    if _number(expected.get(k)) and expected[k] > 0} or None
    source = {"path": Path(path).name, "sha256": hashlib.sha256(raw).hexdigest(), "source": value.get("source")}
    return profile, expected, source


def _fit(values: Sequence[float], fallback: dict[str, Any]) -> dict[str, Any]:
    """Lognormal parameters (median and sigma of the logs) with the observed range; the fallback when too few."""
    positive = sorted(float(v) for v in values if v and v > 0)
    if len(positive) < 2:
        return {k: v for k, v in fallback.items() if k != "note"}
    logs = [math.log(v) for v in positive]
    mean = sum(logs) / len(logs)
    sigma = math.sqrt(sum((x - mean) ** 2 for x in logs) / (len(logs) - 1))
    return {"distribution": "lognormal", "median": round(math.exp(mean), 1), "sigma": round(sigma, 3),
            "min": max(1, int(positive[0])), "max": max(1, int(math.ceil(positive[-1]))), "n": len(positive)}


def profile_from_database(path: Path) -> dict[str, Any]:
    """Size profile and expected tokens per entry from the public reviews of a census scores database (read-only).

    Only sizes and token counts are read (entries per review, citing sentences per entry and their length, abstract
    length, title words, the tokens of answered first calls); no text leaves the database and no verdict is read.
    """
    from . import scores_db
    judged = ("c.cited_inbody=1 AND c.identity IN ('resolved','resolved_unverified') AND (c.journal, c.doi) IN "
              "(SELECT journal, doi FROM reviews WHERE provenance_class='public' AND document_sha256 IS NOT NULL)")
    with closing(scores_db.open_readonly(Path(path))) as connection:
        reviews = [row[0] for row in connection.execute(
            f"SELECT COUNT(*) FROM citations c WHERE {judged} GROUP BY c.journal, c.doi")]
        sentences = Counter(min(3, row[0]) for row in connection.execute(
            f"SELECT COUNT(*) FROM loci l JOIN citations c ON c.journal=l.journal AND c.doi=l.doi AND c.label=l.label "
            f"WHERE l.sent=1 AND {judged} GROUP BY l.journal, l.doi, l.label"))
        lengths = [row[0] for row in connection.execute(
            f"SELECT length(l.sentence) FROM loci l JOIN citations c ON c.journal=l.journal AND c.doi=l.doi AND "
            f"c.label=l.label WHERE l.sent=1 AND l.sentence NOT LIKE 'sha256:%' AND {judged}")]
        abstracts = connection.execute(
            f"SELECT e.characters, e.text FROM citations c LEFT JOIN evidence e ON e.evidence_id=c.abs_evidence_id "
            f"WHERE {judged}").fetchall()
        titles = [len(str(row[0]).split()) for row in connection.execute(
            f"SELECT w.title FROM citations c JOIN works w ON w.work_key=c.cited_work_key WHERE {judged} "
            "AND w.title IS NOT NULL AND w.title != ''")]
        calls = connection.execute(
            "SELECT COUNT(*), SUM(n_items), SUM(prompt_tokens), SUM(completion_tokens) FROM calls WHERE tier='abstract' "
            "AND variant='primary' AND kind='first' AND error IS NULL AND prompt_tokens IS NOT NULL AND "
            "completion_tokens IS NOT NULL AND (journal, doi) IN (SELECT journal, doi FROM reviews WHERE "
            "provenance_class='public')").fetchone()
    if not reviews:
        raise ValidationError("the database holds no prepared public review with judged entries")
    present = [row[0] for row in abstracts if row[0]]
    texts = [row[1] for row in abstracts if row[0] and row[1]]
    abstract = {**_fit(present, PROFILE["abstractCharacters"]),
                "missingShare": round(1 - len(present) / len(abstracts), 4) if abstracts else 0.0,
                "structuredShare": (round(sum(1 for t in texts if len(_STRUCTURED.findall(t)) >= 2) / len(texts), 4)
                                    if texts else PROFILE["abstractCharacters"]["structuredShare"])}
    total = sum(sentences.values())
    profile = {"reviewEntries": _fit(reviews, PROFILE["reviewEntries"]),
               "citingSentences": {"weights": [round(sentences.get(k, 0) / total, 4) for k in (1, 2, 3)]
                                   if total else PROFILE["citingSentences"]["weights"]},
               "sentenceCharacters": _fit(lengths, PROFILE["sentenceCharacters"]),
               "abstractCharacters": abstract}
    if len(titles) >= 2:
        ordered = sorted(titles)
        profile["titleWords"] = {"min": max(1, ordered[len(ordered) // 100]), "max": max(1, ordered[-1 - len(ordered) // 100])}
    validate_profile(profile)
    count, items, prompt, completion = calls
    expected = ({"promptTokensPerEntry": round(prompt / items, 1), "outputTokensPerEntry": round(completion / items, 1),
                 "calls": count, "entries": items} if count and items else None)
    return {"format": PROFILE_FORMAT, "createdAt": utc_now(), "profile": profile, "expected": expected,
            "source": {"database": Path(path).name, "publicReviews": len(reviews), "entries": sum(reviews),
                       "citingSentences": len(lengths), "abstracts": len(abstracts), "firstCalls": count or 0,
                       "note": "sizes and token counts of public reviews only; no text and no verdict"}}


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
    run_above_allowed: bool = False
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
    shared_tolerance: float = 0.0
    shared_min_samples: int = 2
    abandon_grace_seconds: float = 30.0
    allowed_streams: int = 60
    target_rate: float = 40.0
    expect_output_tokens: float | None = None
    expect_prompt_tokens: float | None = None
    expect_tolerance: float = 0.2
    profile: dict[str, Any] | None = None
    profile_expected: dict[str, Any] | None = None
    profile_source: dict[str, Any] | None = None
    seed: int = 1
    journal: str = DEFAULT_JOURNAL
    heartbeat_seconds: float = 30.0
    plan_calls: int = 32
    repair: bool = False
    tier: str = TIER
    store: str | None = None
    store_root: str | None = None
    review_prefixes: tuple[str, ...] = ()
    full_text_entries: int = FULL_TEXT_ENTRIES
    fetch_workers: int = FETCH_WORKERS
    full_text_cache: str | None = None
    hypothetical_repair: bool = False
    review_route: str = "entitled"
    on_premises: bool = False
    policy: str | None = None

    def validate(self, *, live: bool) -> None:
        if live and not self.base_url:
            raise ValidationError("--base-url is required unless --dry-run is given")
        if self.tier not in TIERS:
            raise ValidationError(f"--tier must be one of {', '.join(TIERS)}")
        if self.tier == FULL_TEXT:
            self._validate_full_text()
        elif (self.store or self.store_root or self.review_prefixes or self.full_text_cache
              or self.hypothetical_repair or self.on_premises or self.policy):
            raise ValidationError("--store, --store-root, --review-doi-prefix, --full-text-cache, --hypothetical-repair, "
                                  "--on-premises and --policy belong to --tier full-text")
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
        for name in ("expect_output_tokens", "expect_prompt_tokens"):
            value = getattr(self, name)
            if value is not None and not (_number(value) and value > 0):
                raise ValidationError(f"--{name.replace('_', '-')} must be positive")
        if not 0 < self.expect_tolerance <= 1:
            raise ValidationError("--expect-tolerance is a share in (0, 1]")
        if self.profile is not None:
            validate_profile(self.profile)
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

    def _validate_full_text(self) -> None:
        """The full-text tier: real loci of one journal's reviews from a literature-evidence@2 projection, one cell."""
        if not self.store:
            raise ValidationError("--tier full-text needs --store: the literature-evidence@2 projection the census reads "
                                  "(its review documents give the real loci)")
        if not Path(self.store).is_absolute():
            raise ValidationError("--store must be an absolute path")
        if self.full_text_cache is not None and not Path(self.full_text_cache).is_absolute():
            raise ValidationError("--full-text-cache must be an absolute path")
        if not self.review_prefixes:
            raise ValidationError("--tier full-text needs --review-doi-prefix: the DOI prefix of the reviews of the "
                                  "journal --journal names (the store does not record journals)")
        if self.journal == DEFAULT_JOURNAL:
            raise ValidationError("--tier full-text sends real reviews' loci: give their journal label with --journal, "
                                  "exactly as the census's enumeration spec spells it (it enters the system prompt)")
        if self.repair:
            raise ValidationError("citation-census@1 has no repair pass at the full-text tier (its full-text repair "
                                  "passes are 0), so --repair has nothing to send; --hypothetical-repair measures a "
                                  "single-item resend of every entry left without a valid verdict, reported as hypothetical")
        if self.profile is not None or self.expect_output_tokens is not None or self.expect_prompt_tokens is not None:
            raise ValidationError("--profile and --expect-*-tokens describe the abstract tier's synthetic entries; the "
                                  "full-text tier sends real loci")
        cells = self.grid_cells()
        if len(cells) != 1:
            raise ValidationError("--tier full-text runs one cell (--cells STREAMSx%d): each prepared call is sent once"
                                  % C.items_per_call(FULL_TEXT))
        if cells[0][1] != C.items_per_call(FULL_TEXT):
            raise ValidationError("the full-text tier sends the instrument's %d items per call (each review's last call "
                                  "carries its remainder): --cells STREAMSx%d" % ((C.items_per_call(FULL_TEXT),) * 2))
        if not 1 <= self.full_text_entries <= 1_000_000:
            raise ValidationError("--full-text-entries must be from 1 to 1000000")
        if not 1 <= self.fetch_workers <= FETCH_WORKERS_LIMIT:
            raise ValidationError(f"--fetch-workers must be from 1 to {FETCH_WORKERS_LIMIT}")
        for prefix in self.review_prefixes:
            if not normal_prefix(prefix) or not normal_prefix(prefix).startswith("10."):
                raise ValidationError(f"--review-doi-prefix takes a DOI prefix such as 10.1234/abc., not {prefix!r}")
        if self.review_route not in ("public", "entitled"):
            raise ValidationError("--review-route is public or entitled (the journal's route class in the protocol)")
        if self.on_premises and not self.policy:
            raise ValidationError("--on-premises needs --policy with the judge endpoint in onPremEndpoints")
        if self.policy and not self.on_premises:
            raise ValidationError("--policy declares the judge on premises: give --on-premises with it")
        if self.review_route == "entitled" and not self.on_premises:
            # The census's own rule (census_runner._egress_gate): entitled review text reaches only a judge the operator
            # declared on premises in an egress policy.
            raise ValidationError("reviews acquired under an entitled route reach only an on-premises judge, as in the "
                                  "census: give --on-premises --policy naming the judge endpoint, or --review-route public "
                                  "for a journal the protocol acquires as public (an open-access journal)")

    def grid_cells(self) -> list[tuple[int, int]]:
        if self.cells:
            return list(dict.fromkeys(self.cells))
        if self.tier == FULL_TEXT:
            return [(FULL_TEXT_STREAMS, C.items_per_call(FULL_TEXT))]
        return [(s, n) for s in dict.fromkeys(self.streams) for n in dict.fromkeys(self.items)]

    def requested_model(self) -> str:
        return self.model or C.instrument().manifest["requiredModel"]["served"]

    def synthetic_profile(self) -> dict[str, Any]:
        return self.profile or PROFILE

    def expectation(self) -> dict[str, Any] | None:
        """Predeclared tokens per entry the census's real entries carry: the options, else the profile's."""
        expected = dict(self.profile_expected or {})
        source = {k: "profile" for k in expected}
        for key, value in (("outputTokensPerEntry", self.expect_output_tokens),
                           ("promptTokensPerEntry", self.expect_prompt_tokens)):
            if value is not None:
                expected[key], source[key] = float(value), "option"
        expected = {k: v for k, v in expected.items() if k in ("outputTokensPerEntry", "promptTokensPerEntry")}
        if not expected:
            return None
        return {**expected, "tolerance": self.expect_tolerance, "source": source}


def instrument_items(tier: str = TIER) -> int:
    """The instrument's items per call at ``tier``: the only geometry a census sends there."""
    return C.items_per_call(tier)


def normal_prefix(value: Any) -> str:
    """A DOI prefix as the census normalises DOIs: lower case, without a resolver in front."""
    text = str(value or "").strip().lower()
    for resolver in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if text.startswith(resolver):
            text = text[len(resolver):]
    return text


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


def cell_role(streams: int, items: int, settings: ProbeSettings) -> str:
    """``decisive`` (the instrument's geometry at the allowed concurrency), ``instrument`` (its geometry at other
    stream counts), or ``exploratory`` (other items per call, which a census never sends); the full-text tier's one
    cell is ``full-text``."""
    if settings.tier == FULL_TEXT:
        return FULL_TEXT
    if items != instrument_items():
        return "exploratory"
    return "decisive" if streams == settings.allowed_streams else "instrument"


def ordered_grid(settings: ProbeSettings) -> list[dict[str, Any]]:
    """The cells in run order, each with its role and the reason it is skipped (``None`` when it runs).

    ``load`` (the default) runs the instrument's geometry within the allowed concurrency first (by ascending
    streams, so the cell the exit criterion is assessed on runs before any exploratory geometry), then the other
    cells within the allowed concurrency, then the cells above it, each group by ascending load (streams x items,
    then streams): an abort rule that fires later never costs the cells the exit criterion is assessed on.
    """
    cells = settings.grid_cells()
    if settings.order == "load":
        native = instrument_items()
        cells.sort(key=lambda cell: (cell[0] > settings.allowed_streams,
                                     not (cell[1] == native and cell[0] <= settings.allowed_streams),
                                     cell[0] * cell[1], cell[0], cell[1]))
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
                     "role": cell_role(streams, items, settings), "aboveAllowed": streams > settings.allowed_streams,
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

    Every value depends only on (seed, scope, review, position) and the size profile, so a cell's call sequence is
    reproducible and two cells (different scopes) never send the same prompt. Batches follow the instrument's geometry
    (``citation_census.paper_batches``): one review's entries in bibliography order, cut into calls of ``n``. Most
    citing sentences restate a span of the entry's own abstract (``citingFromAbstract``), so the verdict mix, and with
    it the length of the judge's lines, resembles real entries more than unrelated text would.
    """

    def __init__(self, seed: int, scope: str, profile: dict[str, Any] | None = None):
        self.seed, self.scope = seed, scope
        self.profile = profile or PROFILE
        self.max_sentences = C.max_citing_sentences()

    def _rng(self, *parts: Any) -> random.Random:
        material = ":".join(str(part) for part in (FORMAT, self.seed, self.scope, *parts)).encode()
        return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:16], "big"))

    def review_size(self, review: int) -> int:
        return _draw(self._rng("size", review), self.profile["reviewEntries"])

    def review_title(self, review: int) -> str:
        rng = self._rng("title", review)
        return "%s %s and %s in %s: a synthetic review" % (
            rng.choice(_ADJECTIVES).capitalize(), rng.choice(_TERMS), rng.choice(_TERMS), rng.choice(_TERMS))

    def _title(self, rng: random.Random) -> str:
        spec = self.profile["titleWords"]
        words = [rng.choice(_TERMS if i % 2 else _ADJECTIVES + _TERMS) for i in range(rng.randint(spec["min"], spec["max"]))]
        text = " ".join(words)
        return text[0].upper() + text[1:]

    def _citing_sentence(self, rng: random.Random, label_text: str, abstract: str) -> str:
        characters = _draw(rng, self.profile["sentenceCharacters"])
        words = max(6, round(characters / 7))
        source = abstract.split()
        if len(source) >= 6 and rng.random() < self.profile["citingFromAbstract"]["share"]:
            start = rng.randrange(max(1, len(source) - words + 1))
            sentence = " ".join(source[start:start + words]).rstrip(" ,;:.")
            sentence = sentence[0].upper() + sentence[1:]
        else:
            sentence = _sentence(rng, words)[:-1]
        if len(sentence) > characters:
            cut = sentence.rfind(" ", 0, characters)
            sentence = sentence[:cut if cut > 0 else characters].rstrip(" ,;:")
        return "%s ⟨B%s⟩." % (sentence, label_text)

    def _abstract(self, rng: random.Random) -> str:
        spec = self.profile["abstractCharacters"]
        characters = _draw(rng, spec)
        if rng.random() >= spec["structuredShare"]:
            return _text(rng, characters)
        return " ".join("%s: %s" % (heading, _text(rng, max(60, int(characters * share))))
                        for heading, share in _ABSTRACT_HEADINGS)

    def item(self, review: int, position: int) -> dict[str, Any]:
        """Entry ``position`` (0-based) of a review; ``printed`` is recorded but never sent, as the instrument requires."""
        rng = self._rng("item", review, position)
        number = str(position + 1)
        abstract = "" if rng.random() < self.profile["abstractCharacters"]["missingShare"] else self._abstract(rng)
        weights = self.profile["citingSentences"]["weights"][:self.max_sentences]
        count = rng.choices(range(1, len(weights) + 1), weights=weights)[0]
        passages = [{"sentence": self._citing_sentence(rng, number, abstract), "section": rng.choice(_SECTIONS)}
                    for _ in range(count)]
        years = self.profile["years"]
        year = max(years["min"], years["max"] - int(rng.expovariate(1 / 8)))
        title = self._title(rng)
        venue = "%s %s" % (rng.choice(_VENUE_FORMS), rng.choice(_VENUE_FIELDS))
        retracted = rng.random() < self.profile["retractedShare"]
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
    return SyntheticCorpus(settings.seed, label(streams, items), settings.synthetic_profile())


def build_request(batch: Batch, *, journal: str, model: str) -> tuple[dict[str, Any], bytes, list[str]]:
    """The instrument's first call for one batch, built as the census builds it: messages, settings, body bytes."""
    from .census_runner import abstract_call
    payload = abstract_call(batch.items, batch.title, journal, kind="first", model=model)
    return payload, C.request_body(payload), [item["label"] for item in batch.items]


def build_repair_request(batch: Batch, labels: Sequence[str], answered: Sequence[str], *, journal: str,
                         model: str) -> tuple[dict[str, Any], bytes, list[str]] | None:
    """The census's repair call for one batch whose first call (over ``labels``) answered only ``answered``: the
    census runner's own selection and builder (``repair_items``, ``abstract_call``); None when nothing is left."""
    from .census_runner import abstract_call, repair_items
    items = repair_items(batch.items, labels, answered)
    if not items:
        return None
    payload = abstract_call(items, batch.title, journal, kind="repair", model=model)
    return payload, C.request_body(payload), [item["label"] for item in items]


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


# ----------------------------------------------------------------------------- the full-text tier's real inputs
class SourceExhausted(Exception):
    """A finite call sequence (the full-text tier's prepared calls) has handed out its last call."""


class _FullTextSource:
    """The prepared full-text calls, shared by the cell's streams; each call is sent once."""

    finite = True

    def __init__(self, calls: Sequence[tuple[Batch, bytes, list[str]]]):
        self._ready: deque = deque(calls)
        self._lock = threading.Lock()
        self.total = len(calls)
        self.exhausted_at: float | None = None

    def next(self) -> tuple[Batch, bytes, list[str]]:
        with self._lock:
            if self._ready:
                return self._ready.popleft()
            if self.exhausted_at is None:
                self.exhausted_at = time.monotonic()
        raise SourceExhausted


def _no_live_lookup(params: dict[str, Any]) -> None:
    """The probe looks cited works up in the store only: a work without a stored Europe PMC record gets no metadata,
    so no PMCID, and is no candidate (a census would look it up live)."""
    return None


class FullTextCache:
    """Europe PMC fullTextXML of the probe's entries, kept in a directory across probe runs.

    Each PMCID's bytes are kept as Europe PMC served them (``PMC<n>.xml``, mode 0600) and a definitive miss (no
    open-access full text) as ``PMC<n>.absent``, so a later run fetches nothing it has asked for before; a transient
    failure is never kept. ``fetch`` is the census's own fetcher (``study_cli.europe_pmc_fetcher``: its headers,
    spacing and retries). Without ``fetch`` (the dry run) nothing is fetched and only what the directory holds is
    returned.
    """

    def __init__(self, directory: Path | None, fetch: Callable[[str], bytes | None] | None):
        self.directory, self.fetch = directory, fetch
        self._lock = threading.Lock()
        self.counts: Counter = Counter()
        if directory is not None and fetch is not None:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _count(self, key: str) -> None:
        with self._lock:
            self.counts[key] += 1

    def __call__(self, pmcid: str) -> bytes | None:
        from .census_runner import RetryableFetch
        name = str(pmcid or "").strip().upper()
        if not PMCID.fullmatch(name):
            self._count("invalidPmcid")
            return None
        body_path = absent_path = None
        if self.directory is not None:
            body_path, absent_path = self.directory / f"{name}.xml", self.directory / f"{name}.absent"
            if body_path.is_file():
                self._count("cacheHits")
                return body_path.read_bytes()
            if absent_path.is_file():
                self._count("cachedAbsent")
                return None
        if self.fetch is None:
            self._count("notFetched")
            return None
        try:
            body = self.fetch(name)
        except RetryableFetch:
            self._count("transient")        # throttled or failed: the entry stays open, nothing is kept
            raise
        if body_path is not None and absent_path is not None:
            atomic_write(body_path if body else absent_path, body or b"", mode=0o600)
        self._count("fetched" if body else "absent")
        return body


# Candidates of the full-text tier in a scratch census database the probe prepared itself: in-body entries with a
# resolved identity whose cited work has a PMCID, in bibliography order. No verdict column is read (none exists in
# that database: nothing in it was ever judged).
FULL_TEXT_CANDIDATES = (
    "SELECT c.journal, c.doi, c.label, c.cited_work_key, r.title FROM citations c JOIN reviews r "
    "ON r.journal=c.journal AND r.doi=c.doi WHERE c.journal=? AND c.doi=? AND c.cited_inbody=1 "
    "AND c.identity IN ('resolved','resolved_unverified') AND c.cited_pmcid IS NOT NULL ORDER BY c.ordinal")


def _seeded(seed: int, scope: str) -> random.Random:
    material = f"{FORMAT}:{seed}:{scope}".encode()
    return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:16], "big"))


class FullTextInputs:
    """The full-text tier's calls over real loci, prepared and retrieved by the census's own code, never judged.

    The reviews are the operator's store's retained review captures whose DOI starts with one of the prefixes, in a
    seeded order. The store must be a ``literature-evidence@2`` projection, the byte cache a census reads (``study
    prepare --store``), and it is read only through the census's guarded reader, which refuses every table outside
    the projection's kept tables: no label, verdict, locus or bibliography row can be read. Each
    review is acquired from the store as a census acquires it without a live route (``ReviewAcquirer``), its cited
    works are derived from its own bibliography (``derive_works``) with title, venue, year and PMCID from the store's
    Europe PMC records (``fill_record`` without a live lookup), and it is prepared into a scratch census database
    (``CensusRun.prepare_review``), whose loci are the census's own. Every in-body entry with a resolved identity
    whose cited work has a PMCID is a candidate: the census's full-text population is the subset its abstract tier
    escalates, which the probe cannot know and never reads. Each review's candidates are shuffled with the seed
    (``citation_census.full_text_groups``) and retrieved and windowed by the tier itself
    (``CensusRun.prepare_full_text_group``: the store first, then Europe PMC through the census's fetcher), review by
    review with ``fetch_workers`` threads, until ``full_text_entries`` entries are retrieved. The calls are the
    census's: five retrieved entries of one review per call, its remainder in the last
    (``citation_census.full_text_chunks``), built by ``census_runner.full_text_call``.
    """

    def __init__(self, settings: ProbeSettings, *, model: str, revision: str,
                 emit: Callable[[dict[str, Any]], None] | None = None):
        self.settings, self.model, self.revision = settings, model, revision
        self.emit = emit or (lambda event: None)
        self.calls: list[tuple[Batch, bytes, list[str]]] = []
        self.record: dict[str, Any] = {}

    def _open(self):
        from .census_inputs import InputCaches
        return InputCaches.open(store=self.settings.store, store_root=self.settings.store_root)

    def _acquirer(self, caches):
        """The census's acquirer over the store alone (no live route): the journal's route class decides the class of
        a retained capture, as the protocol's route does in a census; a Europe PMC document is always public."""
        from .census_inputs import ReviewAcquirer, Route
        return ReviewAcquirer(caches, routes={self.settings.journal: Route(self.settings.review_route)}, live=None)

    def _reviews(self, caches) -> tuple[list[str], int]:
        """(review DOIs with one of the prefixes, in the seeded order; DOIs the store's retained-source index holds)."""
        from .census_inputs import normal_doi
        rows = caches.store.rows("SELECT DISTINCT doi FROM evidence.retained_sources ORDER BY doi")
        wanted = tuple(normal_prefix(prefix) for prefix in self.settings.review_prefixes)
        dois = sorted({doi for doi in (normal_doi(row[0]) for row in rows) if doi and doi.startswith(wanted)})
        _seeded(self.settings.seed, "full-text-reviews").shuffle(dois)
        if not dois:
            raise ValidationError("the store holds no retained review whose DOI starts with "
                                  + ", ".join(self.settings.review_prefixes))
        return dois, len(rows)

    def _prepare_review(self, run, acquirer, caches, doi: str, stats: Counter, acquired: Counter
                        ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]] | None:
        """One review into the scratch database, as a census prepares it; its candidates and their works, or None."""
        from .census_abstracts import fill_record
        from .census_inputs import CLASS_OF
        from .census_works import derived_record
        record: dict[str, Any] = {"journal": self.settings.journal, "doi": doi}
        acquisition = acquirer.acquire(record)
        if acquisition.state != "acquired":
            stats["unavailable"] += 1
            return None
        acquired[acquisition.provenance] += 1
        # The record as the acquisition step writes it (acquired_record), with the document in memory instead of a file.
        record = {**record, "document": {"kind": acquisition.kind,
                                         "sha256": hashlib.sha256(acquisition.body).hexdigest()},
                  "provenanceClass": CLASS_OF[acquisition.provenance]}
        record = derived_record(record, acquisition.body, acquisition.kind, acquisition.tables)
        if (record.get("worksDerivation") or {}).get("state") != "derived":
            stats["extractionFailed"] += 1
            return None
        record = fill_record(record, _no_live_lookup, cache=caches)
        try:
            result = run.prepare_review(record, acquisition.body, acquisition.tables,
                                        provenance=(acquisition.provenance, acquisition.detail, None))
        except ValidationError:
            stats["refused"] += 1
            return None
        if result.get("state") != "prepared":
            stats["notPrepared"] += 1
            return None
        stats["prepared"] += 1
        stats["judgeableEntries"] += int(result.get("judgeable") or 0)
        with run.lock:
            rows = [dict(row) for row in run.connection.execute(FULL_TEXT_CANDIDATES, (self.settings.journal, doi))]
            keys = sorted({row["cited_work_key"] for row in rows})
            works: dict[str, dict[str, Any]] = {}
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                works.update({row["work_key"]: dict(row) for row in run.connection.execute(
                    f"SELECT * FROM works WHERE work_key IN ({','.join('?' * len(chunk))})", chunk)})
        stats["candidates"] += len(rows)
        if not rows:
            return None
        return C.full_text_groups(rows, self.settings.seed, review=lambda row: row["doi"])[0][1], works

    def _record(self, caches, scratch: Path, *, dois: Sequence[str], retained: int, considered: int, stats: Counter,
                acquired: Counter, sources: Counter, kept: Sequence[tuple[int, str, str, list]], cache: "FullTextCache",
                started: float, live: bool) -> dict[str, Any]:
        settings = self.settings
        sizes = Counter(len(labels) for _, _, labels in self.calls)
        windows = [len(item["fulltext_window"]) for *_, entries in kept for item in entries]
        return {
            "store": caches.store.version, "storePath": settings.store, "journal": settings.journal,
            "reviewDoiPrefixes": list(settings.review_prefixes), "seed": settings.seed,
            "reviewRoute": settings.review_route, "onPremises": settings.on_premises,
            "selection": ("in-body entries with a resolved identity whose cited work has a PMCID, from the store's "
                          "retained reviews with these DOI prefixes, prepared by the census's own code; not the census's "
                          "escalated entries (the probe never reads a verdict, and nothing it prepared was judged)"),
            "reviews": {"retainedInStore": retained, "withPrefix": len(dois), "considered": considered,
                        "prepared": stats["prepared"], "used": len(kept), "unavailable": stats["unavailable"],
                        "extractionFailed": stats["extractionFailed"], "refused": stats["refused"],
                        "notPrepared": stats["notPrepared"], "acquisitions": dict(sorted(acquired.items()))},
            "entries": {"target": settings.full_text_entries, "judgeable": stats["judgeableEntries"],
                        "candidates": stats["candidates"], "tried": stats["tried"], "retrieved": stats["retrieved"],
                        "withoutFullText": stats["withoutFullText"], "transient": stats["transient"],
                        "bySource": dict(sorted(sources.items()))},
            "fullTexts": {"cacheDirectory": str(cache.directory) if cache.directory else None, "live": live,
                          **dict(sorted(cache.counts.items())),
                          "fetcher": ("study_cli.europe_pmc_fetcher: the census's own Europe PMC fullTextXML fetcher "
                                      "(its User-Agent, 0.4 s spacing per thread, 3 attempts)") if live else None,
                          "fetchWorkers": settings.fetch_workers},
            "calls": {"count": len(self.calls), "byItems": {str(k): sizes[k] for k in sorted(sizes)},
                      "meanItems": _round(sum(n * c for n, c in sizes.items()) / len(self.calls), 2) if self.calls else None},
            "windows": {"meanCharacters": _round(sum(windows) / len(windows), 1) if windows else None,
                        "maxCharacters": max(windows) if windows else None,
                        "budgetCharacters": C.window_characters()},
            "scratchDatabase": str(scratch), "seconds": round(time.monotonic() - started, 1),
        }

    def prepare(self, scratch: Path, cache: "FullTextCache") -> dict[str, Any]:
        """Prepare reviews, retrieve and window their candidates, build the calls; returns the inputs record."""
        import concurrent.futures
        from .census_runner import CensusRun, full_text_call
        settings, started = self.settings, time.monotonic()
        caches = self._open()
        try:
            dois, retained = self._reviews(caches)
            run = CensusRun(scratch, seed=settings.seed, revision=self.revision)
            try:
                run.pin_inputs(caches)
                acquirer = self._acquirer(caches)
                stats, acquired, sources = Counter(), Counter(), Counter()
                kept: list[tuple[int, str, str, list]] = []
                position, hit_rate = 0, None
                with concurrent.futures.ThreadPoolExecutor(max_workers=settings.fetch_workers) as pool:
                    while stats["retrieved"] < settings.full_text_entries and position < len(dois):
                        # Prepare reviews (here, while no retrieval runs) until their candidates cover the entries
                        # still needed, at the share of candidates retrieved so far.
                        wanted = (settings.full_text_entries - stats["retrieved"]) / (hit_rate or 0.7)
                        groups, candidates = [], 0
                        while candidates < wanted and position < len(dois):
                            doi = dois[position]
                            position += 1
                            prepared = self._prepare_review(run, acquirer, caches, doi, stats, acquired)
                            if prepared:
                                groups.append((position, doi, *prepared))
                                candidates += len(prepared[0])
                            if position % 10 == 0:
                                self.emit({"event": "full-text-inputs", "phase": "reviews", "considered": position,
                                           "prepared": stats["prepared"], "candidates": stats["candidates"]})
                        futures = [pool.submit(run.prepare_full_text_group, doi, rows, works, cache, cache=caches)
                                   for _, doi, rows, works in groups]
                        for (ordinal, doi, rows, _), future in zip(groups, futures):
                            if stats["retrieved"] >= settings.full_text_entries:
                                future.cancel()     # one already running finishes; its full texts stay in the cache
                                continue
                            _, journal, title, entries, retry = future.result()
                            stats["tried"] += len(rows)
                            stats["retrieved"] += len(entries)
                            stats["transient"] += retry
                            stats["withoutFullText"] += len(rows) - len(entries) - retry
                            sources.update(item["_entry"].get("label") or "supplied" for item in entries)
                            if entries:
                                kept.append((ordinal, doi, title, entries))
                            self.emit({"event": "full-text-inputs", "phase": "retrieval", "retrieved": stats["retrieved"],
                                       "tried": stats["tried"], "fetched": cache.counts["fetched"],
                                       "cacheHits": cache.counts["cacheHits"]})
                        if stats["tried"]:
                            hit_rate = max(0.05, stats["retrieved"] / stats["tried"])
                for number, (_, doi, title, entries) in enumerate(kept):
                    for part, chunk in enumerate(C.full_text_chunks(entries)):
                        payload = full_text_call(chunk, title, settings.journal, model=self.model)
                        self.calls.append((Batch(len(self.calls), number, part, title, chunk), C.request_body(payload),
                                           [item["label"] for item in chunk]))
            finally:
                run.close()
            self.record = self._record(caches, scratch, dois=dois, retained=retained, considered=position, stats=stats,
                                       acquired=acquired, sources=sources, kept=kept, cache=cache, started=started,
                                       live=cache.fetch is not None)
        finally:
            caches.close()
        entitled = sum(count for label, count in self.record["reviews"]["acquisitions"].items()
                       if label.endswith("-entitled"))
        if entitled and not self.settings.on_premises:
            raise ValidationError(f"{entitled} reviews were acquired as entitled; they reach only an on-premises judge")
        if not self.calls:
            raise ValidationError("no candidate's full text could be retrieved (no PMCID with an open-access full text "
                                  "among the reviews considered); nothing to send")
        self.emit({"event": "full-text-inputs", "phase": "done", "calls": len(self.calls),
                   "retrieved": self.record["entries"]["retrieved"], "seconds": self.record["seconds"]})
        return self.record

    def plan(self, cache: "FullTextCache") -> dict[str, Any]:
        """Without any network: the store's reviews for the prefixes, and the first ``PLAN_REVIEWS`` of them prepared
        in a temporary database, with the full texts the store or the cache directory already hold."""
        import tempfile
        from .census_runner import CensusRun, full_text_call
        settings, started = self.settings, time.monotonic()
        caches = self._open()
        try:
            dois, retained = self._reviews(caches)
            stats, acquired, sources = Counter(), Counter(), Counter()
            kept: list[tuple[int, str, str, list]] = []
            with tempfile.TemporaryDirectory(prefix="ocsl-capacity-plan-") as temporary:
                scratch = Path(temporary) / FULL_TEXT_INPUTS_NAME
                run = CensusRun(scratch, seed=settings.seed, revision=self.revision)
                try:
                    run.pin_inputs(caches)
                    acquirer = self._acquirer(caches)
                    for position, doi in enumerate(dois[:PLAN_REVIEWS], 1):
                        prepared = self._prepare_review(run, acquirer, caches, doi, stats, acquired)
                        if not prepared:
                            continue
                        rows, works = prepared
                        _, journal, title, entries, retry = run.prepare_full_text_group(doi, rows, works, cache,
                                                                                         cache=caches)
                        stats["tried"] += len(rows)
                        stats["retrieved"] += len(entries)
                        sources.update(item["_entry"].get("label") or "supplied" for item in entries)
                        if entries:
                            kept.append((position, doi, title, entries))
                    for number, (_, doi, title, entries) in enumerate(kept):
                        for part, chunk in enumerate(C.full_text_chunks(entries)):
                            payload = full_text_call(chunk, title, settings.journal, model=self.model)
                            self.calls.append((Batch(len(self.calls), number, part, title, chunk),
                                               C.request_body(payload), [item["label"] for item in chunk]))
                finally:
                    run.close()
            record = self._record(caches, Path("(temporary)"), dois=dois, retained=retained,
                                  considered=min(PLAN_REVIEWS, len(dois)), stats=stats, acquired=acquired,
                                  sources=sources, kept=kept, cache=cache, started=started, live=False)
        finally:
            caches.close()
        per_review = stats["candidates"] / stats["prepared"] if stats["prepared"] else None
        record["entries"]["note"] = ("the dry run fetches nothing: retrieved counts only the full texts the store or the "
                                     "cache directory hold")
        record["sample"] = {"reviews": min(PLAN_REVIEWS, len(dois)), "candidatesPerReview": _round(per_review, 1),
                            "note": "prepared offline; only full texts the store or the cache directory hold were used"}
        record["estimate"] = {
            "reviewsForTarget": math.ceil(settings.full_text_entries / per_review) if per_review else None,
            "fetchSecondsAtLeast": round(settings.full_text_entries * 0.4 / settings.fetch_workers),
            "note": ("one Europe PMC request per candidate the store and the cache directory do not hold, until the "
                     "target is retrieved (target / share of PMCIDs with an open-access full text); each fetch thread "
                     "spaces its requests 0.4 s apart, so fetching takes at least target x 0.4 s / threads plus the "
                     "downloads, and a rerun with the same cache directory fetches nothing it fetched before")}
        return record


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


def may_have_run(record: dict[str, Any]) -> bool:
    """Whether a call without an answer may still have run on the model server (and used its tokens).

    A refusal with a 4xx status (bad request, context length, authentication, rate limit) and a connection that was
    refused or never resolved reached no model; every other unanswered call may have, up to its ``max_tokens``.
    """
    if record.get("outcome") == "answered":
        return False
    failure = str(record.get("failure") or "")
    return not (failure.startswith("http-4") or failure in ("refused", "dns"))


def _integer(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _same(text: str) -> str:
    return text


def _detail(body: bytes, scrub: Callable[[str], str] = _same) -> str:
    """A short, single-line description of a refused or malformed response, for the cell's failure examples.

    ``scrub`` runs over the whole message before it is cut, so a key the server echoed cannot survive in part.
    """
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
    return re.sub(r"\s+", " ", scrub(text)).strip()[:DETAIL_CHARACTERS]


def malformed_entries(problems: Sequence[str], missing: Sequence[str]) -> int:
    """How many of an answer's entries without a valid line had a line the validator rejected (``malformed``).

    A line with a wanted label but a bad category names its entry (``citation_census.parse_labels`` reports it as
    ``bad category for <label>: ...``); every other rejected line (unparseable, not JSON, an unexpected label) may
    have been the line of any one entry still without a valid line. The other entries without a valid line got no
    line at all (``missing``): the judge returned fewer lines than items.
    """
    named = [problem for problem in problems if problem.startswith("bad category for ")]
    hit = sum(1 for label in missing if any(problem.startswith(f"bad category for {label}: ") for problem in named))
    return hit + min(len(missing) - hit, len(problems) - len(named))


def classify_response(status: int, body: bytes, labels: Sequence[str],
                      scrub: Callable[[str], str] = _same, *, stage: str = TIER) -> dict[str, Any]:
    """One HTTP response: a transport failure, or an answered call validated by the instrument's own label parser.

    Transport failures are a non-2xx status (``http-<status>``), a body that is not a JSON object (``invalid-json``)
    and a JSON object that is no chat completion (``invalid-response``); each carries a short ``detail`` from the
    body. An answered call counts its entries with a valid line, and of those without one (``missing``, the census's
    invalid outputs) how many had a rejected line (``malformed``); an empty completion is an answer whose entries
    are all invalid outputs. ``stage`` is the validator's stage (``fulltext`` at the full-text tier, as the census
    parses it). ``scrub`` cleans every string the server supplied (detail, model, fingerprint).
    """
    if not 200 <= status < 300:
        return {"outcome": "failed", "failure": f"http-{status}", "detail": _detail(body, scrub)}
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {"outcome": "failed", "failure": "invalid-json", "detail": _detail(body, scrub)}
    if not isinstance(payload, dict):
        return {"outcome": "failed", "failure": "invalid-json", "detail": _detail(body, scrub)}
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {"outcome": "failed", "failure": "invalid-response", "detail": "no choices"}
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(message, dict) or (content is not None and not isinstance(content, str)):
        return {"outcome": "failed", "failure": "invalid-response", "detail": "no message text"}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    records, problems, missing = C.parse_labels(content or "", list(labels), stage=stage)
    model, fingerprint = payload.get("model"), payload.get("system_fingerprint")
    finish = choices[0].get("finish_reason")
    return {"outcome": "answered", "failure": None, "valid": len(records), "missing": len(missing),
            "malformed": malformed_entries(problems, missing),
            "missingLabels": list(missing), "problems": len(problems), "empty": not content,
            "finishReason": scrub(finish)[:40] if isinstance(finish, str) else None,
            "model": scrub(model) if isinstance(model, str) else None,
            "fingerprint": scrub(fingerprint) if isinstance(fingerprint, str) else None,
            "promptTokens": _integer(usage.get("prompt_tokens")),
            "completionTokens": _integer(usage.get("completion_tokens")),
            "cachedTokens": _integer(details.get("cached_tokens")),
            "examples": {"missing": missing[:5], "problems": problems[:3]} if missing or problems else None}


def redact_key(text: str, key: str | None, *, fragment: int = KEY_FRAGMENT) -> str:
    """``text`` without the key: its whole forms, then any run of ``fragment`` or more of its characters."""
    if not key or not text:
        return text
    for form in sorted({key, json.dumps(key)[1:-1], json.dumps(key, ensure_ascii=False)[1:-1]}, key=len, reverse=True):
        if form:
            text = text.replace(form, "[redacted]")
    size = len(key)
    for length in range(size - 1, fragment - 1, -1):
        for start in range(size - length + 1):
            piece = key[start:start + length]
            if piece in text:
                text = text.replace(piece, "[redacted]")
    return text


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
    """vLLM load gauges and counters, summed over engines and label values (KV cache usage: the fullest engine's)."""
    load = {
        "running": _first(series, ("vllm:num_requests_running",), sum),
        "waiting": _first(series, ("vllm:num_requests_waiting",), sum),
        "kvCacheUsage": _first(series, ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"), max),
        "preemptions": _first(series, ("vllm:num_preemptions_total", "vllm:num_preemptions"), sum),
        "promptTokens": _first(series, ("vllm:prompt_tokens_total",), sum),
        "generationTokens": _first(series, ("vllm:generation_tokens_total",), sum),
        "requestSuccess": _first(series, ("vllm:request_success_total",), sum),
        "prefixCacheQueries": _first(series, ("vllm:prefix_cache_queries_total", "vllm:gpu_prefix_cache_queries_total"), sum),
        "prefixCacheHits": _first(series, ("vllm:prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits_total"), sum),
    }
    load["recognized"] = load["running"] is not None
    return load


COUNTERS = ("preemptions", "promptTokens", "generationTokens", "requestSuccess", "prefixCacheQueries", "prefixCacheHits")


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
    """A cell's abort rules: the transport-failure share over a trailing window, sustained saturation, and metrics
    that stay unreadable while our calls are in flight.

    The failure share counts once the window holds ``abort_min_calls`` calls or ``abort_min_failures`` failures, so
    neither one early failure nor a slow cell whose window never fills decides alone. A sample without metrics
    neither extends nor breaks a saturation streak; metrics missing for longer than ``saturation_seconds`` while our
    calls are in flight stop the cell, since saturation can no longer be watched.
    """

    def __init__(self, settings: ProbeSettings, on_abort: Callable[[dict[str, Any]], None]):
        self.share, self.window = settings.abort_failure_share, settings.abort_window_seconds
        self.min_calls, self.saturation_seconds = settings.abort_min_calls, settings.saturation_seconds
        self.min_failures = settings.abort_min_failures
        self._on_abort = on_abort
        self._calls: deque = deque()
        self._failures = 0
        self._saturated_since: float | None = None
        self._missing_since: float | None = None
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

    def sample(self, t: float, saturated: bool | None, signals: Sequence[str], in_flight: int = 1) -> None:
        with self._lock:
            if saturated is None:
                if in_flight <= 0:
                    self._missing_since = None
                elif self._missing_since is None:
                    self._missing_since = t
                elif t - self._missing_since > self.saturation_seconds:
                    self._fire({"rule": "metrics-unavailable", "at": round(t, 3), "since": round(self._missing_since, 3),
                                "seconds": round(t - self._missing_since, 3), "limitSeconds": self.saturation_seconds})
                return
            self._missing_since = None
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
    window shorter than a few call latencies (long calls at 40 items) is not biased by synchronised completions. The
    calls a window cuts must run under the same load throughout, which the cell ensures: the window starts once the
    ramp's calls have finished, and the streams keep sending past its end until the calls that overlap it finish.
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


# ----------------------------------------------------------------------------- the repair pass
def item_bucket(items: Any) -> str | None:
    """The census gates' bucket of a batch by the items of its first call (``census_runner.ITEM_BUCKETS``)."""
    from .census_runner import ITEM_BUCKETS
    if not isinstance(items, int) or isinstance(items, bool) or items < 1:
        return None
    for name, low, high in ITEM_BUCKETS:
        if items >= low and (high is None or items <= high):
            return name
    return None


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 5) if whole else None


def repair_summary(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Invalid outputs before and after the census's repair pass, overall and by the items of each batch's first call.

    ``records`` are a cell's calls with ``kind`` first or repair, joined by batch. Before the repair pass: labels an
    answered first call left without a parseable line, over the entries those calls carried (the share the probe
    reports without ``--repair``). After it: labels still without one once the batch's repair call was answered, over
    the entries decided, which is how the census gates count them. A batch whose repair call got no answer, or was not
    sent (an abort rule, or the cell's end cut it off), leaves its labels open: undecided, as in the census, counted
    apart and never as valid or invalid. A failed first call is repaired in full, as the census does.
    """
    from .census_runner import ITEM_BUCKETS
    names = [name for name, _, _ in ITEM_BUCKETS]
    firsts: dict[int, dict[str, Any]] = {}
    repairs: dict[int, dict[str, Any]] = {}
    for record in records:
        (repairs if record.get("kind") == "repair" else firsts)[record["batch"]] = record
    before = {name: {"entries": 0, "invalid": 0} for name in names}
    after = {name: {"decided": 0, "invalid": 0, "open": 0} for name in names}
    needed = 0
    for index, first in firsts.items():
        size = first.get("items") or 0
        name = item_bucket(size)
        if name is None:
            continue
        repair = repairs.get(index)
        repaired = bool(repair and repair.get("outcome") == "answered")
        if first.get("outcome") == "answered":
            missing = first.get("missing") or 0
            before[name]["entries"] += size
            before[name]["invalid"] += missing
            if not missing:
                after[name]["decided"] += size
                continue
            needed += 1
            if repaired:
                after[name]["decided"] += size
                after[name]["invalid"] += repair.get("missing") or 0
            else:
                after[name]["decided"] += size - missing
                after[name]["open"] += missing
        elif first.get("outcome") == "failed":
            needed += 1
            if repaired:
                after[name]["decided"] += size
                after[name]["invalid"] += repair.get("missing") or 0
            else:
                after[name]["open"] += size
        else:
            after[name]["open"] += size       # cut off before an answer: never decided
    for row in before.values():
        row["share"] = _share(row["invalid"], row["entries"])
    for row in after.values():
        row["share"] = _share(row["invalid"], row["decided"])
    calls = list(repairs.values())
    answered = [r for r in calls if r.get("outcome") == "answered"]
    latencies = [r["seconds"] for r in answered if isinstance(r.get("seconds"), (int, float))]
    entries, invalid_before = sum(r["entries"] for r in before.values()), sum(r["invalid"] for r in before.values())
    decided, invalid_after = sum(r["decided"] for r in after.values()), sum(r["invalid"] for r in after.values())
    return {
        "builder": "census_runner.repair_items and abstract_call (kind repair, the instrument's repair note)",
        "batchesNeedingRepair": needed,
        "calls": {"sent": len(calls), "answered": len(answered),
                  "failed": sum(1 for r in calls if r.get("outcome") == "failed"),
                  "abandoned": sum(1 for r in calls if r.get("outcome") == "abandoned"),
                  "notSent": max(0, needed - len(calls)),
                  "items": sum(r.get("items") or 0 for r in calls),
                  "recovered": sum(r.get("valid") or 0 for r in answered),
                  "stillWithoutLine": sum(r.get("missing") or 0 for r in answered),
                  "emptyCompletions": sum(1 for r in answered if r.get("empty")),
                  "truncatedCalls": sum(1 for r in answered if r.get("finishReason") == "length"),
                  "promptTokens": sum(r.get("promptTokens") or 0 for r in answered),
                  "completionTokens": sum(r.get("completionTokens") or 0 for r in answered),
                  "failures": dict(sorted(Counter(r.get("failure") or "unknown" for r in calls
                                                  if r.get("outcome") == "failed").items())),
                  "latencySeconds": {"p50": _round(percentile(latencies, 0.5)), "p95": _round(percentile(latencies, 0.95))}},
        "beforeRepair": {"entries": entries, "invalid": invalid_before, "share": _share(invalid_before, entries),
                         "byItemsPerCall": before},
        "afterRepair": {"decided": decided, "invalid": invalid_after, "share": _share(invalid_after, decided),
                        "open": sum(r["open"] for r in after.values()), "byItemsPerCall": after},
    }


# ----------------------------------------------------------------------------- the full-text tier's shares
def census_full_text_repair() -> dict[str, Any]:
    """What citation-census@1 does with full-text entries left without a parseable verdict, from its manifest and the
    census runner: the instrument's full-text tier has no repair pass, ``census_runner.run_full_text`` records an
    answered call's entries without a parseable verdict as missing (they stay unsure and are never sent again), and
    only a call without any response is sent again whole, in a further pass. The census gate
    ``fullTextInvalidOutputsAtMostOnePercent`` counts the missing entries over the decided ones."""
    spec = C.settings(FULL_TEXT)
    passes = int((spec.get("repair") or {}).get("passes") or 0)
    attempts = int(spec["transport"]["attempts"])
    return {"repairPasses": passes, "repairPass": passes > 0,
            "statement": (f"citation-census@1 has no repair pass at the full-text tier (instrument: full-text repair "
                          f"passes {passes}): the census records an answered call's entries without a parseable verdict "
                          "as missing, never sends them again, and its gate fullTextInvalidOutputsAtMostOnePercent "
                          "counts them; so the share before any repair is the census's own share")
            if passes == 0 else "the instrument declares a full-text repair pass the probe does not send",
            "retry": (f"only a call without any response is sent again: up to {attempts} transport attempts per call, "
                      "then again whole in up to --transport-retries further passes (study full-text, 3 by default)")}


def _entry_counts(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Valid, missing (no line at all) and malformed (a rejected line) entries of answered calls, with their shares."""
    entries = sum(r.get("items") or 0 for r in rows)
    valid = sum(r.get("valid") or 0 for r in rows)
    invalid = sum(r.get("missing") or 0 for r in rows)
    malformed = sum(r.get("malformed") or 0 for r in rows)
    missing = invalid - malformed
    return {"calls": len(rows), "entries": entries, "valid": valid, "missing": missing, "malformed": malformed,
            "invalid": invalid, "validShare": _share(valid, entries), "missingShare": _share(missing, entries),
            "malformedShare": _share(malformed, entries), "invalidShare": _share(invalid, entries)}


def hypothetical_repair_summary(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The full-text tier's entries without a valid verdict before and after a single-item resend (``--hypothetical-
    repair``), overall and by the items of the entry's first call.

    Before: over the entries of answered first calls. After: an entry the first call left invalid is decided by its
    answered resend (valid or still invalid); one whose resend got no answer or was not sent stays open, and so do the
    entries of a first call that got no answer (the census sends such a call again whole). Shares after are over the
    decided entries.
    """
    size = C.items_per_call(FULL_TEXT)
    firsts = {r["batch"]: r for r in records if r.get("kind", "first") == "first"}
    resends: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("kind") == HYPOTHETICAL_REPAIR:
            resends.setdefault(record["batch"], []).append(record)
    before = {str(n): {"entries": 0, "invalid": 0} for n in range(1, size + 1)}
    after = {str(n): {"decided": 0, "invalid": 0, "open": 0} for n in range(1, size + 1)}
    needed = 0
    for index, first in firsts.items():
        name = str(first.get("items") or 0)
        if name not in before:
            continue
        items = first.get("items") or 0
        if first.get("outcome") != "answered":
            after[name]["open"] += items
            continue
        invalid = first.get("missing") or 0
        before[name]["entries"] += items
        before[name]["invalid"] += invalid
        needed += invalid
        answered = [r for r in resends.get(index, []) if r.get("outcome") == "answered"]
        unresolved = max(0, invalid - len(answered))
        after[name]["decided"] += items - unresolved
        after[name]["invalid"] += sum(r.get("missing") or 0 for r in answered)
        after[name]["open"] += unresolved
    for row in before.values():
        row["share"] = _share(row["invalid"], row["entries"])
    for row in after.values():
        row["share"] = _share(row["invalid"], row["decided"])
    calls = [r for rows in resends.values() for r in rows]
    answered = [r for r in calls if r.get("outcome") == "answered"]
    latencies = [r["seconds"] for r in answered if isinstance(r.get("seconds"), (int, float))]
    entries, invalid_before = sum(r["entries"] for r in before.values()), sum(r["invalid"] for r in before.values())
    decided, invalid_after = sum(r["decided"] for r in after.values()), sum(r["invalid"] for r in after.values())
    prompt = sum(r.get("promptTokens") or 0 for r in answered)
    completion = sum(r.get("completionTokens") or 0 for r in answered)
    return {
        "hypothetical": True,
        "label": ("hypothetical: citation-census@1 sends no such call (its full-text repair passes are 0); adopting it "
                  "is a predeclared change of the instrument, decided by the author before the run"),
        "resend": ("one call per entry an answered full-text call left without a valid verdict, carrying that entry "
                   "alone, sent by the same stream right after the call: census_runner.full_text_call([entry], title, "
                   "journal) - citation_census.build_full_text_messages and request_payload with items 1 at the "
                   "full-text tier: the same system prompt, the entry's citing sentences and window, temperature 0, "
                   f"reasoning {C.settings(FULL_TEXT)['request'].get('reasoningEffort')}, max_tokens "
                   f"{C.max_tokens(1, FULL_TEXT)}; one resend per entry and nothing further; a call without any "
                   "response is not resent (the census sends it again whole)"),
        "entriesNeedingResend": needed,
        "calls": {"sent": len(calls), "answered": len(answered),
                  "failed": sum(1 for r in calls if r.get("outcome") == "failed"),
                  "abandoned": sum(1 for r in calls if r.get("outcome") == "abandoned"),
                  "notSent": max(0, needed - len(calls)),
                  "recovered": sum(r.get("valid") or 0 for r in answered),
                  "stillInvalid": sum(r.get("missing") or 0 for r in answered),
                  "stillMissing": sum((r.get("missing") or 0) - (r.get("malformed") or 0) for r in answered),
                  "stillMalformed": sum(r.get("malformed") or 0 for r in answered),
                  "truncatedCalls": sum(1 for r in answered if r.get("finishReason") == "length"),
                  "promptTokens": prompt, "completionTokens": completion,
                  "promptTokensPerEntry": _round(prompt / len(answered), 1) if answered else None,
                  "completionTokensPerEntry": _round(completion / len(answered), 1) if answered else None,
                  "failures": dict(sorted(Counter(r.get("failure") or "unknown" for r in calls
                                                  if r.get("outcome") == "failed").items())),
                  "latencySeconds": {"p50": _round(percentile(latencies, 0.5)),
                                     "p95": _round(percentile(latencies, 0.95))}},
        "beforeRepair": {"entries": entries, "invalid": invalid_before, "share": _share(invalid_before, entries),
                         "byItemsPerCall": before},
        "afterRepair": {"decided": decided, "invalid": invalid_after, "share": _share(invalid_after, decided),
                        "open": sum(r["open"] for r in after.values()), "byItemsPerCall": after},
    }


def full_text_summary(records: Sequence[dict[str, Any]], *, hypothetical: bool) -> dict[str, Any]:
    """The full-text cell's entries: valid, missing (no line) and malformed (a rejected line) per call and overall.

    Over answered first calls: overall, by the call's items (1 to 5: each review's last call carries its remainder),
    and per call (how many calls were all valid, had a missing or a malformed entry, and how many entries each left
    invalid); tokens per entry; the census's own handling (``census_full_text_repair``: no repair pass, so these are
    the census's shares); and with ``hypothetical`` the single-item resend.
    """
    firsts = [r for r in records if r.get("kind", "first") == "first"]
    answered = [r for r in firsts if r.get("outcome") == "answered"]
    # A call the judge refused (HTTP 400, 413 or 422) is a transport failure here; the census counts it as answered
    # and its entries as missing, so its gate share adds them.
    refused = [r for r in firsts if r.get("outcome") == "failed" and r.get("failure") in REFUSED_FAILURES]
    size = C.items_per_call(FULL_TEXT)
    by_invalid = Counter(min(r.get("missing") or 0, size) for r in answered)
    prompt = sum(r.get("promptTokens") or 0 for r in answered)
    completion = sum(r.get("completionTokens") or 0 for r in answered)
    entries = sum(r.get("items") or 0 for r in answered)
    calls = {"answered": len(answered),
             "allValid": sum(1 for r in answered if not r.get("missing")),
             "withMissing": sum(1 for r in answered if (r.get("missing") or 0) > (r.get("malformed") or 0)),
             "withMalformed": sum(1 for r in answered if r.get("malformed")),
             "emptyCompletions": sum(1 for r in answered if r.get("empty")),
             "truncatedCalls": sum(1 for r in answered if r.get("finishReason") == "length"),
             "byInvalidEntries": {str(k): by_invalid[k] for k in range(0, size + 1)}}
    calls["shares"] = {key: _share(calls[key], len(answered)) for key in ("allValid", "withMissing", "withMalformed")}
    counts = _entry_counts(answered)
    refused_entries = sum(r.get("items") or 0 for r in refused)
    counts.update({"refusedCalls": len(refused), "refusedEntries": refused_entries,
                   "censusInvalidShare": _share(counts["invalid"] + refused_entries, counts["entries"] + refused_entries)})
    summary = {"census": census_full_text_repair(), "entries": counts,
               "byItemsPerCall": {str(n): _entry_counts([r for r in answered if r.get("items") == n])
                                  for n in range(1, size + 1)},
               "calls": calls,
               "tokensPerEntry": {"prompt": _round(prompt / entries, 1) if entries else None,
                                  "output": _round(completion / entries, 1) if entries else None}}
    if hypothetical:
        summary["hypotheticalRepair"] = hypothetical_repair_summary(records)
    return summary


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
    def __init__(self, probe: "CapacityProbe", entry: dict[str, Any], source: Any = None):
        self.probe, self.settings = probe, probe.settings
        self.entry = entry
        self.source = source            # a finite source of prepared calls (full-text tier); default synthetic batches
        self.stage = FULL_TEXT_STAGE if self.settings.tier == FULL_TEXT else TIER
        self.streams, self.items, self.label = entry["streams"], entry["itemsPerCall"], entry["cell"]
        self.records: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.aborted = threading.Event()        # an abort rule fired: no new calls
        self.abandon_now = threading.Event()    # ... and the calls in flight are cut off (transport failures, defects)
        self.abandoning = threading.Event()
        self.gauge = _Inflight()
        self.monitor = AbortMonitor(self.settings, self._on_abort)
        self.saturation_responses = 0
        self.errors: list[str] = []
        self.invalid_examples: list[dict[str, Any]] = []
        self.failure_examples: dict[str, list[dict[str, Any]]] = {}
        self.last_preemptions: float | None = None
        self.carried = {"calls": 0, "tokens": 0, "prompt": 0}   # earlier cells' abandoned calls the server may still run
        self._open_calls: dict[int, float] = {}   # calls in flight: id -> start (seconds after t0)
        self._call_ids = itertools.count()
        self.t0 = 0.0

    def _on_abort(self, detail: dict[str, Any]) -> None:
        """Saturation and unreadable metrics stop new calls and let those in flight finish (they measure the server);
        transport failures and probe errors also cut off the calls in flight."""
        self.aborted.set()
        if detail.get("rule") not in STOP_RULES:
            self.abandon_now.set()

    # -------------------------------------------------------------- calls
    def _call(self, pool: ConnectionPool, body: bytes, labels: list[str]) -> dict[str, Any]:
        connection, reused = pool.acquire()
        status, data, failure, server_closed = None, b"", None, False
        call_id = next(self._call_ids)
        self.gauge.up()
        started = time.monotonic()
        with self.lock:
            self._open_calls[call_id] = started - self.t0
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
            with self.lock:
                self._open_calls.pop(call_id, None)
            pool.release(connection, broken=failure is not None, server_closed=server_closed)
        if failure is not None:
            result = {"outcome": "abandoned" if self.abandoning.is_set() else "failed", "failure": failure}
        else:
            result = classify_response(status, data, labels, scrub=self.probe.scrub_server, stage=self.stage)
        return {**result, "status": status, "reused": reused, "start": round(started - self.t0, 4),
                "end": round(ended - self.t0, 4), "seconds": round(ended - started, 4)}

    def _worker(self, index: int, pool: ConnectionPool, source: _BatchSource) -> None:
        if self.stop.wait(max(0.0, self.t0 + index * self.settings.ramp_seconds / self.streams - time.monotonic())):
            return
        seq = 0
        try:
            while not self.stop.is_set() and not self.aborted.is_set():
                try:
                    batch, body, labels = source.next()
                except SourceExhausted:
                    break
                if self.stop.is_set() or self.aborted.is_set():
                    break
                try:
                    outcome = self._call(pool, body, labels)
                except PoolClosed:
                    break
                missing = outcome.pop("missingLabels", None)
                examples, detail = outcome.pop("examples", None), outcome.pop("detail", None)
                record = {"cell": self.label, "stream": index, "seq": seq, "batch": batch.index, "review": batch.review,
                          "items": len(labels), "bodyBytes": len(body), "bodySha256": hashlib.sha256(body).hexdigest(),
                          **outcome}
                if self.settings.repair or self.settings.tier == FULL_TEXT:
                    record["kind"] = "first"
                self._record(record, examples, detail)
                seq += 1
                if self.settings.repair or self.settings.hypothetical_repair:
                    try:
                        if self.settings.repair:
                            seq += self._repair(index, seq, pool, batch, labels, outcome, missing or [])
                        else:
                            seq += self._hypothetical_repair(index, seq, pool, batch, labels, outcome, missing or [])
                    except PoolClosed:
                        break
        except Exception as error:  # noqa: BLE001 - a probe defect stops the grid and is reported
            with self.lock:
                self.errors.append(f"{type(error).__name__}: {str(error)[:200]}")
            self.monitor.fire({"rule": "internal-error", "at": round(time.monotonic() - self.t0, 3)})

    def _repair(self, stream: int, seq: int, pool: ConnectionPool, batch: Batch, labels: list[str],
                first: dict[str, Any], missing: Sequence[str]) -> int:
        """The census's one repair call for ``batch`` (``--repair``), sent by the same stream right after the first call:
        the labels the first call left without a parseable line, or every label of a failed first call, built by the
        census runner. Nothing is sent once an abort rule fired or the calls in flight are being cut off; the batch's
        open labels then stay undecided, as they would in the census. Returns the number of calls made (0 or 1)."""
        if first["outcome"] == "answered":
            if not missing:
                return 0
            left = set(missing)
            answered = [label for label in labels if label not in left]
        elif first["outcome"] == "failed":
            answered = []
        else:
            return 0                    # abandoned: the cell is cutting its calls off
        if self.aborted.is_set() or self.abandoning.is_set():
            return 0
        request = build_repair_request(batch, labels, answered, journal=self.settings.journal, model=self.probe.model)
        if request is None:
            return 0
        _, body, repair_labels = request
        outcome = self._call(pool, body, repair_labels)
        outcome.pop("missingLabels", None)
        examples, detail = outcome.pop("examples", None), outcome.pop("detail", None)
        record = {"cell": self.label, "stream": stream, "seq": seq, "batch": batch.index, "review": batch.review,
                  "kind": "repair", "items": len(repair_labels), "firstItems": len(labels), "bodyBytes": len(body),
                  "bodySha256": hashlib.sha256(body).hexdigest(), **outcome}
        self._record(record, examples, detail)
        return 1

    def _hypothetical_repair(self, stream: int, seq: int, pool: ConnectionPool, batch: Batch, labels: list[str],
                             first: dict[str, Any], missing: Sequence[str]) -> int:
        """``--hypothetical-repair`` (full-text tier): every entry an answered call left without a valid verdict is
        sent again alone, by the same stream right after the call, as the census's own full-text call of one item
        (``census_runner.full_text_call``: the same system prompt, window and citing sentences, reasoning low,
        ``max_tokens`` for one item). citation-census@1 sends no such call (its full-text repair passes are 0), so what
        this measures is hypothetical. A failed call is not resent (the census sends it again whole in a further
        pass). Nothing is sent once an abort rule fired or the calls in flight are being cut off. Returns the calls
        made."""
        from .census_runner import full_text_call
        if first["outcome"] != "answered" or not missing:
            return 0
        left, sent = set(missing), 0
        for item in batch.items:
            if item["label"] not in left:
                continue
            if self.aborted.is_set() or self.abandoning.is_set():
                break
            payload = full_text_call([item], batch.title, self.settings.journal, model=self.probe.model)
            body = C.request_body(payload)
            outcome = self._call(pool, body, [item["label"]])
            outcome.pop("missingLabels", None)
            examples, detail = outcome.pop("examples", None), outcome.pop("detail", None)
            record = {"cell": self.label, "stream": stream, "seq": seq + sent, "batch": batch.index,
                      "review": batch.review, "kind": HYPOTHETICAL_REPAIR, "items": 1, "firstItems": len(labels),
                      "bodyBytes": len(body), "bodySha256": hashlib.sha256(body).hexdigest(), **outcome}
            self._record(record, examples, detail)
            sent += 1
        return sent

    def _record(self, record: dict[str, Any], examples: dict[str, Any] | None, detail: str | None = None) -> None:
        with self.lock:
            self.records.append(record)
            if record.get("failure") in ("http-429", "http-503"):
                self.saturation_responses += 1
            if examples and len(self.invalid_examples) < 3:
                kind = {"kind": record["kind"]} if record.get("kind") else {}
                self.invalid_examples.append({"batch": record["batch"], **kind, **examples})
            if detail is not None:
                kept = self.failure_examples.setdefault(record["failure"], [])
                if len(kept) < 3:
                    kept.append({"batch": record["batch"], "status": record.get("status"), "detail": detail})
        if record["outcome"] == "abandoned" or record.get("failure") == "timeout":
            self.probe.abandoned(self.t0 + record["end"], C.max_tokens(record["items"], self.settings.tier),
                                 record["bodyBytes"] + PROMPT_OVERHEAD)
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
            for key in ("running", "waiting", "kvCacheUsage", *COUNTERS):
                sample[key] = load.get(key)
        signals = []
        if recognized:
            server = (sample["running"] or 0) + (sample["waiting"] or 0)
            # Our own count is an upper bound of our requests on the server: a call counts from before it is sent
            # until its whole response is read, and the sample takes the peak since its mark.
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
            self.monitor.sample(t, sample["saturated"], signals, ours)   # the ramp's queue is no saturation
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
        source = self.source
        if source is None:
            corpus = cell_corpus(settings, self.streams, self.items)
            source = _BatchSource(corpus, self.items, journal=settings.journal, model=probe.model, prefill=self.streams)
        # A finite source (the full-text tier's prepared calls) ends the cell once its last call is handed out, or at
        # --cell-seconds, whichever comes first: no call is sent twice, so there is no tail past that point.
        finite = bool(getattr(source, "finite", False))
        pool = ConnectionPool(probe.endpoint, self.streams, settings.request_timeout)
        started_at = utc_now()
        self.t0 = time.monotonic()
        self.carried = probe.carried(settings.abandon_grace_seconds)
        self._sample("before")      # idle: our requests in flight are 0; the token and request counters start here
        self.t0 = time.monotonic()
        end_at = self.t0 + settings.cell_seconds
        sampler_stop = threading.Event()
        sampler = threading.Thread(target=self._sampler, args=(sampler_stop,), name=f"probe-{self.label}-sampler",
                                   daemon=True)
        workers = [threading.Thread(target=self._worker, args=(k, pool, source), name=f"probe-{self.label}-{k}",
                                    daemon=True) for k in range(self.streams)]
        sampler.start()
        for worker in workers:
            worker.start()
        abandoned_in_flight, interrupted = 0, False
        window_end, tail = settings.cell_seconds, {"seconds": 0.0, "complete": False, "edgeCalls": 0}
        cut: list[float] = []
        try:
            while not self.aborted.is_set() and time.monotonic() < end_at:
                if finite and source.exhausted_at is not None:
                    break
                self.aborted.wait(max(0.0, min(0.1 if finite else 1.0, end_at - time.monotonic())))
            exhausted = finite and source.exhausted_at is not None
            stopped = min(time.monotonic(), end_at, source.exhausted_at if exhausted else end_at)
            window_end = stopped - self.t0
            if not self.aborted.is_set() and not exhausted:
                # The tail: the streams keep sending until every call that overlaps the window's end has finished,
                # so those calls run under full load (a server emptying behind them would make the window read high).
                with self.lock:
                    edge = set(self._open_calls)
                tail["edgeCalls"] = len(edge)
                deadline = stopped + settings.drain_seconds
                while edge and not self.aborted.is_set() and time.monotonic() < deadline:
                    self.aborted.wait(0.02)
                    with self.lock:
                        edge &= set(self._open_calls)
                tail["complete"] = not edge and not self.aborted.is_set()
                tail["seconds"] = round(max(0.0, time.monotonic() - stopped), 3)
            # Sending stops here. The calls still in flight finish on an emptying server (or are cut off), so the
            # steady-state window ends no later than the earliest start among them.
            with self.lock:
                cut = list(self._open_calls.values())
            self.stop.set()
            # Calls still in flight finish (outside the window) unless an abort cuts them off.
            deadline = time.monotonic() + settings.drain_seconds
            while (not self.abandon_now.is_set() and time.monotonic() < deadline
                   and any(worker.is_alive() for worker in workers)):
                self.abandon_now.wait(max(0.0, min(0.05, deadline - time.monotonic())))
        except KeyboardInterrupt:
            interrupted = True          # the calls so far are kept; the probe reports and re-raises
            window_end = min(time.monotonic(), end_at) - self.t0
            with self.lock:
                cut = list(self._open_calls.values())
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
        summary = self.summarize(started_at=started_at, window_end=window_end, tail=tail, cut=cut,
                                 drained=drained_at - self.t0, abandoned_in_flight=abandoned_in_flight,
                                 pool=pool.stats(), unjoined=sum(worker.is_alive() for worker in workers))
        if finite:
            summary["timing"]["inputs"] = {"calls": source.total, "exhausted": source.exhausted_at is not None,
                                           "exhaustedAtSeconds": round(source.exhausted_at - self.t0, 3)
                                           if source.exhausted_at is not None else None}
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
    def summarize(self, *, started_at: str, window_end: float, tail: dict[str, Any], cut: Sequence[float],
                  drained: float, abandoned_in_flight: int, pool: dict[str, int], unjoined: int) -> dict[str, Any]:
        settings, every = self.settings, list(self.records)
        # The first calls give every statistic E0 compares (a repair call, with --repair, is reported apart); every
        # call counts for our concurrency, the server counters and the served model.
        records = [r for r in every if r.get("kind", "first") == "first"]
        abort = self.monitor.fired
        answered = [r for r in records if r["outcome"] == "answered"]
        failed = [r for r in records if r["outcome"] == "failed"]
        abandoned = [r for r in records if r["outcome"] == "abandoned"]
        # The steady-state window: it starts after the warm-up and once every call started during the ramp has
        # finished (those calls began on a server with fewer of our calls), and it ends at the cell's end, or earlier
        # at the earliest start of the calls still in flight when sending stopped (a stop rule fired, or the tail ran
        # out): calls finishing on an emptying server would read high if spread into the window.
        planned_end = window_end
        ramp_ends = [r["end"] for r in answered if r["start"] < settings.ramp_seconds]
        window_start = max(settings.warmup_seconds, max(ramp_ends, default=0.0))
        window_end = min([planned_end, *cut])
        measured = window_end - window_start if window_end > window_start else 0.0
        valid = lambda r: r.get("valid") or 0
        rate = time_weighted_rate(records, window_start, window_end, valid) if measured else None
        answered_items = sum(r["items"] for r in answered)
        missing = sum(r.get("missing") or 0 for r in answered)
        in_window = [r for r in answered if window_start <= r["start"] < window_end]
        latency_scope = "calls started inside the measured window" if in_window else "all answered calls"
        latencies = [r["seconds"] for r in (in_window or answered)]
        cached_calls = [r for r in answered if r.get("cachedTokens") is not None and r.get("promptTokens")]
        prompt = sum(r.get("promptTokens") or 0 for r in answered)
        completion = sum(r.get("completionTokens") or 0 for r in answered)
        concurrency = mean_concurrency(every, window_start, window_end) if measured else None
        answered_all = [r for r in every if r["outcome"] == "answered"]
        served_models = Counter(r.get("model") or "(none)" for r in answered_all)
        fingerprints = Counter(r.get("fingerprint") for r in answered_all if r.get("fingerprint"))
        required = C.instrument().manifest["requiredModel"]["served"]
        # A cell stopped by saturation or lost metrics stopped dispatching and let its calls finish: its window
        # measured the server, though without the tail.
        state = "completed" if abort is None else STOP_RULES.get(abort["rule"], "aborted")
        server = self._server_summary(every, [r for r in every if r["outcome"] == "failed"],
                                      [r for r in every if r["outcome"] == "abandoned"], window_end)
        cached_share, cached_source = None, None
        if cached_calls:
            cached_share = _round(sum(r["cachedTokens"] for r in cached_calls) / sum(r["promptTokens"] for r in cached_calls), 4)
            cached_source = "usage"
        elif (server.get("counters") or {}).get("prefixCacheHitShare") is not None:
            cached_share, cached_source = server["counters"]["prefixCacheHitShare"], "server-prefix-cache"
        summary = {
            "position": self.entry["position"], "cell": self.label, "streams": self.streams, "itemsPerCall": self.items,
            "role": self.entry.get("role") or cell_role(self.streams, self.items, settings),
            "state": state, "startedAt": started_at, "abort": abort,
            "timing": {"cellSeconds": settings.cell_seconds, "warmupSeconds": settings.warmup_seconds,
                       "rampSeconds": settings.ramp_seconds, "windowStartSeconds": round(window_start, 3),
                       "windowEndSeconds": round(window_end, 3), "windowLimitSeconds": round(planned_end, 3),
                       "truncatedSeconds": round(max(0.0, planned_end - window_end), 3),
                       "measuredSeconds": round(measured, 3),
                       "tailSeconds": tail["seconds"], "tailComplete": tail["complete"], "edgeCalls": tail["edgeCalls"],
                       "drainSeconds": round(max(0.0, drained - planned_end - tail["seconds"]), 3),
                       "wallSeconds": round(drained, 3)},
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
                       "cachedReportedCalls": len(cached_calls), "cachedShare": cached_share,
                       "cachedShareSource": cached_source,
                       "promptPerCall": _round(prompt / len(answered), 1) if answered else None,
                       "promptPerEntry": _round(prompt / answered_items, 1) if answered_items else None,
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
                       "withoutFingerprint": sum(1 for r in answered_all if not r.get("fingerprint")),
                       "requiredModel": required,
                       "matchesInstrument": bool(answered) and set(served_models) == {required}},
            "server": server,
            "client": {**pool, "rssMaxMB": _stats([s.get("clientRssMB") for s in self.samples], 1)["max"],
                       "rssEndMB": self.samples[-1].get("clientRssMB") if self.samples else None},
            "proxies": self._proxy_summary(),
            "internalErrors": list(self.errors) + ([f"{unjoined} stream threads did not stop"] if unjoined else []),
        }
        if settings.repair:
            repair = repair_summary(every)
            repair["entriesPerSecondWithRepair"] = (_round(time_weighted_rate(every, window_start, window_end, valid), 2)
                                                    if measured else None)
            summary["repair"] = repair
            summary["invalidOutputShareAfterRepair"] = repair["afterRepair"]["share"]
        if settings.tier == FULL_TEXT:
            full_text = full_text_summary(every, hypothetical=settings.hypothetical_repair)
            full_text["entriesPerSecond"] = summary["entries"]["perSecond"]
            if settings.hypothetical_repair:
                full_text["hypotheticalRepair"]["entriesPerSecondWithRepair"] = (
                    _round(time_weighted_rate(every, window_start, window_end, valid), 2) if measured else None)
            summary["fullText"] = full_text
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

    def _counters(self, records: list[dict[str, Any]], before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        """Token and request counters between the idle samples before and after the cell, against our own calls.

        Without other clients, the server's prompt and generation tokens equal what our answers carried plus what
        our unanswered calls that may have run could have used (their body bytes and ``max_tokens``), and its
        finished requests equal our answered calls plus those; earlier cells' abandoned calls are allowed for too.
        A refusal with a 4xx status gets no allowance. Preemptions make the token counters incomparable.
        """
        delta = lambda key: (after[key] - before[key]) if after.get(key) is not None and before.get(key) is not None else None
        answered = [r for r in records if r["outcome"] == "answered"]
        ran = [r for r in records if may_have_run(r)]
        carried = self.carried
        our_completion = sum(r.get("completionTokens") or 0 for r in answered)
        our_prompt = sum(r.get("promptTokens") or 0 for r in answered)
        generation_bound = sum(C.max_tokens(r["items"], self.settings.tier) for r in ran) + carried["tokens"]
        prompt_bound = sum(r["bodyBytes"] + PROMPT_OVERHEAD for r in ran) + carried["prompt"]
        request_bound = len(ran) + carried["calls"]
        margin = COUNTER_MARGIN[0] + COUNTER_MARGIN[1] * (len(answered) + request_bound)
        generation, prompt, successes = delta("generationTokens"), delta("promptTokens"), delta("requestSuccess")
        preemptions = delta("preemptions")
        tokens_comparable = not preemptions
        generation_excess = generation - our_completion if generation is not None else None
        prompt_excess = prompt - our_prompt if prompt is not None else None
        request_excess = successes - len(answered) if successes is not None else None
        foreign = {
            "generation": bool(tokens_comparable and generation_excess is not None
                               and generation_excess > generation_bound + margin),
            "prompt": bool(tokens_comparable and prompt_excess is not None and prompt_excess > prompt_bound + margin),
            "requests": bool(request_excess is not None and request_excess > request_bound),
        }
        queries, hits = delta("prefixCacheQueries"), delta("prefixCacheHits")
        return {"generationTokens": generation, "promptTokens": prompt, "requestSuccess": successes,
                "preemptions": preemptions, "prefixCacheQueries": queries, "prefixCacheHits": hits,
                "prefixCacheHitShare": round(hits / queries, 4) if queries and hits is not None else None,
                "ourCompletionTokens": our_completion, "ourPromptTokens": our_prompt, "ourAnsweredCalls": len(answered),
                "unansweredMayHaveRun": len(ran), "carried": dict(carried),
                "generationExcess": generation_excess, "unansweredTokenBound": generation_bound,
                "promptExcess": prompt_excess, "promptBound": prompt_bound,
                "requestExcess": request_excess, "requestBound": request_bound,
                "carriedTokenBound": carried["tokens"], "margin": margin,
                "comparable": generation is not None and tokens_comparable,
                "tokensComparable": tokens_comparable,
                "coverage": round(generation / our_completion, 3) if generation is not None and our_completion
                and tokens_comparable else None,
                "foreignBy": sorted(k for k, v in foreign.items() if v), "foreign": any(foreign.values())}

    def _server_summary(self, records: list[dict[str, Any]], failed: list, abandoned: list,
                        window_end: float) -> dict[str, Any]:
        """Server load over the cell, and whether it shows other clients (``shared``).

        ``shared`` is True on evidence of other clients: at least ``shared_min_samples`` samples with more requests
        on the server than ours (beyond the tolerance), or more generated or prompt tokens, or more finished
        requests, between the idle samples before and after the cell than our calls account for (see
        ``_counters``). It is False only when there is no such evidence and the metrics demonstrably cover our own
        calls (in-flight load and generated tokens both at least ``COVERAGE_MIN`` of ours): metrics of one replica
        behind a balancer, or a proxy that queues our calls, cannot show that a server is not shared. Otherwise it
        is None.
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
        counters = self._counters(records, before, after) if before and after else None
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
                 environ: dict[str, str] | None = None, fetch_xml: Callable[[str], bytes | None] | None = None):
        self.settings = settings
        self.progress = progress
        # The full-text tier's Europe PMC fetcher: the census's own (study_cli.europe_pmc_fetcher) unless a test gives one.
        self._fetch_xml = fetch_xml
        self._environ = os.environ if environ is None else environ
        self._key: str | None = None
        self.endpoint: Endpoint | None = None
        self.metrics: MetricsClient | None = None
        self.model = settings.requested_model()
        self.chat_path = ""
        self.headers: dict[str, str] = {}
        self._abandoned: list[tuple[float, int, int]] = []
        self._abandoned_lock = threading.Lock()

    def abandoned(self, at: float, max_tokens: int, prompt_tokens: int = 0) -> None:
        """Record a call that was abandoned or timed out (monotonic seconds): the server may still be running it,
        so later samples allow for it and later counters for up to its ``max_tokens`` and prompt bound."""
        with self._abandoned_lock:
            self._abandoned.append((at, max_tokens, prompt_tokens))

    def _recent(self, grace: float) -> list[tuple[float, int, int]]:
        horizon = time.monotonic() - grace
        with self._abandoned_lock:
            self._abandoned = [entry for entry in self._abandoned if entry[0] >= horizon]
            return list(self._abandoned)

    def abandoned_recently(self, grace: float) -> int:
        return len(self._recent(grace))

    def abandoned_tokens(self, grace: float) -> int:
        return sum(entry[1] for entry in self._recent(grace))

    def carried(self, grace: float) -> dict[str, int]:
        recent = self._recent(grace)
        return {"calls": len(recent), "tokens": sum(entry[1] for entry in recent),
                "prompt": sum(entry[2] for entry in recent)}

    # -------------------------------------------------------------- secrets
    def scrub(self, text: str) -> str:
        """Remove the key from any text the probe writes or prints (a server could echo it)."""
        if not self._key:
            return text
        for form in {self._key, json.dumps(self._key)[1:-1], json.dumps(self._key, ensure_ascii=False)[1:-1]}:
            if form:
                text = text.replace(form, "[redacted]")
        return text

    def scrub_server(self, text: str) -> str:
        """A string the server supplied, without the key or any run of ``KEY_FRAGMENT`` of its characters."""
        return redact_key(text, self._key)

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
        if settings.tier == FULL_TEXT:
            return self._plan_full_text()
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
                            "promptTokensPerEntryEstimate": round(sum(tokens) / sum(counts), 1),
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
                "expectation": settings.expectation(),
                "endpoint": {"baseUrl": self._safe_url(settings.base_url, "base URL"),
                             "metricsUrl": self._safe_url(settings.metrics_url, "metrics URL"), "contacted": False},
                "grid": self._grid_record(grid), "requestSizes": [sizes[k] for k in sorted(sizes)], "load": load,
                "abortRules": self._abort_record(), "decision": self._decision_record(),
                "estimatedSeconds": {"min": round(minimum),
                                     "max": round(minimum + len(runnable) * 2 * settings.drain_seconds)}}

    def _plan_full_text(self) -> dict[str, Any]:
        """The full-text cell, its inputs and request sizes, without any network: the store is read and the first
        reviews are prepared offline, with only the full texts the store or the cache directory already hold."""
        settings = self.settings
        directory = Path(settings.full_text_cache) if settings.full_text_cache else None
        cache = FullTextCache(directory if directory is not None and directory.is_dir() else None, None)
        inputs = FullTextInputs(settings, model=self.model, revision=_code_revision())
        record = inputs.plan(cache)
        grid = ordered_grid(settings)
        system = C.system_prompt(settings.journal)
        bodies = [len(body) for _, body, _ in inputs.calls]
        users = [len(json.loads(body)["messages"][1]["content"]) for _, body, _ in inputs.calls]
        size = C.items_per_call(FULL_TEXT)
        budget = len(system) + size * (C.window_characters() + 1200)
        tokens = [(len(system) + u) / TOKEN_CHARACTERS for u in users]
        entry = grid[0]
        # At least target / 5 calls: five entries per call, each review's last call carrying its remainder.
        calls_for_target = math.ceil(settings.full_text_entries / size)
        return {"format": FORMAT, "dryRun": True, "tier": FULL_TEXT, "instrument": self._instrument_record(),
                "request": self._request_record(),
                "endpoint": {"baseUrl": self._safe_url(settings.base_url, "base URL"),
                             "metricsUrl": self._safe_url(settings.metrics_url, "metrics URL"), "contacted": False},
                "fullTextInputs": record, "census": census_full_text_repair(),
                "grid": self._grid_record(grid),
                "requestSizes": {"sampleCalls": len(bodies),
                                 "bodyBytes": {"p50": percentile(bodies, 0.5), "max": max(bodies)} if bodies else None,
                                 "promptTokensEstimate": {"p50": round(percentile(tokens, 0.5)),
                                                          "max": round(max(tokens))} if tokens else None,
                                 "promptTokensUpperBound": round(budget / TOKEN_CHARACTERS),
                                 "maxTokens": {str(n): C.max_tokens(n, FULL_TEXT) for n in range(1, size + 1)}},
                "load": {**entry, "callsForTargetAtLeast": calls_for_target,
                         "promptTokensInFlightUpperBound": entry["streams"] * round(budget / TOKEN_CHARACTERS)},
                "abortRules": self._abort_record(), "decision": self._decision_record(),
                "estimatedSeconds": {"cellAtMost": round(settings.cell_seconds + settings.drain_seconds),
                                     "fetchAtLeast": record["estimate"]["fetchSecondsAtLeast"]}}

    def _safe_url(self, url: str | None, what: str) -> str | None:
        return Endpoint.parse(url, what).url() if url else None

    def _instrument_record(self) -> dict[str, Any]:
        instrument, tier = C.instrument(), self.settings.tier
        return {"id": instrument.id, "digest": instrument.digest, "tier": tier, "itemsPerCall": instrument_items(tier),
                "requiredModel": instrument.manifest["requiredModel"]["served"],
                "validator": instrument.manifest["validator"]["id"],
                "systemPromptSha256": hashlib.sha256(C.system_prompt(self.settings.journal).encode("utf-8")).hexdigest()}

    def _request_record(self) -> dict[str, Any]:
        tier = self.settings.tier
        request = C.settings(tier)["request"]
        rule = request["maxOutputTokens"]
        sizes = (range(1, C.items_per_call(FULL_TEXT) + 1) if tier == FULL_TEXT
                 else sorted({c[1] for c in self.settings.grid_cells()}))
        record = {"model": self.model, "journal": self.settings.journal, "temperature": float(request["temperature"]),
                  "reasoningEffort": request.get("reasoningEffort"), "stream": False,
                  "maxTokensRule": f"max({rule['floor']}, {rule['perItem']} * items)",
                  "maxTokens": {str(n): C.max_tokens(n, tier) for n in sizes},
                  "requestTimeoutSeconds": self.settings.request_timeout,
                  "instrumentTimeoutSeconds": request.get("timeoutSeconds"), "repairPass": self.settings.repair}
        if tier == FULL_TEXT:
            record.update({"builder": "census_runner.full_text_call (citation_census.build_full_text_messages)",
                           "windowCharacters": C.window_characters(), "windowField": "FULL TEXT",
                           "hypotheticalRepair": self.settings.hypothetical_repair})
        else:
            record["abstractCut"] = C.abstract_cut()
        return record

    def _synthetic_record(self) -> dict[str, Any] | None:
        if self.settings.tier == FULL_TEXT:
            return None             # real loci from the store (fullTextInputs), no synthetic entries
        return {"seed": self.settings.seed, "scope": "one call sequence per cell (seed and cell label)",
                "profile": self.settings.synthetic_profile(),
                "profileSource": self.settings.profile_source or {"source": "built-in"},
                "source": "generated from a fixed vocabulary; no corpus data or entitled text"}

    def _grid_record(self, grid: list[dict[str, Any]]) -> dict[str, Any]:
        settings = self.settings
        return {"order": settings.order, "cells": grid, "maxStreams": settings.max_streams,
                "allowedStreams": settings.allowed_streams, "runAboveAllowed": settings.run_above_allowed,
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
                "saturationClock": "samples after the warm-up; a sample without metrics neither extends nor breaks it",
                "metricsUnavailable": "metrics unreadable for longer than the saturation clock while our calls are in flight",
                "scope": "every rule stops the cell and the grid; transport failures cut off the calls in flight, "
                         "saturation and unreadable metrics let them finish and keep the cell's measurement"}

    def _decision_record(self) -> dict[str, Any]:
        settings = self.settings
        if settings.tier == FULL_TEXT:
            streams, items = settings.grid_cells()[0]
            return {"cell": label(streams, items), "gate": "fullTextInvalidOutputsAtMostOnePercent",
                    "limit": 0.01, "itemsPerCall": items,
                    "requires": ["answered by the instrument's required model",
                                 "the share of entries without a parseable verdict over the entries of answered calls, "
                                 "as the census gate counts them (no repair pass at this tier)"],
                    "throughput": "reported only: a census's full-text calls arrive at the pace of its Europe PMC "
                                  "retrieval"}
        return {"cell": label(settings.allowed_streams, instrument_items()), "targetEntriesPerSecond": settings.target_rate,
                "allowedStreams": settings.allowed_streams, "itemsPerCall": instrument_items(),
                "requires": ["answered by the instrument's required model",
                             f"measured for at least {DECISION_CYCLES} p95 call latencies or the planned window",
                             "a cell stopped early decides with the lower of its time-weighted and completed-call rates"],
                "aboveAllowed": "cells above the allowed concurrency run only while the server can be shown unshared"
                                + (" (overridden: --run-above-allowed)" if settings.run_above_allowed else "")}

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
                record["models"] = [self.scrub_server(m) for m in listed if isinstance(m, str)][:20]
                record["requestedModelListed"] = self.model in record["models"]
            except (ValueError, AttributeError, TypeError):
                pass
        return record

    def _check_metrics(self) -> None:
        """The metrics URL must answer with vLLM's request gauges before any load is sent: saturation and the
        shared-server checks depend on them."""
        load = self.metrics.fetch()
        if not load.get("ok"):
            raise ValidationError(f"the metrics URL cannot be read ({load.get('error')}); fix --metrics-url or leave it out")
        if not load.get("recognized"):
            raise ValidationError("the metrics URL shows no vLLM request gauge (vllm:num_requests_running); give the "
                                  "vLLM server's own /metrics or leave --metrics-url out")

    def _baseline(self) -> dict[str, Any]:
        """Server load before the first cell, while none of our calls exist.

        Other clients show as requests on the server (beyond the tolerance, in ``shared_min_samples`` samples) or as
        token or request counters that advance between the samples, which also catches short requests that start
        and finish between two samples.
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
                                               "promptTokens", "generationTokens", "requestSuccess", "error")}
            if load.get("ok") and load.get("recognized"):
                sample["excess"] = (load.get("running") or 0) + (load.get("waiting") or 0)
                sample["foreign"] = sample["excess"] > settings.shared_tolerance
            samples.append(sample)
        known = [s for s in samples if s.get("recognized")]
        foreign = sum(1 for s in known if s.get("foreign"))
        advanced = {}
        for key in ("promptTokens", "generationTokens", "requestSuccess"):
            values = [s[key] for s in known if s.get(key) is not None]
            advanced[key] = values[-1] - values[0] if len(values) > 1 else None
        active = any(value for value in advanced.values())
        return {"metrics": "recognized" if known else "unavailable", "samples": samples,
                "foreignSamples": foreign, "countersAdvanced": advanced,
                "foreign": (active or foreign >= min(settings.shared_min_samples, len(known))) if known else None}

    def _check_on_premises(self) -> dict[str, Any] | None:
        """``--on-premises --policy`` (full-text tier): the judge endpoint must be an on-prem endpoint of the operator's
        egress policy, exactly as ``study full-text --on-premises --policy`` requires before entitled text is sent."""
        settings = self.settings
        if not settings.on_premises:
            return None
        from .claim_check import ConfidentialPolicy
        from .errors import ConfidentialityError
        policy = ConfidentialPolicy.load(Path(settings.policy or ""))
        endpoint = (settings.base_url or "").rstrip("/") + "/chat/completions"
        if not policy.endpoint_on_prem(endpoint):
            raise ConfidentialityError(f"{Endpoint.parse(settings.base_url or '', 'base URL').url()}/chat/completions is "
                                       "not an on-prem endpoint of the policy")
        return {"policySha256": policy.digest, "endpointOnPremises": True}

    def run(self, output: Path) -> dict[str, Any]:
        settings = self.settings
        settings.validate(live=True)
        on_premises = self._check_on_premises()
        self._prepare_transport()
        output = Path(output)
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValidationError(f"the output directory must be new or empty: {output}")
        grid = ordered_grid(settings)
        C.instrument()
        full_text = settings.tier == FULL_TEXT
        revision = _code_revision()
        report: dict[str, Any] = {
            "format": FORMAT, "state": "running", "tier": settings.tier, "startedAt": utc_now(), "finishedAt": None,
            "code": {"revision": revision, "python": platform.python_version()},
            "client": {"host": socket.gethostname(), "platform": platform.platform(terse=True)},
            "instrument": self._instrument_record(), "request": self._request_record(),
            "endpoint": {"baseUrl": self.endpoint.url(), "chatPath": self.chat_path,
                         "metricsUrl": self._safe_url(settings.metrics_url, "metrics URL"),
                         "apiKeyEnv": settings.api_key_env, "keySent": bool(self._key),
                         "connection": "direct HTTP/1.1 keep-alive; proxies from the environment are not used"
                                       + ("; the full-text tier's Europe PMC fetches use the census's fetcher, which "
                                          "honours them" if full_text else "")},
            "synthetic": self._synthetic_record(), "expectation": settings.expectation(),
            "grid": self._grid_record(grid), "abortRules": self._abort_record(), "decision": self._decision_record(),
            "preflight": None, "baseline": None, "cells": [], "abort": None,
        }
        if full_text:
            report.update({"fullTextInputs": None, "census": census_full_text_repair(), "onPremises": on_premises})
        try:
            report["preflight"] = self._preflight()
            if self.metrics:
                self._check_metrics()
        except BaseException:
            if self.metrics:
                self.metrics.close()
            raise
        output.mkdir(parents=True, exist_ok=True)
        files = {"json": output / REPORT_NAME, "markdown": output / MARKDOWN_NAME, "calls": output / CALLS_NAME}
        report["files"] = {k: str(v) for k, v in files.items()}
        atomic_write(files["calls"], "")
        source = None
        try:
            if full_text:
                # The inputs first (minutes of Europe PMC fetches the first time), then the idle baseline, then the cell.
                self._write(report, files, final=False)
                from .study_cli import europe_pmc_fetcher
                directory = Path(settings.full_text_cache) if settings.full_text_cache else output / FULL_TEXT_CACHE_NAME
                cache = FullTextCache(directory, self._fetch_xml or europe_pmc_fetcher())
                inputs = FullTextInputs(settings, model=self.model, revision=revision, emit=self.emit)
                report["fullTextInputs"] = inputs.prepare(output / FULL_TEXT_INPUTS_NAME, cache)
                source = _FullTextSource(inputs.calls)
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
                blocked = above_allowed_reason(report, settings) if entry["aboveAllowed"] else None
                if blocked:
                    report["cells"].append({**self._skipped(entry), "reason": blocked})
                    self.emit({"event": "cell-skipped", "cell": entry["cell"], "reason": blocked})
                    continue
                self.emit({"event": "cell-start", "cell": entry["cell"], "position": entry["position"],
                           "of": len(runnable)})
                cell = _CellRun(self, entry, source=source)
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
            if isinstance(error, ValidationError):
                report["errorMessage"] = str(error)[:300]
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
                "itemsPerCall": entry["itemsPerCall"], "role": entry.get("role"), "state": "skipped"}

    def _write(self, report: dict[str, Any], files: dict[str, Path], *, final: bool) -> None:
        if final:
            report["finishedAt"] = utc_now()
        report["server"] = server_verdict(report)
        report["exitCriterion"] = exit_criterion(report, self.settings)
        report["allowedConcurrency"] = allowed_concurrency(report, self.settings)
        text = self.scrub(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)) + "\n"
        atomic_write(files["json"], text.encode("utf-8"))
        # The markdown is rendered from the scrubbed report, so escaping for the table cannot hide a key from the scrub.
        atomic_write(files["markdown"], self.scrub(render_markdown(json.loads(text))).encode("utf-8"))


def _code_revision() -> str:
    try:
        from .census_runner import code_revision
        return code_revision()
    except Exception:  # noqa: BLE001 - the revision is informative only
        from . import __version__
        return f"ocsl-review {__version__}"


# ----------------------------------------------------------------------------- verdicts
def server_verdict(report: dict[str, Any]) -> dict[str, Any]:
    """Shared server: True on any evidence of other clients; False only when the baseline and every cell that ran
    were measured without such evidence by metrics that cover our own calls; None otherwise."""
    baseline = report.get("baseline") or {}
    ran = [c for c in report.get("cells", []) if c.get("state") != "skipped"]
    evidence, gaps = [], []
    if baseline.get("foreign"):
        evidence.append("baseline: other clients' requests, tokens or finished requests on the server before the first cell")
    elif baseline.get("metrics") != "recognized":
        gaps.append("no server load before the first cell")
    for cell in ran:
        server = cell.get("server") or {}
        if server.get("foreignSamples"):
            evidence.append(f"{cell['cell']}: {server['foreignSamples']} samples with more requests than ours "
                            f"(max excess {server.get('maxExcess')})")
        counters = server.get("counters") or {}
        by = set(counters.get("foreignBy") or ())
        if "generation" in by:
            evidence.append(f"{cell['cell']}: the server generated {counters.get('generationExcess')} more tokens than "
                            f"our answers carried (allowance {counters.get('unansweredTokenBound', 0)} for unanswered "
                            f"calls and a margin of {counters.get('margin')})")
        if "prompt" in by:
            evidence.append(f"{cell['cell']}: the server read {counters.get('promptExcess')} more prompt tokens than our "
                            f"answers carried (allowance {counters.get('promptBound', 0)} for unanswered calls and a "
                            f"margin of {counters.get('margin')})")
        if "requests" in by:
            evidence.append(f"{cell['cell']}: the server finished {counters.get('requestExcess')} more requests than we "
                            f"had answered (allowance {counters.get('requestBound', 0)} for unanswered calls)")
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


def above_allowed_reason(report: dict[str, Any], settings: ProbeSettings) -> str | None:
    """Why a cell above the allowed concurrency must not run now (None when it may).

    Such a cell can only matter when the server is shown not to be shared; otherwise the census concurrency stays at
    the allowed streams, and the cell would only load other users of a shared server.
    """
    if settings.run_above_allowed:
        return None
    override = " (--run-above-allowed runs it anyway)"
    if not (report.get("endpoint") or {}).get("metricsUrl"):
        return ("no --metrics-url, so the report cannot show the server unshared and a cell above the allowed "
                f"{settings.allowed_streams} streams cannot raise the census concurrency" + override)
    verdict = server_verdict(report)
    if verdict["shared"] is True:
        return (f"the server showed other clients ({verdict['evidence'][0] if verdict['evidence'] else 'shared'}); a "
                f"cell above the allowed {settings.allowed_streams} streams would load other users" + override)
    baseline = report.get("baseline") or {}
    ran = [c for c in report.get("cells", []) if c.get("state") != "skipped"]
    if baseline.get("metrics") != "recognized" or any((c.get("server") or {}).get("shared") is None for c in ran):
        return ("the server could not be shown unshared so far (" + "; ".join(verdict["gaps"][:2] or ["metrics gaps"])
                + f"), so a cell above the allowed {settings.allowed_streams} streams cannot raise the census "
                  "concurrency" + override)
    return None


def assess_cell(cell: dict[str, Any], settings: ProbeSettings) -> dict[str, Any]:
    """Whether a cell's measurement can decide, and the rate it decides with.

    It must have measured the server (completed, or stopped by saturation or lost metrics with its calls finished),
    be answered by the instrument's required model only (the census pauses on any other model and accepts no label),
    and have a steady-state window of at least ``DECISION_CYCLES`` p95 call latencies, or its whole planned window
    when that is shorter (a cell that stopped sending at once has its window end at the earliest start of the calls
    it cut, about one call latency earlier). It decides with its time-weighted rate over that window. When tokens per
    entry are predeclared and the cell's output tokens per entry depart from them by more than the tolerance, the
    rate is also normalised to the expected output tokens (throughput read as output-token bound) and the lower one
    decides.
    """
    state = cell.get("state")
    out: dict[str, Any] = {"cell": cell.get("cell"), "streams": cell.get("streams"), "itemsPerCall": cell.get("itemsPerCall"),
                           "role": cell.get("role"), "state": state, "eligible": False, "reason": None, "rate": None}
    if state not in MEASURED_STATES:
        reasons = {"skipped": f"skipped ({cell.get('reason')})", "aborted": "stopped by the "
                   f"{(cell.get('abort') or {}).get('rule', 'transport-failures')} rule with its calls cut off",
                   "interrupted": "interrupted"}
        out["reason"] = reasons.get(state, f"state {state}")
        return out
    entries, timing = cell.get("entries") or {}, cell.get("timing") or {}
    latency, tokens, served = cell.get("latencySeconds") or {}, cell.get("tokens") or {}, cell.get("served") or {}
    out.update({"entriesPerSecond": entries.get("perSecond"), "entriesPerSecondCompleted": entries.get("perSecondCompleted"),
                "measuredSeconds": timing.get("measuredSeconds"), "p95Seconds": latency.get("p95"),
                "invalidOutputShare": cell.get("invalidOutputShare"), "transportFailureShare": cell.get("transportFailureShare"),
                "invalidOutputShareAfterRepair": cell.get("invalidOutputShareAfterRepair"),
                "promptTokensPerEntry": tokens.get("promptPerEntry"), "outputTokensPerEntry": tokens.get("completionPerEntry"),
                "servedModels": sorted(served.get("models") or {})})
    required_model = served.get("requiredModel") or C.instrument().manifest["requiredModel"]["served"]
    if not served.get("matchesInstrument"):
        models = ", ".join(f"`{m}`" for m in out["servedModels"]) or "no model"
        out["reason"] = (f"the server answered as {models}, not the instrument's required model `{required_model}`: the "
                         "census would pause on model drift and accept no label")
        return out
    cell_seconds, warmup = timing.get("cellSeconds"), timing.get("warmupSeconds")
    planned = (settings.cell_seconds if cell_seconds is None else cell_seconds) - (
        settings.warmup_seconds if warmup is None else warmup)
    p95 = latency.get("p95")
    required = planned if p95 is None else min(planned, DECISION_CYCLES * p95)
    measured = timing.get("measuredSeconds") or 0.0
    out["requiredSeconds"] = round(required, 3)
    if measured < required:
        indicative = (f"; indicative {entries['perSecond']:.1f} entries/s" if entries.get("perSecond") is not None else "")
        out["reason"] = (f"a steady-state window of {measured:g} s, less than the {required:g} s needed ({DECISION_CYCLES} "
                         f"p95 call latencies of {p95 if p95 is not None else 'n/a'} s, or the planned window){indicative}; "
                         f"rerun it alone for longer (--cells {cell.get('cell')} --cell-seconds 900"
                         + (" and a larger --saturation-seconds)" if state != "completed" else ")"))
        return out
    rate = entries.get("perSecond")
    if rate is None:
        out["reason"] = "no entries were measured"
        return out
    out["basis"] = "time-weighted rate over the steady-state window"
    expectation = settings.expectation()
    measured_output = tokens.get("completionPerEntry")
    if expectation and expectation.get("outputTokensPerEntry") and measured_output:
        departure = measured_output / expectation["outputTokensPerEntry"] - 1
        out["outputDeparture"] = round(departure, 4)
        if abs(departure) > expectation["tolerance"]:
            out["normalisedRate"] = round(rate * measured_output / expectation["outputTokensPerEntry"], 2)
            rate = min(rate, out["normalisedRate"])
    if expectation and expectation.get("promptTokensPerEntry") and tokens.get("promptPerEntry"):
        out["promptDeparture"] = round(tokens["promptPerEntry"] / expectation["promptTokensPerEntry"] - 1, 4)
    out.update({"eligible": True, "rate": round(rate, 2)})
    return out


def _percent(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f} %"


def _signed(value: float) -> str:
    return f"{value * 100:+.0f} %"


def _tokens_note(assessment: dict[str, Any], settings: ProbeSettings) -> str:
    expectation = settings.expectation() or {}
    prompt, output = assessment.get("promptTokensPerEntry"), assessment.get("outputTokensPerEntry")
    note = (f"tokens per entry: prompt {prompt if prompt is not None else 'n/a'}, output "
            f"{output if output is not None else 'n/a'}")
    expected = [f"{name} {expectation[key]:g} ({_signed(assessment[departure])})"
                for name, key, departure in (("prompt", "promptTokensPerEntry", "promptDeparture"),
                                             ("output", "outputTokensPerEntry", "outputDeparture"))
                if expectation.get(key) and assessment.get(departure) is not None]
    if expected:
        note += " against the expected " + ", ".join(expected)
    if assessment.get("normalisedRate") is not None:
        note += (f"; WARNING: output tokens per entry depart from the expectation by more than "
                 f"{expectation['tolerance'] * 100:g} %, token-normalised rate {assessment['normalisedRate']:.1f} entries/s")
    elif not expected:
        note += " (no expectation predeclared: --expect-output-tokens or --profile)"
    return note


def full_text_exit(report: dict[str, Any], settings: ProbeSettings) -> dict[str, Any]:
    """The full-text cell against the census gate ``fullTextInvalidOutputsAtMostOnePercent``.

    The share is the entries of answered calls without a parseable verdict over the entries of those calls, as the
    census counts them: citation-census@1 has no repair pass at this tier. Throughput is reported, never a criterion
    (a census's full-text calls arrive at the pace of its Europe PMC retrieval). The hypothetical single-item resend
    is reported beside the share and never changes the verdict.
    """
    head = ("Full-text tier (census gate fullTextInvalidOutputsAtMostOnePercent: at most 1 % of full-text entries "
            "without a parseable verdict; citation-census@1 has no repair pass at this tier):")
    base = {"tier": FULL_TEXT, "gate": "fullTextInvalidOutputsAtMostOnePercent", "limit": 0.01}
    ran = [c for c in report.get("cells", []) if c.get("state") != "skipped"]
    if not ran:
        return {**base, "passes": None, "line": f"{head} not assessed - no cell ran."}
    cell = ran[0]
    full_text = cell.get("fullText") or {}
    entries = full_text.get("entries") or {}
    if not entries.get("entries"):
        return {**base, "cell": cell.get("cell"), "passes": None,
                "line": f"{head} not assessed - {cell.get('cell')}: no answered call (state {cell.get('state')})."}
    served = cell.get("served") or {}
    models = ", ".join(f"`{m}`" for m in sorted(served.get("models") or {})) or "no model"
    if not served.get("matchesInstrument"):
        return {**base, "cell": cell.get("cell"), "passes": None,
                "line": (f"{head} not assessed - {cell.get('cell')}: the server answered as {models}, not the "
                         f"instrument's required model `{served.get('requiredModel')}`.")}
    share = entries.get("censusInvalidShare", entries.get("invalidShare"))
    passes = share is not None and share <= 0.01
    buckets = ", ".join(f"{name}: {_percent(row.get('invalidShare'), 2)}"
                        for name, row in (full_text.get("byItemsPerCall") or {}).items() if row.get("entries"))
    tokens = full_text.get("tokensPerEntry") or {}
    state = "" if cell.get("state") == "completed" else f" (cell {cell.get('state')})"
    refusals = (f"; with {entries['refusedEntries']} entries of {entries['refusedCalls']} calls the judge refused, "
                "which the census counts as missing" if entries.get("refusedCalls") else "")
    line = (f"{head} {'PASS' if passes else 'FAIL'} - {cell.get('cell')}{state}: invalid {_percent(share, 2)} of "
            f"{entries['entries']} entries in {entries['calls']} answered calls (missing {_percent(entries.get('missingShare'), 2)}, "
            f"malformed {_percent(entries.get('malformedShare'), 2)}; by items per call {buckets}{refusals}); transport failures "
            f"{_percent(cell.get('transportFailureShare'), 2)}; {_num(full_text.get('entriesPerSecond'), 2)} entries/s; "
            f"tokens per entry: prompt {_num(tokens.get('prompt'), 1)}, output {_num(tokens.get('output'), 1)}; served "
            f"model {models}")
    hypothetical = full_text.get("hypotheticalRepair")
    result = {**base, "cell": cell.get("cell"), "passes": passes, "invalidShare": share,
              "missingShare": entries.get("missingShare"), "malformedShare": entries.get("malformedShare")}
    if hypothetical:
        after = hypothetical.get("afterRepair") or {}
        line += (f"; hypothetical single-item resend (not part of citation-census@1): invalid "
                 f"{_percent(after.get('share'), 2)} after it ({after.get('open')} open), "
                 f"{_num(hypothetical.get('entriesPerSecondWithRepair'), 2)} entries/s with it")
        result["hypotheticalShareAfter"] = after.get("share")
    result["line"] = line + ("." if passes else "; the author decides in D1.")
    return result


def exit_criterion(report: dict[str, Any], settings: ProbeSettings) -> dict[str, Any]:
    """The instrument's geometry at the allowed concurrency, and whether it reaches the target rate.

    Only that cell decides (the census sends nothing else); the other geometries at or below the allowed streams are
    reported as exploratory and never pass the criterion. The full-text tier is assessed against its census gate
    instead (``full_text_exit``).
    """
    if settings.tier == FULL_TEXT:
        return full_text_exit(report, settings)
    target, allowed, items = settings.target_rate, settings.allowed_streams, instrument_items()
    name = label(allowed, items)
    head = (f"Exit criterion (≥ {target:g} entries/s with the instrument's {items} items per call at the allowed "
            f"concurrency of {allowed} streams):")
    cells = report.get("cells", [])
    decisive = next((c for c in cells if c.get("streams") == allowed and c.get("itemsPerCall") == items), None)
    exploratory = [assess_cell(c, settings) for c in cells
                   if c.get("streams", 0) <= allowed and c.get("itemsPerCall") != items]
    best_exploratory = max((a for a in exploratory if a["eligible"]), key=lambda a: a["rate"], default=None)
    side = (f" Exploratory geometries at ≤ {allowed} streams (not the instrument's, never deciding): best "
            f"{best_exploratory['cell']} at {best_exploratory['rate']:.1f} entries/s." if best_exploratory else "")
    base = {"targetEntriesPerSecond": target, "allowedStreams": allowed, "itemsPerCall": items, "cell": name,
            "exploratoryBest": best_exploratory}
    if decisive is None:
        return {**base, "assessment": None, "passes": None,
                "line": f"{head} not assessed - the grid has no {name} cell.{side}"}
    assessment = assess_cell(decisive, settings)
    if not assessment["eligible"]:
        return {**base, "assessment": assessment, "passes": None,
                "line": f"{head} not assessed - {name}: {assessment['reason']}.{side}"}
    passes = assessment["rate"] >= target
    # The census's 1 % limit applies after its one repair pass: with --repair that share decides, otherwise the first
    # calls' share stands in for it.
    repaired = decisive.get("repair") is not None
    invalid = assessment.get("invalidOutputShareAfterRepair") if repaired else assessment["invalidOutputShare"]
    quality = (invalid is not None and invalid <= 0.01 and (assessment["transportFailureShare"] or 0) <= 0.005)
    invalid_text = (f"{_percent(assessment['invalidOutputShare'], 2)} before and "
                    f"{_percent(assessment.get('invalidOutputShareAfterRepair'), 2)} after the repair pass"
                    if repaired else _percent(assessment["invalidOutputShare"], 2))
    timing = decisive.get("timing") or {}
    window = f" over a {assessment['measuredSeconds']:g} s steady-state window"
    stopped = window
    if decisive.get("state") != "completed":
        stopped = (f"{window} (stopped by the {(decisive.get('abort') or {}).get('rule')} rule; the window ends at the "
                   f"earliest start of the calls the stop cut off; completed calls "
                   f"{_num(assessment['entriesPerSecondCompleted'], 1)} entries/s)")
    elif not timing.get("tailComplete", True):
        stopped = (f"{window} (the window's last calls outlasted the tail, so it ends at the earliest start of the calls "
                   f"still open; completed calls {_num(assessment['entriesPerSecondCompleted'], 1)} entries/s)")
    model = ", ".join(assessment["servedModels"])
    line = (f"{head} {'PASS' if passes else 'FAIL'} - {name}: {assessment['rate']:.1f} entries/s{stopped}; invalid outputs "
            f"{invalid_text}, transport failures "
            f"{_percent(assessment['transportFailureShare'], 2)} ({'within' if quality else 'outside'} the census limits "
            f"of 1 % and 0.5 %); {_tokens_note(assessment, settings)}; served model `{model}`"
            + ("." if passes else "; the author decides.") + side)
    return {**base, "assessment": assessment, "passes": passes, "withinQualityLimits": quality,
            "qualityBasis": "after the repair pass" if repaired else "first calls", "line": line}


def allowed_concurrency(report: dict[str, Any], settings: ProbeSettings) -> dict[str, Any]:
    """The allowed concurrency stays at ``allowed_streams`` unless the report shows the server is not shared."""
    shared = (report.get("server") or {}).get("shared")
    if settings.tier == FULL_TEXT:
        streams = settings.grid_cells()[0][0]
        return {"streams": streams, "serverShared": shared, "mayExceed": False, "bestAbove": None,
                "exploratoryAbove": None,
                "line": (f"Concurrency: {streams} streams for the full-text tier, a probe setting and no census one: a "
                         "census's full-text calls arrive at the pace of its Europe PMC retrieval (study full-text "
                         "--fetch-workers, 0.4 s between a thread's requests), so the judge holds far fewer of them than "
                         f"the abstract tier's {settings.allowed_streams} streams.")}
    allowed, items = settings.allowed_streams, instrument_items()
    above = [assess_cell(c, settings) for c in report.get("cells", []) if c.get("streams", 0) > allowed]
    eligible = [a for a in above if a["eligible"]]
    native = max((a for a in eligible if a["itemsPerCall"] == items), key=lambda a: a["rate"], default=None)
    other = max((a for a in eligible if a["itemsPerCall"] != items), key=lambda a: a["rate"], default=None)
    if shared is True:
        basis = "the server load showed other clients (shared server)"
    elif shared is False:
        basis = "no other clients in any sample or counter: the server is not shared, so the author may allow more streams"
    elif not settings.metrics_url:
        basis = "no metrics endpoint (--metrics-url), so the report cannot show that the server is not shared"
    else:
        basis = ("the server load was not measured before and in every cell by metrics that account for our own "
                 "calls, so the report cannot show that the server is not shared")
    line = f"Allowed concurrency: {allowed} streams - {basis}."
    if shared is False and native:
        line += (f" Best cell above {allowed} streams with the instrument's {items} items per call: {native['cell']}, "
                 f"{native['rate']:.1f} entries/s.")
    if shared is False and other:
        line += f" Exploratory geometries above {allowed} streams: best {other['cell']} at {other['rate']:.1f} entries/s."
    return {"streams": allowed, "serverShared": shared, "mayExceed": shared is False, "bestAbove": native,
            "exploratoryAbove": other, "line": line}


# ----------------------------------------------------------------------------- markdown
def _num(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _full_text_inputs_lines(inputs: dict[str, Any] | None) -> list[str]:
    """The full-text tier's inputs: store, reviews, entries, full texts and calls (counts only)."""
    if not inputs:
        return ["- Inputs: not prepared"]
    reviews, entries, texts, calls = inputs["reviews"], inputs["entries"], inputs["fullTexts"], inputs["calls"]
    store = inputs.get("store") or {}
    sources = ", ".join(f"{k} {v}" for k, v in (entries.get("bySource") or {}).items()) or "none"
    fetched = ", ".join(f"{k} {texts[k]}" for k in ("fetched", "cacheHits", "absent", "cachedAbsent", "transient",
                                                     "notFetched") if texts.get(k))
    return [f"- Inputs: real loci from the store `{_cell(str(store.get('merkleRoot', ''))[:16])}` ({store.get('id')}), "
            f"journal label {_cell(inputs['journal'])}, review DOI prefixes {_cell(', '.join(inputs['reviewDoiPrefixes']))}; "
            f"{_cell(inputs['selection'])}",
            f"- Reviews: {reviews['withPrefix']} with the prefixes of {reviews['retainedInStore']} retained in the store; "
            f"{reviews['considered']} considered, {reviews['prepared']} prepared, {reviews['used']} used "
            f"(unavailable {reviews['unavailable']}, extraction failed {reviews['extractionFailed']}, refused "
            f"{reviews['refused']}, not prepared {reviews['notPrepared']})",
            f"- Entries: {entries['judgeable']} judgeable, {entries['candidates']} with a PMCID, {entries['tried']} tried, "
            f"{entries['retrieved']} retrieved (target {entries['target']}; without a full text {entries['withoutFullText']}, "
            f"left open by a transient failure {entries['transient']}); sources: {sources}",
            f"- Full texts: {fetched or 'nothing fetched'}; cache directory {_cell(texts.get('cacheDirectory') or '-')}; "
            f"{texts.get('fetchWorkers')} fetch threads",
            f"- Calls: {calls['count']} (mean {_num(calls['meanItems'], 2)} items; by items "
            f"{', '.join(f'{k}: {v}' for k, v in calls['byItems'].items())}); windows mean "
            f"{_num((inputs.get('windows') or {}).get('meanCharacters'), 0)} characters of at most "
            f"{(inputs.get('windows') or {}).get('budgetCharacters')}; prepared in {inputs.get('seconds')} s"]


def _full_text_markdown(report: dict[str, Any]) -> list[str]:
    """The full-text cell's entries by items per call, its calls, and the hypothetical single-item resend."""
    cells = [cell for cell in report.get("cells", []) if cell["state"] != "skipped" and cell.get("fullText")]
    if not cells:
        return []
    census = (report.get("census") or cells[0]["fullText"].get("census") or {})
    lines = ["", f"Full-text entries of answered calls ({census.get('statement', '')}):",
             "| Cell | Items/call | Calls | Entries | Valid | Missing (no line) | Malformed (rejected line) | Invalid |",
             "|---|---|---|---|---|---|---|---|"]
    for cell in cells:
        full_text = cell["fullText"]
        for name, row in [*full_text["byItemsPerCall"].items(), ("all", full_text["entries"])]:
            lines.append(f"| {cell['cell']} | {name} | {row['calls']} | {row['entries']} | {_percent(row['validShare'], 2)} | "
                         f"{_percent(row['missingShare'], 2)} | {_percent(row['malformedShare'], 2)} | "
                         f"{_percent(row['invalidShare'], 2)} |")
    for cell in cells:
        calls = cell["fullText"]["calls"]
        shares = calls["shares"]
        lines.append(f"- {cell['cell']} calls: {calls['answered']} answered; all valid {_percent(shares['allValid'])}, with a "
                     f"missing entry {_percent(shares['withMissing'])}, with a malformed entry {_percent(shares['withMalformed'])}; "
                     f"empty completions {calls['emptyCompletions']}, truncated {calls['truncatedCalls']}; calls by entries "
                     f"left invalid: {', '.join(f'{k}: {v}' for k, v in calls['byInvalidEntries'].items())}")
    if census.get("retry"):
        lines.append(f"- The census's retry: {census['retry']}")
    repaired = [cell for cell in cells if cell["fullText"].get("hypotheticalRepair")]
    if repaired:
        label_text = repaired[0]["fullText"]["hypotheticalRepair"]["label"]
        lines += ["", f"Hypothetical single-item resend - {label_text}. Decided entries; open: the resend got no answer "
                      "or was not sent, or the call itself got none:",
                  "| Cell | Items/call | Entries (answered calls) | Invalid before | Share before | Decided | Invalid after | "
                  "Share after | Open |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for cell in repaired:
            repair = cell["fullText"]["hypotheticalRepair"]
            before, after = repair["beforeRepair"], repair["afterRepair"]
            for bucket in [*before["byItemsPerCall"], "all"]:
                b = before if bucket == "all" else before["byItemsPerCall"][bucket]
                a = after if bucket == "all" else after["byItemsPerCall"][bucket]
                lines.append(f"| {cell['cell']} | {bucket} | {b['entries']} | {b['invalid']} | {_percent(b['share'], 2)} | "
                             f"{a['decided']} | {a['invalid']} | {_percent(a['share'], 2)} | {a['open']} |")
        for cell in repaired:
            repair = cell["fullText"]["hypotheticalRepair"]
            calls = repair["calls"]
            lines.append(f"- {cell['cell']} resend calls: {calls['sent']} sent for {repair['entriesNeedingResend']} entries "
                         f"({calls['answered']} answered, {calls['failed']} failed, {calls['abandoned']} cut off, "
                         f"{calls['notSent']} not sent), {calls['recovered']} recovered, {calls['stillInvalid']} still "
                         f"without a valid verdict; entries/s with the resend "
                         f"{_num(repair.get('entriesPerSecondWithRepair'), 2)}")
        lines.append(f"- The resend: {repaired[0]['fullText']['hypotheticalRepair']['resend']}")
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Capacity probe: {report['instrument']['id']} ({report['instrument']['tier']} tier)", ""]
    endpoint, request, grid = report["endpoint"], report["request"], report["grid"]
    full_text = report["instrument"]["tier"] == FULL_TEXT
    profile_source = (report.get("synthetic") or {}).get("profileSource") or {}
    if full_text:
        calls = ("the census's full-text calls (no repair pass at this tier)"
                 + ("; with the hypothetical single-item resend (--hypothetical-repair)"
                    if request.get("hypotheticalRepair") else ""))
    else:
        calls = ("first calls and the census's repair pass (--repair)" if request.get("repairPass")
                 else "first calls only (no repair pass)")
    lines += [f"- State: **{report['state']}**; started {report['startedAt']}, finished {report.get('finishedAt') or '-'}",
              f"- Endpoint: {endpoint['baseUrl']} (metrics: {endpoint.get('metricsUrl') or 'not configured'}); "
              f"requested model `{request['model']}`",
              f"- Requests: temperature {request['temperature']:g}, reasoning {request['reasoningEffort']}, "
              f"stream off, max_tokens = {request['maxTokensRule']}, " + calls]
    if full_text:
        lines += _full_text_inputs_lines(report.get("fullTextInputs"))
        lines.append(f"- Instrument digest `{report['instrument']['digest'][:16]}`; journal label in the system prompt "
                     f"{_cell(request['journal'])}")
        lines.append(f"- Cell: each prepared call sent once, for at most {grid['cellSeconds']:g} s with a "
                     f"{grid['warmupSeconds']:g} s warm-up and a drain of up to {grid['drainSeconds']:g} s")
    else:
        lines += [f"- Synthetic entries: seed {report['synthetic']['seed']}, profile "
                  f"{_cell(profile_source.get('path') or profile_source.get('source') or 'built-in')}, instrument digest "
                  f"`{report['instrument']['digest'][:16]}`",
                  f"- Grid: order {grid['order']}; each cell {grid['cellSeconds']:g} s with a {grid['warmupSeconds']:g} s "
                  f"warm-up, a tail until the window's last calls finish and a drain of up to {grid['drainSeconds']:g} s"]
    if report.get("errorMessage"):
        lines.append(f"- Error: {_cell(report['errorMessage'])}")
    lines.append("")
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
    lines += ["| Cell | Role | State | Calls answered | Items/call | Window s | Entries/s | Entries/s (completed) | "
              "Concurrency | p50 s | p95 s | p99 s | Invalid | Transport failures | Failure classes |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for cell in report.get("cells", []):
        role = cell.get("role") or "-"
        if cell["state"] == "skipped":
            lines.append(f"| {cell['cell']} | {role} | skipped: {_cell(cell.get('reason'))} |  |  |  |  |  |  |  |  |  |  |  |  |")
            continue
        failures = ", ".join(f"{k} {v}" for k, v in cell["failures"].items()) or "-"
        latency, timing = cell["latencySeconds"], cell.get("timing") or {}
        lines.append(
            f"| {cell['cell']} | {role} | {cell['state']} | {cell['calls']['answered']} | {_num(cell['calls']['meanItems'], 1)} | "
            f"{_num(timing.get('measuredSeconds'), 0)} | "
            f"{_num(cell['entries']['perSecond'], 2)} | {_num(cell['entries']['perSecondCompleted'], 2)} | "
            f"{_num(cell['entries']['meanConcurrency'], 1)} | {_num(latency['p50'], 2)} | {_num(latency['p95'], 2)} | "
            f"{_num(latency['p99'], 2)} | {_percent(cell['invalidOutputShare'], 2)} | "
            f"{_percent(cell['transportFailureShare'], 2)} | {failures} |")
    lines += ["", "| Cell | Prompt tokens/entry | Output tokens/entry | Cached share | Server running max (mean) | "
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
        cached = _percent(tokens.get("cachedShare")) + (" (server)" if tokens.get("cachedShareSource") ==
                                                        "server-prefix-cache" else "")
        lines.append(
            f"| {cell['cell']} | {_num(tokens.get('promptPerEntry'), 1)} | {_num(tokens['completionPerEntry'], 1)} | "
            f"{cached} | {_num(running.get('max'), 0)} ({_num(running.get('mean'), 1)}) | "
            f"{_num((srv.get('waiting') or {}).get('max'), 0)} | {_percent((srv.get('kvCacheUsage') or {}).get('max'))} | "
            f"{shared} | {_num(client.get('rssMaxMB'), 0)} | {client.get('connectionsOpened')} | "
            f"{_cell(models)} / {_cell(fingerprints)} |")
    repaired = [cell for cell in report.get("cells", []) if cell["state"] != "skipped" and cell.get("repair")]
    if repaired:
        lines += ["", "Invalid outputs before and after the repair pass, by the items of the batch's first call (decided "
                      "entries; open: the repair call got no answer or was not sent):",
                  "| Cell | Items/call | Entries (answered first calls) | Invalid before | Share before | Decided | "
                  "Invalid after | Share after | Open |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for cell in repaired:
            before, after = cell["repair"]["beforeRepair"], cell["repair"]["afterRepair"]
            for bucket in [*before["byItemsPerCall"], "all"]:
                b = before if bucket == "all" else before["byItemsPerCall"][bucket]
                a = after if bucket == "all" else after["byItemsPerCall"][bucket]
                lines.append(f"| {cell['cell']} | {bucket} | {b['entries']} | {b['invalid']} | {_percent(b['share'], 2)} | "
                             f"{a['decided']} | {a['invalid']} | {_percent(a['share'], 2)} | {a['open']} |")
        for cell in repaired:
            calls = cell["repair"]["calls"]
            lines.append(f"- {cell['cell']} repair calls: {calls['sent']} sent for {cell['repair']['batchesNeedingRepair']} "
                         f"batches ({calls['answered']} answered, {calls['failed']} failed, {calls['abandoned']} cut off, "
                         f"{calls['notSent']} not sent), {calls['items']} items, {calls['recovered']} recovered, "
                         f"{calls['stillWithoutLine']} still without a parseable line; entries/s with repair calls "
                         f"{_num(cell['repair'].get('entriesPerSecondWithRepair'), 2)}")
    lines += _full_text_markdown(report)
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


def render_full_text_plan(plan: dict[str, Any]) -> str:
    request, grid, inputs, sizes = plan["request"], plan["grid"], plan["fullTextInputs"], plan["requestSizes"]
    load, estimate = plan["load"], inputs["estimate"]
    sample = inputs["sample"]
    lines = ["Capacity probe plan, full-text tier (dry run: nothing is sent and nothing is fetched)",
             f"instrument {plan['instrument']['id']} {plan['instrument']['digest'][:16]}, full-text tier; model "
             f"{request['model']}; journal label {request['journal']}",
             f"requests: the census's full-text calls ({request['builder']}): temperature {request['temperature']:g}, "
             f"reasoning {request['reasoningEffort']}, stream off, max_tokens = {request['maxTokensRule']}, windows of at "
             f"most {request['windowCharacters']} characters in the {request['windowField']} field"
             + ("; hypothetical single-item resend of every entry left without a valid verdict"
                if request.get("hypotheticalRepair") else ""),
             f"census: {plan['census']['statement']}",
             f"endpoint: {plan['endpoint']['baseUrl'] or '(no --base-url)'} (not contacted)", ""]
    lines += [line[2:] for line in _full_text_inputs_lines(inputs)]
    lines += ["",
              f"sample: the first {sample['reviews']} reviews prepared offline, {_num(sample['candidatesPerReview'], 1)} "
              f"candidates per review; {sample['note']}",
              f"estimate: about {estimate['reviewsForTarget'] if estimate['reviewsForTarget'] is not None else 'n/a'} "
              f"reviews for the {inputs['entries']['target']} entries; fetching at least "
              f"{_duration(estimate['fetchSecondsAtLeast'])}; {estimate['note']}",
              f"request sizes of the sample's calls: body bytes p50 "
              f"{_num((sizes.get('bodyBytes') or {}).get('p50'), 0)}, max {(sizes.get('bodyBytes') or {}).get('max', 'n/a')}; "
              f"~prompt tokens p50 {(sizes.get('promptTokensEstimate') or {}).get('p50', 'n/a')}, max "
              f"{(sizes.get('promptTokensEstimate') or {}).get('max', 'n/a')} (upper bound "
              f"{sizes['promptTokensUpperBound']} at {TOKEN_CHARACTERS} characters per token)",
              f"cell {load['cell']}: {load['streams']} streams, at least {load['callsForTargetAtLeast']} calls (five "
              f"entries each, a review's last call its remainder), each sent once; at most "
              f"{_duration(grid['cellSeconds'])} plus a drain of up to {_duration(grid['drainSeconds'])}", ""]
    return "\n".join(lines)


def render_plan(plan: dict[str, Any]) -> str:
    if plan.get("tier") == FULL_TEXT:
        return render_full_text_plan(plan)
    request, grid, decision = plan["request"], plan["grid"], plan["decision"]
    source = (plan["synthetic"].get("profileSource") or {})
    expectation = plan.get("expectation") or {}
    lines = ["Capacity probe plan (dry run: nothing is sent)",
             f"instrument {plan['instrument']['id']} {plan['instrument']['digest'][:16]}, {plan['instrument']['tier']} tier; "
             f"model {request['model']}",
             f"requests: temperature {request['temperature']:g}, reasoning {request['reasoningEffort']}, stream off, "
             f"max_tokens = {request['maxTokensRule']}; "
             + ("first calls and the census's repair pass (--repair: one repair call per batch whose first call left "
                "labels without a parseable line, built by the census runner)" if request.get("repairPass")
                else "first calls only"),
             f"endpoint: {plan['endpoint']['baseUrl'] or '(no --base-url)'} (not contacted); metrics: "
             f"{plan['endpoint']['metricsUrl'] or 'not configured'}",
             f"synthetic entries: seed {plan['synthetic']['seed']}, profile {source.get('path') or source.get('source')}; "
             f"abstracts cut by the instrument at {request['abstractCut']} characters",
             "expected tokens per entry: " + (", ".join(f"{k.replace('TokensPerEntry', '')} {v:g}" for k, v in expectation.items()
                                                       if k.endswith("PerEntry")) or "none predeclared"),
             f"exit criterion: cell {decision['cell']} only (the instrument's {decision['itemsPerCall']} items per call at "
             f"the allowed {decision['allowedStreams']} streams), ≥ {decision['targetEntriesPerSecond']:g} entries/s; "
             f"{decision['aboveAllowed']}", "",
             f"Request sizes (first {plan['requestSizes'][0]['callsSampled'] if plan['requestSizes'] else 0} calls per "
             f"geometry; tokens estimated at {TOKEN_CHARACTERS} characters per token):",
             "| items/call | mean items | user chars p50 | user chars max | body bytes p50 | body bytes max | "
             "~prompt tokens p50 | ~prompt tokens p95 | ~prompt tokens/entry | max_tokens | cut abstracts |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for size in plan["requestSizes"]:
        lines.append(f"| {size['itemsPerCall']} | {size['meanItems']} | {size['userCharacters']['p50']:.0f} | "
                     f"{size['userCharacters']['max']} | {size['bodyBytes']['p50']:.0f} | {size['bodyBytes']['max']} | "
                     f"{size['promptTokensEstimate']['p50']} | {size['promptTokensEstimate']['p95']} | "
                     f"{size['promptTokensPerEntryEstimate']} | {size['maxTokens']} | {_percent(size['cutAbstractShare'])} |")
    lines += ["", f"Grid (order {grid['order']}; cell {grid['cellSeconds']:g} s incl. {grid['warmupSeconds']:g} s "
                  f"warm-up; tail and drain up to {grid['drainSeconds']:g} s each; pause {grid['pauseSeconds']:g} s):",
              "| # | cell | role | state | items in flight | ~prompt tokens in flight | ~KV ceiling tokens | "
              "latency budget s | entries for target |",
              "|---|---|---|---|---|---|---|---|---|"]
    for row in plan["load"]:
        if row["skip"]:
            lines.append(f"| {row['position']} | {row['cell']} | {row['role']} | skipped: {row['skip']} |  |  |  |  |  |")
            continue
        state = "runs if the server can be shown unshared" if row["aboveAllowed"] and not grid["runAboveAllowed"] else "runs"
        lines.append(f"| {row['position']} | {row['cell']} | {row['role']} | {state} | {row['itemsInFlight']} | "
                     f"{row['promptTokensInFlightEstimate']} | {row['kvCeilingTokensEstimate']} | "
                     f"{row['latencyBudgetSeconds']} | {row['entriesForTarget']} |")
    rules, seconds = plan["abortRules"], plan["estimatedSeconds"]
    lines += ["", "latency budget: the mean call latency at which a cell reaches the target rate (streams x items / target).",
              f"abort: transport failures above {_percent(rules['transportFailureShare'], 0)} of calls in any "
              f"{rules['windowSeconds']:g} s window (once it holds {rules['minCalls']} calls or {rules['minFailures']} "
              f"failures), saturation (requests waiting, preemptions, HTTP 429/503) in every sample for more than "
              f"{rules['saturationSeconds']:g} s after the warm-up, or metrics unreadable that long; each stops the cell "
              "and the grid.",
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
                    "entries, or (--tier full-text) the census's full-text calls over real loci from a "
                    "literature-evidence@2 projection. See docs/capacity-probe.md.")
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
                      help="load (default: the instrument's geometry within --allowed-streams first, then the other "
                           "cells within it, then those above it, each group by ascending streams x items), streams, "
                           "items, as-given or shuffle (seeded)")
    grid.add_argument("--max-streams", type=int, help="skip cells with more streams than this")
    grid.add_argument("--run-above-allowed", action="store_true",
                      help="run cells above --allowed-streams even when the server is shared or cannot be shown unshared")
    timing = probe.add_argument_group("timing")
    timing.add_argument("--cell-seconds", type=float, default=300.0, help="fixed duration of each cell (default 300)")
    timing.add_argument("--warmup-seconds", type=float, default=30.0,
                        help="earliest start of the measured window (default 30; it also waits for the ramp's calls)")
    timing.add_argument("--ramp-seconds", type=float, default=10.0, help="stagger of the stream starts (default 10)")
    timing.add_argument("--drain-seconds", type=float, default=180.0,
                        help="limit of the tail after the window and of the drain after it (default 180 each)")
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
                       help="stop when every sample after the warm-up shows saturation, or no metrics, for longer than "
                            "this (default 60)")
    rules.add_argument("--saturation-waiting", type=float, default=1.0,
                       help="requests waiting on the server that count as saturation (default 1)")
    rules.add_argument("--saturation-kv", type=float,
                       help="also count KV cache usage at or above this share as saturation (off by default)")
    rules.add_argument("--shared-tolerance", type=float, default=0.0,
                       help="requests beyond ours a sample tolerates before it counts as another client's (default 0)")
    rules.add_argument("--allowed-streams", type=int, default=60,
                       help="allowed concurrency the exit criterion is assessed at (default 60)")
    rules.add_argument("--target-rate", type=float, default=40.0, help="exit criterion in entries/s (default 40)")
    synthetic = probe.add_argument_group("synthetic entries and expectations")
    synthetic.add_argument("--profile", help="size profile and expected tokens per entry (capacity profile output)")
    synthetic.add_argument("--expect-output-tokens", type=float,
                           help="predeclared output tokens per entry of the census's real entries")
    synthetic.add_argument("--expect-prompt-tokens", type=float,
                           help="predeclared prompt tokens per entry of the census's real entries")
    synthetic.add_argument("--expect-tolerance", type=float, default=0.2,
                           help="departure from the expected tokens per entry that warns and normalises (default 0.2)")
    probe.add_argument("--seed", type=int, default=1, help="seed of the synthetic entries (default 1)")
    probe.add_argument("--journal", default=DEFAULT_JOURNAL, help="journal parameter of the system prompt")
    probe.add_argument("--repair", action="store_true",
                       help="also send the census's one repair call for every batch whose first call left labels without "
                            "a parseable line (or failed), built by the census runner, and report invalid outputs before "
                            "and after the repair pass, overall and by items per call (1, 2-9, 10-39, 40)")
    full_text = probe.add_argument_group("full-text tier")
    full_text.add_argument("--tier", choices=TIERS, default=TIER,
                           help="abstract (default: synthetic entries, the grid) or full-text (the census's full-text "
                                "calls over real loci from --store, one cell, default %dx%d)"
                                % (FULL_TEXT_STREAMS, C.items_per_call(FULL_TEXT)))
    full_text.add_argument("--store", help="literature-evidence@2 projection (absolute path), the byte cache the census "
                                           "reads: its retained reviews give the real loci; read through the census's "
                                           "guarded reader (no label or verdict can be read)")
    full_text.add_argument("--store-root", help="Merkle root the store must have (the pinned dataset version)")
    full_text.add_argument("--review-doi-prefix", action="append", default=[],
                           help="DOI prefix of the reviews to use (repeatable), all of the journal --journal names")
    full_text.add_argument("--full-text-entries", type=int, default=FULL_TEXT_ENTRIES,
                           help=f"retrieved entries to prepare and send (default {FULL_TEXT_ENTRIES})")
    full_text.add_argument("--fetch-workers", type=int, default=FETCH_WORKERS,
                           help=f"Europe PMC fetch threads (default {FETCH_WORKERS}, as study full-text)")
    full_text.add_argument("--full-text-cache",
                           help="directory (absolute) that keeps fetched fullTextXML across runs (default: "
                                f"{FULL_TEXT_CACHE_NAME} in --output)")
    full_text.add_argument("--review-route", choices=("public", "entitled"), default="entitled",
                           help="the journal's route class in the protocol (default entitled, as a census treats a "
                                "journal without a route): public for an open-access journal; entitled reviews reach "
                                "only an on-premises judge (--on-premises --policy)")
    full_text.add_argument("--on-premises", action="store_true",
                           help="the judge runs on premises (entitled review text allowed); needs --policy naming its "
                                "endpoint, as study full-text does")
    full_text.add_argument("--policy", help="operator egress policy (JSON, version 2) whose onPremEndpoints lists the judge")
    full_text.add_argument("--hypothetical-repair", action="store_true",
                           help="also resend every entry a full-text call left without a valid verdict alone (a "
                                "single-item call built by the census's full-text builder) and report the share after "
                                "it as hypothetical: citation-census@1 has no repair pass at that tier")
    probe.add_argument("--output", help="new or empty directory for the report, markdown table and call log")
    probe.add_argument("--dry-run", action="store_true", help="print the grid, request sizes and expected load; send nothing")
    register(probe, "capacity_probe", "command", "operator", state_required=False)
    profile = commands.add_parser(
        "profile", help="size profile of a census database's public inputs, for capacity probe --profile",
        description="Entries per review, citing sentences and their length, abstract length and the tokens per entry of "
                    "answered first calls, from the public reviews of a census scores database (read-only; no text, no "
                    "verdict).")
    profile.add_argument("--database", required=True, help="census scores database (working or frozen)")
    profile.add_argument("--output", required=True, help="profile file to write (JSON; must not exist)")
    register(profile, "capacity_probe", "profile_command", "workspace", state_required=False)


def settings_from_args(args: Any) -> ProbeSettings:
    profile = expected = source = None
    if getattr(args, "profile", None):
        profile, expected, source = load_profile(Path(args.profile))
    return ProbeSettings(
        base_url=args.base_url, model=args.model, api_key_env=args.api_key_env, metrics_url=args.metrics_url,
        proxy_pids=tuple(args.proxy_pid or ()), streams=parse_counts(args.streams, "--streams"),
        items=parse_counts(args.items, "--items"), cells=parse_cells(args.cells), order=args.order,
        max_streams=args.max_streams, run_above_allowed=bool(args.run_above_allowed),
        cell_seconds=args.cell_seconds, warmup_seconds=args.warmup_seconds,
        ramp_seconds=args.ramp_seconds, drain_seconds=args.drain_seconds, pause_seconds=args.pause_seconds,
        request_timeout=args.request_timeout, sample_seconds=args.sample_seconds,
        abort_failure_share=args.abort_failure_share, abort_window_seconds=args.abort_window_seconds,
        abort_min_calls=args.abort_min_calls, abort_min_failures=args.abort_min_failures,
        saturation_seconds=args.saturation_seconds,
        saturation_waiting=args.saturation_waiting, saturation_kv=args.saturation_kv,
        shared_tolerance=args.shared_tolerance, allowed_streams=args.allowed_streams, target_rate=args.target_rate,
        expect_output_tokens=args.expect_output_tokens, expect_prompt_tokens=args.expect_prompt_tokens,
        expect_tolerance=args.expect_tolerance, profile=profile, profile_expected=expected, profile_source=source,
        seed=args.seed, journal=args.journal, repair=bool(getattr(args, "repair", False)),
        tier=getattr(args, "tier", None) or TIER, store=getattr(args, "store", None),
        store_root=getattr(args, "store_root", None), review_prefixes=tuple(getattr(args, "review_doi_prefix", None) or ()),
        full_text_entries=getattr(args, "full_text_entries", FULL_TEXT_ENTRIES),
        fetch_workers=getattr(args, "fetch_workers", FETCH_WORKERS),
        full_text_cache=getattr(args, "full_text_cache", None),
        hypothetical_repair=bool(getattr(args, "hypothetical_repair", False)),
        review_route=getattr(args, "review_route", None) or "entitled",
        on_premises=bool(getattr(args, "on_premises", False)), policy=getattr(args, "policy", None))


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


def profile_command(args: Any) -> Any:
    output = Path(args.output)
    if output.exists():
        raise ValidationError("the profile file already exists; write it to a new path")
    value = profile_from_database(Path(args.database))
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return {"output": str(output), "source": value["source"], "expected": value["expected"]}


__all__ = ["CapacityProbe", "ProbeSettings", "SyntheticCorpus", "ConnectionPool", "AbortMonitor", "classify_response",
           "build_repair_request", "repair_summary", "item_bucket", "FullTextInputs", "FullTextCache",
           "full_text_summary", "hypothetical_repair_summary", "census_full_text_repair", "full_text_exit",
           "malformed_entries", "render_full_text_plan",
           "parse_prometheus", "server_load", "time_weighted_rate", "completed_rate", "percentile", "ordered_grid",
           "parse_cells", "build_request", "render_markdown", "render_plan", "exit_criterion", "allowed_concurrency",
           "server_verdict", "assess_cell", "above_allowed_reason", "profile_from_database", "load_profile",
           "validate_profile", "redact_key"]
