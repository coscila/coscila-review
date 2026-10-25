# SPDX-License-Identifier: Apache-2.0
"""Bounded, paired calibration through the maintained corpus executor.

Preparation and reporting are local. Execution uses the real authorized broker;
there is no alternate inference transport. Agreement is stability, not accuracy.
"""
from __future__ import annotations

import json
import math
from datetime import datetime

from .corpus_assessment import PROTOCOL_VERSION, TaskPlanner
from .corpus_dispatch import CorpusExecutor, controller_lock
from .corpus_library import add_analysis, now
from .corpus_store import canonical
from .errors import ValidationError

VARIANTS = (("none1", 1, "none"), ("none4", 4, "none"),
            ("reorderednone4", 4, "none"), ("low1", 1, "low"))
MAX_SAMPLE_SIZE = 300
MAX_VALIDATION_LOCI = 1000


def _read_state(repo, analysis, phase):
    with repo.connect() as connection:
        row = connection.execute("SELECT checkpoint_json FROM processing_state WHERE analysis_id=? AND task_key='calibration' AND phase=?",
                                 (analysis, phase)).fetchone()
    return json.loads(row[0]) if row else None


def _checkpoint(repo, analysis, phase, value, status="completed"):
    with repo.connect(write=True) as connection:
        connection.execute("INSERT INTO processing_state VALUES (?,'calibration',?,?,0,?,?) "
                           "ON CONFLICT(analysis_id,task_key,phase) DO UPDATE SET status=excluded.status,checkpoint_json=excluded.checkpoint_json,updated_at=excluded.updated_at",
                           (analysis, phase, status, canonical(value), now()))


def _sample(repo, analysis, count, *, accepted_only=False, target_only=False, exclude=()):
    # SQLite ranks hash-shaped task IDs within strata. Only selected source
    # artifacts are loaded, rather than materializing 600k task inputs in Python.
    filters, parameters = [], [analysis]
    if accepted_only:
        filters.append("EXISTS (SELECT 1 FROM assessments a WHERE a.analysis_id=t.analysis_id AND a.locus_id=t.locus_id AND a.validation_state='accepted' AND a.protocol_version='citation-support-v2' AND json_extract(a.verdict_json,'$.taskId')=t.task_id)")
    if target_only:
        filters.append("(CASE WHEN json_valid(l.locator) THEN json_extract(l.locator,'$.claimType') END IN ('numeric','causal','method','quotation','novelty','dataset','software') OR EXISTS (SELECT 1 FROM assessments a WHERE a.analysis_id=t.analysis_id AND a.locus_id=t.locus_id AND a.validation_state='accepted' AND json_extract(a.verdict_json,'$.supportVerdict') IN ('contradicted','not-addressed','scope-caveat','partial-indirect','abstain')))")
    if exclude:
        filters.append("t.locus_id NOT IN (" + ",".join("?" for _ in exclude) + ")")
        parameters.extend(exclude)
    suffix = " AND " + " AND ".join(filters) if filters else ""
    with repo.connect() as connection:
        rows = [dict(r) for r in connection.execute("""
          WITH eligible AS (
            SELECT t.task_id,t.locus_id,t.spec_sha256,
              coalesce(json_extract(r.strata_json,'$.journal'),'unknown') AS journal,
              CASE WHEN json_valid(l.locator) THEN
                coalesce(json_extract(l.locator,'$.claimType'),'unknown') ELSE 'unknown' END AS claim_type,
              t.tier AS evidence
            FROM corpus_tasks t JOIN claim_loci l USING(locus_id)
            JOIN bibliography_entries b USING(entry_id) JOIN reviews r USING(review_id)
            WHERE t.analysis_id=? AND t.tier='abstract' AND t.state!='blocked'
        """ + suffix + """
          ), ranked AS (
            SELECT *,row_number() OVER (PARTITION BY journal,claim_type,evidence ORDER BY task_id) AS rank
            FROM eligible
          ) SELECT * FROM ranked ORDER BY rank,journal,claim_type,evidence,task_id LIMIT ?
        """, (*parameters, count))]
    if not rows:
        return []
    if len({r["locus_id"] for r in rows}) != len(rows):
        raise ValidationError("multiple task versions share a locus; freeze the intended primary task version")
    for row in rows:
        spec = json.loads(repo.read_artifact(row["spec_sha256"]))
        if TaskPlanner().plan(spec)["action"] != "judge":
            raise ValidationError("calibration sample contains a blocked source; repair task preparation")
    return [{"baseTaskId": r["task_id"], "locusId": r["locus_id"], "specSha256": r["spec_sha256"],
             "stratum": [r["journal"], r["claim_type"], r["evidence"]]} for r in rows]


