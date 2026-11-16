# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review followup``: a compromised-evidence dependency audit on a branch of a frozen census.

Every command works on one follow-up branch database (``--database``) and returns one JSON result; with the global
``--result-file`` the companion writes it as ``ocsl-review-result-v1``, so a Study Run step can run the command and
read its outcome. Judging commands take the study commands' judge options (``--model``, ``--base-url``,
``--broker-socket``, ``--on-premises --policy``, ``--concurrency``, ``--transport-retries``, ``--transport-cooldown``).

This module registers the family and its argument lists, the command contract that Study Run steps build against. It
implements the branch commands (``init``, ``pin-bundle``, ``verify``, ``describe``, ``gates``); each evidence command
(status observations, support and function judging, dependency, cited-by traversal, facts and exports) and each
analysis command (hotspot test, strata, numeric checks, second-generation matching, retraction lag and estimands) runs
the function of its implementing module that ``VERBS`` names.
"""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

from .errors import ValidationError

INVALID_SHARE_BOUND = 0.01
# Evidence and analysis commands: the implementing module and function of each.
VERBS = {
    "status-import": ("followup_status", "cmd_import"),
    "status-fetch": ("followup_status", "cmd_fetch"),
    "status-resolve": ("followup_status", "cmd_resolve"),
    "support": ("followup_support", "cmd_support"),
    "function": ("followup_function", "cmd_function"),
    "dependency": ("followup_dependency", "cmd_dependency"),
    "citedby": ("followup_citedby", "cmd_citedby"),
    "facts": ("followup_facts", "cmd_facts"),
    "verify-facts": ("followup_facts", "cmd_verify_facts"),
    "export": ("followup_exports", "cmd_export"),
    "hotspots": ("followup_stats", "cmd_hotspots"),
    "strata": ("followup_strata", "cmd_strata"),
    "numeric": ("followup_numeric", "cmd_numeric"),
    "second-gen": ("followup_secondgen", "cmd_second_gen"),
    "lag": ("followup_stats", "cmd_lag"),
    "estimands": ("followup_estimands", "cmd_estimands"),
}
READ_ONLY = frozenset({"verify-facts"})
JUDGING = frozenset({"support", "function", "numeric", "second-gen"})


def judge_arguments(parser: Any) -> None:
    """The judge options every judging command shares (the study commands' names, so Study Run steps reuse them)."""
    parser.add_argument("--model", help="served model the protocol pins (model.served)")
    parser.add_argument("--base-url", help="chat-completions base URL of a judge called directly")
    parser.add_argument("--api-key-env", help="environment variable holding the direct judge's key")
    parser.add_argument("--broker-socket", help="the run principal's broker socket (default: OCSL_BROKER_SOCKET)")
    parser.add_argument("--broker-worker", help="bulk worker of the run principal (needed when it offers several)")
    parser.add_argument("--concurrency", type=int, default=8, help="calls in flight")
    parser.add_argument("--on-premises", action="store_true",
                        help="the judge runs on premises (entitled text allowed); needs --policy naming it")
    parser.add_argument("--policy", help="operator egress policy (JSON, version 2) listing on-premises judges")
    parser.add_argument("--transport-retries", type=int, default=3, help="further passes over items left open")
    parser.add_argument("--transport-cooldown", type=float, default=60.0, help="seconds before the first further pass")
    parser.add_argument("--variant", choices=("primary", "replicate"), default="primary")
    parser.add_argument("--scope", choices=("pilot", "all"), default="all",
                        help="pilot: only units of the frozen pilot subsample")
    parser.add_argument("--public-only", action="store_true",
                        help="send only items with public evidence (for a judge that is not on premises)")
    parser.add_argument("--limit", type=int, help="at most this many items (a smoke test)")


def add_commands(families: Any, register: Any) -> None:
    family = families.add_parser("followup", help="compromised-evidence dependency audit on a branch of a frozen census")
    commands = family.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="make a follow-up branch over a frozen census scores database")
    init.add_argument("--database", required=True, help="new follow-up branch database")
    init.add_argument("--parent", required=True, help="frozen census scores database (absolute path; read-only)")
    init.add_argument("--parent-text", help="private frozen copy that keeps entitled citing sentences (--keep-text)")
    init.add_argument("--protocol", help="follow-up protocol (JSON, schemas/followup-protocol.schema.json)")
    init.add_argument("--allow-incomplete-parent", action="store_true",
                      help="branch a census frozen as incomplete (recorded; for tests and pilots only, never a study "
                           "run)")
    init.add_argument("--study-bundle", help="study bundle file (ocsl-study-bundle-v1) or its sha256, pinned before "
                                             "the first judge call and recorded in every receipt")
    register(init, "followup_cli", "command", "workspace", state_required=False)
    pin = commands.add_parser("pin-bundle", help="pin the study bundle before the first judge call")
    pin.add_argument("--database", required=True)
    pin.add_argument("--study-bundle", required=True,
                     help="study bundle file (ocsl-study-bundle-v1) or its sha256")
    register(pin, "followup_cli", "command", "workspace", state_required=False)
    verify = commands.add_parser("verify", help="parent digest unchanged, schema intact, every reference resolvable")
    verify.add_argument("--database", required=True)
    verify.add_argument("--light", action="store_true", help="compare size and modification time instead of the sha256")
    register(verify, "followup_cli", "command", "read-only", state_required=False)
    describe = commands.add_parser("describe", help="branch metadata, dataset versions, freezes and row counts")
    describe.add_argument("--database", required=True)
    register(describe, "followup_cli", "command", "read-only", state_required=False)
    gates = commands.add_parser("gates", help="process-validity checks of the follow-up run")
    gates.add_argument("--database", required=True)
    register(gates, "followup_cli", "command", "read-only", state_required=False)
    parsers = {}
    parsers["status-import"] = commands.add_parser(
        "status-import", help="import a Retraction Watch CSV snapshot (a dataset input) as status observations")
    parsers["status-import"].add_argument("--csv", required=True, help="the CSV snapshot file (read locally, no network)")
    parsers["status-import"].add_argument("--declared-version", required=True,
                                          help="the snapshot's declared version (for example its publication date)")
    parsers["status-fetch"] = commands.add_parser(
        "status-fetch", help="observe the status of every cited work at Crossref, PubMed and Europe PMC")
    parsers["status-fetch"].add_argument("--source", action="append", choices=("crossref", "pubmed", "europe-pmc"),
                                         help="sources to ask (default: the protocol's live sources)")
    parsers["status-fetch"].add_argument("--limit", type=int, help="at most this many works (a smoke test)")
    parsers["status-fetch"].add_argument("--workspace", help="workspace whose egress ledger records the requests")
    parsers["status-fetch"].add_argument("--snapshot", help="continue this status snapshot instead of starting one")
    parsers["status-fetch"].add_argument("--continue", dest="continue_open", action="store_true",
                                         help="continue the latest snapshot that did not finish (a fixed Study Run "
                                              "step passes it; a new snapshot starts when none is open)")
    parsers["status-resolve"] = commands.add_parser(
        "status-resolve", help="one status row (source and date) for every cited work of the parent census")
    parsers["status-resolve"].add_argument("--snapshot", action="append",
                                           help="status snapshots to resolve (default: the latest live snapshot and "
                                                "the latest Retraction Watch import)")
    parsers["support"] = commands.add_parser(
        "support", help="support verdicts (the protocol's support instrument: citation-support@2, or "
                        "citation-support-verbatim@1, which also runs through the broker) for the frozen strata at one "
                        "evidence tier")
    parsers["support"].add_argument("--tier", required=True, choices=("abstract", "full-text"))
    parsers["support"].add_argument("--store", help="literature-evidence@2 projection read as a byte cache")
    parsers["support"].add_argument("--store-root", help="Merkle root the store must have")
    parsers["support"].add_argument("--provider-cache", help="provider response cache read as a byte cache")
    parsers["support"].add_argument("--documents", help="directory of the parent census's review documents, for the "
                                                        "sentences before and after each citing sentence")
    parsers["support"].add_argument("--entitled-documents",
                                    help="directory of entitled full texts the study supplies (entitled-documents.json; "
                                         "on-premises judges only)")
    parsers["support"].add_argument("--europe-pmc", action="store_true",
                                    help="fetch open full text from Europe PMC for works the caches do not hold")
    parsers["support"].add_argument("--fetch-workers", type=int, default=4)
    parsers["support"].add_argument("--retry-failed", action="store_true",
                                    help="try again for works recorded as fetch-failed")
    parsers["support"].add_argument("--workspace", help="workspace whose egress ledger records the requests")
    parsers["function"] = commands.add_parser(
        "function", help="citation-function@1 classes for the loci of the frozen strata")
    parsers["function"].add_argument("--documents", help="directory of the parent census's review documents, for the "
                                                         "sentences before and after each citing sentence")
    parsers["dependency"] = commands.add_parser(
        "dependency", help="sole, essential, redundant or undetermined support per S1 and S2 locus")
    parsers["dependency"].add_argument("--scope", choices=("pilot", "all"), default="all",
                                       help="pilot: (re)compute only the rows of the pilot anchors")
    parsers["citedby"] = commands.add_parser(
        "citedby", help="works citing the affected reviews (Europe PMC citations, OpenAlex when available)")
    parsers["citedby"].add_argument("--source", action="append", choices=("europe-pmc", "openalex"),
                                    help="sources to ask (default: the protocol's second-generation sources)")
    parsers["citedby"].add_argument("--limit", type=int, help="at most this many targets (a smoke test)")
    parsers["citedby"].add_argument("--workspace", help="workspace whose egress ledger records the requests")
    parsers["citedby"].add_argument("--widen", action="store_true",
                                    help="traverse every review with an S1 locus, not only the affected reviews")
    parsers["citedby"].add_argument("--max-pages", type=int, default=100,
                                    help="pages per target and source before the traversal counts as truncated")
    parsers["facts"] = commands.add_parser("facts", help="facts v1 with per-fact SQL over a closed snapshot")
    parsers["facts"].add_argument("--output", required=True, help="new directory for facts.sqlite3 and facts.json")
    parsers["verify-facts"] = commands.add_parser("verify-facts", help="postflight of a follow-up facts export")
    parsers["verify-facts"].add_argument("--facts", required=True)
    parsers["export"] = commands.add_parser(
        "export", help="compromised-claim database, per-article alert records and the propagation graph")
    parsers["export"].add_argument("--output", required=True, help="new directory for the exports")
    parsers["export"].add_argument("--allow-entitled-text", action="store_true",
                                   help="keep entitled text in the exports (a private export; never share it)")
    parsers["hotspots"] = commands.add_parser(
        "hotspots", help="works cited without support beyond the journal x year base (beta-binomial, BH-FDR)")
    parsers["strata"] = commands.add_parser(
        "strata", help="build the frame and strata S0-S3 with co-cited loci and inclusion probabilities")
    parsers["strata"].add_argument("--freeze", action="store_true",
                                   help="freeze strata, sizes, estimands and denominators into the protocol digest "
                                        "(required before any follow-up judging)")
    parsers["numeric"] = commands.add_parser(
        "numeric", help="numeric-check@1 parse of S3 claims and the deterministic comparison with a table source")
    parsers["numeric"].add_argument("--tables", required=True,
                                    help="table source (a pinned dataset version; synthetic or licence-verified)")
    parsers["numeric"].add_argument("--allow-synthetic-tables", action="store_true",
                                    help="accept a table source whose licensing is synthetic (tests only, never a "
                                         "study run)")
    parsers["second-gen"] = commands.add_parser(
        "second-gen", help="sample citing articles, acquire them, rank candidate sentences (BM25), confirm (claim-match@1)")
    parsers["second-gen"].add_argument("--stage", choices=("sample", "acquire", "candidates", "judge", "all"),
                                       default="all")
    parsers["second-gen"].add_argument("--workspace", help="workspace whose egress ledger records the requests")
    parsers["lag"] = commands.add_parser("lag", help="retraction-lag rows and the lag model")
    parsers["estimands"] = commands.add_parser(
        "estimands", help="the predeclared estimands with coverage tables (weighted, review-clustered bootstrap)")
    parsers["estimands"].add_argument("--b", type=int, help="bootstrap replicates (default: the protocol's; another "
                                                            "value makes exploratory estimates, never a study run)")
    parsers["estimands"].add_argument("--bootstrap-seed", type=int,
                                      help="bootstrap seed (default: the protocol's; as --b)")
    parsers["estimands"].add_argument("--scope", choices=("pilot", "all"), default="all",
                                      help="pilot: the judged estimands over the pilot subsample, weighted by the "
                                           "pilot probabilities, under followup.pilot.* keys")
    for name, parser in parsers.items():
        if name != "verify-facts":
            parser.add_argument("--database", required=True, help="follow-up branch database")
        if name in JUDGING:
            judge_arguments(parser)
        module, function = VERBS[name]
        register(parser, module, function, "read-only" if name in READ_ONLY else "workspace", state_required=False)


