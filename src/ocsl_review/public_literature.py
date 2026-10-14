"""Bind public census disclosure to the active, user-configured Lab worker.

This is an additional narrow authorization route, not a nonconfidential default.
The broker's trusted worker configuration grants the permission; workspace flags
only classify the material and cannot grant it. Explicit operator plans prevail.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
from pathlib import Path
from typing import Any

from .confidentiality import _assert_no_symlink_components, _audit_event
from .errors import ConfidentialityError
from .util import is_within
from .worker_configuration import matches_pinned_worker_configuration


def broker_catalog() -> dict[str, Any]:
    """Read authority directly from the scoped broker, not a workspace script."""
    address = os.environ.get("PUPPENCLAW_ORCHESTRATION_SOCKET", "")
    if not os.path.isabs(address) or "\0" in address:
        raise ConfidentialityError("public literature dispatch requires the active Lab worker broker")
    connection = http.client.HTTPConnection("localhost", timeout=10)
    try:
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(10)
        connection.sock.connect(address)
        connection.request("GET", "/configs")
        response = connection.getresponse()
        body = response.read(4 * 1024 * 1024 + 1)
        if response.status != 200 or len(body) > 4 * 1024 * 1024:
            raise ValueError("worker authority unavailable or too large")
        catalog = json.loads(body)
        if not isinstance(catalog, dict):
            raise ValueError("invalid worker authority")
        return catalog
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ConfidentialityError("cannot verify public literature permission from the active Lab broker") from exc
    finally:
        connection.close()


def authorize_public_census(workspace: Path, run: dict[str, Any],
                            tasks: list[dict[str, Any]]) -> dict[str, Any]:
    from . import literature as L

    catalog = broker_catalog()
    authority = catalog.get("authorization", {})
    if (not isinstance(authority, dict) or authority.get("publicLiteratureAllowed") is not True
            or not authority.get("attemptId")
            or authority["attemptId"] != os.environ.get("PUPPENCLAW_ORCHESTRATION_ATTEMPT_ID")):
        raise ConfidentialityError("public literature dispatch requires an approved execution attempt")
    parent = Path(authority.get("workspace", ""))
    if not parent.is_absolute():
        raise ConfidentialityError("worker authority has no absolute workspace boundary")
    _assert_no_symlink_components(parent, label="worker authority workspace")
    if not is_within(workspace, parent.resolve()):
        raise ConfidentialityError("public literature permission belongs to another chat workspace")
    spec = run["spec"]
    workers = catalog.get("workers")
    if not isinstance(workers, list):
        raise ConfidentialityError("worker authority has no configured workers")
    matches = [worker for worker in workers
               if isinstance(worker, dict) and worker.get("id") == spec["workerConfigId"]]
    if (catalog.get("revision") != spec["workerConfigRevision"] or len(matches) != 1
            or not matches_pinned_worker_configuration(spec, matches[0])
            or matches[0].get("evidencePolicy") != "public-literature"):
        raise ConfidentialityError("selected worker has no matching public-literature permission; enable it in Lab and prepare a new run")

    state = L._read_json(L._path(workspace, L.LEDGER_PATH, exists=True))
    manuscript = L._read_json(L._path(workspace, state["manuscriptPath"], exists=True))
    if (manuscript.get("confidential") is not False
            or manuscript.get("source", {}).get("sha256") != spec["sourceManuscriptSha256"]
            or L.digest(manuscript) != spec.get("sourceManuscriptArtifactSha256")):
        raise ConfidentialityError("public literature permission excludes confidential or changed manuscripts")
    publication = manuscript.get("structuredSource", {}).get("publication", {})
    if not any(publication.get(key) for key in ("doi", "citation_doi", "pmid", "pmcid", "pmc")):
        raise ConfidentialityError("public literature permission requires an identified published manuscript; local attachments need an explicit operator plan")
    sources = []
    for task in tasks:
        source = task.get("source") or {}
        if (source.get("provider") not in {"pubmed", "europe-pmc", "unpaywall"}
                or not str(source.get("sourceUrl", "")).startswith("https://")):
            raise ConfidentialityError("public literature permission excludes sources without public acquisition provenance")
        document = L._verified_source(workspace, source)
        payload = json.loads(task["input"]["messages"][1]["content"])
        if (payload.get("sourceSha256") != source["artifactSha256"]
                or payload.get("evidenceUnits") != document["blocks"]):
            raise ConfidentialityError("worker evidence differs from the verified public source")
        sources.append({"artifactSha256": source["artifactSha256"],
                        "documentSha256": source["documentSha256"]})
    event = _audit_event("platform-worker:" + spec["workerConfigId"], "citation_census",
                         {"manuscriptEvidence": [task["input"] for task in tasks],
                          "workerAssignment": matches[0]["assignment"]},
                         decision="allowed-configured-public-literature")
    event.update({"attemptId": authority["attemptId"], "workerConfigSha256": spec["workerConfigSha256"],
                  "workerConfigRevision": catalog["revision"],
                  "sourceManifestSha256": L.digest({"manuscript": spec["sourceManuscriptSha256"], "sources": sources})})
    return event
