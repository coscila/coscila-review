# SPDX-License-Identifier: Apache-2.0
"""Exports of a follow-up branch: the compromised-claim database, alert records and the propagation graph.

``followup export --output <new directory>`` writes

- ``compromised-claims.sqlite3`` (closed): one row per claim and cited work for the S1 loci (cited works with a
  dated retraction, expression of concern or correction) and the S2 loci (works the census shows cited systematically
  without support): the citing sentence, the cited work, its status with source and date, the support verdicts with
  their quotes, the citation function, the dependency, an adjudication-history table (empty until a study adjudicates)
  and the judge receipts behind every verdict;
- ``alerts.jsonl`` and ``fu_alerts``: one record per affected claim and cited work, for the authors of the citing
  review and for study teams: the claim, the cited work, its status, the evidence, the confidence "check suggested"
  and a suggested action. A review that notes the notice itself (function notes-retraction) gets no alert;
- ``propagation-graph.json``: compromised works, the claims citing them, and the later articles whose sentences restate
  a claim on the review's authority (confirmed second-generation matches);
- ``manifest.json``: the sha256 and size of every file, the branch and parent digests and the redaction applied.

Research-integrity tooling for authors, reviewers and study teams; every adverse record reads "check suggested". A
public export drops entitled text as ``scores_db.redact_entitled`` does (entitled citing sentences become
``sha256:<hex>``, quotes from entitled sentences or evidence are removed); ``--allow-entitled-text`` makes a private
export that keeps them.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import scores_db
from .errors import ValidationError
from .followup_branch import FollowupBranch, now
from .followup_model import canonical, digest
from .followup_schema import ADVERSE, COMPROMISED

FORMAT = "ocsl-followup-exports-v1"
ALERT_FORMAT = "ocsl-followup-alert-v1"
GRAPH_FORMAT = "ocsl-followup-propagation-v1"
CONFIDENCE = "check suggested"
ACTIONS = {
    "retracted": "check whether the claim still holds without the retracted work; if it rests on that work, correct "
                 "the claim or cite the retraction notice",
    "expression-of-concern": "check the claim against the concern the notice raises; if the claim rests on this work, "
                             "state that an expression of concern was issued",
    "corrected": "check whether the correction changes the finding the claim cites; if it does, update the claim",
    "systematic-miscitation": "check the claim against the full text of the cited work, which the full-text judgment "
                              "did not find to support it",
}
DEPENDENCY_NOTES = {
    "sole": "the sentence cites no other work for this claim",
    "essential": "no other work cited in the sentence supports the claim at full text",
    "redundant": "another work cited in the sentence, which carries no notice of its own, supports the claim at full "
                 "text",
    "undetermined": "whether another work cited in the sentence supports the claim could not be assessed",
}
# An essential claim whose other cited works support it only where they carry a notice themselves.
COMPROMISED_ONLY_NOTE = ("the other works cited in the sentence that support the claim at full text carry a "
                         "retraction, expression of concern or correction themselves")
CLAIM_COLUMNS = (
    "locus_id", "claim_id", "basis", "strata", "journal", "review_doi", "label", "seq", "section", "sentence",
    "sentence_sha256", "provenance_class", "review_first_online", "work_key", "cited_doi", "cited_pmid", "cited_title",
    "cited_year", "status", "reinstated", "notice_doi", "notice_date", "date_precision", "status_source",
    "abstract_verdict", "abstract_reason", "abstract_quote", "fulltext_verdict", "fulltext_reason", "fulltext_quote",
    "function_class", "function_reason", "function_quote", "dependency_class", "cocited", "cocited_supporting",
    "cocited_unassessable", "judgments")
DICTIONARY = {
    "meta": {"key": "export metadata key", "value": "value"},
    "claims": {
        "locus_id": "follow-up locus", "claim_id": "citing sentence (loci of one claim share it)",
        "basis": "status (S1: a compromised cited work) or systematic-miscitation (S2)",
        "strata": "follow-up strata of the locus", "journal": "journal of the citing review",
        "review_doi": "citing review DOI", "label": "bibliography label", "seq": "locus order",
        "section": "section trail (removed for entitled reviews in a public export)",
        "sentence": "citing sentence (sha256:<hex> of it for entitled reviews in a public export)",
        "sentence_sha256": "sha256 of the citing sentence", "provenance_class": "public or entitled citing text",
        "review_first_online": "first-online date of the citing review", "work_key": "cited work",
        "cited_doi": "cited work DOI", "cited_pmid": "cited work PMID", "cited_title": "cited work title",
        "cited_year": "cited work year", "status": "resolved status of the cited work (status-resolution@1)",
        "reinstated": "1 when a later reinstatement reverses a retraction", "notice_doi": "DOI of the deciding notice",
        "notice_date": "date of the deciding notice", "date_precision": "day, month or year",
        "status_source": "source of the deciding notice", "abstract_verdict": "citation-support@2 abstract verdict",
        "abstract_reason": "why the abstract tier has no verdict", "abstract_quote": "first exact quote (abstract)",
        "fulltext_verdict": "citation-support@2 full-text verdict", "fulltext_reason": "why the full-text tier has no "
        "verdict", "fulltext_quote": "first exact quote (full text; removed for entitled evidence in a public export)",
        "function_class": "citation-function@1 class", "function_reason": "why the function is not classified",
        "function_quote": "exact quote of the function judgment", "dependency_class": "dependency-aggregation@1 class",
        "cocited": "other works cited in the sentence", "cocited_supporting": "co-cited works supporting at full text",
        "cocited_unassessable": "co-cited works without an assessable full-text verdict",
        "judgments": "judge receipts (call ids) behind the verdicts, JSON"},
    "observations": {"work_key": "cited work", "source": "status source", "status": "status the source reports",
                     "notice_doi": "notice DOI", "notice_pmid": "notice PMID", "notice_date": "notice date",
                     "date_precision": "precision", "retrieved_at": "time the source answered",
                     "snapshot_id": "status snapshot"},
    "receipts": {"call_id": "receipt id", "instrument_id": "instrument", "instrument_digest": "instrument digest",
                 "tier": "tier", "variant": "primary or replicate", "kind": "first or repair call",
                 "judge": "judge name", "judge_location": "on-prem or external", "request_model": "model requested",
                 "served_model": "model that answered", "fingerprint": "response fingerprint",
                 "request_sha256": "stored request body (in the private branch)", "response_sha256": "stored response "
                 "body (in the private branch)", "outcome": "call outcome", "started_at": "call start",
                 "finished_at": "call end"},
    "adjudications": {"locus_id": "locus", "label": "adjudicated label", "label_source": "who adjudicated "
                      "(adjudication)", "decided_at": "time decided", "detail_json": "adjudication record"},
    "data_dictionary": {"table_name": "table", "column_name": "column", "meaning": "meaning"},
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_record(path: Path, rows: int | None = None) -> dict[str, Any]:
    record = {"path": path.name, "sha256": scores_db.file_sha256(path), "bytes": path.stat().st_size}
    if rows is not None:
        record["rows"] = rows
    return record


def claim_units(connection: Any) -> list[dict[str, Any]]:
    """S1 and S2 loci with their status, support, function and dependency rows."""
    rows = connection.execute(
        "SELECT f.*, group_concat(s.stratum, ',') AS strata, ws.status, ws.reinstated, ws.notice_doi, ws.notice_date, "
        "ws.date_precision, ws.decided_by FROM fu_frame f JOIN fu_strata s ON s.locus_id=f.locus_id "
        "LEFT JOIN fu_work_status ws ON ws.work_key=f.work_key WHERE s.stratum IN ('S1','S2') GROUP BY f.locus_id "
        "ORDER BY f.journal, f.doi, f.unit, f.label, f.seq").fetchall()
    units = []
    for row in rows:
        unit = dict(row)
        unit["strata"] = ",".join(sorted(set((unit["strata"] or "").split(","))))
        unit["basis"] = "status" if "S1" in unit["strata"] and unit["status"] in COMPROMISED else (
            "systematic-miscitation")
        work = connection.execute("SELECT doi, pmid, title, year FROM census.works WHERE work_key=?",
                                  (unit["work_key"],)).fetchone()
        unit["cited"] = dict(work) if work else {}
        unit["support"] = {row["tier"]: dict(row) for row in connection.execute(
            "SELECT * FROM fu_support WHERE locus_id=?", (unit["locus_id"],))}
        function = connection.execute("SELECT * FROM fu_function WHERE locus_id=?", (unit["locus_id"],)).fetchone()
        unit["function"] = dict(function) if function else None
        dependency = connection.execute("SELECT * FROM fu_dependency WHERE locus_id=?", (unit["locus_id"],)).fetchone()
        unit["dependency"] = dict(dependency) if dependency else None
        unit["calls"] = sorted({row[0] for row in connection.execute(
            "SELECT DISTINCT call_id FROM fu_judgments WHERE call_id IS NOT NULL AND item_key IN (?,?,?)",
            (unit["locus_id"], f"{unit['locus_id']}|abstract", f"{unit['locus_id']}|full-text"))})
        units.append(unit)
    return units


class Redactor:
    """What a public export keeps: entitled citing text and quotes from entitled sentences or evidence are dropped."""

    def __init__(self, private: bool):
        self.private = private
        self.counts: Counter = Counter()

    def sentence(self, unit: Mapping[str, Any], text: str | None) -> str | None:
        if text is None:
            return None
        if self.private or unit.get("provenance_class") == "public":
            return text
        self.counts["sentences"] += 1
        return "sha256:" + _sha(text)

    def section(self, unit: Mapping[str, Any]) -> str | None:
        if self.private or unit.get("provenance_class") == "public":
            return unit.get("section")
        if unit.get("section"):
            self.counts["sections"] += 1
        return None

    def support_quote(self, row: Mapping[str, Any] | None) -> str | None:
        if not row or not row.get("quote"):
            return None
        if self.private or row.get("provenance_class") == "public":
            return row["quote"]
        self.counts["supportQuotes"] += 1
        return None

    def function_quote(self, unit: Mapping[str, Any]) -> str | None:
        row = unit.get("function") or {}
        if not row.get("quote"):
            return None
        if self.private or unit.get("provenance_class") == "public":
            return row["quote"]
        self.counts["functionQuotes"] += 1
        return None


def claim_row(branch: FollowupBranch, connection: Any, unit: Mapping[str, Any], redactor: Redactor) -> dict[str, Any]:
    text, _ = branch.sentence(connection, unit["journal"], unit["doi"], unit["label"], unit["seq"])
    support = unit["support"]
    function = unit["function"] or {}
    dependency = unit["dependency"] or {}
    cited = unit["cited"]
    return {
        "locus_id": unit["locus_id"], "claim_id": unit["claim_id"], "basis": unit["basis"], "strata": unit["strata"],
        "journal": unit["journal"], "review_doi": unit["doi"], "label": unit["label"], "seq": unit["seq"],
        "section": redactor.section(unit), "sentence": redactor.sentence(unit, text),
        "sentence_sha256": unit["sentence_sha256"], "provenance_class": unit["provenance_class"],
        "review_first_online": unit["review_first_online"], "work_key": unit["work_key"],
        "cited_doi": cited.get("doi"), "cited_pmid": cited.get("pmid"), "cited_title": cited.get("title"),
        "cited_year": cited.get("year"), "status": unit["status"], "reinstated": unit["reinstated"],
        "notice_doi": unit["notice_doi"], "notice_date": unit["notice_date"], "date_precision": unit["date_precision"],
        "status_source": unit["decided_by"],
        "abstract_verdict": (support.get("abstract") or {}).get("verdict"),
        "abstract_reason": (support.get("abstract") or {}).get("reason"),
        "abstract_quote": redactor.support_quote(support.get("abstract")),
        "fulltext_verdict": (support.get("full-text") or {}).get("verdict"),
        "fulltext_reason": (support.get("full-text") or {}).get("reason"),
        "fulltext_quote": redactor.support_quote(support.get("full-text")),
        "function_class": function.get("class"), "function_reason": function.get("reason"),
        "function_quote": redactor.function_quote(unit), "dependency_class": dependency.get("class"),
        "cocited": dependency.get("cocited"), "cocited_supporting": dependency.get("cocited_supporting"),
        "cocited_unassessable": dependency.get("cocited_unassessable"), "judgments": canonical(unit["calls"])}


def affected(unit: Mapping[str, Any]) -> bool:
    """A claim gets an alert when it rests on a compromised work it does not itself flag, or when an S2 claim's
    full-text verdict is adverse."""
    if (unit.get("function") or {}).get("class") == "notes-retraction":
        return False
    if unit["basis"] == "status":
        return True
    return (unit["support"].get("full-text") or {}).get("verdict") in ADVERSE


def _compromised_supporting(dependency: Mapping[str, Any] | None) -> int:
    """Co-cited works that support the claim but carry a notice themselves (dependency-aggregation@1 detail)."""
    try:
        detail = json.loads((dependency or {}).get("detail_json") or "{}")
    except ValueError:
        return 0
    value = detail.get("compromisedSupporting") if isinstance(detail, dict) else None
    return value if isinstance(value, int) else 0


def alert_record(row: Mapping[str, Any], unit: Mapping[str, Any], created: str) -> dict[str, Any]:
    reason = unit["basis"] if unit["basis"] == "systematic-miscitation" else unit["status"]
    dependency = (unit.get("dependency") or {}).get("class")
    action = ACTIONS[reason]
    note = DEPENDENCY_NOTES.get(dependency or "")
    compromised_support = _compromised_supporting(unit.get("dependency"))
    if dependency == "essential" and compromised_support:
        note = COMPROMISED_ONLY_NOTE
    authors = f"For the authors of the citing review: {action}" + (f" ({note})." if note else ".")
    team = ("For a study team: treat the claim as resting on compromised evidence until it is checked; the record is a "
            "model measurement with its evidence, not an established finding.")
    support = {tier: {"verdict": row[f"{prefix}_verdict"], "reason": row[f"{prefix}_reason"],
                      "quote": row[f"{prefix}_quote"]}
               for tier, prefix in (("abstract", "abstract"), ("full-text", "fulltext"))}
    after = None
    if row["notice_date"] and row["review_first_online"]:
        from .followup_status import compare_dates
        order = compare_dates(row["review_first_online"], "day", row["notice_date"], row["date_precision"])
        after = None if order in (None, 0) else order > 0
    return {
        "format": ALERT_FORMAT, "alertId": "alert-" + digest(row["claim_id"], row["work_key"])[:32],
        "basis": unit["basis"],
        "claim": {"journal": row["journal"], "reviewDoi": row["review_doi"], "claimId": row["claim_id"],
                  "locusId": row["locus_id"], "sentence": row["sentence"], "section": row["section"]},
        "citedWork": {"workKey": row["work_key"], "doi": row["cited_doi"], "pmid": row["cited_pmid"],
                      "title": row["cited_title"], "year": row["cited_year"]},
        "status": {"status": row["status"], "noticeDoi": row["notice_doi"], "noticeDate": row["notice_date"],
                   "datePrecision": row["date_precision"], "source": row["status_source"],
                   "reinstated": bool(row["reinstated"])},
        "evidence": {"support": support,
                     "function": {"class": row["function_class"], "reason": row["function_reason"],
                                  "quote": row["function_quote"]},
                     "dependency": {"class": dependency, "cocited": row["cocited"],
                                    "cocitedSupporting": row["cocited_supporting"],
                                    "cocitedUnassessable": row["cocited_unassessable"],
                                    "cocitedCompromisedSupporting": compromised_support},
                     "timing": {"reviewFirstOnline": row["review_first_online"], "noticeDate": row["notice_date"],
                                "citedAfterNotice": after},
                     "receipts": json.loads(row["judgments"])},
        "confidence": CONFIDENCE, "suggestedAction": {"authors": authors, "studyTeam": team},
        "createdAt": created}


def propagation_graph(connection: Any, rows: Sequence[Mapping[str, Any]], units: Sequence[Mapping[str, Any]]
                      ) -> dict[str, Any]:
    """Compromised works -> claims citing them -> later articles with confirmed restatements."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    by_locus = {unit["locus_id"]: unit for unit in units}
    for row in rows:
        if row["basis"] != "status":
            continue
        work = f"work:{row['work_key']}"
        claim = f"claim:{row['claim_id']}"
        nodes.setdefault(work, {"id": work, "kind": "work", "workKey": row["work_key"], "doi": row["cited_doi"],
                                "title": row["cited_title"], "status": row["status"],
                                "noticeDate": row["notice_date"], "datePrecision": row["date_precision"]})
        nodes.setdefault(claim, {"id": claim, "kind": "claim", "claimId": row["claim_id"], "journal": row["journal"],
                                 "reviewDoi": row["review_doi"], "sentenceSha256": row["sentence_sha256"]})
        edges.append({"from": work, "to": claim, "kind": "cited-by", "locusId": row["locus_id"],
                      "function": row["function_class"], "dependency": row["dependency_class"],
                      "fullTextVerdict": row["fulltext_verdict"]})
    loci = [row["locus_id"] for row in rows if row["basis"] == "status"]
    if loci:
        marks = ",".join("?" for _ in loci)
        for match in connection.execute(
                f"SELECT * FROM fu_secondgen_matches WHERE anchor_locus_id IN ({marks}) AND assessable=1 AND "
                "match='same-claim' AND reliance='cites-review-for-claim' ORDER BY anchor_locus_id, sentence_id",
                loci).fetchall():
            unit = by_locus[match["anchor_locus_id"]]
            article = f"article:{match['citing_key']}"
            edge = connection.execute("SELECT citing_doi, citing_pmid, citing_title, citing_year, citing_first_online "
                                      "FROM fu_citedby_edges WHERE target_doi=lower(?) AND citing_key=? ORDER BY source "
                                      "LIMIT 1", (unit["doi"], match["citing_key"])).fetchone()
            details = dict(edge) if edge else {}
            nodes.setdefault(article, {"id": article, "kind": "article", "citingKey": match["citing_key"],
                                       "doi": details.get("citing_doi"), "pmid": details.get("citing_pmid"),
                                       "title": details.get("citing_title"), "year": details.get("citing_year"),
                                       "firstOnline": details.get("citing_first_online")})
            edges.append({"from": f"claim:{unit['claim_id']}", "to": article, "kind": "restated-by",
                          "sentenceId": match["sentence_id"], "match": match["match"], "reliance": match["reliance"],
                          "inclusionProbability": match["inclusion_probability"]})
    kinds = Counter(node["kind"] for node in nodes.values())
    return {"format": GRAPH_FORMAT, "nodes": sorted(nodes.values(), key=lambda node: node["id"]),
            "edges": sorted(edges, key=lambda edge: (edge["from"], edge["to"], edge.get("locusId") or "",
                                                     edge.get("sentenceId") or "")),
            "counts": {"works": kinds["work"], "claims": kinds["claim"], "articles": kinds["article"],
                       "edges": len(edges)}}