def gates(database: Path) -> dict[str, Any]:
    """Checks a Study Run can require: parent unchanged, references resolve, a status row for every cited work, strata
    frozen before any judging, invalid outputs within the bound, fingerprints, one digest per instrument, no entitled
    evidence sent to a judge that is not on premises."""
    from .followup_branch import FollowupBranch, instrument_pins
    branch = FollowupBranch(database)
    verified = branch.verify(deep=True)
    notes = list(verified["problems"]) + list(verified.get("notes") or [])
    with closing(branch.connect(check_parent=False)) as connection:
        first_call = connection.execute("SELECT MIN(started_at) FROM fu_calls").fetchone()[0]
        first_freeze = connection.execute("SELECT MIN(frozen_at) FROM fu_protocol_freeze").fetchone()[0]
        invalid = connection.execute("""
          SELECT instrument_digest, tier, variant, COUNT(DISTINCT item_key) AS decided,
                 COUNT(DISTINCT CASE WHEN outcome='rejected' AND item_key NOT IN (
                   SELECT a.item_key FROM fu_judgments a WHERE a.instrument_digest=j.instrument_digest AND a.tier=j.tier
                   AND a.variant=j.variant AND a.outcome='accepted') THEN item_key END) AS invalid
          FROM fu_judgments j WHERE outcome IN ('accepted','rejected') GROUP BY instrument_digest, tier, variant""").fetchall()
        responded = connection.execute("SELECT COUNT(*), SUM(fingerprint IS NOT NULL) FROM fu_calls "
                                       "WHERE served_model IS NOT NULL").fetchone()
        # Every variant counts: a replicate under another digest would compare two instruments.
        digests = connection.execute("SELECT instrument_id, COUNT(DISTINCT instrument_digest) FROM fu_calls "
                                     "GROUP BY instrument_id").fetchall()
        meta = dict(connection.execute("SELECT key, value FROM fu_meta").fetchall())
        bundle = meta.get("studyBundleSha256")
        without_bundle = connection.execute(
            "SELECT COUNT(*) FROM fu_calls WHERE study_bundle_sha256 IS NULL OR study_bundle_sha256!=?",
            (bundle or "",)).fetchone()[0]
        pins = instrument_pins(connection)
        replicated = {tuple(row) for row in connection.execute(
            "SELECT DISTINCT instrument_digest, tier FROM fu_judgments WHERE variant='replicate' AND outcome='accepted'")}
        agreement, settings = set(), set()
        for estimand, detail in connection.execute("SELECT DISTINCT estimand, detail_json FROM fu_estimates"):
            value = json.loads(detail or "{}")
            if not isinstance(value, dict):
                continue
            if estimand == "replicate-agreement":
                agreement.add((value.get("instrumentDigest"), value.get("tier")))
            if value.get("b") is not None:
                settings.add((int(value["b"]), str(value.get("seed"))))
        off_pin = sum(count for instrument_id, digest, count in connection.execute(
            "SELECT instrument_id, instrument_digest, COUNT(*) FROM fu_calls GROUP BY instrument_id, instrument_digest")
            if any(pinned != digest for pinned in pins.get(instrument_id, {}).values()))
        entitled_external = connection.execute(
            "SELECT COUNT(*) FROM fu_calls WHERE provenance_classes LIKE '%\"entitled\"%' AND judge_location!='on-prem'"
        ).fetchone()[0]
        drift = connection.execute("SELECT COUNT(*) FROM fu_calls WHERE outcome='model-drift'").fetchone()[0]
    shares = {f"{row['instrument_digest'][:12]}:{row['tier']}:{row['variant']}":
              (row["invalid"] / row["decided"] if row["decided"] else None) for row in invalid}
    checks = {
        "parentDigestUnchanged": verified["parent"].get("parentSha256") == verified["parent"]["recordedSha256"],
        "referencesResolve": not any(verified["references"].values()),
        "everyCitedWorkHasStatusRow": verified["citedWorksWithoutStatusRow"] == 0,
        "strataFrozenBeforeJudging": first_freeze is not None and (first_call is None or first_freeze <= first_call),
        "invalidOutputsWithinBound": all(share is None or share <= INVALID_SHARE_BOUND for share in shares.values()),
        "everyReceiptHasFingerprint": (responded[0] or 0) > 0 and (responded[1] or 0) == responded[0],
        "oneDigestPerInstrument": all(count == 1 for _, count in digests),
        "noEntitledEvidenceToExternalJudges": entitled_external == 0,
        "noModelDrift": drift == 0,
        "parentComplete": json.loads(meta.get("parentMeta") or "{}").get("complete") == "1",
    }
    if bundle:
        checks["everyReceiptCarriesTheStudyBundle"] = without_bundle == 0
    if pins:
        checks["instrumentDigestsAsPinned"] = off_pin == 0
    if replicated:
        # The replicate gate (as in the census): agreement with the primary verdicts reported for every replicated
        # instrument and tier (followup estimands writes it).
        checks["replicateAgreementReported"] = replicated <= agreement
    if settings:
        # Every estimate with the protocol's predeclared bootstrap replicates and seed (no --b or --bootstrap-seed).
        declared = json.loads(meta.get("protocol") or "{}").get("bootstrap") or {}
        checks["bootstrapAsProtocol"] = settings == {(declared.get("b"), str(declared.get("seed")))}
    if not checks["parentComplete"]:
        notes.append("the parent census records an incomplete run (followup init --allow-incomplete-parent)")
    if not responded[0]:
        notes.append("no judge call has a response yet")
    return {"ok": all(checks.values()), "checks": checks, "invalidShares": shares, "notes": notes,
            "references": verified["references"]}


def command(args: Any) -> Any:
    from .followup_branch import FollowupBranch
    if args.command == "init":
        from .census_runner import code_revision
        return FollowupBranch.create(Path(args.database), Path(args.parent).absolute(),
                                     parent_text=Path(args.parent_text).absolute() if args.parent_text else None,
                                     protocol=args.protocol, allow_incomplete=args.allow_incomplete_parent,
                                     code_revision=code_revision(), study_bundle=getattr(args, "study_bundle", None))
    if args.command == "pin-bundle":
        from .followup_branch import pin_study_bundle
        with FollowupBranch(Path(args.database)).writing() as connection:
            return pin_study_bundle(connection, args.study_bundle)
    if args.command == "verify":
        report = FollowupBranch(Path(args.database)).verify(deep=not args.light)
        if not report["ok"]:
            raise ValidationError("follow-up branch verification failed: " + "; ".join(report["problems"]))
        return report
    if args.command == "describe":
        return FollowupBranch(Path(args.database)).describe()
    if args.command == "gates":
        return gates(Path(args.database))
    raise ValidationError(f"unknown followup command: {args.command}")
