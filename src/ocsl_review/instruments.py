# SPDX-License-Identifier: Apache-2.0
"""Registry of versioned measurement instruments.

An instrument bundle lives in ``instruments/<name>/<version>/instrument.json``
and declares its prompt and schema files (with digests), evidence policy,
geometry, request settings, required model, validator and class map. The
bundle digest covers the canonical manifest, every file digest and every
module digest, so a changed prompt, schema, builder or validator is a
different instrument. Execution code (transport, retries, storage) is
deliberately outside the digest: capacity and reliability fixes never force a
new analysis.

An instrument that declares ``transport.messages: verbatim`` exports one broker
bundle per tier and parameter set (``ocsl-review instruments export``): the
body the execution daemon installs with ``PUT /v2/instruments/{digest}``, under
a digest derived from the registry digest, the tier, the parameters and the
bundle itself.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from .errors import ValidationError

HOME = Path(__file__).resolve().parents[2]
ROOT = HOME / "instruments"
PACKAGE = Path(__file__).resolve().parent
SCHEMA_PATH = HOME / "schemas/instrument.schema.json"
ID_PATTERN = re.compile(r"^([a-z][a-z0-9-]*)@([1-9][0-9]*)$")
DIGEST_FORMAT = "ocsl-instrument-digest-v1"
BROKER_FORMAT = "ocsl-broker-instrument-v1"


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_id(instrument_id: str) -> tuple[str, int]:
    match = ID_PATTERN.fullmatch(instrument_id or "")
    if not match:
        raise ValidationError(f"instrument id must look like name@version: {instrument_id!r}")
    return match.group(1), int(match.group(2))


def module_path(module: str) -> Path:
    if not re.fullmatch(r"ocsl_review\.[a-z_][a-z0-9_]*", module or ""):
        raise ValidationError(f"instrument module must be an ocsl_review module: {module!r}")
    path = PACKAGE / (module.split(".", 1)[1] + ".py")
    if not path.is_file():
        raise ValidationError(f"instrument module is missing: {module}")
    return path


def module_digest(module: str) -> str:
    return sha256_bytes(module_path(module).read_bytes())


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


@dataclass(frozen=True)
class Instrument:
    id: str
    name: str
    version: int
    directory: Path
    manifest: dict[str, Any] = field(repr=False)

    def file_path(self, role: str) -> Path:
        record = self.manifest["files"].get(role)
        if record is None:
            raise ValidationError(f"{self.id} declares no {role} file")
        if "repoPath" in record:
            base, relative = HOME, record["repoPath"]
        else:
            base, relative = self.directory, record["path"]
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValidationError(f"{self.id}: file {role} escapes its root")
        path = (base / candidate).resolve()
        if not path.is_relative_to(base.resolve()) or not path.is_file():
            raise ValidationError(f"{self.id}: file {role} is missing or escapes its root")
        return path

    def read_bytes(self, role: str) -> bytes:
        data = self.file_path(role).read_bytes()
        if sha256_bytes(data) != self.manifest["files"][role]["sha256"]:
            raise ValidationError(f"{self.id}: {role} differs from its pinned digest")
        return data

    def text(self, role: str) -> str:
        return self.read_bytes(role).decode("utf-8")

    def tier(self, name: str) -> dict[str, Any]:
        tiers = self.manifest["tiers"]
        if name not in tiers:
            raise ValidationError(f"{self.id} has no {name} tier")
        return tiers[name]

    def modules(self) -> dict[str, str]:
        selected = {}
        for role in ("validator", "builder"):
            record = self.manifest.get(role)
            if record:
                selected[role] = record["module"]
        return selected

    def file_digests(self) -> dict[str, str]:
        return {role: sha256_bytes(self.file_path(role).read_bytes()) for role in sorted(self.manifest["files"])}

    def module_digests(self) -> dict[str, str]:
        return {role: module_digest(module) for role, module in sorted(self.modules().items())}

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical({"format": DIGEST_FORMAT, "manifest": self.manifest,
                                       "files": self.file_digests(), "modules": self.module_digests()}).encode("utf-8"))

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.manifest["status"], "description": self.manifest["description"],
                "digest": self.digest, "directory": str(self.directory.relative_to(HOME)),
                "files": self.file_digests(), "modules": self.module_digests(),
                "tiers": sorted(self.manifest["tiers"]), "requiredModel": self.manifest["requiredModel"],
                "validator": self.manifest["validator"]["id"], "classMap": self.manifest["classMap"]["id"]}


def verify(instrument: Instrument) -> dict[str, Any]:
    """Check the manifest schema, file digests and pinned module digests."""
    problems = []
    errors = sorted(jsonschema.Draft202012Validator(_schema()).iter_errors(instrument.manifest),
                    key=lambda error: list(map(str, error.absolute_path)))
    problems.extend(f"schema: {'.'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors[:10])
    if instrument.manifest.get("id") != instrument.id:
        problems.append("manifest id differs from its directory")
    files = {}
    for role, record in sorted(instrument.manifest.get("files", {}).items()):
        try:
            actual = sha256_bytes(instrument.file_path(role).read_bytes())
        except ValidationError as error:
            problems.append(str(error))
            continue
        files[role] = actual
        if actual != record.get("sha256"):
            problems.append(f"file {role} differs from its pinned digest")
    modules = {}
    for role in ("validator", "builder"):
        record = instrument.manifest.get(role) or {}
        if not record:
            continue
        try:
            actual = module_digest(record["module"])
        except ValidationError as error:
            problems.append(str(error))
            continue
        modules[role] = actual
        if record.get("sha256") and record["sha256"] != actual:
            problems.append(f"{role} module {record['module']} differs from its pinned digest")
    for role, token in ((role, spec.get("token")) for role, spec in instrument.manifest.get("parameters", {}).items()):
        if token and "systemPrompt" in files:
            occurrences = instrument.file_path("systemPrompt").read_text(encoding="utf-8").count(token)
            if occurrences != 1:
                problems.append(f"parameter {role} token occurs {occurrences} times in the system prompt")
    return {"id": instrument.id, "ok": not problems, "problems": problems, "files": files, "modules": modules,
            "digest": instrument.digest if not problems else None}


def available() -> list[str]:
    found = []
    for manifest in sorted(ROOT.glob("*/*/instrument.json")):
        name, version = manifest.parent.parent.name, manifest.parent.name
        if ID_PATTERN.fullmatch(f"{name}@{version}"):
            found.append(f"{name}@{version}")
    return sorted(found, key=lambda value: (parse_id(value)[0], parse_id(value)[1]))


def load(instrument_id: str, *, check: bool = True) -> Instrument:
    name, version = parse_id(instrument_id)
    directory = ROOT / name / str(version)
    path = directory / "instrument.json"
    if not path.is_file():
        raise ValidationError(f"unknown instrument: {instrument_id}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValidationError(f"{instrument_id}: instrument.json is not JSON") from error
    instrument = Instrument(instrument_id, name, version, directory, manifest)
    if check:
        report = verify(instrument)
        if not report["ok"]:
            raise ValidationError(f"{instrument_id} failed verification: " + "; ".join(report["problems"]))
    return instrument


def study_bundle_digest(*, protocol: Any, study_prompt: str | bytes, instruments: dict[str, str],
                        dataset_versions: dict[str, str], package_tags: dict[str, str],
                        template: Any = None) -> dict[str, Any]:
    """Pin a study by its protocol, prompt, template, instrument digests, datasets and packages.

    New analyses pin this digest instead of a source-tree hash; the code tag
    of each call is recorded per receipt.
    """
    if not instruments or not all(re.fullmatch(r"[0-9a-f]{64}", v or "") for v in instruments.values()):
        raise ValidationError("a study bundle needs instrument digests")
    prompt = study_prompt.encode("utf-8") if isinstance(study_prompt, str) else study_prompt
    record = {"format": "ocsl-study-bundle-v1", "protocolSha256": sha256_bytes(canonical(protocol).encode("utf-8")),
              "studyPromptSha256": sha256_bytes(prompt),
              "templateSha256": sha256_bytes(canonical(template).encode("utf-8")) if template is not None else None,
              "instruments": dict(sorted(instruments.items())), "datasetVersions": dict(sorted(dataset_versions.items())),
              "packageTags": dict(sorted(package_tags.items()))}
    return {**record, "sha256": sha256_bytes(canonical(record).encode("utf-8"))}


def _parameter_value(instrument: Instrument, name: str, value: Any, token: str | None) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or (token and token in value):
        raise ValidationError(f"{instrument.id}: parameter {name} must be one non-empty line")
    return value


def broker_bundle(instrument_id: str, *, tier: str | None = None,
                  parameters: dict[str, str] | None = None) -> dict[str, Any]:
    """The bundle the execution daemon installs for one tier of a verbatim instrument, with its broker digest.

    ``bundle`` is the exact ``PUT /v2/instruments/{digest}`` body: the system prompt with the parameters
    substituted (byte for byte what the builder sends), no response schema (the label parser validates),
    the output-token rule, sampling, reasoning, geometry, the required served model (when a mismatch
    pauses) and ``transport.messages: verbatim``. ``digest`` is the sha256 of the canonical JSON of the
    registry digest, tier, parameters and bundle digest, so a different body never reuses a digest.
    """
    from .census_broker import canonical_sha256
    instrument = load(instrument_id)
    manifest = instrument.manifest
    if (manifest.get("transport") or {}).get("messages") != "verbatim":
        raise ValidationError(f"{instrument.id} declares no verbatim message transport; only instruments that do export "
                              "a broker bundle")
    tiers = sorted(manifest["tiers"])
    if tier is None:
        if len(tiers) != 1:
            raise ValidationError(f"{instrument.id} has the tiers {', '.join(tiers)}: choose one with --tier")
        tier = tiers[0]
    spec = instrument.tier(tier)
    declared = manifest.get("parameters") or {}
    values = dict(parameters or {})
    unknown = sorted(set(values) - set(declared))
    if unknown:
        raise ValidationError(f"{instrument.id} has no parameter {', '.join(unknown)}")
    prompt = instrument.text("systemPrompt")
    for name, declaration in sorted(declared.items()):
        token = declaration.get("token")
        if name not in values:
            if declaration.get("required"):
                raise ValidationError(f"{instrument.id} needs --param {name}=VALUE")
            continue
        value = _parameter_value(instrument, name, values[name], token)
        if token:
            if prompt.count(token) != 1:
                raise ValidationError(f"{instrument.id}: parameter {name} token must occur once in the system prompt")
            prompt = prompt.replace(token, value)
    request = spec["request"]
    # The broker sends reasoning_effort always and a seed whenever the bundle has one; the census executor sends
    # reasoning_effort only when the tier declares it and never a seed. A tier for which the two requests would
    # differ cannot run verbatim: every call would stop as a request mismatch.
    if not request.get("reasoningEffort"):
        raise ValidationError(f"{instrument.id} tier {tier} declares no reasoningEffort: the executor would send no "
                              "reasoning_effort while the broker always sends one; such a tier cannot run verbatim")
    if request.get("seed") is not None:
        raise ValidationError(f"{instrument.id} tier {tier} declares a seed the executor does not send: the broker would "
                              "add it to every request; such a tier cannot run verbatim")
    rule = request["maxOutputTokens"]
    required = manifest.get("requiredModel") or {}
    bundle = {"instrumentId": instrument.id, "version": instrument.version, "systemPrompt": prompt,
              "responseSchema": None,
              "maxOutputTokens": {"perItem": int(rule["perItem"]), "minimum": int(rule["floor"])},
              "temperature": request["temperature"], "seed": request.get("seed"),
              "reasoning": request.get("reasoningEffort") or "none",
              "itemsPerCall": int(spec["geometry"]["itemsPerCall"]),
              "requiredModelFingerprint": required.get("served") if required.get("onMismatch") == "pause" else None,
              "transport": {"messages": "verbatim"}}
    registry = instrument.digest
    bundle_sha = canonical_sha256(bundle)
    digest = canonical_sha256({"format": BROKER_FORMAT, "instrument": instrument.id, "registryDigest": registry,
                               "tier": tier, "parameters": values, "bundleSha256": bundle_sha})
    return {"format": BROKER_FORMAT, "instrumentId": instrument.id, "registryDigest": registry, "tier": tier,
            "parameters": values, "digest": digest, "bundleSha256": bundle_sha,
            "install": f"PUT /v2/instruments/{digest}", "bundle": bundle}


def cmd_export(args: Any) -> dict[str, Any]:
    parameters: dict[str, str] = {}
    for item in getattr(args, "param", None) or []:
        name, separator, value = item.partition("=")
        if not separator or not name:
            raise ValidationError("--param takes NAME=VALUE")
        if name in parameters:
            raise ValidationError(f"--param {name} is given twice")
        parameters[name] = value
    exported = broker_bundle(args.instrument, tier=getattr(args, "tier", None), parameters=parameters)
    return exported["bundle"] if getattr(args, "body", False) else exported


def cmd_list(args: Any) -> dict[str, Any]:
    rows = []
    for instrument_id in available():
        instrument = load(instrument_id, check=False)
        report = verify(instrument)
        rows.append({"id": instrument_id, "status": instrument.manifest.get("status"), "ok": report["ok"],
                     "digest": report["digest"], "tiers": sorted(instrument.manifest.get("tiers", {}))})
    return {"instruments": rows}


def cmd_show(args: Any) -> dict[str, Any]:
    instrument = load(args.instrument)
    return {**instrument.describe(), "manifest": instrument.manifest}


def cmd_verify(args: Any) -> dict[str, Any]:
    selected = [args.instrument] if getattr(args, "instrument", None) else available()
    reports = [verify(load(item, check=False)) for item in selected]
    if not all(report["ok"] for report in reports):
        failed = [f"{r['id']}: {'; '.join(r['problems'])}" for r in reports if not r["ok"]]
        raise ValidationError("instrument verification failed: " + " | ".join(failed))
    return {"ok": True, "instruments": reports}
