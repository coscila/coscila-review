# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review instruments`` and ``ocsl-review study``: instrument registry and census study commands.

Controllers call these commands instead of writing drivers: prepare reviews
from a manifest, run the abstract and full-text tiers, the predeclared
no-truncation sensitivity, gates, freezing, exports, estimands and facts.
"""
from __future__ import annotations

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
    for name in ("prepare", "judge", "full-text", "no-truncation", "status", "gates", "freeze", "export",
                 "estimands", "facts", "verify-facts", "cross-validate"):
        command = actions.add_parser(name)
        if name in ("prepare", "judge", "full-text", "no-truncation", "status", "gates", "freeze"):
            command.add_argument("--database", required=True, help="working census database")
        if name in ("prepare", "judge", "full-text", "no-truncation"):
            command.add_argument("--seed", type=int, required=True, help="protocol seed")
        if name == "prepare":
            command.add_argument("--input", required=True, help="JSONL review records; document paths are relative to it")
        if name in ("judge", "full-text", "no-truncation"):
            command.add_argument("--base-url", required=True, help="chat-completions base URL of the judge")
            command.add_argument("--api-key-env", help="environment variable holding the judge key")
            command.add_argument("--model", help="model name to request (defaults to the required served model)")
            command.add_argument("--concurrency", type=int, default=60)
            command.add_argument("--on-premises", action="store_true", help="the judge runs on premises (entitled text allowed)")
        if name == "judge":
            command.add_argument("--limit", type=int)
        if name == "full-text":
            command.add_argument("--europe-pmc", action="store_true", help="fetch Europe PMC fullTextXML by PMCID")
            command.add_argument("--fetch-workers", type=int, default=4)
        if name == "no-truncation":
            command.add_argument("--entries", type=int)
        if name == "freeze":
            command.add_argument("--output", required=True)
            command.add_argument("--keep-text", action="store_true")
        if name in ("export", "estimands", "facts"):
            command.add_argument("--scores", required=True, help="frozen scores database")
        if name in ("export", "facts"):
            command.add_argument("--output", required=True)
        if name == "export":
            command.add_argument("--no-parquet", action="store_true")
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


def europe_pmc_fetcher(spacing: float = 0.4):
    """Fetch fullTextXML by PMCID; each fetching thread pauses ``spacing`` seconds between its requests."""
    state = threading.local()

    def fetch(pmcid: str) -> bytes | None:
        wait = getattr(state, "last", 0.0) + spacing - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        state.last = time.monotonic()
        try:
            with urllib.request.urlopen(EUROPE_PMC_FULL_TEXT % pmcid, timeout=90) as response:
                body = response.read()
                return body if response.status == 200 and body.strip() else None
        except (urllib.error.URLError, OSError):
            return None
    return fetch


def gates(status: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "invalidOutputsAtMostOnePercent": status["invalidShare"] is not None and status["invalidShare"] <= 0.01,
        "transportFailuresAtMostHalfPercent": status["transportFailureShare"] is None or status["transportFailureShare"] <= 0.005,
        "uncoveredIsZero": status["uncoveredReviews"] == 0,
        "noDuplicateJudgments": status["duplicateJudgments"] == 0,
        "everyReceiptHasFingerprint": status["abstractCalls"] > 0 and status["receiptsWithFingerprint"] == status["abstractCalls"],
    }
    return {"ok": all(checks.values()), "checks": checks, "status": status}


def command(args) -> Any:
    from .census_runner import load_records, read_document
    action = args.study_command
    if action == "prepare":
        run = _run(args)
        try:
            source = Path(args.input)
            results = [run.prepare_review(record, read_document(source.parent, record)) for record in load_records(source)]
        finally:
            run.close()
        return {"prepared": sum(r["state"] == "prepared" for r in results), "results": results}
    if action in ("judge", "full-text", "no-truncation"):
        run = _run(args, _transport(args))
        try:
            if action == "judge":
                return run.run_abstract(limit=args.limit)
            if action == "full-text":
                return run.run_full_text(fetch_xml=europe_pmc_fetcher() if args.europe_pmc else None,
                                         fetch_workers=args.fetch_workers)
            return run.run_no_truncation(entries=args.entries)
        finally:
            run.close()
    if action in ("status", "gates"):
        from . import scores_db
        from .census_runner import CensusRun
        with closing(scores_db.connect(Path(args.database))) as connection:
            seed = int(scores_db.meta(connection).get("seed", "0"))
        run = CensusRun(Path(args.database), seed=seed)
        try:
            status = run.status()
        finally:
            run.close()
        return status if action == "status" else gates(status)
    if action == "freeze":
        from .scores_db import freeze
        return freeze(Path(args.database), Path(args.output), keep_text=args.keep_text)
    if action == "export":
        from .scores_db import export
        return export(Path(args.scores), Path(args.output), parquet=not args.no_parquet)
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
                              ratio_pair=tuple(args.ratio) if args.ratio else None, trend_years=trend)
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
