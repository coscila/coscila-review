# SPDX-License-Identifier: Apache-2.0
"""Synthetic fixtures for the follow-up audit: a frozen parent census, a protocol, judges and provider transports.

Everything here is invented: journals, reviews, works, identifiers (the 10.5555 test prefix), sentences and numbers.
Databases are built at test time (no SQLite file is committed). Provider responses come from
tests/fixtures/followup; no test makes a network request.
"""
from __future__ import annotations

import copy
import json
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping

from ocsl_review import scores_db
from ocsl_review.scholarly import RawResponse

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "followup"
JOURNAL_A = "Journal of Synthetic Reviews A"
JOURNAL_B = "Journal of Synthetic Reviews B"
MODEL = "synthetic-judge-model"

REVIEWS = [
    # journal, doi, year, first online, provenance class, topic
    (JOURNAL_A, "10.5555/fu.review.a1", 2021, "2021-03-01", "public", "topic-x"),
    (JOURNAL_A, "10.5555/fu.review.a2", 2023, "2023-06-10", "entitled", "topic-y"),
    (JOURNAL_B, "10.5555/fu.review.b1", 2022, "2022-02-15", "public", "topic-x"),
]
WORKS = [
    # work key, doi, pmid, title, year
    ("w-retracted", "10.5555/fu.work.retracted", "9000001", "Fictional cohort study one", "2018"),
    ("w-concern", "10.5555/fu.work.concern", "9000002", "Fictional trial two", "2019"),
    ("w-corrected", "10.5555/fu.work.corrected", "9000003", "Fictional survey three", "2017"),
    ("w-plain-1", "10.5555/fu.work.plain1", "9000004", "Fictional mechanism study four", "2016"),
    ("w-plain-2", "10.5555/fu.work.plain2", "9000005", "Fictional imaging study five", "2020"),
    ("w-plain-3", "10.5555/fu.work.plain3", None, "Fictional registry analysis six", "2015"),
    ("w-report", "10.5555/fu.work.report", None, "Synthetic disease statistics report, second edition", "2020"),
]
# journal, review doi, label, work key, abstract category, full-text category, sentences [(unit, sentence)]
CITATIONS = [
    (JOURNAL_A, "10.5555/fu.review.a1", "1", "w-retracted", "true", None,
     [(3, "Marker zeta doubles the risk in adults ⟨R1⟩ ⟨R2⟩ .")]),
    (JOURNAL_A, "10.5555/fu.review.a1", "2", "w-plain-1", "true", None,
     [(3, "Marker zeta doubles the risk in adults ⟨R1⟩ ⟨R2⟩ .")]),
    (JOURNAL_A, "10.5555/fu.review.a1", "3", "w-report", "false", None,
     [(5, "The disease caused 12.5 thousand deaths in Exampleland in 2019 ⟨R3⟩ .")]),
    (JOURNAL_A, "10.5555/fu.review.a1", "4", "w-plain-2", "true", None,
     [(7, "Imaging was used as in earlier work ⟨R4⟩ .")]),
    (JOURNAL_A, "10.5555/fu.review.a2", "1", "w-concern", "false", "false",
     [(2, "The trial reported a large benefit ⟨S1⟩ , although it was later questioned .")]),
    (JOURNAL_A, "10.5555/fu.review.a2", "2", "w-retracted", "true", None,
     [(4, "A retracted cohort study had suggested the same association ⟨S2⟩ .")]),
    (JOURNAL_A, "10.5555/fu.review.a2", "3", "w-plain-3", "true", None,
     [(6, "Registries cover most of the population ⟨S3⟩ .")]),
    (JOURNAL_B, "10.5555/fu.review.b1", "1", "w-corrected", "true", None,
     [(1, "Survey data show a rising trend ⟨T1⟩ .")]),
    (JOURNAL_B, "10.5555/fu.review.b1", "2", "w-retracted", "false", None,
     [(2, "Marker zeta predicts outcome ⟨T2⟩ ⟨T3⟩ .")]),
    (JOURNAL_B, "10.5555/fu.review.b1", "3", "w-plain-1", "true", None,
     [(2, "Marker zeta predicts outcome ⟨T2⟩ ⟨T3⟩ .")]),
    (JOURNAL_B, "10.5555/fu.review.b1", "4", "w-report", "true", None,
     [(4, "Incidence reached 40 per population unit in Exampleland in 2018 ⟨T4⟩ .")]),
]


