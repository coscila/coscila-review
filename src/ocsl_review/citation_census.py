# SPDX-License-Identifier: Apache-2.0
"""citation-census@1: the entry-level citation support instrument.

This module is the instrument's builder and validator; its digest is part of
the instrument digest. It holds only pure functions: prompt construction for
the abstract and full-text tiers, the label parser, batch geometry, the
claim-targeted full-text window, the JATS-to-text conversion, deterministic
rows and the three-class map. Transport, retrieval and storage live in
``census_runner``; changing them never changes the instrument.

Item input (one per printed bibliography entry)::

    {"label": "12",
     "citing_passages": [{"sentence": "... ⟨rid⟩ ...", "section": "Results > Subgroups"}, ...],
     "cited_work": {"title": ..., "venue": ..., "year": ..., "retracted": false},
     "abstract": "..."}

Only these fields reach the judge. Identifiers, the printed entry and the
abstract provider are recorded beside the verdict, never sent.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from functools import lru_cache
from typing import Any, Callable, Iterable, Sequence
from xml.etree import ElementTree as ET

from .errors import ValidationError

INSTRUMENT_ID = "citation-census@1"
JOURNAL_TOKEN = "{{journal}}"
CATEGORIES = {"T": "true", "F": "false", "N": "not_assessable", "U": "unsure"}
SUPPORT_TYPES = {"direct": "direct-support", "scope": "scope-caveat", "partial": "partial-indirect",
                 "contra": "contradicted", "notaddr": "not-addressed", "abstain": "abstain"}
CLASSES = ("sure_supported", "unsure", "sure_not_supported")
NOT_CITED = "not_cited"
_WINDOW_STOPWORDS = ("with", "that", "this", "from", "were", "have", "which", "their", "these", "been", "also", "than",
                     "into", "study", "patients", "cancer", "cells", "tumor", "tumour")


@lru_cache(maxsize=1)
def instrument():
    from .instruments import load
    return load(INSTRUMENT_ID)


def settings(tier: str = "abstract") -> dict[str, Any]:
    return instrument().tier(tier)


def abstract_cut() -> int:
    return int(settings("abstract")["evidencePolicy"]["abstract"]["cutCharacters"])


def max_citing_sentences() -> int:
    return int(settings("abstract")["evidencePolicy"]["citingSentences"]["max"])


def items_per_call(tier: str = "abstract") -> int:
    return int(settings(tier)["geometry"]["itemsPerCall"])


def window_characters() -> int:
    return int(settings("full-text")["evidencePolicy"]["window"]["characters"])


def repair_note() -> str:
    return settings("abstract")["repair"]["note"]


def max_tokens(items: int, tier: str = "abstract") -> int:
    rule = settings(tier)["request"]["maxOutputTokens"]
    if not isinstance(items, int) or items < 1:
        raise ValidationError("a call needs at least one item")
    return max(int(rule["floor"]), int(rule["perItem"]) * items)


# ----------------------------------------------------------------------------- prompts
def system_prompt(journal: str) -> str:
    if not isinstance(journal, str) or not journal.strip() or "\n" in journal or JOURNAL_TOKEN in journal:
        raise ValidationError("the journal parameter must be a one-line journal name")
    template = instrument().text("systemPrompt")
    if template.count(JOURNAL_TOKEN) != 1:
        raise ValidationError("the system prompt must contain the journal parameter exactly once")
    return template.replace(JOURNAL_TOKEN, journal)


def full_text_note() -> str:
    return instrument().text("fullTextNote")


def _abstract_text(item: dict[str, Any]) -> str:
    return (item.get("abstract") or "").strip().replace("\n", " ")


def _limit(cut: int | None | bool) -> int | None:
    return abstract_cut() if cut is True else (None if cut is None else int(cut))


def abstract_evidence(item: dict[str, Any], cut: int | None | bool = True) -> dict[str, Any]:
    """What the builder sends for an entry's abstract, recorded beside the verdict (same ``cut`` as the builder)."""
    text = _abstract_text(item)
    limit = _limit(cut)
    sent = (text[:limit] if limit is not None else text) if text else ""
    return {"rawCharacters": len(item.get("abstract") or ""), "characters": len(text), "sentCharacters": len(sent),
            "cut": bool(text) and limit is not None and len(text) > limit, "cutCharacters": limit,
            "available": bool(text), "sentSha256": hashlib.sha256(sent.encode("utf-8")).hexdigest() if sent else None}