def _prepare(repo, worker_id, low_worker_id, sample_size, validation_cap):
    parent = repo.analysis()
    request = {"protocol": PROTOCOL_VERSION, "workerId": worker_id, "lowWorkerId": low_worker_id,
               "sampleSize": sample_size, "additionalValidationLociCap": validation_cap}
    with controller_lock(repo):
        manifest = _read_state(repo, parent["analysis_id"], "manifest")
        if manifest is not None:
            if manifest["request"] != request or manifest["parentProtocolSha256"] != parent["protocol_sha256"]:
                raise ValidationError("calibration settings changed; use a new analysis namespace")
        else:
            sample = _sample(repo, parent["analysis_id"], sample_size)
            if not sample:
                raise ValidationError("prepare eligible abstract tasks before calibration")
            manifest = {"version": 1, "parentAnalysisId": parent["analysis_id"],
                        "parentProtocolSha256": parent["protocol_sha256"], "request": request,
                        "sample": sample, "createdAt": now(),
                        "sampling": "deterministic round-robin journal/claim-type/evidence strata; task hash order",
                        "thresholds": {"validItemFraction": 0.99, "pairedVerdictAgreement": 0.95,
                                       "reorderedVerdictAgreement": 0.95},
                        "plannedItemJudgments": len(sample) * len(VARIANTS),
                        "formatRepair": "executor bounded retry, reported separately",
                        "additionalValidation": {"uniqueLociCap": validation_cap,
                                                 "automaticDispatch": False}}
            manifest["manifestArtifactSha256"] = repo.json_artifact(manifest, "calibration-manifest")
            _checkpoint(repo, parent["analysis_id"], "manifest", manifest)
        variants = []
        for name, batch_size, effort in VARIANTS:
            variant_id = parent["analysis_id"] + ":calibration:" + name
            worker = low_worker_id if effort == "low" else worker_id
            protocol = {**parent["protocol"], "kind": "calibration", "parentAnalysisId": parent["analysis_id"],
                        "variant": name, "batchSize": batch_size, "workerId": worker, "reasoning": effort,
                        "calibrationManifestSha256": manifest["manifestArtifactSha256"]}
            with repo.connect(write=True) as connection:
                existing = connection.execute("SELECT r.protocol_json FROM analyses a JOIN runs r USING(run_id) WHERE analysis_id=?", (variant_id,)).fetchone()
                if existing and json.loads(existing[0]) != protocol:
                    raise ValidationError("calibration variant protocol differs from pinned manifest")
                if not existing:
                    add_analysis(connection, parent["corpus_id"], parent["question"] + " (calibration " + name + ")",
                                 variant_id, protocol=protocol, code_revision=parent["code_revision"])
            tasks = []
            for item in manifest["sample"]:
                spec = json.loads(repo.read_artifact(item["specSha256"]))
                spec.pop("id", None)
                tasks.append(repo.add_task(variant_id, item["locusId"], "abstract", spec))
            order = list(tasks)
            if name == "reorderednone4":
                order = [task for i in range(0, len(tasks), 4) for task in reversed(tasks[i:i+4])]
            variants.append({"name": name, "analysisId": variant_id, "workerId": worker,
                             "effort": effort, "batchSize": batch_size,
                             "taskIds": tasks, "taskOrder": order})
    return manifest, variants


