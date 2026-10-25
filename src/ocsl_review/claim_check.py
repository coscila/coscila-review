# SPDX-License-Identifier: Apache-2.0
"""Claim checking with 0..n citations per claim: task ``claim-check@1``.

Research-integrity tooling for authors, reviewers and study teams. The judged unit is the
sentence that carries a citation marker; a citation never carries over to a neighbouring
sentence, and a clause inside a sentence is not parsed out: every citation is checked against
its whole sentence. Sentences without a citation get no attribution; they are flagged only when
the claim inventory says they need one, together with a store-wide retrieval receipt.

For each cited work the task retrieves evidence (BM25 over the work's units, with a receipt),
sanitises claim, context and evidence, and asks a judge for a verdict with citation-support@2
semantics (exact quotes, explicit abstention). Every model call has a receipt whose request and
response bytes are stored by hash, so a replay can re-derive each verdict without a model call.

``ConfidentialPolicy`` is the confidential-manuscript mode (for example an author checking an
unpublished draft): only identifiers leave the machine and only an on-prem model sees text. Every
egress decision is written to a hash-chained ledger that stores names and digests, never content.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .author_year import RESOLVER_VERSION
from .claim_inventory import _claim_type, _origin, citation_requirement
from .confidentiality import IDENTIFIER_PATTERNS, EgressPlan, append_egress_event, authorize_egress
from .corpus_assessment import (
    MAX_BATCH_ITEMS,
    PROMPT_PATH,
    PROTOCOL_VERSION,
    SCHEMA_PATH,
    AssessmentValidator,
    TaskPlanner,
)
from .errors import ConfidentialityError, ValidationError
from .integrity import INTEGRITY_VERSION
from .retrieval import RETRIEVAL_VERSION, TOKENIZER, EvidenceIndex, EvidenceUnit, canonical, sha256_text
from .sanitize import SANITIZER_VERSION, assert_prompt_clean, sanitize_text

TASK = "claim-check@1"
HOME = Path(__file__).resolve().parents[2]
EXTENSION_SCHEMA = HOME / "schemas" / "claim-check.sql"
LITERATURE_SCHEMA = HOME / "skills" / "ocsl-review-reference-analysis" / "assets" / "literature-schema.sql"
EXTRACTOR_VERSION = "claim-sentence-1"
ERROR_CATEGORIES = {
    "contradicted": "contradicts",
    "not-addressed": "not substantiated",
    "scope-caveat": "oversimplifies",
    "partial-indirect": "indirect",
}
CALIBRATION_STATEMENT = (
    "No calibration set exists for this field, so no adverse flag has a measured positive predictive value; "
    "every adverse flag reads 'check suggested' and needs a reader's confirmation.")


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def instrument_definition(*, k: int = 5, max_evidence_characters: int = 11000) -> dict[str, Any]:
    """Everything that fixes a claim-check@1 verdict; its digest is recorded on every run."""

    return {
        "id": TASK,
        "unit": "sentence carrying the citation marker; no carry-over, no clause parsing",
        "verdictProtocol": PROTOCOL_VERSION,
        "promptSha256": sha256_bytes(PROMPT_PATH.read_bytes()),
        "schemaSha256": sha256_bytes(SCHEMA_PATH.read_bytes()),
        "retrieval": {"version": RETRIEVAL_VERSION, "tokenizer": TOKENIZER, "k": k,
                      "maxEvidenceCharacters": max_evidence_characters},
        "evidencePolicy": "full text when available (retrieved window), otherwise the complete abstract",
        "batch": {"maxItems": MAX_BATCH_ITEMS, "groupedBy": "evidence tier"},
        "settings": {"temperature": 0, "maxTokens": "max(3000, 700 * items)"},
        "repairPasses": 1,
        "sanitizer": SANITIZER_VERSION,
        "authorYearResolver": RESOLVER_VERSION,
        "integrity": INTEGRITY_VERSION,
        "extractor": EXTRACTOR_VERSION,
        "errorCategories": ERROR_CATEGORIES,
    }


def instrument_digest(definition: Mapping[str, Any]) -> str:
    return sha256_text(canonical(definition))


def judge_settings(items: int) -> dict[str, Any]:
    return {"temperature": 0, "max_tokens": max(3000, 700 * items)}


# Egress ledger and confidential policy -----------------------------------------------------

class EgressLedger:
    """Append-only JSON Lines with a per-event hash chain; records names and digests only.

    The run ledger travels with the run. With a ``workspace``, every decision is also
    appended to that workspace's egress ledger (confidentiality.append_egress_event),
    linked by the run record's hash, so the workspace record of what left it is
    complete; allowed provider fetches are already recorded there by the scholarly
    client and are not repeated.
    """

    def __init__(self, path: Path, *, workspace: Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace = Path(workspace) if workspace is not None else None
        self._last = "0" * 64
        self._seq = 0
        if self.path.exists():
            for event in self.events():
                self._last, self._seq = event["hash"], event["seq"]

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def append(self, **event: Any) -> dict[str, Any]:
        record = {"seq": self._seq + 1, "at": now(), **event, "prev": self._last}
        record["hash"] = sha256_text(canonical(record))
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(canonical(record) + "\n")
        self._last, self._seq = record["hash"], record["seq"]
        if self.workspace is not None and not (record.get("operation") == "http-get"
                                               and record.get("decision") == "allowed"):
            append_egress_event(self.workspace, {
                "timestamp": record["at"], "provider": record.get("destination"), "operation": record.get("operation"),
                "decision": record.get("decision"), "argumentNames": record.get("argumentNames", []),
                "payloadSha256": record.get("payloadSha256"),
                **{key: record[key] for key in ("destinationClass", "contentClass", "items", "policyDigest")
                   if key in record},
                "source": TASK, "runLedgerSha256": record["hash"]})
        return record

    def verify(self) -> dict[str, Any]:
        previous = "0" * 64
        events = self.events()
        for index, event in enumerate(events, 1):
            body = {key: value for key, value in event.items() if key != "hash"}
            if event.get("seq") != index or event.get("prev") != previous or sha256_text(canonical(body)) != event.get("hash"):
                return {"valid": False, "events": len(events), "brokenAt": index}
            previous = event["hash"]
        return {"valid": True, "events": len(events), "head": previous}


def _payload_digest(value: Any) -> str:
    return sha256_text(canonical(value) if not isinstance(value, (bytes, str)) else
                       (value.decode("utf-8", "replace") if isinstance(value, bytes) else value))


@dataclass(frozen=True)
class ConfidentialPolicy:
    """Operator-managed egress policy for one screen run; its digest is recorded with the run.

    ``confidential-manuscript``: only identifiers (DOI, PMID, PMCID) may reach external
    providers, and only an on-prem judge may receive manuscript or cited text.
    ``public-literature``: the manuscript is public; providers and judges are still recorded.
    """

    mode: str = "confidential-manuscript"
    on_prem_hosts: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
    allow_private_networks: bool = True
    provider_hosts: frozenset[str] = frozenset({"doi.org", "api.crossref.org", "www.ebi.ac.uk",
                                                 "eutils.ncbi.nlm.nih.gov"})

    MODES = ("confidential-manuscript", "public-literature")

    def __post_init__(self) -> None:
        if self.mode not in self.MODES:
            raise ValidationError(f"unknown policy mode: {self.mode}")

    @property
    def confidential(self) -> bool:
        return self.mode == "confidential-manuscript"

    def record(self) -> dict[str, Any]:
        return {"policy": "claim-check-egress", "version": 1, "mode": self.mode,
                "onPremHosts": sorted(self.on_prem_hosts), "allowPrivateNetworks": self.allow_private_networks,
                "providerHosts": sorted(self.provider_hosts)}

    @property
    def digest(self) -> str:
        return sha256_text(canonical(self.record()))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ConfidentialPolicy:
        known = {"policy", "version", "mode", "onPremHosts", "allowPrivateNetworks", "providerHosts"}
        if set(value) - known:
            raise ValidationError(f"unknown policy fields: {sorted(set(value) - known)}")
        if value.get("policy", "claim-check-egress") != "claim-check-egress" or value.get("version", 1) != 1:
            raise ValidationError("unsupported claim-check policy")
        hosts = value.get("onPremHosts", sorted(cls.on_prem_hosts))
        providers = value.get("providerHosts", sorted(cls.provider_hosts))
        if not all(isinstance(item, str) and item for item in [*hosts, *providers]):
            raise ValidationError("policy host lists must contain non-empty strings")
        private = value.get("allowPrivateNetworks", True)
        if not isinstance(private, bool):
            raise ValidationError("allowPrivateNetworks must be boolean")
        return cls(str(value.get("mode", "confidential-manuscript")), frozenset(h.lower() for h in hosts), private,
                   frozenset(h.lower() for h in providers))

    @classmethod
    def load(cls, path: Path | None) -> ConfidentialPolicy:
        if path is None:
            return cls()
        data = Path(path).read_bytes()
        if len(data) > 64 * 1024:
            raise ValidationError("policy file exceeds 64 KiB")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValidationError("policy file is not UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("policy file must hold one JSON object")
        return cls.from_mapping(value)

    def egress_plan(self) -> EgressPlan:
        # Free-text queries never leave in confidential mode: identifiers only.
        return EgressPlan(confidential=True, allow_sanitized_queries=False) if self.confidential else \
            EgressPlan(confidential=False)

    def host_is_on_prem(self, host: str) -> bool:
        host = (host or "").strip("[]").lower()
        if host in self.on_prem_hosts:
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return address.is_loopback or (self.allow_private_networks and address.is_private)

    def authorize_provider(self, ledger: EgressLedger | None, provider: str, operation: str,
                           arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Identifier-only egress to a provider; refusal is recorded before it is raised."""

        names = sorted(arguments)
        try:
            if self.confidential:
                for key, value in arguments.items():
                    if key not in IDENTIFIER_PATTERNS or not isinstance(value, str) or \
                            not IDENTIFIER_PATTERNS[key].match(value.strip()):
                        raise ConfidentialityError(f"confidential mode lets only identifiers leave: {key}")
                authorize_egress(provider=provider, operation=operation, arguments=arguments, plan=self.egress_plan())
            content = "identifiers-only" if all(key in IDENTIFIER_PATTERNS for key in arguments) else "public-metadata"
        except ConfidentialityError:
            if ledger is not None:
                ledger.append(destination=provider, destinationClass="external-provider", operation=operation,
                              decision="refused", argumentNames=names, payloadSha256=_payload_digest(dict(arguments)),
                              policyDigest=self.digest)
            raise
        event = {"destination": provider, "destinationClass": "external-provider", "operation": operation,
                 "decision": "allowed", "argumentNames": names, "payloadSha256": _payload_digest(dict(arguments)),
                 "contentClass": content, "policyDigest": self.digest}
        return ledger.append(**event) if ledger is not None else event

    def judge_allowed(self, judge: Any) -> bool:
        """Whether this judge may receive manuscript text under the policy (no ledger entry)."""

        if not self.confidential:
            return True
        return getattr(judge, "location", "") == "on-prem" and self.host_is_on_prem(getattr(judge, "host", ""))

    def authorize_model(self, ledger: EgressLedger | None, judge: Any, *, payload_sha256: str, items: int) -> dict[str, Any]:
        """Manuscript and cited text may reach only an on-prem judge in confidential mode."""

        on_prem = getattr(judge, "location", "") == "on-prem" and self.host_is_on_prem(getattr(judge, "host", ""))
        destination_class = "on-prem-model" if on_prem else "external-model"
        event = {"destination": f"{getattr(judge, 'name', 'judge')}@{getattr(judge, 'host', '')}",
                 "destinationClass": destination_class, "operation": TASK, "argumentNames": ["messages"],
                 "payloadSha256": payload_sha256, "items": items, "contentClass": "manuscript-and-cited-text",
                 "policyDigest": self.digest}
        if self.confidential and not on_prem:
            if ledger is not None:
                ledger.append(**event, decision="refused")
            raise ConfidentialityError("confidential mode sends manuscript text only to an on-prem judge")
        return ledger.append(**event, decision="allowed") if ledger is not None else {**event, "decision": "allowed"}