def build_abstract_messages(items: Sequence[dict[str, Any]], review_title: str, journal: str, *,
                            note: str = "", cut: int | None | bool = True) -> list[dict[str, str]]:
    """Messages for one abstract-tier call (a first call, or the repair call with ``note``).

    ``cut=True`` applies the instrument's abstract cut; ``cut=None`` is the
    predeclared no-truncation sensitivity; an integer overrides the cut.
    """
    limit = _limit(cut)
    passages = max_citing_sentences()
    lines = ["Review article under audit: %s" % review_title]
    if note:
        lines.append(note)
    lines.append("")
    lines.append("ITEMS (%d):" % len(items))
    for it in items:
        cw = it.get("cited_work") or {}
        lines.append("### id=%s" % it["label"])
        for p in (it.get("citing_passages") or [])[:passages]:
            s = (p.get("sentence") or "").strip().replace("\n", " ")
            if s:
                lines.append("REVIEW SAYS: %s%s" % (("[%s] " % p.get("section")) if p.get("section") else "", s))
        lines.append("CITED WORK: %s | %s | %s%s" % (
            cw.get("title") or "(no title)", cw.get("venue") or "?", cw.get("year") or "?",
            " | RETRACTED" if cw.get("retracted") else ""))
        ab = _abstract_text(it)
        lines.append("ABSTRACT: %s" % ((ab[:limit] if limit is not None else ab) if ab else "(none available)"))
    lines.append("")
    lines.append("Emit exactly %d JSONL lines. Each line's \"l\" must be the value after id=, "
                 "copied exactly (no \"item \" prefix). Nothing else." % len(items))
    return [{"role": "system", "content": system_prompt(journal)},
            {"role": "user", "content": "\n".join(lines)}]


def build_repair_messages(items: Sequence[dict[str, Any]], review_title: str, journal: str, *,
                          cut: int | None | bool = True) -> list[dict[str, str]]:
    return build_abstract_messages(items, review_title, journal, note=repair_note(), cut=cut)


def build_full_text_messages(items: Sequence[dict[str, Any]], review_title: str, journal: str) -> list[dict[str, str]]:
    """Messages for one full-text call; each item carries ``fulltext_window``."""
    passages = int(settings("full-text")["evidencePolicy"]["citingSentences"]["max"])
    lines = ["Review article under audit: %s" % review_title, "", full_text_note(), "", "ITEMS (%d):" % len(items)]
    for it in items:
        lines.append("### id=%s" % it["label"])
        for p in it["citing_passages"][:passages]:
            lines.append("REVIEW SAYS: [%s] %s" % (p.get("section", ""), p["sentence"]))
        cw = it["cited_work"]
        lines.append("CITED WORK: %s | %s | %s" % (cw.get("title") or "", cw.get("venue") or "", cw.get("year") or ""))
        lines.append("FULL TEXT OF THE CITED WORK (%d characters):" % len(it["fulltext_window"]))
        lines.append(it["fulltext_window"])
        lines.append("")
    lines.append("Emit exactly %d JSONL lines. Each line's \"l\" must be the value after id=, copied exactly. "
                 "Nothing else." % len(items))
    return [{"role": "system", "content": system_prompt(journal)},
            {"role": "user", "content": "\n".join(lines)}]


def request_payload(messages: list[dict[str, str]], *, model: str, items: int, tier: str = "abstract") -> dict[str, Any]:
    """The chat-completions request; key order matches the bytes the instrument sends."""
    request = settings(tier)["request"]
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens(items, tier),
               "temperature": float(request["temperature"]), "stream": False}
    if request.get("reasoningEffort"):
        payload["reasoning_effort"] = request["reasoningEffort"]
    return payload