def _variant_report(repo, variant, sample):
    analysis = variant["analysisId"]
    by_locus = {}
    with repo.connect() as connection:
        states = dict(connection.execute("SELECT state,count(*) FROM corpus_tasks WHERE analysis_id=? GROUP BY state", (analysis,)))
        assessments, order = repo.library.ordered("assessments", "a")
        observations = [dict(r) for r in connection.execute(f"SELECT a.*,m.usage_json FROM {assessments} a JOIN model_receipts m USING(receipt_id) WHERE a.analysis_id=? ORDER BY a.created_at,{order}", (analysis,))]
        receipts = [dict(r) for r in connection.execute("SELECT usage_json,outcome,model,provider,started_at,finished_at FROM model_receipts WHERE analysis_id=?", (analysis,))]
        repairs = connection.execute("SELECT sum(max(attempts-1,0)) FROM corpus_tasks WHERE analysis_id=?", (analysis,)).fetchone()[0] or 0
        pin = connection.execute("SELECT checkpoint_json FROM processing_state WHERE analysis_id=? AND task_key='executor:abstract' AND phase='pin'", (analysis,)).fetchone()
    for observation in observations:
        # The first response measures batching validity. Repairs cannot erase
        # a bad initial observation or inflate agreement by selective retry.
        by_locus.setdefault(observation["locus_id"], observation)
    valid, verdicts = 0, {}
    for item in sample:
        first = by_locus.get(item["locusId"])
        if first and first["validation_state"] == "accepted":
            valid += 1
            verdicts[item["locusId"]] = json.loads(first["verdict_json"])["supportVerdict"]
    usage = {}
    for receipt in receipts:
        for key, value in json.loads(receipt["usage_json"]).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] = usage.get(key, 0) + value
    expected = len(sample)
    terminal = sum(states.get(s, 0) for s in ("completed", "blocked", "failed", "cancelled"))
    starts = [r["started_at"] for r in receipts if r["started_at"]]
    ends = [r["finished_at"] for r in receipts if r["finished_at"]]
    elapsed = ((datetime.fromisoformat(max(ends)) - datetime.fromisoformat(min(starts))).total_seconds()
               if starts and ends else None)
    return {"name": variant["name"], "analysisId": analysis, "workerId": variant["workerId"],
            "batchSize": variant["batchSize"], "reasoning": variant["effort"],
            "taskStates": states, "expectedItems": expected, "firstValidItems": valid,
            "validItemFraction": valid / expected if expected else 0,
            "complete": terminal == expected, "observedItems": len(by_locus),
            "requestReceipts": len(receipts), "recordedUsage": usage, "repairItemAttempts": repairs,
            "firstVerdicts": verdicts, "taskOrder": variant["taskOrder"],
            "actualModels": sorted({r["model"] for r in receipts}),
            "actualProviders": sorted({r["provider"] for r in receipts}),
            "enqueueToImportSeconds": elapsed,
            "firstValidItemsPerSecond": valid / elapsed if elapsed and elapsed > 0 else None,
            "timingDefinition": "Observed first batch enqueue to latest result import; includes queue and polling delays.",
            "instrument": json.loads(pin[0]) if pin else None}


def _agreement(left, right, sample):
    paired, same = 0, 0
    for item in sample:
        locus = item["locusId"]
        if locus in left and locus in right:
            paired += 1
            same += left[locus] == right[locus]
    interval = None
    if paired:
        proportion, z = same / paired, 1.959963984540054
        scale = 1 + z * z / paired
        center = (proportion + z * z / (2 * paired)) / scale
        half = z * math.sqrt(proportion * (1 - proportion) / paired + z * z / (4 * paired * paired)) / scale
        interval = [max(0, center - half), min(1, center + half)]
    return {"agreeingItems": same, "pairedValidItems": paired, "expectedItems": len(sample),
            "fractionOfSample": same / len(sample) if sample else 0,
            "fractionOfValidPairs": same / paired if paired else None,
            "confidenceInterval95": interval,
            "intervalMethod": "Wilson interval for valid-pair agreement; descriptive only, does not adjust for review clustering or enriched selection."}


