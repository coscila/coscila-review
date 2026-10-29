# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review instruments`` and ``ocsl-review study``: instrument registry and census study commands.

Controllers call these commands instead of writing drivers: enumerate the
population live, acquire review documents, derive cited works from each
review's own bibliography, look up cited-work abstracts and metadata from the
study's byte caches or live routes, record protocol exclusions, prepare reviews
from a manifest (pinning the study bundle), run the abstract and full-text
tiers directly or through the run principal's broker socket, the predeclared
no-truncation sensitivity, gates, freezing, exports, estimands and facts, and
compare a stored manifest with the fresh enumeration afterwards.
``instruments export`` prints the broker bundle of a verbatim instrument.
"""
from __future__ import annotations

import hashlib
import http.client
import os
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
    instruments = families.add_parser("instruments", help="list, show, verify and export measurement instruments")
    commands = instruments.add_subparsers(dest="instrument_command", required=True)
    for name in ("list", "show", "verify", "export"):
        command = commands.add_parser(name)
        if name in ("show", "export"):
            command.add_argument("instrument")
        if name == "verify":
            command.add_argument("instrument", nargs="?")
        if name == "export":
            command.add_argument("--tier", help="instrument tier (required when the instrument has several)")
            command.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                                 help="instrument parameter, for example journal=NAME (the reviews' journal)")
            command.add_argument("--body", action="store_true",
                                 help="print only the bundle: the exact body of PUT /v2/instruments/{digest}")
        register(command, "instruments", f"cmd_{name}", "read-only", state_required=False)

    study = families.add_parser("study", help="run a census study with a registered instrument")
    actions = study.add_subparsers(dest="study_command", required=True)
    for name in ("enumerate", "acquire", "works", "abstracts", "exclude", "prepare", "judge", "full-text",
                 "no-truncation", "replicate", "status", "gates", "freeze", "export", "estimands", "facts", "verify-facts",
                 "cross-validate", "compare-manifest"):
        command = actions.add_parser(name)
        if name in ("exclude", "prepare", "judge", "full-text", "no-truncation", "replicate", "status", "gates", "freeze",
                    "compare-manifest"):
            command.add_argument("--database", required=True, help="working census database")
        if name in ("prepare", "judge", "full-text", "no-truncation", "replicate"):
            command.add_argument("--seed", type=int, required=True, help="protocol seed")
        if name in ("acquire", "abstracts", "prepare", "full-text"):
            command.add_argument("--store", help="literature-evidence@2 projection read as a byte cache (absolute path)")
            command.add_argument("--store-root", help="Merkle root the store must have (the pinned dataset version)")
            command.add_argument("--provider-cache", help="provider response cache database read as a byte cache")
        if name == "enumerate":
            command.add_argument("--spec", required=True, help="enumeration spec (JSON): groups of journal, query, stratum")
            command.add_argument("--output", required=True, help="JSONL review manifest; pages and receipt are kept beside it")
        if name == "acquire":
            command.add_argument("--input", required=True, help="JSONL review records (for example a fresh enumeration)")
            command.add_argument("--output", required=True,
                                 help="JSONL review records with documents; documents go to documents/ beside it")
            command.add_argument("--route", action="append", default=[],
                                 help="JOURNAL=public|entitled[:jats,html]: live route and document order of a journal")
            command.add_argument("--live", action="store_true",
                                 help="acquire documents the caches do not hold through the journal's live route")
            command.add_argument("--retry-unavailable", action="store_true",
                                 help="try again for reviews an earlier run recorded as unavailable")
        if name == "works":
            command.add_argument("--input", required=True, help="JSONL review records with documents (study acquire output)")
            command.add_argument("--output", required=True,
                                 help="JSONL review records with cited works derived from each review's bibliography "
                                      "(keep it beside the input so document paths resolve)")
        if name == "exclude":
            command.add_argument("--input", required=True,
                                 help="JSONL of enumerated reviews not in the study: journal, doi and the protocol reason")
        if name == "gates":
            command.add_argument("--require-fresh-enumeration", action="store_true",
                                 help="also require that the study is pinned to a verified fresh enumeration and that "
                                      "every enumerated review is in the study or excluded")
        if name == "prepare":
            command.add_argument("--input", required=True, help="JSONL review records; document paths are relative to it")
            command.add_argument("--study-bundle",
                                 help="study bundle file (ocsl-study-bundle-v1) or its sha256, pinned before the first "
                                      "call and recorded in every call receipt")
            command.add_argument("--require-fresh-enumeration", action="store_true",
                                 help="prepare only reviews of a verified live enumeration, acquired with provenance")
            command.add_argument("--enumeration", help="enumeration receipt (<manifest>.receipt.json) of study enumerate")
        if name == "abstracts":
            command.add_argument("--input", required=True, help="JSONL review records")
            command.add_argument("--output", required=True, help="JSONL review records with Europe PMC abstracts")
            command.add_argument("--overwrite", action="store_true",
                                 help="look up every work again, also those already looked up")
        if name == "compare-manifest":
            command.add_argument("--stored", required=True,
                                 help="stored manifest (JSONL, JSON, CSV/TSV with a doi column, or one DOI per line)")
            command.add_argument("--label", default="stored", help="comparison label (lower case, digits, hyphens)")
            command.add_argument("--output", required=True, help="comparison report (JSON) with the per-review lists")
            command.add_argument("--allow-incomplete", action="store_true",
                                 help="compare before the census run is complete (recorded with the comparison)")
        if name in ("judge", "full-text", "no-truncation", "replicate"):
            command.add_argument("--base-url", help="chat-completions base URL of a judge called directly")
            command.add_argument("--api-key-env", help="environment variable holding the direct judge's key")
            command.add_argument("--broker-socket",
                                 help="the run principal's broker socket (default: OCSL_BROKER_SOCKET when no --base-url "
                                      "is given); the broker holds every credential")
            command.add_argument("--broker-worker", help="bulk worker of the run principal (needed when it offers several)")
            command.add_argument("--model", help="model name to request (defaults to the required served model)")
            command.add_argument("--concurrency", type=int, default=60, help="calls in flight")
            command.add_argument("--on-premises", action="store_true",
                                 help="the judge runs on premises (entitled text allowed); needs --policy naming its "
                                      "endpoint, or for the broker its provider")
            command.add_argument("--policy", help="operator egress policy (JSON, version 2) whose onPremEndpoints lists the "
                                                  "judge, or whose onPremBrokerProviders lists the broker provider")
            command.add_argument("--transport-retries", type=int, default=3,
                                 help="further passes over entries left open by an outage before the run pauses")
            command.add_argument("--transport-cooldown", type=float, default=60.0,
                                 help="seconds before the first further pass (doubling for each next pass)")
        if name == "judge":
            command.add_argument("--limit", type=int)
        if name == "full-text":
            command.add_argument("--europe-pmc", action="store_true",
                                 help="fetch Europe PMC fullTextXML by PMCID (required with --store or --provider-cache)")
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
    """The direct chat-completions transport (``--base-url``) or the broker transport (``--broker-socket`` or
    ``OCSL_BROKER_SOCKET``); entitled text reaches only a judge the operator's policy names as on premises."""
    from .census_broker import SOCKET_ENV
    from .claim_check import ConfidentialPolicy
    from .errors import ConfidentialityError
    base_url, socket_path = getattr(args, "base_url", None), getattr(args, "broker_socket", None)
    if base_url and socket_path:
        raise ValidationError("give either --base-url (a judge called directly) or --broker-socket (the run principal's "
                              "broker), not both")
    on_premises, policy = bool(getattr(args, "on_premises", False)), getattr(args, "policy", None)
    if base_url:
        from .census_runner import HttpChatTransport
        if on_premises:
            # Entitled text goes only to an endpoint the operator's policy lists, never on a self-declared flag.
            if not policy:
                raise ConfidentialityError("--on-premises needs --policy with the judge endpoint in onPremEndpoints")
            endpoint = base_url.rstrip("/") + "/chat/completions"
            if not ConfidentialPolicy.load(Path(policy)).endpoint_on_prem(endpoint):
                raise ConfidentialityError(f"{endpoint} is not an on-prem endpoint of the policy")
        return HttpChatTransport(base_url, api_key_env=getattr(args, "api_key_env", None), on_premises=on_premises)
    socket_path = socket_path or os.environ.get(SOCKET_ENV) or None
    if not socket_path:
        raise ValidationError(f"give --base-url for a judge called directly, or --broker-socket (or {SOCKET_ENV}) for the "
                              "run principal's broker socket")
    if getattr(args, "api_key_env", None):
        raise ValidationError("the broker transport holds no provider credential: the broker injects it; drop --api-key-env")
    from .census_broker import BrokerTransport
    transport = BrokerTransport(socket_path, worker=getattr(args, "broker_worker", None))
    transport.open()
    if on_premises:
        if not policy:
            raise ConfidentialityError("--on-premises needs --policy with the broker provider in onPremBrokerProviders")
        if not ConfidentialPolicy.load(Path(policy)).broker_provider_on_prem(transport.provider_id, transport.worker_model):
            raise ConfidentialityError(f"broker provider {transport.provider_id} is not an on-prem broker provider of the "
                                       "policy")
        transport.on_premises = True
    return transport