def write_claims_database(path: Path, rows: Sequence[Mapping[str, Any]], connection: Any,
                          meta: Mapping[str, str]) -> dict[str, int]:
    works = sorted({row["work_key"] for row in rows if row["work_key"]})
    calls = sorted({call for row in rows for call in json.loads(row["judgments"])})
    with closing(sqlite3.connect(path)) as target:
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        target.executemany("INSERT INTO meta VALUES(?,?)", sorted(meta.items()))
        target.execute(f"CREATE TABLE claims({', '.join(CLAIM_COLUMNS)}, PRIMARY KEY(locus_id))")
        target.executemany(f"INSERT INTO claims VALUES({','.join('?' for _ in CLAIM_COLUMNS)})",
                           [tuple(row[column] for column in CLAIM_COLUMNS) for row in rows])
        observation_columns = tuple(DICTIONARY["observations"])
        target.execute(f"CREATE TABLE observations({', '.join(observation_columns)})")
        for start in range(0, len(works), 500):
            chunk = works[start:start + 500]
            target.executemany(f"INSERT INTO observations VALUES({','.join('?' for _ in observation_columns)})", [
                tuple(row) for row in connection.execute(
                    f"SELECT {', '.join(observation_columns)} FROM fu_status_observations WHERE work_key IN "
                    f"({','.join('?' for _ in chunk)}) ORDER BY work_key, source, retrieved_at", chunk)])
        receipt_columns = tuple(DICTIONARY["receipts"])
        target.execute(f"CREATE TABLE receipts({', '.join(receipt_columns)}, PRIMARY KEY(call_id))")
        for start in range(0, len(calls), 500):
            chunk = calls[start:start + 500]
            target.executemany(f"INSERT INTO receipts VALUES({','.join('?' for _ in receipt_columns)})", [
                tuple(row) for row in connection.execute(
                    f"SELECT {', '.join(receipt_columns)} FROM fu_calls WHERE call_id IN "
                    f"({','.join('?' for _ in chunk)}) ORDER BY call_id", chunk)])
        target.execute("CREATE TABLE adjudications(locus_id TEXT NOT NULL, label TEXT NOT NULL, label_source TEXT NOT "
                       "NULL, decided_at TEXT NOT NULL, detail_json TEXT NOT NULL)")
        target.execute("CREATE TABLE data_dictionary(table_name TEXT NOT NULL, column_name TEXT NOT NULL, meaning TEXT "
                       "NOT NULL, PRIMARY KEY(table_name, column_name))")
        target.executemany("INSERT INTO data_dictionary VALUES(?,?,?)", [
            (table, column, meaning) for table, columns in sorted(DICTIONARY.items())
            for column, meaning in columns.items()])
        target.commit()
        counts = {table: target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("claims", "observations", "receipts")}
        target.execute("VACUUM")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(path) + suffix).exists():
            raise ValidationError("the compromised-claim database left a journal behind")
    os.chmod(path, 0o444)
    return counts