def _report(repo, manifest, variants):
    results = {v["name"]: _variant_report(repo, v, manifest["sample"]) for v in variants}
    paired = _agreement(results["none1"]["firstVerdicts"], results["none4"]["firstVerdicts"], manifest["sample"])
    reordered = _agreement(results["none4"]["firstVerdicts"], results["reorderednone4"]["firstVerdicts"], manifest["sample"])
    low = _agreement(results["none1"]["firstVerdicts"], results["low1"]["firstVerdicts"], manifest["sample"])
    complete = all(r["complete"] for r in results.values())
    enough = len(manifest["sample"]) == manifest["request"]["sampleSize"] and len(manifest["sample"]) >= 4
    pinned = all(r["instrument"] is not None for r in results.values())
    qualified = (complete and enough and pinned and min(r["validItemFraction"] for r in results.values()) >= 0.99
                 and paired["fractionOfSample"] >= 0.95 and reordered["fractionOfSample"] >= 0.95)
    result = {"version": 1, "protocol": PROTOCOL_VERSION, "parentAnalysisId": manifest["parentAnalysisId"],
              "manifestSha256": manifest["manifestArtifactSha256"], "complete": complete,
              "qualified": qualified, "batchSize": 4 if qualified else 1,
              "requestedSampleSize": manifest["request"]["sampleSize"], "sampleSize": len(manifest["sample"]),
              "sufficientSample": enough, "thresholds": manifest["thresholds"],
              "pairedAgreement": paired, "reorderedAgreement": reordered, "lowReasoningAgreement": low,
              "variants": results, "additionalValidation": manifest["additionalValidation"],
              "instrument": results["none4"]["instrument"],
              "interpretation": "Same-model stability and quote validity, not scientific accuracy or human ground truth.",
              "updatedAt": now()}
    result["reportArtifactSha256"] = repo.json_artifact(result, "calibration-report")
    _checkpoint(repo, manifest["parentAnalysisId"], "decision", result, "completed" if complete else "pending")
    return result


def calibrate(repo, paths, worker_id="w1", low_worker_id="w2", sample_size=300,
              validation_cap=1000, execute=False, concurrency=60):
    """Prepare/report, or advance one bounded round of authorized broker work.

    Reinvoke with execute=True to harvest jobs and advance remaining work.
    Preparation alone never asserts a live calibration result.
    """
    if (isinstance(sample_size, bool) or not isinstance(sample_size, int) or not 1 <= sample_size <= MAX_SAMPLE_SIZE
            or isinstance(validation_cap, bool) or not isinstance(validation_cap, int)
            or not 0 <= validation_cap <= MAX_VALIDATION_LOCI):
        raise ValidationError("calibration needs 1–300 sample loci and an additional validation cap of 0–1000")
    if not worker_id or not low_worker_id or worker_id == low_worker_id:
        raise ValidationError("select distinct none and low worker configurations")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 2**53-1:
        raise ValidationError("calibration concurrency must be a positive safe integer")
    manifest, variants = _prepare(repo, worker_id, low_worker_id, sample_size, validation_cap)
    execution = []
    paused = False
    if execute:
        executors = [(variant, CorpusExecutor(repo, paths, analysis_id=variant["analysisId"])) for variant in variants]
        for variant, executor in executors:
            _, worker = executor._catalog_worker(variant["workerId"])
            effort = worker.get("effort") or (worker.get("modelProvider") or {}).get("reasoningProfile")
            if effort != variant["effort"]:
                raise ValidationError("calibration worker reasoning differs from its pinned variant")
        for variant, executor in executors:
            resumed = executor.resume()
            paused = paused or any(j["state"] == "paused" for j in resumed.get("jobs", []))
            submitted = {} if paused else executor.submit(variant["workerId"], tier="abstract", batch_size=variant["batchSize"],
                                                         concurrency=concurrency, max_jobs=4, task_order=variant["taskOrder"])
            execution.append({"variant": variant["name"], "resumed": resumed.get("jobs", []), **submitted})
    return {"manifest": manifest, "report": _report(repo, manifest, variants), "execution": execution, "paused": paused}