def _run(args, transport=None):
    from .census_runner import CensusRun
    return CensusRun(Path(args.database), seed=args.seed, transport=transport,
                     request_model=getattr(args, "model", None), concurrency=getattr(args, "concurrency", 60))


def _caches(args):
    from .census_inputs import InputCaches
    return InputCaches.open(store=getattr(args, "store", None), store_root=getattr(args, "store_root", None),
                            provider_cache=getattr(args, "provider_cache", None))


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


def gates(status: dict[str, Any], *, require_fresh_enumeration: bool = False) -> dict[str, Any]:
    from .census_inputs import QUERY_RULE
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
    if status.get("brokerCalls"):
        # A verdict counts only from a broker call whose receipt attests the request the instrument built.
        checks["everyBrokerVerdictFromAVerifiedRequest"] = status.get("brokerVerdictsFromUnverifiedRequests") == 0
    if status.get("brokerRefusals"):
        notes.append(f"{status['brokerRefusals']} broker calls were refused before any provider call (the installed bundle, "
                     "the worker's model or the socket's authority does not match the instrument's request)")
    if status.get("brokerRequestMismatches"):
        notes.append(f"{status['brokerRequestMismatches']} broker calls reported another request than the instrument "
                     "built; they are invalid and their outputs were discarded")
    if status.get("studyBundleSha256"):
        checks["everyReceiptCarriesTheStudyBundle"] = status.get("receiptsWithoutStudyBundle") == 0
        if status.get("studyBundleVerified") is False:
            notes.append("the study bundle was pinned by its sha256 alone: no bundle file was checked against that digest "
                         "and the instrument's registry digest (pin the bundle file with study prepare --study-bundle)")
    if (status.get("brokerPrincipals") or 0) > 1:
        notes.append(f"broker calls ran under {status['brokerPrincipals']} run principals: calls in flight when a principal "
                     "was revoked may also have been judged under it, outside this database")
    if responded and not status["fingerprintOffered"]:
        notes.append("the judge server offered no system_fingerprint on any response, so the fingerprint gate cannot "
                     "pass with this server")
    if status.get("instrumentDigestMatches") is False:
        notes.append("the database is pinned to a different instrument digest than the installed code")
    enumeration = status.get("enumeration")
    if require_fresh_enumeration:
        checks["freshEnumerationPinned"] = bool(enumeration)
    if enumeration:
        # A fresh-enumeration study: every review comes from the live enumeration and carries acquisition provenance,
        # every cache hit (review, abstract, full text) names the pinned cache version, and every abstract comes from
        # a recorded lookup.
        provenance = status.get("reviewProvenance") or {}
        checks["reviewsComeFromTheEnumeration"] = enumeration["reviewsOutsideEnumeration"] == 0
        checks["everyReviewHasAcquisitionProvenance"] = not (provenance.get("supplied") or provenance.get("unrecorded"))
        inputs = status.get("inputChecks") or {}
        checks["cacheHitsVerified"] = not any(inputs.get(name) for name in (
            "unverifiedReviewCacheHits", "unverifiedAbstractCacheHits", "unverifiedFullTextCacheHits"))
        checks["everyAbstractFromARecordedLookup"] = not inputs.get("abstractsWithoutLookup")
        # Every input of the study derived in this run: no identity map, country or topic carried in a record, works
        # derived from each review's bibliography with their metadata from a recorded lookup, cache hits only where
        # the live route ran as well, and enumeration queries that select a population, never reviews one by one.
        checks["noCarriedIdentityMapsOrCountries"] = not (inputs.get("suppliedIdentityMaps") or inputs.get("suppliedCountries"))
        checks["everyWorkDerivedFromItsReview"] = not inputs.get("worksWithoutDerivation")
        checks["everyWorkMetadataFromARecordedLookup"] = not inputs.get("worksWithoutRecordedMetadata")
        checks["reviewsAcquiredThroughLiveRoutes"] = not inputs.get("reviewsAcquiredWithoutLiveRoute")
        checks["fullTextCacheHitsBackedByLiveRetrieval"] = not inputs.get("fullTextCacheHitsWithoutLiveRetrieval")
        checks["enumerationQueriesPopulationLevel"] = enumeration.get("queryRule") == QUERY_RULE
        if require_fresh_enumeration:
            checks["everyEnumeratedReviewInStudyOrExcluded"] = enumeration.get("unexplained", 1) == 0
        if enumeration.get("excluded"):
            notes.append(f"{enumeration['excluded']} enumerated reviews are excluded by the protocol, each with a recorded reason")
        if enumeration.get("unexplained"):
            notes.append(f"{enumeration['unexplained']} enumerated reviews are neither in the study nor excluded")
        comparisons = status.get("comparisons") or []
        if comparisons:
            # A comparison froze the population: nothing may have been prepared or excluded after it.
            checks["populationUnchangedSinceComparison"] = all(item["populationUnchanged"] for item in comparisons)
    return {"ok": all(checks.values()), "checks": checks, "notes": notes, "status": status}