class PolicyTransport:
    """Wrap a scholarly transport: enforce provider hosts and record every outbound request."""

    def __init__(self, inner: Any, policy: ConfidentialPolicy, ledger: EgressLedger | None):
        self.inner, self.policy, self.ledger = inner, policy, ledger

    def get(self, url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None,
            **kwargs: Any) -> Any:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        names = sorted((params or {}).keys())
        if host not in self.policy.provider_hosts:
            if self.ledger is not None:
                self.ledger.append(destination=host, destinationClass="external-provider", operation="http-get",
                                   decision="refused", argumentNames=names,
                                   payloadSha256=_payload_digest({"url": url, "params": dict(params or {})}),
                                   policyDigest=self.policy.digest)
            raise ConfidentialityError(f"provider host is outside the policy: {host}")
        if self.ledger is not None:
            self.ledger.append(destination=host, destinationClass="external-provider", operation="http-get",
                               decision="allowed", argumentNames=names,
                               payloadSha256=_payload_digest({"url": url, "params": dict(params or {})}),
                               contentClass="identifiers-only" if self.policy.confidential else "public-metadata",
                               policyDigest=self.policy.digest)
        return self.inner.get(url, params=params, headers=headers, **kwargs)

    def get_document(self, url: str, **kwargs: Any) -> Any:
        raise ConfidentialityError("full-text downloads are outside the claim-check egress policy")