def _validation_prepare(repo, worker_id, sample_size):
    parent = repo.analysis()
    calibration = _read_state(repo, parent["analysis_id"], "manifest")
    if calibration is None:
        raise ValidationError("prepare calibration first to pin the additional validation budget")
    cap = calibration["additionalValidation"]["uniqueLociCap"]
    if sample_size > cap:
        raise ValidationError("validation sample exceeds this analysis's pinned unique-locus budget")
    request = {"workerId": worker_id, "sampleSize": sample_size, "uniqueLociCap": cap}
    with controller_lock(repo):
        manifest = _read_state(repo, parent["analysis_id"], "validation-manifest")
        if manifest:
            if manifest["request"] != request:
                raise ValidationError("validation settings changed; keep the pinned sample or create a new analysis")
        else:
            # Half representative, then targeted scientific uncertainty/complexity.
            # Both components retain separate labels; their mixture is not an
            # unbiased population prevalence estimator.
            general = _sample(repo, parent["analysis_id"], (sample_size + 1) // 2, accepted_only=True)
            used = [s["locusId"] for s in general]
            targeted = _sample(repo, parent["analysis_id"], sample_size - len(general),
                               accepted_only=True, target_only=True, exclude=used)
            used.extend(s["locusId"] for s in targeted)
            remainder = _sample(repo, parent["analysis_id"], sample_size - len(used),
                                accepted_only=True, exclude=used) if len(used) < sample_size else []
            sample = [{**s, "selection": label} for group, label in
                      ((general, "stratified"), (targeted, "targeted"), (remainder, "stratified-fill")) for s in group]
            if not sample:
                raise ValidationError("accepted primary abstract judgments are required before blind validation")
            if len({s["locusId"] for s in sample}) != len(sample):
                raise ValidationError("multiple task versions share a locus; freeze the intended primary task version")
            manifest = {"version": 1, "parentAnalysisId": parent["analysis_id"],
                        "parentProtocolSha256": parent["protocol_sha256"], "request": request,
                        "calibrationManifestSha256": calibration["manifestArtifactSha256"],
                        "sample": sample, "createdAt": now(),
                        "sampling": "half stratified; remainder targeted complex/adverse/uncertain, with stratified fill",
                        "blind": True, "plannedItemJudgments": len(sample)}
            manifest["manifestArtifactSha256"] = repo.json_artifact(manifest, "validation-manifest")
            _checkpoint(repo, parent["analysis_id"], "validation-manifest", manifest)
        analysis_id = parent["analysis_id"] + ":validation:blind"
        protocol = {**parent["protocol"], "kind": "validation", "parentAnalysisId": parent["analysis_id"],
                    "workerId": worker_id, "reasoning": "none", "batchSize": 1,
                    "validationManifestSha256": manifest["manifestArtifactSha256"]}
        with repo.connect(write=True) as connection:
            existing = connection.execute("SELECT r.protocol_json FROM analyses a JOIN runs r USING(run_id) WHERE analysis_id=?", (analysis_id,)).fetchone()
            if existing and json.loads(existing[0]) != protocol:
                raise ValidationError("validation protocol differs from pinned sample")
            if not existing:
                add_analysis(connection, parent["corpus_id"], parent["question"] + " (selected blind validation)",
                             analysis_id, protocol=protocol, code_revision=parent["code_revision"])
        task_ids = []
        for item in manifest["sample"]:
            spec = json.loads(repo.read_artifact(item["specSha256"]))
            spec.pop("id", None)
            task_ids.append(repo.add_task(analysis_id, item["locusId"], "abstract", spec))
        variant = {"name": "blind", "analysisId": analysis_id, "workerId": worker_id,
                   "effort": "none", "batchSize": 1, "taskIds": task_ids, "taskOrder": task_ids}
    return manifest, variant


def validate(repo, paths, worker_id="w3", sample_size=1000, execute=False, concurrency=60):
    """Prepare/advance one frozen, budgeted blind reliability sample.

    No primary verdict is added to a worker input. Adverse and uncertain loci
    are deliberately enriched, and are reported separately from the general
    stratified sample rather than treated as a prevalence estimate.
    """
    if (isinstance(sample_size, bool) or not isinstance(sample_size, int)
            or not 1 <= sample_size <= MAX_VALIDATION_LOCI or not worker_id):
        raise ValidationError("validation needs a worker and 1–1000 sampled unique loci")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 2**53-1:
        raise ValidationError("validation concurrency must be a positive safe integer")
    manifest, variant = _validation_prepare(repo, worker_id, sample_size)
    execution = {}
    paused = False
    if execute:
        executor = CorpusExecutor(repo, paths, analysis_id=variant["analysisId"])
        _, worker = executor._catalog_worker(worker_id)
        if (worker.get("effort") or (worker.get("modelProvider") or {}).get("reasoningProfile")) != "none":
            raise ValidationError("blind validation requires its pinned none reasoning worker")
        resumed = executor.resume()
        paused = any(j["state"] == "paused" for j in resumed.get("jobs", []))
        execution = {} if paused else executor.submit(worker_id, tier="abstract", batch_size=1, concurrency=concurrency,
                                                      max_jobs=4, task_order=variant["taskOrder"])
        execution["resumed"] = resumed.get("jobs", [])
    observed = _variant_report(repo, variant, manifest["sample"])
    with repo.connect() as connection:
        assessments, order = repo.library.ordered("assessments")
        primary = [dict(r) for r in connection.execute(f"SELECT locus_id,verdict_json FROM {assessments} WHERE analysis_id=? AND validation_state='accepted' AND protocol_version=? AND locus_id IN (SELECT json_extract(value,'$.locusId') FROM json_each(?)) ORDER BY created_at,{order}",
                                                       (manifest["parentAnalysisId"], PROTOCOL_VERSION, canonical(manifest["sample"]))) ]
    expected_tasks = {s["locusId"]: s["baseTaskId"] for s in manifest["sample"]}
    verdicts = {}
    for row in primary:
        verdict = json.loads(row["verdict_json"])
        if expected_tasks.get(row["locus_id"]) == verdict.get("taskId"):
            verdicts.setdefault(row["locus_id"], verdict["supportVerdict"])
    reports = {}
    for label in ("all", "stratified", "targeted", "stratified-fill"):
        selected = manifest["sample"] if label == "all" else [s for s in manifest["sample"] if s["selection"] == label]
        reports[label] = _agreement(verdicts, observed["firstVerdicts"], selected)
    report = {"version": 1, "parentAnalysisId": manifest["parentAnalysisId"],
              "manifestSha256": manifest["manifestArtifactSha256"], "blind": True,
              "complete": observed["complete"], "sampleSize": len(manifest["sample"]),
              "requestedSampleSize": sample_size,
              "uniqueLociCap": manifest["request"]["uniqueLociCap"], "variant": observed,
              "agreementBySelection": reports,
              "interpretation": "Same-model blind reliability; enriched targeted findings are not population prevalence or accuracy.",
              "updatedAt": now()}
    report["reportArtifactSha256"] = repo.json_artifact(report, "validation-report")
    _checkpoint(repo, manifest["parentAnalysisId"], "validation-decision", report,
                "completed" if report["complete"] else "pending")
    return {"manifest": manifest, "report": report, "execution": execution, "paused": paused}