def fill_abstracts(source: Path, output: Path, get: Any, *, overwrite: bool = False, cache: Any = None) -> dict[str, Any]:
    """Stream records through the Europe PMC ladder into ``<output>.partial``; resume it; rename when complete.

    ``cache`` (census_inputs.InputCaches) is asked before the live lookup for every work.
    """
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
    provenance: dict[str, int] = {}
    counts = {"records": 0, "resumed": len(done), "lookupFailed": 0}
    with partial.open("a", encoding="utf-8") as handle:
        for index, record in enumerate(iter_records(source)):
            if index < len(done):
                if (record.get("journal"), record.get("doi")) != done[index]:
                    raise ValidationError(f"{partial} does not continue this input (record {index + 1} differs)")
                filled = record
            else:
                filled = fill_record(record, get, overwrite=overwrite, cache=cache)
                handle.write(json.dumps(filled, ensure_ascii=False) + "\n")
                handle.flush()
                counts["lookupFailed"] += bool(abstract_lookup_failed(filled))
                for work in filled.get("works", {}).values():
                    name = work.get("abstractSource") or "none"
                    sources[name] = sources.get(name, 0) + 1
                    label = (work.get("abstractProvenance") or {}).get("provenance") or "not-looked-up"
                    provenance[label] = provenance.get(label, 0) + 1
            counts["records"] += 1
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, output)
    return {**counts, "output": str(output), "abstractSources": sources, "abstractProvenance": dict(sorted(provenance.items())),
            "next": "run study abstracts again on the output for works whose lookup failed" if counts["lookupFailed"] else None}