def export(database: Path, output: Path, *, allow_entitled_text: bool = False) -> dict[str, Any]:
    branch = FollowupBranch(Path(database))
    output = Path(output)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValidationError("the export directory is not empty; export into a new directory")
    report = branch.verify(deep=True)
    if not report["ok"]:
        raise ValidationError("the follow-up branch does not verify: " + "; ".join(report["problems"]))
    output.mkdir(parents=True, exist_ok=True)
    redactor = Redactor(allow_entitled_text)
    branch_meta = branch.meta()
    created = now()
    with branch.writing() as connection:
        units = claim_units(connection)
        rows = [claim_row(branch, connection, unit, redactor) for unit in units]
        freeze = connection.execute("SELECT freeze_id FROM fu_protocol_freeze ORDER BY frozen_at DESC LIMIT 1"
                                    ).fetchone()
        meta = {"format": FORMAT, "createdAt": created, "private": "1" if allow_entitled_text else "0",
                "parentSha256": branch_meta.get("parentSha256", ""), "protocolSha256":
                branch_meta.get("protocolSha256", ""), "freezeId": freeze[0] if freeze else "",
                "confidence": CONFIDENCE, "interpretation": "model measurements with evidence; every adverse record "
                "reads check suggested"}
        alerts, seen = [], set()
        for row, unit in zip(rows, units):
            if not affected(unit) or (row["claim_id"], row["work_key"]) in seen:
                continue
            seen.add((row["claim_id"], row["work_key"]))
            alerts.append(alert_record(row, unit, created))
        connection.execute("DELETE FROM fu_alerts")
        connection.executemany("INSERT INTO fu_alerts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (alert["alertId"], alert["claim"]["journal"], alert["claim"]["reviewDoi"], alert["claim"]["claimId"],
             alert["claim"]["locusId"], alert["citedWork"]["workKey"], alert["status"]["status"] or "unknown",
             alert["status"]["noticeDate"], alert["evidence"]["function"]["class"],
             alert["evidence"]["dependency"]["class"], canonical(alert["evidence"]["support"]),
             canonical({key: value for key, value in alert["evidence"].items() if key != "support"}),
             alert["confidence"], alert["suggestedAction"]["authors"] + " " + alert["suggestedAction"]["studyTeam"],
             created) for alert in alerts])
        graph = propagation_graph(connection, rows, units)
        counts = write_claims_database(output / "compromised-claims.sqlite3", rows, connection, meta)
    with (output / "alerts.jsonl").open("w", encoding="utf-8") as handle:
        for alert in alerts:
            handle.write(json.dumps(alert, ensure_ascii=False, sort_keys=True) + "\n")
    (output / "propagation-graph.json").write_text(json.dumps(graph, indent=2, sort_keys=True, ensure_ascii=False)
                                                   + "\n", encoding="utf-8")
    files = [_file_record(output / "compromised-claims.sqlite3", counts["claims"]),
             _file_record(output / "alerts.jsonl", len(alerts)),
             _file_record(output / "propagation-graph.json", len(graph["edges"]))]
    manifest = {"format": FORMAT, "createdAt": created, "private": allow_entitled_text,
                "branch": {"sha256": scores_db.file_sha256(branch.path), "parentSha256": meta["parentSha256"],
                           "protocolSha256": meta["protocolSha256"], "freezeId": meta["freezeId"]},
                "files": files, "counts": {**counts, "alerts": len(alerts), **graph["counts"]},
                "redaction": "none (private export)" if allow_entitled_text else dict(redactor.counts),
                "confidence": CONFIDENCE}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"output": str(output), **manifest}


def cmd_export(args: Any) -> dict[str, Any]:
    return export(Path(args.database), Path(args.output),
                  allow_entitled_text=bool(getattr(args, "allow_entitled_text", False)))
