# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review instruments`` and ``ocsl-review study``: instrument registry and census study commands.

Controllers call these commands instead of writing drivers: prepare reviews
from a manifest, run the abstract and full-text tiers, the predeclared
no-truncation sensitivity, gates, freezing, exports, estimands and facts.
"""
from __future__ import annotations

import http.client
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any

from .errors import ValidationError

EUROPE_PMC_FULL_TEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/%s/fullTextXML"


def add_commands(families, register):
    instruments = families.add_parser("instruments", help="list, show and verify measurement instruments")
    commands = instruments.add_subparsers(dest="instrument_command", required=True)
    for name in ("list", "show", "verify"):
        command = commands.add_parser(name)
        if name == "show":
            command.add_argument("instrument")
        if name == "verify":
            command.add_argument("instrument", nargs="?")
        register(command, "instruments", f"cmd_{name}", "read-only", state_required=False)

    study = families.add_parser("study", help="run a census study with a registered instrument")
    actions = study.add_subparsers(dest="study_command", required=True)
    for name in ("abstracts", "prepare", "judge", "full-text", "no-truncation", "replicate", "status", "gates", "freeze",
                 "export", "estimands", "facts", "verify-facts", "cross-validate"):
        command = actions.add_parser(name)
        if name in ("prepare", "judge", "full-text", "no-truncation", "replicate", "status", "gates", "freeze"):
            command.add_argument("--database", required=True, help="working census database")
        if name in ("prepare", "judge", "full-text", "no-truncation", "replicate"):
            command.add_argument("--seed", type=int, required=True, help="protocol seed")
        if name == "prepare":
            command.add_argument("--input", required=True, help="JSONL review records; document paths are relative to it")
        if name == "abstracts":
            command.add_argument("--input", required=True, help="JSONL review records")
            command.add_argument("--output", required=True, help="JSONL review records with Europe PMC abstracts")
            command.add_argument("--overwrite", action="store_true",
                                 help="look up every work again, also those already looked up")
        if name in ("judge", "full-text", "no-truncation", "replicate"):
            command.add_argument("--base-url", required=True, help="chat-completions base URL of the judge")
            command.add_argument("--api-key-env", help="environment variable holding the judge key")
            command.add_argument("--model", help="model name to request (defaults to the required served model)")
            command.add_argument("--concurrency", type=int, default=60)
            command.add_argument("--on-premises", action="store_true", help="the judge runs on premises (entitled text allowed)")
            command.add_argument("--transport-retries", type=int, default=3,
                                 help="further passes over entries left open by an outage before the run pauses")
            command.add_argument("--transport-cooldown", type=float, default=60.0,
                                 help="seconds before the first further pass (doubling for each next pass)")
        if name == "judge":
            command.add_argument("--limit", type=int)
        if name == "full-text":
            command.add_argument("--europe-pmc", action="store_true", help="fetch Europe PMC fullTextXML by PMCID")
            command.add_argument("--fetch-workers", type=int, default=4)
            command.add_argument("--retry-failed", action="store_true",
                                 help="reopen entries an earlier run recorded as fetch-failed")
        if name == "no-truncation":
            command.add_argument("--entries", type=int)
        if name == "replicate":
            command.add_argument("--entries", type=int, required=True, help="entries to re-judge, stratified by journal and year")
        if name == "freeze":
            command.add_argument("--output", required=True)
            command.add_argument("--keep-text", action="store_true",
                                 help="keep evidence and entitled review text (a private dataset; exports refuse it)")
        if name in ("freeze", "facts"):
            command.add_argument("--allow-incomplete", action="store_true",
                                 help="accept a paused run or pending reviews and record the run as incomplete")
        if name in ("export", "estimands", "facts"):
            command.add_argument("--scores", required=True, help="frozen scores database")
        if name in ("export", "facts"):
            command.add_argument("--output", required=True)
        if name == "export":
            command.add_argument("--no-parquet", action="store_true")
            command.add_argument("--allow-entitled-text", action="store_true",
                                 help="export a database frozen with --keep-text (a private export)")
        if name in ("estimands", "facts"):
            command.add_argument("--journal", action="append", required=True)
            command.add_argument("--b", type=int, default=10_000)
            command.add_argument("--bootstrap-seed", type=int, required=True)
            command.add_argument("--ratio", nargs=2, metavar=("NUMERATOR", "DENOMINATOR"))
            command.add_argument("--trend-from", type=int)
            command.add_argument("--trend-to", type=int)
        if name == "verify-facts":
            command.add_argument("--facts", required=True)
        if name == "cross-validate":
            command.add_argument("--jats", required=True, help="JATS document of a review")
            command.add_argument("--html", required=True, help="publisher HTML of the same review")
            command.add_argument("--minimum-share", type=float, default=0.95)
        trust = "read-only" if name in ("status", "gates", "estimands", "verify-facts", "cross-validate") else "workspace"
        register(command, "study_cli", "command", trust, state_required=False)


def _transport(args):
    from .census_runner import HttpChatTransport
    return HttpChatTransport(args.base_url, api_key_env=args.api_key_env, on_premises=args.on_premises)


def _run(args, transport=None):
    from .census_runner import CensusRun
    return CensusRun(Path(args.database), seed=args.seed, transport=transport,
                     request_model=getattr(args, "model", None), concurrency=getattr(args, "concurrency", 60))


def europe_pmc_fetcher(spacing: float = 0.4, *, attempts: int = 3, backoff: tuple[float, ...] = (2.0, 5.0, 10.0),
                       opener: Any = None, sleep: Any = time.sleep):
    """Fetch fullTextXML by PMCID; each fetching thread pauses ``spacing`` seconds between its requests.

    A 404 or another definitive client error means there is no open-access full text (None). Throttling
    (429), server errors, timeouts and dropped connections are retried with backoff and then raise
    ``RetryableFetch``, so the entry stays open for the next run instead of becoming a failed retrieval.
    """
    from .census_runner import RetryableFetch
    state = threading.local()

    def fetch(pmcid: str) -> bytes | None:
        error = "no attempt"
        for attempt in range(max(1, attempts)):
            wait = getattr(state, "last", 0.0) + spacing - time.monotonic()
            if wait > 0:
                sleep(wait)
            state.last = time.monotonic()
            try:
                with (opener or urllib.request.urlopen)(EUROPE_PMC_FULL_TEXT % pmcid, timeout=90) as response:
                    body = response.read()
                    status = getattr(response, "status", 200)
                if status == 200:
                    return body if body.strip() else None
                if status in (404, 410):
                    return None
                error = f"HTTP {status}"
            except urllib.error.HTTPError as failure:
                if failure.code in (404, 410) or (400 <= failure.code < 500 and failure.code not in (408, 425, 429)):
                    return None
                error = f"HTTP {failure.code}"
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as failure:
                error = type(failure).__name__
            if attempt + 1 < attempts:
                sleep(backoff[min(attempt, len(backoff) - 1)])
        raise RetryableFetch(f"{pmcid}: {error}")
    return fetch


def gates(status: dict[str, Any]) -> dict[str, Any]:
    responded = status["receiptsWithResponse"]
    checks = {
        "invalidOutputsAtMostOnePercent": status["invalidShare"] is not None and status["invalidShare"] <= 0.01,
        "transportFailuresAtMostHalfPercent": status["transportFailureShare"] is None or status["transportFailureShare"] <= 0.005,
        "uncoveredIsZero": status["uncoveredReviews"] == 0,
        "noDuplicateJudgments": status["duplicateJudgments"] == 0,
        # Over every receipt that received a response (all tiers and variants); failed calls have none to carry.
        "everyReceiptHasFingerprint": responded > 0 and status["receiptsWithFingerprint"] == responded,
        "abstractTierComplete": status["openEntries"] == 0 and not status["reviews"].get("pending")
        and not str(status["runState"]).startswith("paused"),
        "fullTextTierComplete": status["openFullTextEntries"] == 0 and not status["fullTextReviews"].get("pending"),
    }
    notes = []
    if responded and not status["fingerprintOffered"]:
        notes.append("the judge server offered no system_fingerprint on any response, so the fingerprint gate cannot "
                     "pass with this server")
    if status.get("instrumentDigestMatches") is False:
        notes.append("the database is pinned to a different instrument digest than the installed code")
    return {"ok": all(checks.values()), "checks": checks, "notes": notes, "status": status}


def fill_abstracts(source: Path, output: Path, get: Any, *, overwrite: bool = False) -> dict[str, Any]:
    """Stream records through the Europe PMC ladder into ``<output>.partial``; resume it; rename when complete."""
    import json
    import os
    from .census_abstracts import fill_record
    from .census_runner import abstract_lookup_failed, iter_records
    if output.resolve() == source.resolve():
        raise ValidationError("write enriched records to a new file")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(output) + ".partial")
    done: list[tuple[Any, Any]] = []
    if partial.exists():
        kept = 0
        with partial.open("rb") as handle:
            for raw in handle:
                try:
                    value = json.loads(raw)
                except ValueError:
                    break               # a line cut off by a crash: rewrite from here
                if not raw.endswith(b"\n"):
                    break
                done.append((value.get("journal"), value.get("doi")))
                kept += len(raw)
        with partial.open("rb+") as handle:   # drop a cut-off tail before appending
            handle.truncate(kept)
    sources: dict[str, int] = {}
    counts = {"records": 0, "resumed": len(done), "lookupFailed": 0}
    with partial.open("a", encoding="utf-8") as handle:
        for index, record in enumerate(iter_records(source)):
            if index < len(done):
                if (record.get("journal"), record.get("doi")) != done[index]:
                    raise ValidationError(f"{partial} does not continue this input (record {index + 1} differs)")
                filled = record
            else:
                filled = fill_record(record, get, overwrite=overwrite)
                handle.write(json.dumps(filled, ensure_ascii=False) + "\n")
                handle.flush()
                counts["lookupFailed"] += bool(abstract_lookup_failed(filled))
                for work in filled.get("works", {}).values():
                    name = work.get("abstractSource") or "none"
                    sources[name] = sources.get(name, 0) + 1
            counts["records"] += 1
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, output)
    return {**counts, "output": str(output), "abstractSources": sources,
            "next": "run study abstracts again on the output for works whose lookup failed" if counts["lookupFailed"] else None}


def prepare(run: Any, source: Path) -> dict[str, Any]:
    """Prepare reviews one record at a time; a review that cannot be read or extracted is recorded, not fatal."""
    from .census_runner import abstract_lookup_failed, iter_records, read_document
    counts: dict[str, int] = {}
    problems = []
    for record in iter_records(source):
        failed = abstract_lookup_failed(record)
        if failed:
            result = {"doi": record.get("doi"), "state": "skipped-abstract-lookup-failed", "works": failed[:20]}
        else:
            try:
                document = read_document(source.parent, record)
            except (OSError, ValidationError) as error:
                result = run.record_unavailable(record, f"{type(error).__name__}: {error}")
            else:
                try:
                    result = run.prepare_review(record, document)
                except ValidationError as error:
                    # An inconsistent record (unknown work, changed document): kept in coverage with its reason.
                    result = {**run.record_unavailable(record, f"input refused: {error}"), "state": "input-refused"}
        counts[result["state"]] = counts.get(result["state"], 0) + 1
        if result["state"] not in ("prepared", "already-prepared"):
            problems.append(result)
    return {"prepared": counts.get("prepared", 0), "states": counts, "problems": problems}


def command(args) -> Any:
    action = args.study_command
    if action == "abstracts":
        from .census_abstracts import http_get
        return fill_abstracts(Path(args.input), Path(args.output), http_get(), overwrite=args.overwrite)
    if action == "prepare":
        run = _run(args)
        try:
            return prepare(run, Path(args.input))
        finally:
            run.close()
    if action in ("judge", "full-text", "no-truncation", "replicate"):
        run = _run(args, _transport(args))
        passes = {"transport_retries": args.transport_retries, "cooldown": args.transport_cooldown}
        try:
            if action == "judge":
                return run.run_abstract(limit=args.limit, **passes)
            if action == "full-text":
                return run.run_full_text(fetch_xml=europe_pmc_fetcher() if args.europe_pmc else None,
                                         fetch_workers=args.fetch_workers, retry_failed=args.retry_failed, **passes)
            if action == "replicate":
                return run.run_replicate(entries=args.entries, **passes)
            return run.run_no_truncation(entries=args.entries, **passes)
        finally:
            run.close()
    if action in ("status", "gates"):
        # Read-only: works on the frozen database and during a run, and never pins run metadata.
        from . import citation_census, scores_db
        from .census_runner import census_status
        with closing(scores_db.open_readonly(Path(args.database))) as connection:
            status = census_status(connection, code_instrument_digest=citation_census.instrument().digest)
        return status if action == "status" else gates(status)
    if action == "freeze":
        from .scores_db import freeze
        return freeze(Path(args.database), Path(args.output), keep_text=args.keep_text,
                      allow_incomplete=args.allow_incomplete)
    if action == "export":
        from .scores_db import export
        return export(Path(args.scores), Path(args.output), parquet=not args.no_parquet,
                      allow_entitled_text=args.allow_entitled_text)
    if action in ("estimands", "facts"):
        trend = (args.trend_from, args.trend_to) if args.trend_from is not None and args.trend_to is not None else None
        if (args.trend_from is None) != (args.trend_to is None):
            raise ValidationError("give both --trend-from and --trend-to")
        if action == "estimands":
            from . import estimands, scores_db
            with closing(scores_db.open_frozen(Path(args.scores))) as connection:
                return estimands.report(connection, args.journal, b=args.b, seed=args.bootstrap_seed,
                                        ratio_pair=tuple(args.ratio) if args.ratio else None, trend_years=trend)
        from .corpus_facts import export_facts
        result = export_facts(Path(args.scores), Path(args.output), args.journal, b=args.b, seed=args.bootstrap_seed,
                              ratio_pair=tuple(args.ratio) if args.ratio else None, trend_years=trend,
                              allow_incomplete=args.allow_incomplete)
        result.pop("report", None)
        return result
    if action == "verify-facts":
        from .corpus_facts import verify_export
        return verify_export(Path(args.facts))
    if action == "cross-validate":
        from .census_extraction import cross_validate, cross_validation_gate, extract_review, uncovered_gate
        jats = extract_review(Path(args.jats).read_bytes())
        html = extract_review(Path(args.html).read_bytes(), html=True)
        result = cross_validate(jats, html)
        return {**result, "gate": cross_validation_gate([result], minimum_median=args.minimum_share),
                "uncovered": {"jats": uncovered_gate(jats), "html": uncovered_gate(html)}}
    raise ValidationError(f"unknown study command: {action}")