def make_working_census(path: Path) -> Path:
    """A complete working census scores database with the synthetic population."""
    connection = scores_db.connect(path, create=True)
    try:
        for order, (journal, doi, year, first_online, provenance, topic) in enumerate(REVIEWS):
            connection.execute(
                "INSERT INTO reviews(journal, doi, title, year, first_online, stratum, route, provenance_class, topic,"
                " review_order, abstract_state, full_text_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (journal, doi, f"Synthetic review {order + 1}", year, first_online, "Review", "jats", provenance, topic,
                 order, "completed", "completed"))
        for key, doi, pmid, title, year in WORKS:
            connection.execute("INSERT INTO works(work_key, doi, pmid, title, venue, year, retracted, identity_version)"
                               " VALUES(?,?,?,?,?,?,?,?)", (key, doi, pmid, title, "Synthetic Venue", year, 0,
                                                            "identity-synthetic"))
        ordinals: dict[tuple[str, str], int] = {}
        for journal, doi, label, work, abs_category, ft_category, sentences in CITATIONS:
            ordinal = ordinals[(journal, doi)] = ordinals.get((journal, doi), 0) + 1
            abs_class = "sure_supported" if abs_category == "true" else "sure_not_supported"
            ft_class = None if ft_category is None else ("sure_supported" if ft_category == "true" else
                                                         "sure_not_supported")
            connection.execute(
                "INSERT INTO citations(journal, doi, label, ordinal, target_id, cited_inbody, n_loci, identity,"
                " cited_work_key, cited_doi, model_judged, abs_category, abs_escalated, abs_class, abs_basis,"
                " ft_category, ft_class) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (journal, doi, label, ordinal, f"ref-{label}", 1, len(sentences), "resolved", work,
                 dict((k, d) for k, d, *_ in WORKS)[work], 1, abs_category, int(ft_category is not None), abs_class,
                 "model", ft_category, ft_class))
            for seq, (unit, sentence) in enumerate(sentences, 1):
                connection.execute("INSERT INTO loci VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (journal, doi, label, seq, sentence, "Results", unit, sentence.index("⟨"), "p",
                                    0, 0, 0, 0, 1))
        scores_db.aggregate_reviews(connection)
        connection.commit()
    finally:
        connection.close()
    return path


def make_parent(root: Path) -> dict[str, Path]:
    """Frozen parent census (entitled sentences redacted) and its private copy that keeps the text."""
    working = make_working_census(root / "census-working.sqlite3")
    parent = root / "census-frozen.sqlite3"
    text = root / "census-frozen-text.sqlite3"
    scores_db.freeze(working, parent)
    scores_db.freeze(working, text, keep_text=True)
    return {"working": working, "parent": parent, "text": text}