def request_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def digests(messages: list[dict[str, str]], body: bytes | None = None) -> dict[str, str]:
    """Receipt digests: system and user text, the message list, and the sent body."""
    system = next(m["content"] for m in messages if m["role"] == "system")
    user = "\n".join(m["content"] for m in messages if m["role"] != "system")
    result = {"systemSha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
              "userSha256": hashlib.sha256(user.encode("utf-8")).hexdigest(),
              "messagesSha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()}
    if body is not None:
        result["bodySha256"] = hashlib.sha256(body).hexdigest()
    return result


# ----------------------------------------------------------------------------- validator
def parse_labels(text: str, want_labels: Sequence[str], stage: str = "abstract"):
    """Return (records_by_label, problems, missing_labels) from one model output."""
    out, problems = {}, []
    want = set(want_labels)
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        i = s.find("{")
        if i < 0:
            if s.count(chr(34)) > 1 or s.endswith(chr(125)):
                problems.append("non-json line: " + s[:60])
            continue
        s = s[i:]
        try:
            o = json.loads(s[:s.rfind("}") + 1]) if s.count("{") <= 1 else json.loads(s)
        except Exception:
            try:
                o = json.loads(s[:s.rfind("}") + 1])
            except Exception:
                problems.append("unparseable: " + s[:70])
                continue
        lab = str(o.get("l", "")).strip()
        if lab not in want:
            alt = lab.lower().replace("item", "").replace("id", "").replace("=", "").strip(
                chr(32) + chr(34) + chr(39) + chr(96) + chr(35) + chr(45) + chr(58))
            if alt in want:
                lab = alt
        if lab not in want:
            problems.append("unexpected label %r" % lab)
            continue
        cat = str(o.get("c", "")).strip().upper()[:1]
        if cat not in CATEGORIES:
            problems.append("bad category for %s: %r" % (lab, o.get("c")))
            continue
        esc = str(o.get("e", "")).strip().lower() in ("1", "true")
        v = str(o.get("v", "")).strip().lower()
        conf = "not-confident" if (esc or cat in ("N", "U")) else "confident"
        try:
            rec = {"label": lab, "category": CATEGORIES[cat], "support_verdict": SUPPORT_TYPES.get(v, "abstain"),
                   "confidence": conf,
                   "evidence_level": "abstract-sufficient" if stage == "abstract" else "full-text",
                   "resolution_status": "unresolved" if cat == "U" else "resolved",
                   "escalate": 1 if esc else 0,
                   "quote": (o.get("q") or "").strip()[:600] if cat == "F" else "",
                   "reason": (o.get("r") or "").strip()[:400],
                   "stage": stage}
        except (AttributeError, TypeError):
            # A non-string quote or reason is a malformed line, never a crash of the whole call.
            problems.append("unparseable: " + s[:70])
            continue
        if cat == "F" and not rec["quote"]:
            rec["quote_missing"] = 1
        out[lab] = rec
    missing = [l for l in want_labels if l not in out]
    return out, problems, missing


def quote_check(record: dict[str, Any], evidence_text: str) -> dict[str, Any]:
    """Quote presence and substring validity, recorded per entry (the quote rule is an instruction only)."""
    quote = record.get("quote") or ""
    if record.get("category") != "false":
        return {"quotePresent": None, "quoteInEvidence": None}
    if not quote:
        return {"quotePresent": False, "quoteInEvidence": False}
    normalise = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
    return {"quotePresent": True, "quoteInEvidence": normalise(quote) in normalise(evidence_text or "")}


# ----------------------------------------------------------------------------- geometry
def paper_batches(items: Sequence[dict[str, Any]], size: int | None = None) -> list[list[dict[str, Any]]]:
    """Cut one review's judged entries, in bibliography order, into calls; never across reviews."""
    width = items_per_call("abstract") if size is None else size
    return [list(items[i:i + width]) for i in range(0, len(items), width)]


def full_text_calls(escalations: Sequence[Any], seed: int, *, review: Callable[[Any], str],
                    retrievable: Callable[[Any], bool], size: int | None = None) -> list[tuple[str, list[Any]]]:
    """Seeded shuffle, grouping by review in first-seen order, then calls of ``size`` retrievable entries."""
    width = items_per_call("full-text") if size is None else size
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValidationError("the full-text geometry needs an integer protocol seed")
    shuffled = list(escalations)
    random.Random(seed).shuffle(shuffled)
    grouped: dict[str, list[Any]] = {}
    for entry in shuffled:
        grouped.setdefault(review(entry), []).append(entry)
    calls = []
    for key, entries in grouped.items():
        kept = [entry for entry in entries if retrievable(entry)]
        calls.extend((key, kept[i:i + width]) for i in range(0, len(kept), width))
    return calls


# ----------------------------------------------------------------------------- full text
def full_text_window(text: str, claim: str, budget: int | None = None) -> str:
    """Claim-targeted window: opening paragraphs and the abstract, then keyword-scored paragraphs."""
    budget = window_characters() if budget is None else budget
    if len(text) <= budget:
        return text
    paras = text.split("\n")
    terms = [t.lower() for t in set(re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", claim or ""))
             if t.lower() not in _WINDOW_STOPWORDS]
    scored = sorted(((sum(1 for t in terms if t in p.lower()), -i, i) for i, p in enumerate(paras) if p.strip()),
                    reverse=True)
    keep = set(i for i, p in enumerate(paras) if p.startswith("## Abstract") or (i < 4))
    total = sum(len(paras[i]) for i in keep)
    for s, _, i in scored:
        if s == 0:
            break
        if total + len(paras[i]) > budget:
            continue
        keep.add(i)
        total += len(paras[i])
    out = "\n".join(paras[i] for i in sorted(keep))
    return out[:budget]


def claim_text(passages: Iterable[dict[str, Any]]) -> str:
    return " ".join(p["sentence"] for p in passages)


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def jats_to_text(xml_bytes: bytes) -> str:
    """Section-headed plain text from JATS: abstract, section titles, paragraphs, tables and figure captions."""
    if re.search(br"<!ENTITY\b", xml_bytes, re.I):
        raise ValidationError("full-text XML contains entity declarations")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as error:
        raise ValidationError("full-text XML is malformed") from error
    parents = {child: parent for parent in root.iter() for child in parent}

    def ancestors(element):
        while element in parents:
            element = parents[element]
            yield element

    def flat(element) -> str:
        return re.sub(r"\s+", " ", "".join(element.itertext())).strip()

    out = []
    body = root.find(".//{*}body")
    abst = root.find(".//{*}abstract")
    if abst is not None:
        out.append("## Abstract\n" + flat(abst))
    if body is None:
        return "\n".join(out)
    for el in body.iter():
        t = _local(el.tag)
        if t == "title" and el in parents and _local(parents[el].tag) == "sec":
            out.append("\n## " + flat(el))
        elif t == "p" and not any(_local(a.tag) in ("table-wrap", "fig") for a in ancestors(el)):
            out.append(flat(el))
        elif t == "table-wrap":
            cap = el.find(".//{*}caption")
            rows = []
            for tr in (node for node in el.iter() if _local(node.tag) == "tr"):
                rows.append(" | ".join(flat(c) for c in tr if _local(c.tag) in ("td", "th")))
            out.append("\n[TABLE] " + (flat(cap) if cap is not None else "") + "\n" + "\n".join(rows[:60]))
        elif t == "fig":
            cap = el.find(".//{*}caption")
            if cap is not None:
                out.append("[FIGURE] " + flat(cap))
    return "\n".join(x for x in out if x)


# ----------------------------------------------------------------------------- rows and classes
def deterministic_record(*, cited: bool, identity_resolved: bool) -> dict[str, Any] | None:
    """Rows decided without the judge. Never cited is its own class; an unresolved identity is unsure."""
    if not cited:
        return {"category": None, "support_verdict": None, "confidence": None, "evidence_level": None,
                "resolution_status": None, "escalate": 0, "quote": "", "reason": "printed in the bibliography but "
                "never cited in the body", "basis": "deterministic:not-cited", "class": NOT_CITED}
    if not identity_resolved:
        return {"category": "unsure", "support_verdict": "abstain", "confidence": "not-confident",
                "evidence_level": "metadata-sufficient", "resolution_status": "unresolved", "escalate": 0, "quote": "",
                "reason": "no identified work for this entry", "basis": "deterministic:unresolved-identity",
                "class": "unsure"}
    return None


def abstract_class(category: str | None, escalate: Any, *, cited: bool = True, identity_resolved: bool = True,
                   model_judged: bool = True) -> str:
    if not cited:
        return NOT_CITED
    if not identity_resolved or not model_judged:
        return "unsure"
    if category == "true" and not escalate:
        return "sure_supported"
    if category == "false" and not escalate:
        return "sure_not_supported"
    return "unsure"


def full_text_class(category: str | None) -> str:
    return {"true": "sure_supported", "false": "sure_not_supported"}.get(category, "unsure")