CARRIED = ("identities", "country", "topic", "documentSha256")


def _gate(record: dict[str, Any], enumeration: Any, excluded: frozenset = frozenset()) -> dict[str, Any] | None:
    """Why a record is not prepared now (it stays out of the database and can be prepared later), or None."""
    from .census_inputs import enumeration_mismatch, normal_doi
    from .census_runner import abstract_lookup_failed
    if (record.get("journal"), normal_doi(record.get("doi"))) in excluded:
        return {"doi": record.get("doi"), "state": "skipped-excluded", "reason": "excluded by the protocol"}
    failed = abstract_lookup_failed(record)
    if failed:
        return {"doi": record.get("doi"), "state": "skipped-abstract-lookup-failed", "works": failed[:20]}
    acquisition = record.get("acquisition")
    if isinstance(acquisition, dict) and acquisition.get("state") == "retry":
        return {"doi": record.get("doi"), "state": "skipped-acquisition-retry",
                "next": "run study acquire again on this manifest"}
    if enumeration is not None:
        problem = enumeration_mismatch(record, enumeration)
        if problem:
            return {"doi": record.get("doi"), "state": "skipped-not-enumerated", "reason": problem}
        if not isinstance(acquisition, dict) or not acquisition.get("provenance"):
            return {"doi": record.get("doi"), "state": "skipped-no-acquisition",
                    "reason": "a fresh-enumeration study takes documents only through study acquire"}
        if acquisition.get("liveRoute") is not True:
            return {"doi": record.get("doi"), "state": "skipped-cache-only-acquisition",
                    "reason": "acquired without the journal's live route: a fresh-enumeration study acquires with --live "
                              "and a --route for the journal, so the caches only stand in for its downloads"}
        carried = [name for name in CARRIED if record.get(name) not in (None, "", {}, [])]
        if carried:
            return {"doi": record.get("doi"), "state": "skipped-carried-inputs",
                    "reason": f"the record carries {', '.join(carried)}: a fresh-enumeration study derives identities, "
                              "country and topic from the document and pins it by its acquisition"}
        if acquisition["provenance"] != "unavailable" and not isinstance(record.get("works"), dict):
            return {"doi": record.get("doi"), "state": "skipped-no-works",
                    "reason": "the record carries no cited-work records (works); run study works on the acquired records"}
    return None