def policy_registry_transport(inner: Callable[[str, str], Any], policy: ConfidentialPolicy,
                              ledger: EgressLedger | None) -> Callable[[str, str], Any]:
    def fetch(method: str, url: str) -> Any:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if host not in policy.provider_hosts:
            raise ConfidentialityError(f"registry host is outside the policy: {host}")
        if ledger is not None:
            ledger.append(destination=host, destinationClass="external-provider", operation=f"registry-{method.lower()}",
                          decision="allowed", argumentNames=["doi"], payloadSha256=_payload_digest(url),
                          contentClass="identifiers-only", policyDigest=policy.digest)
        return inner(method, url)
    return fetch


# Judges ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class JudgeResponse:
    text: str
    model: str
    usage: Mapping[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None


class Judge(Protocol):
    name: str
    model: str
    location: str
    host: str

    def complete(self, messages: list[dict[str, str]], *, settings: Mapping[str, Any]) -> JudgeResponse:
        ...


class FunctionJudge:
    """A judge backed by a local callable (an in-process model, a broker adapter, a test)."""

    def __init__(self, function: Callable[[list[dict[str, str]], Mapping[str, Any]], str | JudgeResponse], *,
                 name: str = "function-judge", model: str = "local", location: str = "on-prem", host: str = "localhost"):
        self.function, self.name, self.model, self.location, self.host = function, name, model, location, host

    def complete(self, messages: list[dict[str, str]], *, settings: Mapping[str, Any]) -> JudgeResponse:
        value = self.function(messages, settings)
        return value if isinstance(value, JudgeResponse) else JudgeResponse(str(value), self.model)


class HttpJudge:
    """An OpenAI-compatible chat-completions endpoint (for example an on-prem model server)."""

    def __init__(self, url: str, model: str, *, location: str = "on-prem", name: str | None = None,
                 timeout: float = 300.0, headers: Mapping[str, str] | None = None,
                 opener: Callable[..., Any] | None = None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValidationError("judge URL must be http(s) with a host")
        self.url, self.model, self.location = url, model, location
        self.host = parsed.hostname
        self.name = name or f"http-judge:{parsed.hostname}"
        self.timeout = timeout
        self.headers = dict(headers or {})
        self.opener = opener or urllib.request.urlopen

    def complete(self, messages: list[dict[str, str]], *, settings: Mapping[str, Any]) -> JudgeResponse:
        body = json.dumps({"model": self.model, "messages": messages, **settings}).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", **self.headers})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read(32 * 1024 * 1024).decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as exc:
            raise ValidationError(f"judge request failed: {type(exc).__name__}") from exc
        try:
            choice = payload["choices"][0]
            return JudgeResponse(str(choice["message"]["content"]), str(payload.get("model") or self.model),
                                 payload.get("usage") or {}, choice.get("finish_reason"))
        except (KeyError, IndexError, TypeError) as exc:
            raise ValidationError("judge response is not a chat completion") from exc


# Claims ------------------------------------------------------------------------------------

@dataclass
class ClaimCitation:
    ordinal: int
    cluster_id: str
    marker: str
    style: str
    entry_id: str | None
    resolution: str
    method: str


@dataclass
class Claim:
    claim_id: str
    anchor: str
    section: str
    text: str
    raw_sentence: str
    context: list[str]
    locator: dict[str, Any]
    citations: list[ClaimCitation]
    origin: str
    claim_type: str
    citation_required: bool
    requirement_reason: str


def _mask(text: str, spans: Iterable[tuple[int, int]]) -> str:
    characters = list(text)
    for start, end in spans:
        for position in range(max(0, start), min(len(text), end)):
            characters[position] = " "
    value = re.sub(r"\s+", " ", "".join(characters))
    return re.sub(r"\s+([.,;:!?])", r"\1", value).strip()


def _cluster_citations(cluster: Mapping[str, Any], start_ordinal: int) -> list[ClaimCitation]:
    citations: list[ClaimCitation] = []
    ordinal = start_ordinal
    marker = cluster.get("authorYear")
    if marker:
        for item in marker["items"]:
            citations.append(ClaimCitation(ordinal, cluster["id"], item["text"], cluster["style"],
                                           item["referenceId"] if item["status"] == "resolved" else None,
                                           item["status"], item.get("method", "")))
            ordinal += 1
        return citations
    for reference_id in cluster.get("referenceIds", []):
        citations.append(ClaimCitation(ordinal, cluster["id"], cluster["raw"], cluster["style"], reference_id,
                                       "resolved", cluster["style"]))
        ordinal += 1
    for marker_text in cluster.get("unresolvedMarkers", []):
        status = "ambiguous" if cluster.get("bindingStatus") == "ambiguous" and cluster.get("referenceIds") else "unresolved"
        citations.append(ClaimCitation(ordinal, cluster["id"], marker_text, cluster["style"], None, status, cluster["style"]))
        ordinal += 1
    return citations


def extract_claims(manuscript: Mapping[str, Any]) -> list[Claim]:
    """One claim per prose sentence or cited table cell; citations bound only within it."""

    sections = {section["id"]: section["title"] for section in manuscript.get("sections", [])}
    paragraphs = {paragraph["id"]: paragraph for paragraph in manuscript.get("paragraphs", [])}
    clusters = {cluster["id"]: cluster for cluster in manuscript.get("citationClusters", [])}
    sentences = manuscript.get("sentences", [])
    by_paragraph: dict[str, list[dict[str, Any]]] = {}
    for sentence in sentences:
        by_paragraph.setdefault(sentence["paragraphId"], []).append(sentence)
    masked: dict[str, str] = {}
    for sentence in sentences:
        start = int(sentence["locator"].get("characterStart") or 0)
        masked[sentence["id"]] = _mask(sentence["text"], [
            (clusters[cluster_id]["locator"]["characterStart"] - start, clusters[cluster_id]["locator"]["characterEnd"] - start)
            for cluster_id in sentence.get("citationClusterIds", [])])
    claims: list[Claim] = []
    for sentence in sentences:
        paragraph = paragraphs.get(sentence["paragraphId"], {})
        if paragraph.get("kind") not in {"body", "figure-caption"}:
            continue
        section = sections.get(sentence["sectionId"], "")
        citations: list[ClaimCitation] = []
        for cluster_id in sentence.get("citationClusterIds", []):
            citations.extend(_cluster_citations(clusters[cluster_id], len(citations) + 1))
        text = masked[sentence["id"]]
        if len(re.findall(r"[^\W\d_]{2,}", text)) < 2:
            continue
        siblings = by_paragraph.get(sentence["paragraphId"], [])
        position = next(index for index, item in enumerate(siblings) if item["id"] == sentence["id"])
        # Neighbouring sentences explain the claim; their markers are masked so that no
        # citation carries over.
        context = [masked[item["id"]] for item in siblings[max(0, position - 1):position]] + \
                  [masked[item["id"]] for item in siblings[position + 1:position + 2]]
        origin = _origin(section, text)
        claim_type = _claim_type(text)
        required, reason = citation_requirement(origin, claim_type, text)
        claims.append(Claim(f"claim-{len(claims) + 1:06d}", sentence["id"], section, text, sentence["text"], context,
                            {**sentence["locator"], "section": section}, citations, origin, claim_type,
                            bool(required), reason))
    for table in manuscript.get("tables", []):
        rows = table.get("rows", [])
        for cluster_id in table.get("citationClusterIds", []):
            cluster = clusters[cluster_id]
            location = cluster["locator"]
            row = rows[location["row"] - 1] if location.get("row") and location["row"] - 1 < len(rows) else []
            header = rows[0] if rows else []
            cells = [f"{header[index]}: {value}" if index < len(header) and header[index] and location["row"] > 1 else value
                     for index, value in enumerate(row)]
            text = _mask("; ".join(cells), [])
            text = re.sub(re.escape(cluster["raw"]), " ", text).strip()
            section = sections.get(table["sectionId"], "")
            origin = _origin(section, text)
            claim_type = _claim_type(text)
            required, reason = citation_requirement(origin, claim_type, text)
            anchor = f"{table['id']}:r{location.get('row')}:c{location.get('column')}"
            claims.append(Claim(f"claim-{len(claims) + 1:06d}", anchor, section, text, text, [],
                                {**location, "section": section}, _cluster_citations(cluster, 1), origin, claim_type,
                                bool(required), reason))
    return claims


# Run store ---------------------------------------------------------------------------------

class RunStore:
    """A literature library for one screen run: literature schema plus the claim-check extension."""

    def __init__(self, root: Path, *, create: bool = False):
        self.root = Path(root)
        self.db = self.root / "screen.sqlite3"
        if create:
            if self.db.exists():
                raise ValidationError(f"screen run already exists: {self.root}")
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            (self.root / "sources").mkdir(mode=0o700, exist_ok=True)
            connection = sqlite3.connect(self.db)
            try:
                connection.executescript(LITERATURE_SCHEMA.read_text(encoding="utf-8"))
                ensure_schema(connection)
                connection.commit()
            finally:
                connection.close()
            os.chmod(self.db, 0o600)
        elif not self.db.is_file():
            raise ValidationError(f"no screen run database: {self.db}")

    def connect(self, *, write: bool = False) -> sqlite3.Connection:
        uri = self.db.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if not write:
            connection.execute("PRAGMA query_only=ON")
        return connection

    def artifact(self, connection: sqlite3.Connection, data: bytes, kind: str, media_type: str) -> str:
        digest = sha256_bytes(data)
        path = self.root / "sources" / digest
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
        connection.execute("INSERT OR IGNORE INTO source_artifacts VALUES (?,?,?,?,?,?)",
                           (digest, "sources/" + digest, len(data), media_type, kind, now()))
        return digest

    def read_artifact(self, digest: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
            raise ValidationError("artifact digest must be lowercase SHA-256")
        data = (self.root / "sources" / digest).read_bytes()
        if sha256_bytes(data) != digest:
            raise ValidationError(f"artifact bytes changed: {digest}")
        return data


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Apply the additive claim-check extension (idempotent)."""

    connection.executescript(EXTENSION_SCHEMA.read_text(encoding="utf-8"))


# Evidence and tasks ------------------------------------------------------------------------

def work_evidence_digest(units: Sequence[EvidenceUnit]) -> str:
    return sha256_text(canonical([[unit.unit_id, unit.text_sha256] for unit in units]))


@dataclass
class EvidenceSelection:
    tier: str | None
    units: list[EvidenceUnit]
    complete: bool
    receipt: Any
    reason: str | None = None


def select_evidence(index: EvidenceIndex, work_id: str, claim_text: str, *, k: int = 5,
                    max_characters: int = 11000) -> EvidenceSelection:
    """Full text when the store has it (a BM25 window), otherwise the complete abstract."""

    units = index.units_for(work_id)
    receipt = index.search(claim_text, work_ids=[work_id], k=max(k, 1))
    full_text = [unit for unit in units if unit.kind == "full-text"]
    abstracts = [unit for unit in units if unit.kind == "abstract"]
    if full_text:
        if sum(len(unit.text) for unit in units) <= max_characters:
            return EvidenceSelection("full-text", list(units), all(unit.complete for unit in units), receipt)
        # Claim-targeted window: the best-ranked passages first, then the abstract while the
        # budget lasts; the supplied units keep their document order.
        chosen: list[EvidenceUnit] = []
        budget = max_characters
        by_id = {unit.unit_id: unit for unit in units}
        ranked = [by_id[hit.unit_id] for hit in receipt.hits if hit.unit_id in by_id and by_id[hit.unit_id].kind == "full-text"]
        for unit in ranked + abstracts:
            if unit in chosen or len(unit.text) > budget:
                continue
            chosen.append(unit)
            budget -= len(unit.text)
        if not any(unit.kind == "full-text" for unit in chosen):
            return EvidenceSelection(None, [], False, receipt, "no full-text passage matches the claim terms")
        order = {unit.unit_id: position for position, unit in enumerate(units)}
        return EvidenceSelection("full-text", sorted(chosen, key=lambda unit: order[unit.unit_id]), False, receipt)
    if abstracts:
        if not all(unit.complete for unit in abstracts):
            return EvidenceSelection(None, [], False, receipt, "complete abstract is unavailable")
        return EvidenceSelection("abstract", abstracts, True, receipt)
    return EvidenceSelection(None, [], False, receipt, "source evidence is unavailable")


def error_category(verdict: Mapping[str, Any], claim: Claim, units: Sequence[Mapping[str, str]]) -> str | None:
    support = verdict.get("supportVerdict")
    if claim.claim_type == "quotation" and support in {"contradicted", "not-addressed", "partial-indirect", "scope-caveat"}:
        quoted = re.findall(r"[\"“]([^\"”]{12,})[\"”]", claim.text)
        if quoted and not any(quote in unit["text"] for quote in quoted for unit in units):
            return "misquotes"
    if support == "not-addressed" and not verdict.get("evidence"):
        return "irrelevant"
    return ERROR_CATEGORIES.get(str(support))




# Persistence, tasks and judging --------------------------------------------------------------

@dataclass
class CheckContext:
    store: RunStore
    analysis_id: str
    review_id: str
    manuscript_sha256: str
    index: EvidenceIndex
    policy: ConfidentialPolicy
    ledger: EgressLedger
    definition: dict[str, Any]
    works: Mapping[str, Any] = field(default_factory=dict)
    k: int = 5
    max_evidence_characters: int = 11000


@dataclass
class PlannedTask:
    task: dict[str, Any]
    claim: Claim
    citation: ClaimCitation
    locus_id: str
    entry_id: str
    work_id: str
    tier: str
    retrieval_id: str


def _sanitization_rows(connection: sqlite3.Connection, analysis_id: str, scope: str, subject: str,
                       result: Any) -> None:
    for position, removal in enumerate(result.removals):
        connection.execute("INSERT OR IGNORE INTO sanitization_records VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            "san-" + sha256_text(canonical([analysis_id, scope, subject, position, removal.sha256]))[:24],
            analysis_id, scope, subject, removal.kind, removal.rule or removal.kind, removal.start, removal.end,
            removal.length, removal.sha256, result.version))


def record_retrieval(connection: sqlite3.Connection, context: CheckContext, claim_id: str, ordinal: int | None,
                     scope: str, receipt: Any) -> str:
    retrieval_id = "ret-" + sha256_text(canonical([context.analysis_id, claim_id, ordinal, scope]))[:24]
    connection.execute("INSERT OR IGNORE INTO retrieval_receipts VALUES (?,?,?,?,?,?,?,?,?)", (
        retrieval_id, context.analysis_id, claim_id, ordinal, scope, receipt.receipt_id, receipt.index_digest,
        canonical(receipt.record()), receipt.created_at or now()))
    return retrieval_id


def record_claims(connection: sqlite3.Connection, context: CheckContext, claims: Sequence[Claim]) -> dict[tuple[str, str], str]:
    """Store every claim with its 0..n citations; one citation locus per (claim, entry)."""

    loci: dict[tuple[str, str], str] = {}
    for claim in claims:
        connection.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            claim.claim_id, context.analysis_id, context.review_id, context.manuscript_sha256, claim.anchor,
            canonical(claim.locator), claim.text, sha256_text(claim.text), canonical(claim.context),
            len(claim.citations), int(claim.citation_required), claim.requirement_reason, claim.origin,
            claim.claim_type, EXTRACTOR_VERSION))
        for citation in claim.citations:
            locus_id = None
            if citation.entry_id:
                key = (claim.claim_id, citation.entry_id)
                if key not in loci:
                    locator = canonical({"claim": claim.claim_id, "anchor": claim.anchor, "cluster": citation.cluster_id,
                                         "unit": "sentence"})
                    loci[key] = "locus-" + sha256_text(canonical([citation.entry_id, context.manuscript_sha256, locator]))[:32]
                    connection.execute("INSERT OR IGNORE INTO claim_loci VALUES (?,?,?,?,?,?,?)", (
                        loci[key], citation.entry_id, context.manuscript_sha256, locator, claim.text,
                        canonical(claim.context), EXTRACTOR_VERSION))
                locus_id = loci[key]
            connection.execute("INSERT INTO claim_citations VALUES (?,?,?,?,?,?,?,?,?)", (
                claim.claim_id, citation.ordinal, citation.cluster_id, citation.marker, citation.style,
                citation.entry_id, citation.resolution, citation.method, locus_id))
    return loci


def _blocked(connection: sqlite3.Connection, context: CheckContext, task_key: str, reason: str,
             detail: Mapping[str, Any]) -> None:
    connection.execute("INSERT OR REPLACE INTO processing_state VALUES (?,?,?,?,?,?,?)", (
        context.analysis_id, task_key, TASK, "blocked", 0, canonical({"reason": reason, **detail}), now()))


def plan_tasks(connection: sqlite3.Connection, context: CheckContext, claims: Sequence[Claim],
               identities: Mapping[str, Mapping[str, Any]], loci: Mapping[tuple[str, str], str]) -> list[PlannedTask]:
    """Retrieve, sanitise and build one citation-support@2 task per (claim, cited entry)."""

    planner = TaskPlanner()
    planned: list[PlannedTask] = []
    unit_cache: dict[str, str] = {}
    for claim in claims:
        clean_claim = sanitize_text(claim.text, scope="claim")
        _sanitization_rows(connection, context.analysis_id, "claim", claim.claim_id, clean_claim)
        clean_context = []
        for position, sentence in enumerate(claim.context):
            result = sanitize_text(sentence, scope="context")
            _sanitization_rows(connection, context.analysis_id, "context", f"{claim.claim_id}#{position}", result)
            if result.text:
                clean_context.append(result.text)
        if not claim.citations:
            if claim.citation_required:
                receipt = context.index.search(clean_claim.text, k=context.k)
                record_retrieval(connection, context, claim.claim_id, None, "store", receipt)
            continue
        seen: set[str] = set()
        for citation in claim.citations:
            task_key = f"{claim.claim_id}#c{citation.ordinal}"
            if not citation.entry_id:
                _blocked(connection, context, task_key, f"citation {citation.resolution}", {"marker": citation.marker})
                continue
            if citation.entry_id in seen:
                continue
            seen.add(citation.entry_id)
            identity = identities.get(citation.entry_id, {})
            work_id = identity.get("workId")
            if not work_id or identity.get("state") not in {"resolved", "resolved_unverified"}:
                _blocked(connection, context, task_key, "cited work identity is unresolved",
                         {"entryId": citation.entry_id, "state": identity.get("state", "unresolved")})
                continue
            selection = select_evidence(context.index, work_id, clean_claim.text or claim.text, k=context.k,
                                        max_characters=context.max_evidence_characters)
            retrieval_id = record_retrieval(connection, context, claim.claim_id, citation.ordinal, "cited-work",
                                            selection.receipt)
            if selection.tier is None or not clean_claim.text:
                _blocked(connection, context, task_key, selection.reason or "claim text was removed by sanitisation",
                         {"entryId": citation.entry_id, "workId": work_id})
                continue
            units = []
            for unit in selection.units:
                if unit.unit_id not in unit_cache:
                    result = sanitize_text(unit.text, scope="evidence")
                    _sanitization_rows(connection, context.analysis_id, "evidence", unit.unit_id, result)
                    unit_cache[unit.unit_id] = result.text
                if unit_cache[unit.unit_id]:
                    units.append({"id": unit.unit_id, "text": unit_cache[unit.unit_id]})
            work = context.works.get(work_id)
            title = sanitize_text(getattr(work, "title", "") or "", scope="cited-title").text
            task = {
                "id": task_key, "claim": clean_claim.text, "claimType": claim.claim_type,
                "citationLocus": {"bindingStatus": "exact", "marker": citation.marker, "anchor": claim.anchor,
                                  "section": claim.section},
                "context": clean_context,
                "citedWork": {key: value for key, value in (("title", title), ("year", getattr(work, "year", None)),
                                                            ("container", getattr(work, "container", ""))) if value},
                "identifiers": dict(getattr(work, "identifiers", {}) or {}),
                "evidenceTier": selection.tier, "complete": selection.complete, "evidenceUnits": units,
                "sourceSha256": work_evidence_digest(selection.units),
            }
            if identity.get("state") == "resolved":
                task["identityVerified"] = True
            decision = planner.plan(task, selection.tier)
            if decision["action"] != "judge":
                _blocked(connection, context, task_key, decision["reason"], {"entryId": citation.entry_id})
                continue
            planned.append(PlannedTask(task, claim, citation, loci[(claim.claim_id, citation.entry_id)],
                                       citation.entry_id, work_id, selection.tier, retrieval_id))
    return planned


def _receipt(connection: sqlite3.Connection, context: CheckContext, task_key: str, request: bytes,
             response: bytes | None, judge: Any, served: str, settings: Mapping[str, Any], usage: Mapping[str, Any],
             outcome: str, started: str, extra: Mapping[str, Any]) -> str:
    request_sha = context.store.artifact(connection, request, "judge-request", "application/json")
    response_sha = context.store.artifact(connection, response, "judge-response", "text/plain") if response is not None else None
    receipt_id = "receipt-" + sha256_text(canonical([context.analysis_id, task_key, request_sha, response_sha]))[:32]
    connection.execute("INSERT INTO model_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
        receipt_id, context.analysis_id, task_key, request_sha, response_sha, getattr(judge, "name", "judge"), served,
        canonical({**settings, "location": getattr(judge, "location", ""), "host": getattr(judge, "host", ""),
                   "instrumentDigest": instrument_digest(context.definition), **extra}),
        canonical(dict(usage)), outcome, started, now()))
    return receipt_id


def judge_tasks(connection: sqlite3.Connection, context: CheckContext, planned: Sequence[PlannedTask],
                judge: Any) -> dict[str, Any]:
    """Batch by tier (at most 40 items), one repair pass, receipts for every call."""

    validator = AssessmentValidator()
    planner = TaskPlanner()
    summary = {"calls": 0, "repairCalls": 0, "accepted": 0, "rejected": 0, "failedCalls": 0}
    by_tier: dict[str, list[PlannedTask]] = {}
    for item in planned:
        by_tier.setdefault(item.tier, []).append(item)
    batch_number = 0
    for tier in sorted(by_tier):
        items = by_tier[tier]
        for offset in range(0, len(items), MAX_BATCH_ITEMS):
            batch = items[offset:offset + MAX_BATCH_ITEMS]
            batch_number += 1
            pending = {item.task["id"]: item for item in batch}
            outcomes: dict[str, tuple[str, dict[str, Any], str]] = {}
            for attempt in ("initial", "repair"):
                if not pending:
                    break
                tasks = [item.task for item in pending.values()]
                messages = planner.build_messages(tasks, tier)
                assert_prompt_clean(messages)
                settings = judge_settings(len(tasks))
                request = canonical({"model": getattr(judge, "model", ""), "messages": messages, "settings": settings}).encode()
                context.policy.authorize_model(context.ledger, judge, payload_sha256=sha256_bytes(request), items=len(tasks))
                task_key = f"{TASK}:batch-{batch_number:04d}" + (":repair" if attempt == "repair" else "")
                started = now()
                summary["calls" if attempt == "initial" else "repairCalls"] += 1
                try:
                    response = judge.complete(messages, settings=settings)
                except Exception as error:  # noqa: BLE001 - every failure is a receipt, never a verdict
                    receipt_id = _receipt(connection, context, task_key, request, None, judge, getattr(judge, "model", ""),
                                          settings, {}, "failed", started, {"error": type(error).__name__})
                    summary["failedCalls"] += 1
                    for task_id in pending:
                        outcomes[task_id] = ("rejected", {"code": "transport_failure", "message": type(error).__name__}, receipt_id)
                    break
                receipt_id = _receipt(connection, context, task_key, request, response.text.encode("utf-8"), judge,
                                      response.model, settings, response.usage, "completed", started,
                                      {"finishReason": response.finish_reason, "tier": tier, "items": len(tasks)})
                result = validator.validate_response(tasks, response.text)
                for task_id, verdict in result["accepted"].items():
                    outcomes[task_id] = ("accepted", verdict, receipt_id)
                    pending.pop(task_id, None)
                retry = {}
                for task_id, error in result["rejected"].items():
                    outcomes[task_id] = ("rejected", error, receipt_id)
                    if error.get("retryable"):
                        retry[task_id] = pending[task_id]
                pending = retry
            for task_id, (state, verdict, receipt_id) in outcomes.items():
                item = next(entry for entry in batch if entry.task["id"] == task_id)
                assessment_id = "assessment-" + sha256_text(canonical([context.analysis_id, task_id]))[:32]
                connection.execute("INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?)", (
                    assessment_id, context.analysis_id, item.locus_id, receipt_id, f"{TASK}/{PROTOCOL_VERSION}", tier,
                    canonical({**verdict, "taskId": task_id, "retrievalId": item.retrieval_id}),
                    state, now()))
                summary[state] += 1
                if state == "accepted":
                    for position, quote in enumerate(verdict.get("evidence", [])):
                        connection.execute("INSERT OR IGNORE INTO assessment_evidence VALUES (?,?,?,?)", (
                            assessment_id, quote["unitId"], quote["quote"], f"quote-{position + 1}"))
    return summary


__all__ = [
    "CALIBRATION_STATEMENT", "CheckContext", "Claim", "ClaimCitation", "ConfidentialPolicy", "EgressLedger",
    "ERROR_CATEGORIES", "FunctionJudge", "HttpJudge", "JudgeResponse", "PlannedTask", "PolicyTransport",
    "RunStore", "TASK", "ensure_schema", "error_category", "extract_claims", "instrument_definition",
    "instrument_digest", "judge_settings", "judge_tasks", "plan_tasks", "policy_registry_transport",
    "record_claims", "record_retrieval", "select_evidence", "work_evidence_digest",
]
