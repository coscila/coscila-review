# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review followup``: a compromised-evidence dependency audit on a branch of a frozen census.

Every command works on one follow-up branch database (``--database``) and returns one JSON result; with the global
``--result-file`` the companion writes it as ``ocsl-review-result-v1``, so a Study Run step can run the command and
read its outcome. Judging commands take the study commands' judge options (``--model``, ``--base-url``,
``--broker-socket``, ``--on-premises --policy``, ``--concurrency``, ``--transport-retries``, ``--transport-cooldown``).

This module registers the family and owns the branch commands (``init``, ``verify``, ``describe``, ``gates``); the
evidence commands are registered by ``followup_cli_a`` and the analysis commands by ``followup_cli_b``. A command whose
implementing module is not part of the build fails with a usage error; it never reports success.
"""
from __future__ import annotations

import importlib
import json
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from .errors import ReviewError, ValidationError

INVALID_SHARE_BOUND = 0.01


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


def dispatch(verbs: Mapping[str, tuple[str, str]], args: Any) -> Any:
    """Call the implementing module of a command; a module missing from the build is a usage error."""
    module_name, function = verbs[args.command]
    qualified = f"ocsl_review.{module_name}"
    try:
        module = importlib.import_module(qualified)
    except ModuleNotFoundError as error:
        if error.name != qualified:
            raise
        raise ReviewError(f"followup {args.command} is not implemented in this build ({module_name})") from error
    handler = getattr(module, function, None)
    if not callable(handler):
        raise ReviewError(f"followup {args.command} is not implemented in this build ({module_name}.{function})")
    return handler(args)


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
    from .followup_cli_a import add_commands as add_evidence_commands
    from .followup_cli_b import add_commands as add_analysis_commands
    add_evidence_commands(commands, register)
    add_analysis_commands(commands, register)


def gates(database: Path) -> dict[str, Any]:
    """Checks a Study Run can require: parent unchanged, references resolve, a status row for every cited work, strata
    frozen before any judging, invalid outputs within the bound, fingerprints, one digest per instrument, no entitled
    evidence sent to a judge that is not on premises."""
    from .followup_branch import FollowupBranch, instrument_pins
    branch = FollowupBranch(database)
    verified = branch.verify(deep=True)
    notes = list(verified["problems"])
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
        agreement = set()
        for (detail,) in connection.execute("SELECT detail_json FROM fu_estimates WHERE estimand='replicate-agreement'"):
            value = json.loads(detail)
            agreement.add((value.get("instrumentDigest"), value.get("tier")))
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