PROTOCOL: dict[str, Any] = {
    "format": "ocsl-followup-protocol-v1",
    "version": "synthetic-1",
    "question": "Which claims of the synthetic reviews rest on compromised cited works?",
    "seed": 11,
    "bootstrap": {"b": 200, "seed": 13},
    "journals": [JOURNAL_A, JOURNAL_B],
    "status": {"sources": ["crossref", "retraction-watch-csv", "pubmed", "europe-pmc"],
               "retractionWatchCsv": {"declaredVersion": "synthetic-snapshot"}, "rule": "status-resolution@1"},
    "strata": {
        "S1": {"statuses": ["retracted", "expression-of-concern", "corrected"], "inBodyOnly": True,
               "identities": ["resolved", "resolved_unverified"]},
        "S2": {"minCitingReviews": 2, "fdr": 0.05, "base": ["journal", "year"],
               "category": "abstract-tier-false-among-true-and-false", "excludeFamilies": ["statistics-report"]},
        "S3": {"family": "statistics-report",
               "familyRules": {"version": "synthetic-rules-1", "titlePatterns": ["statistics report"]},
               "licensingVerified": False},
        "S0": {"size": 2, "matchOn": ["journal", "year"]},
        "coCited": {"definition": "same-citing-sentence"},
        "pilot": {"s1Share": 0.5, "s0Count": 1},
        "replicate": {"share": 0.5},
    },
    "instruments": {"support": "citation-support@2", "function": "citation-function@1", "numeric": "numeric-check@1",
                    "match": "claim-match@1"},
    "model": {"served": MODEL, "location": "on-premises"},
    "secondGeneration": {"edges": 2, "k": 3, "sources": ["europe-pmc", "openalex"]},
    "estimands": [
        {"id": "compromised-share", "numerator": "in-body identity-resolved loci citing a compromised work",
         "denominator": "in-body identity-resolved loci", "by": ["journal", "year", "status"],
         "weights": "realised-inclusion-probability"},
        {"id": "sole-or-essential", "numerator": "S1 loci with no co-cited work supporting the claim at full text",
         "denominator": "S1 loci with assessable full-text dependency", "by": ["journal", "year"],
         "weights": "realised-inclusion-probability"},
    ],
    "rules": {"abstractSilenceIsNotUnsupported": True, "failedDownloadIsNotUnsupported": True,
              "unassessableToCoverage": True, "entitledTextOnPremisesOnly": True},
    "gates": ["smoke", "pilot", "capacity", "production", "replicate"],
}


def protocol(**changes: Any) -> dict[str, Any]:
    value = copy.deepcopy(PROTOCOL)
    value.update(changes)
    return value


class ScriptedJudge:
    """A judge that answers from a function of the request; records every request it receives."""

    def __init__(self, answer: Callable[[list[dict[str, str]], Mapping[str, Any]], str], *, model: str = MODEL,
                 served: str | None = None, location: str = "on-prem", fingerprint: str | None = "fp-synthetic"):
        self.answer, self.model, self.location = answer, model, location
        self.served = served or model
        self.fingerprint = fingerprint
        self.name, self.host, self.endpoint = "scripted-judge", "localhost", None
        self.requests: list[tuple[list[dict[str, str]], dict[str, Any]]] = []

    def complete(self, messages, *, settings):
        from ocsl_review.followup_judge import JudgeReply
        self.requests.append((messages, dict(settings)))
        return JudgeReply(self.answer(messages, settings), self.served, {"prompt_tokens": 10, "completion_tokens": 5},
                          "stop", self.fingerprint)


def items_of(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    return json.loads(messages[1]["content"])["items"]


class FixtureTransport:
    """A scholarly transport answering from fixture files by URL; records every request (URL, params, headers)."""

    def __init__(self, routes: Mapping[str, tuple[int, str | bytes]], *, user_agent: str = "ocsl-review/0.1 (test)"):
        self.routes = dict(routes)
        self.user_agent = user_agent
        self.requests: list[dict[str, Any]] = []

    def get(self, url, *, params=None, headers=None, **kwargs):
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
        target = f"{url}?{query}" if query else url
        self.requests.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {}),
                              "target": target, "userAgent": self.user_agent})
        for prefix, (status, body) in self.routes.items():
            if target.startswith(prefix):
                data = body if isinstance(body, bytes) else (FIXTURES / body).read_bytes()
                if status >= 400:
                    from ocsl_review.errors import ProviderError
                    raise ProviderError(f"provider HTTP {status}: fixture")
                return RawResponse(data, status, {"Content-Type": "application/json"}, target)
        from ocsl_review.errors import ProviderError
        raise ProviderError(f"provider HTTP 404: no fixture for {target}")

    def get_document(self, url, **kwargs):
        return self.get(url)