def prepare(run: Any, source: Path, *, caches: Any = None, enumeration: Any = None) -> dict[str, Any]:
    """Prepare reviews one record at a time; a review that cannot be read or extracted is recorded, not fatal.

    With ``caches`` the study pins its byte caches and every cache-hit claim is re-checked against them. With
    ``enumeration`` (a verified fresh enumeration) only enumerated reviews acquired with provenance are prepared,
    and a cache-hit claim that cannot be re-checked is refused.
    """
    from . import scores_db
    from .census_inputs import review_provenance, verify_claim
    from .census_runner import iter_records, read_document, read_table_pages
    from .census_works import works_mismatch
    if enumeration is None and scores_db.meta(run.connection).get("enumerationSha256"):
        raise ValidationError("this study is pinned to a fresh enumeration; prepare with --require-fresh-enumeration "
                              "and --enumeration naming its receipt (nothing is imported from other manifests)")
    if run.connection.execute("SELECT 1 FROM manifest_comparisons LIMIT 1").fetchone():
        raise ValidationError("a manifest comparison is recorded: the study population is frozen and no review is "
                              "prepared after it")
    if enumeration is not None:
        run.pin_enumeration(enumeration)
    run.pin_inputs(caches)
    excluded = frozenset((row[0], row[1]) for row in run.connection.execute("SELECT journal, doi FROM exclusions"))
    counts: dict[str, int] = {}
    problems = []
    for record in iter_records(source):
        result = _gate(record, enumeration, excluded)
        if result is None:
            try:
                document = read_document(source.parent, record)
                tables = read_table_pages(source.parent, record)
            except (OSError, ValidationError) as error:
                result = run.record_unavailable(record, f"{type(error).__name__}: {error}")
            else:
                verified, reason = verify_claim(record, document, caches)
                claimed = str((record.get("acquisition") or {}).get("provenance") or "").startswith("cache-hit-")
                # A fresh study derives the works again from the document and takes only those (never carried over).
                derived = lambda: (works_mismatch(record, document, (record.get("document") or {}).get("kind"), tables)
                                   if enumeration is not None and document is not None else None)
                if verified is False or (enumeration is not None and claimed and verified is not True):
                    result = {"doi": record.get("doi"), "state": "skipped-provenance-unverified", "reason": reason}
                elif mismatch := derived():
                    result = {"doi": record.get("doi"), "state": "skipped-works-not-derived", "reason": mismatch}
                else:
                    try:
                        label, detail = review_provenance(record, None if document is None else
                                                          hashlib.sha256(document).hexdigest())
                        result = run.prepare_review(record, document, tables, provenance=(label, detail, verified))
                    except ValidationError as error:
                        # An inconsistent record (unknown work, changed document): kept in coverage with its reason.
                        result = {**run.record_unavailable(record, f"input refused: {error}"), "state": "input-refused"}
        counts[result["state"]] = counts.get(result["state"], 0) + 1
        if result["state"] not in ("prepared", "already-prepared"):
            problems.append(result)
    return {"prepared": counts.get("prepared", 0), "states": counts, "problems": problems,
            "inputs": (caches.describe() if caches is not None else None),
            "enumeration": ({"sha256": enumeration.sha256, "reviews": len(enumeration.reviews)}
                            if enumeration is not None else None)}


