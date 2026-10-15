# SPDX-License-Identifier: Apache-2.0
"""Exhaustive cursor-based enumeration with saved provider pages and denominators."""
from __future__ import annotations

from .corpus_store import canonical, identity
from .corpus_library import now
from .errors import ValidationError
from .scholarly import EUROPE_PMC, normalize_europe_pmc


def enumerate_reviews(repo, acquirer, *, query: str, label: str):
    if not query.strip() or not label.strip():
        raise ValidationError("enumeration requires a query and stratum label")
    client = acquirer.client
    key = "enumeration:" + identity(query, label)
    analysis = repo.analysis()["analysis_id"]
    with repo.connect() as connection:
        old = connection.execute("SELECT checkpoint_json FROM processing_state WHERE analysis_id=? AND task_key=? AND phase='enumeration'", (analysis, key)).fetchone()
    import json
    state = json.loads(old[0]) if old else {"query": query, "label": label, "cursor": "*", "pages": [], "records": 0, "complete": False}
    if state["complete"]:
        return state
    while True:
        cursor = state["cursor"]
        client._authorize("europe-pmc", "search_europe_pmc", {"query": query, "cursor": cursor, "rows": 1000})
        response = client.transport.get(f"{EUROPE_PMC}/search", params={"query": query, "cursorMark": cursor, "pageSize": 1000, "resultType": "core", "format": "json"})
        payload = response.json()
        rows = payload.get("resultList", {}).get("result", [])
        total = payload.get("hitCount")
        if not isinstance(rows, list) or not isinstance(total, int) or total < 0:
            raise ValidationError("enumeration provider omitted a valid hit count or result list")
        if "expectedRecords" in state and state["expectedRecords"] != total:
            raise ValidationError("enumeration population changed during paging; restart under a new stratum label")
        state["expectedRecords"] = total
        digest = repo.artifact(response.body, "enumeration-provider-page")
        for raw in rows:
            record = normalize_europe_pmc(raw)
            record.update(label=label, stratum=label, journal=record.get("container"))
            if not any(record.get(k) for k in ("doi", "pmid", "pmcid")):
                record["id"] = "provider-record:" + identity(raw)
            repo.review(record, reason="enumeration:" + key)
        state["records"] += len(rows)
        state["pages"].append(digest)
        next_cursor = payload.get("nextCursorMark")
        if state["records"] >= total:
            if state["records"] != total:
                raise ValidationError("enumeration returned more records than its declared population")
            state["complete"] = True
        elif not rows or not next_cursor or next_cursor == cursor:
            raise ValidationError("enumeration stopped before its declared population was exhausted")
        state["cursor"] = next_cursor
        with repo.connect(write=True) as connection:
            connection.execute("INSERT INTO processing_state VALUES (?,?,?,'running',0,?,?) ON CONFLICT(analysis_id,task_key,phase) DO UPDATE SET checkpoint_json=excluded.checkpoint_json,status=excluded.status,updated_at=excluded.updated_at", (analysis, key, "enumeration", canonical(state), now()))
            if state["complete"]:
                connection.execute("UPDATE processing_state SET status='completed' WHERE analysis_id=? AND task_key=? AND phase='enumeration'", (analysis, key))
        if state["complete"]:
            return state