def command(args) -> Any:
    action = args.study_command
    if action == "enumerate":
        import json
        from .census_inputs import enumerate_reviews, search_pages
        from .census_runner import code_revision
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        return enumerate_reviews(spec, Path(args.output), search_pages(), code_revision=code_revision())
    if action == "acquire":
        from .census_inputs import LiveSources, ReviewAcquirer, acquire_records, parse_routes
        routes = parse_routes(args.route)
        caches = _caches(args)
        try:
            acquirer = ReviewAcquirer(caches, routes=routes, live=LiveSources.default() if args.live else None)
            result = acquire_records(Path(args.input), Path(args.output), acquirer, retry_unavailable=args.retry_unavailable)
        finally:
            if caches is not None:
                caches.close()
        return {**result, "inputs": caches.describe() if caches is not None else None}
    if action == "works":
        from .census_works import fill_works
        return fill_works(Path(args.input), Path(args.output))
    if action == "exclude":
        from .census_runner import iter_records, record_exclusions
        return record_exclusions(Path(args.database), iter_records(Path(args.input)))
    if action == "abstracts":
        from .census_abstracts import http_get
        caches = _caches(args)
        try:
            result = fill_abstracts(Path(args.input), Path(args.output), http_get(), overwrite=args.overwrite, cache=caches)
        finally:
            if caches is not None:
                caches.close()
        return {**result, "inputs": caches.describe() if caches is not None else None}
    if action == "prepare":
        from .census_inputs import load_enumeration
        if args.require_fresh_enumeration and not args.enumeration:
            raise ValidationError("--require-fresh-enumeration needs --enumeration with the receipt of study enumerate")
        if args.enumeration and not args.require_fresh_enumeration:
            raise ValidationError("--enumeration is read only with --require-fresh-enumeration")
        enumeration = load_enumeration(Path(args.enumeration)) if args.require_fresh_enumeration else None
        caches = _caches(args)
        try:
            run = _run(args)
            try:
                bundle = run.pin_study_bundle(args.study_bundle) if args.study_bundle else None
                result = prepare(run, Path(args.input), caches=caches, enumeration=enumeration)
                return {**result, "studyBundle": bundle or ({"studyBundleSha256": run.study_bundle}
                                                            if run.study_bundle else None)}
            finally:
                run.close()
        finally:
            if caches is not None:
                caches.close()
    if action == "compare-manifest":
        from .census_runner import record_comparison
        return record_comparison(Path(args.database), Path(args.stored), Path(args.output), label=args.label,
                                 allow_incomplete=args.allow_incomplete)
    if action in ("judge", "full-text", "no-truncation", "replicate"):
        run = _run(args, _transport(args))
        passes = {"transport_retries": args.transport_retries, "cooldown": args.transport_cooldown}
        try:
            if action == "judge":
                return run.run_abstract(limit=args.limit, **passes)
            if action == "full-text":
                caches = _caches(args)
                if caches is not None and not args.europe_pmc:
                    caches.close()
                    raise ValidationError("--store and --provider-cache stand in only for downloads the run makes: give "
                                          "--europe-pmc with them, so every escalated work is retrieved live as well")
                try:
                    run.pin_inputs(caches)
                    return run.run_full_text(fetch_xml=europe_pmc_fetcher() if args.europe_pmc else None,
                                             fetch_workers=args.fetch_workers, retry_failed=args.retry_failed,
                                             cache=caches, **passes)
                finally:
                    if caches is not None:
                        caches.close()
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
        return status if action == "status" else gates(status, require_fresh_enumeration=args.require_fresh_enumeration)
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
