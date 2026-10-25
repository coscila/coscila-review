# SPDX-License-Identifier: Apache-2.0
"""Deterministic, labelled author-year corpus in JATS form (synthetic; Apache-2.0).

Each generated article is open JATS: citations are <xref ref-type="bibr" rid="..."> elements
inside running prose and the bibliography is a <ref-list> of printed references. The xref
targets are the gold labels. The evaluation strips the markup, renders the article as Markdown
(the form an author's draft takes), ingests it, and scores the author-year resolver against
the gold targets: a link is one cited item bound to one reference-list entry.
"""

from __future__ import annotations

import hashlib
import random
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

SURNAMES = [
    "Smith", "Johnson", "Chen", "Kim", "Nguyen", "Okafor", "Patel", "Rossi", "Kowalski", "Haddad",
    "Müller", "García", "Søndergaard", "Ødegaard", "Łukasiewicz", "Dvořák", "Núñez", "Çelik",
    "García-López", "Smith-Jones", "Lévy-Bruhl", "van der Berg", "de Souza", "van Dijk", "von Braun",
    "Da Silva", "de la Cruz", "Le Roux", "ter Haar", "O'Brien", "D'Angelo", "McDonald", "MacLeod",
    "Ivanova", "Tanaka", "Yamamoto", "Abebe", "Mensah", "Fischer", "Schmidt", "Andersson", "Nilsson",
    "Brown", "Wilson", "Taylor", "Anderson", "Thomas", "Moore", "Martin", "Jackson", "Lee", "Park",
    "Choi", "Wang", "Li", "Zhang", "Liu", "Singh", "Kumar", "Silva", "Santos", "Oliveira", "Costa",
    "Bianchi", "Romano", "Dubois", "Lefebvre", "Moreau", "Novak", "Horvat", "Popescu", "Kaya",
    "Hosseini", "Rahman", "Cohen", "Levi", "Friedman", "Murphy", "Kelly", "Walsh", "Byrne",
]
CORPORATE = [
    ("World Health Organization", "WHO"),
    ("National Cancer Institute", "NCI"),
    ("European Centre for Disease Prevention and Control", "ECDC"),
    ("International Agency for Research on Cancer", "IARC"),
    ("GTEx Consortium", None),
]
GIVEN = ["Anna", "Ben", "Carla", "David", "Elena", "Farid", "Grace", "Hiro", "Ines", "Jonas", "Kofi",
         "Lena", "Marta", "Nikhil", "Olga", "Pablo", "Qing", "Rosa", "Sven", "Tomás", "Uma", "Victor"]
WORDS = ["immune", "signalling", "cohort", "tumour", "response", "metabolic", "pathway", "variant",
         "expression", "network", "clinical", "outcome", "regulation", "model", "single-cell",
         "transcriptome", "microbiome", "inflammation", "resistance", "biomarker", "therapy", "risk"]
JOURNALS = ["Journal of Example Biology", "Example Methods", "Synthetic Reports", "Archives of Test Medicine",
            "Open Example Science", "Review of Placeholder Studies"]
FILLER = [
    "The mechanism remains incompletely understood",
    "Several groups have examined this question",
    "These observations motivate the present analysis",
    "Effect sizes differed across settings",
    "Measurement protocols were heterogeneous",
    "Replication in independent cohorts is limited",
]
DISTRACTORS = [
    "(n = 2,019 participants)", "(between 2018 and 2020)", "(Figure 2)", "(approved in 2017)",
    "(95% CI 1.2-2.0)", "(in 2019)", "(Phase 3, 2021)", "(Table 1)", "(see Methods)", "(since 2015)",
]
NARRATIVE_DISTRACTORS = ["In the United States (2020)", "During June (2021)", "In Europe (2019)"]
REFERENCE_STYLES = ("apa", "harvard", "compact", "name-year", "chicago", "inverted-full")
CITATION_STYLES = ("apa", "harvard", "chicago")
# Author-side inconsistencies, fixed before any evaluation was run. The gold target stays the
# intended work (the xref target), so a resolver that refuses a garbled marker loses recall and
# one that binds the wrong entry loses precision.
NOISE = {
    "year_typo": 0.01,        # printed citation year off by one
    "drop_et_al": 0.02,       # a work with three or more authors cited by its first surname alone
    "et_al_for_two": 0.02,    # "et al." for a two-author work
    "ascii_fold": 0.05,       # diacritics dropped in the citation
    "particle_case": 0.03,    # "Van der Berg" for "van der Berg"
    "drop_particle": 0.02,    # "Berg" for "van der Berg"
    "omit_suffix": 0.01,      # "2020" for "2020a"
    "nbsp_et_al": 0.05,       # a no-break space inside "et al."
    "wrap_reference": 0.10,   # a reference printed over two lines
}
_FOLD = str.maketrans({"ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "æ": "ae", "Æ": "Ae"})


def _ascii(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.translate(_FOLD))
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def _noisy_surname(name: str, rng: random.Random) -> str:
    if rng.random() < NOISE["ascii_fold"]:
        name = _ascii(name)
    parts = name.split()
    if len(parts) > 1 and parts[0].islower():
        roll = rng.random()
        if roll < NOISE["drop_particle"]:
            name = parts[-1]
        elif roll < NOISE["drop_particle"] + NOISE["particle_case"]:
            name = parts[0].capitalize() + " " + " ".join(parts[1:])
    return name


def _noisy_year(reference: "Reference", rng: random.Random) -> str:
    year = reference.year
    if rng.random() < NOISE["year_typo"]:
        year += rng.choice([-1, 1])
    suffix = "" if rng.random() < NOISE["omit_suffix"] else reference.suffix
    return f"{year}{suffix}"


def _et_al(rng: random.Random) -> str:
    return "et\u00a0al." if rng.random() < NOISE["nbsp_et_al"] else "et al."


@dataclass
class Reference:
    rid: str
    surnames: list[str]
    given: list[str]
    corporate: str | None
    alias: str | None
    year: int
    title: str
    journal: str
    volume: int
    pages: tuple[int, int]
    doi: str
    suffix: str = ""


@dataclass
class Article:
    seed: int
    reference_style: str
    citation_style: str
    references: list[Reference]
    paragraphs: list[list[tuple[str, str | None]]] = field(default_factory=list)


def _initials(given: str, *, periods: bool = True, spaced: bool = True) -> str:
    letters = [part[0] for part in re.split(r"[\s\-]+", given) if part]
    if not periods:
        return "".join(letters)
    return (" " if spaced else "").join(letter + "." for letter in letters)


def _cited_names(reference: Reference, style: str, rng: random.Random) -> str:
    if reference.corporate:
        return reference.corporate
    names = [_noisy_surname(name, rng) for name in reference.surnames]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        if rng.random() < NOISE["et_al_for_two"]:
            return names[0] + " " + _et_al(rng)
        return names[0] + (" & " if style == "apa" else " and ") + names[1]
    if rng.random() < NOISE["drop_et_al"]:
        return names[0]
    return names[0] + " " + _et_al(rng)


def _narrative_names(reference: Reference, rng: random.Random) -> str:
    if reference.corporate:
        return reference.corporate
    names = [_noisy_surname(name, rng) for name in reference.surnames]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        if rng.random() < NOISE["et_al_for_two"]:
            return names[0] + " " + _et_al(rng)
        return names[0] + " and " + names[1]
    if rng.random() < NOISE["drop_et_al"]:
        return names[0]
    return names[0] + (" and colleagues" if rng.random() < 0.1 else " " + _et_al(rng))


def _label_key(reference: Reference) -> tuple[str, int]:
    if reference.corporate:
        return reference.corporate, reference.year
    names = reference.surnames
    key = names[0] if len(names) == 1 else " & ".join(names) if len(names) == 2 else names[0] + " et al."
    return key, reference.year


def build_article(seed: int) -> Article:
    rng = random.Random(seed)
    reference_style = REFERENCE_STYLES[seed % len(REFERENCE_STYLES)]
    citation_style = CITATION_STYLES[(seed // len(REFERENCE_STYLES)) % len(CITATION_STYLES)]
    references: list[Reference] = []
    pool = rng.sample(SURNAMES, 40)
    count = rng.randint(36, 48)
    for index in range(count):
        rid = f"R{seed}-{index + 1}"
        corporate = alias = None
        roll = rng.random()
        if roll < 0.06:
            corporate, alias = rng.choice(CORPORATE)
            surnames, given = [], []
        else:
            authors = rng.choice([1, 1, 2, 2, 2, 3, 3, 4, 5, 6, 8])
            surnames = [pool[rng.randrange(len(pool))] for _ in range(authors)]
            while len(set(surnames)) < len(surnames):
                surnames = [pool[rng.randrange(len(pool))] for _ in range(authors)]
            given = [rng.choice(GIVEN) + (" " + rng.choice(GIVEN) if rng.random() < 0.3 else "") for _ in surnames]
        year = rng.randint(1988, 2025)
        title = " ".join(rng.choice(WORDS) for _ in range(rng.randint(4, 9))).capitalize()
        references.append(Reference(rid, surnames, given, corporate, alias, year, title, rng.choice(JOURNALS),
                                    rng.randint(1, 120), (start := rng.randint(1, 900), start + rng.randint(2, 30)),
                                    f"10.5555/syn.{seed}.{index + 1}"))
    # Deliberate collisions: same first author and year with different co-authors, the same
    # author string in two years, and identical citation labels that need a/b suffixes.
    for _ in range(rng.randint(3, 5)):
        base = rng.choice([reference for reference in references if reference.surnames])
        twin = rng.choice([reference for reference in references if reference.surnames and reference is not base])
        twin.surnames = [base.surnames[0]] + twin.surnames[1:]
        twin.given = [base.given[0]] + twin.given[1:]
        if rng.random() < 0.7:
            twin.year = base.year
        if rng.random() < 0.5 and len(base.surnames) == len(twin.surnames):
            twin.surnames = list(base.surnames)
            twin.given = list(base.given)
    references.sort(key=lambda reference: (
        (reference.corporate or reference.surnames[0]).casefold(), reference.year, reference.title))
    groups: dict[tuple[str, int], list[Reference]] = {}
    for reference in references:
        groups.setdefault(_label_key(reference), []).append(reference)
    for members in groups.values():
        if len(members) > 1:
            for index, member in enumerate(sorted(members, key=lambda item: item.title)):
                member.suffix = "abcdefghij"[index]
    article = Article(seed, reference_style, citation_style, references)
    cited_counts = {reference.rid: 0 for reference in references}
    uncited = set(rng.sample([reference.rid for reference in references], k=2))
    candidates = [reference for reference in references if reference.rid not in uncited]
    for _ in range(rng.randint(14, 20)):
        paragraph: list[tuple[str, str | None]] = []
        for _ in range(rng.randint(3, 6)):
            sentence = rng.choice(FILLER)
            roll = rng.random()
            if roll < 0.40:
                group = rng.sample(candidates, k=rng.choice([1, 1, 2, 2, 3]))
                paragraph.append((sentence + " ", None))
                _parenthetical(paragraph, group, citation_style, rng, references)
                paragraph.append((".", None))
                for reference in group:
                    cited_counts[reference.rid] += 1
            elif roll < 0.72:
                reference = rng.choice(candidates)
                names = _narrative_names(reference, rng)
                possessive = rng.random() < 0.08 and not names.endswith("colleagues")
                year = _noisy_year(reference, rng)
                if possessive:
                    head = names + "'s (" + year + ") analysis"
                    paragraph.append((head, reference.rid))
                    paragraph.append((" reported that " + sentence.lower() + ".", None))
                else:
                    paragraph.append((names + " (" + year + ")", reference.rid))
                    paragraph.append((" showed that " + sentence.lower() + ".", None))
                cited_counts[reference.rid] += 1
            elif roll < 0.80:
                paragraph.append((sentence + " " + rng.choice(DISTRACTORS) + ".", None))
            elif roll < 0.84:
                paragraph.append((rng.choice(NARRATIVE_DISTRACTORS) + " " + sentence.lower() + ".", None))
            elif roll < 0.88:
                # An author-year mention of a work absent from the list: no gold link exists.
                name = rng.choice([s for s in SURNAMES if s not in pool])
                paragraph.append((sentence + f" ({name}, {rng.randint(1990, 2024)}).", None))
            else:
                paragraph.append((sentence + ".", None))
            paragraph.append((" ", None))
        article.paragraphs.append(paragraph)
    return article


def _parenthetical(paragraph: list[tuple[str, str | None]], group: list[Reference], style: str,
                   rng: random.Random, references: list[Reference]) -> None:
    paragraph.append(("(", None))
    prefix = rng.choice(["", "", "", "", "e.g., ", "see ", "see also ", "cf. ", "reviewed in "])
    if prefix:
        paragraph.append((prefix, None))
    for position, reference in enumerate(group):
        if position:
            paragraph.append(("; ", None))
        names = _cited_names(reference, style, rng)
        separator = ", " if style == "apa" else " "
        paragraph.append((names + separator + _noisy_year(reference, rng), reference.rid))
        # A second work by the same author string can share the author phrase: "Smith, 2019, 2020".
        sibling = next((other for other in references if other is not reference and other.rid != reference.rid
                        and _label_key(other)[0] == _label_key(reference)[0] and other.year != reference.year), None)
        if sibling is not None and rng.random() < 0.3:
            paragraph.append((", ", None))
            paragraph.append((_noisy_year(sibling, rng), sibling.rid))
        if rng.random() < 0.12:
            paragraph.append((rng.choice([", p. 12", ", pp. 3-5", ", Fig. 2", ", chap. 4"]), None))
    paragraph.append((")", None))


def format_reference(reference: Reference, style: str) -> str:
    year = f"{reference.year}{reference.suffix}"
    first, last = reference.pages
    if reference.corporate:
        name = reference.corporate + (f" ({reference.alias})" if reference.alias and style == "apa" else "")
        if style in {"apa", "harvard"}:
            return f"{name}. ({year}). {reference.title}. {reference.journal}, {reference.volume}, {first}-{last}."
        if style == "compact":
            return f"{name} ({year}) {reference.title}. {reference.journal} {reference.volume}:{first}-{last}"
        return f"{name}. {year}. {reference.title}. {reference.journal} {reference.volume}, {first}-{last}."
    people = list(zip(reference.surnames, reference.given))
    if style == "apa":
        units = [f"{surname}, {_initials(given)}" for surname, given in people]
        authors = units[0] if len(units) == 1 else ", ".join(units[:-1]) + ", & " + units[-1]
        return (f"{authors} ({year}). {reference.title}. {reference.journal}, {reference.volume}, "
                f"{first}-{last}. https://doi.org/{reference.doi}")
    if style == "harvard":
        units = [f"{surname}, {_initials(given, spaced=False)}" for surname, given in people]
        authors = units[0] if len(units) == 1 else ", ".join(units[:-1]) + " and " + units[-1]
        return f"{authors} ({year}) '{reference.title}', {reference.journal}, {reference.volume}, pp. {first}-{last}."
    if style == "compact":
        units = [f"{surname} {_initials(given, periods=False)}" for surname, given in people]
        if len(units) > 6:
            units = units[:3] + ["et al."]
            authors = ", ".join(units[:-1]) + ", et al."
        else:
            authors = ", ".join(units)
        return f"{authors} ({year}) {reference.title}. {reference.journal} {reference.volume}:{first}-{last}. doi:{reference.doi}"
    if style == "name-year":
        units = [f"{surname}, {_initials(given, spaced=False)}" for surname, given in people]
        return f"{', '.join(units)}, {year}. {reference.title}. {reference.journal} {reference.volume}, {first}-{last}."
    if style == "inverted-full":
        units = [f"{surname}, {given}" for surname, given in people]
        authors = units[0] if len(units) == 1 else ", ".join(units[:-1]) + ", & " + units[-1]
        return f"{authors} ({year}). {reference.title}. {reference.journal}, {reference.volume}, {first}-{last}."
    surname, given = people[0]
    rest = [f"{g} {s}" for s, g in people[1:]]
    authors = f"{surname}, {given}"
    if rest:
        authors += (", " + ", ".join(rest[:-1]) + "," if len(rest) > 1 else "") + " and " + rest[-1]
    return f"{authors}. {year}. \"{reference.title}.\" {reference.journal} {reference.volume}: {first}-{last}."


def to_jats(article: Article) -> str:
    """Open JATS for one article; xref elements carry the gold targets."""

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="research-article">',
             f'<front><article-meta><article-id pub-id-type="doi">10.5555/syn.article.{article.seed}</article-id>',
             f'<title-group><article-title>Synthetic author-year article {article.seed}</article-title></title-group>',
             '</article-meta></front><body><sec><title>Introduction</title>']
    for paragraph in article.paragraphs:
        rendered = []
        for text, rid in paragraph:
            rendered.append(f'<xref ref-type="bibr" rid="{rid}">{escape(text)}</xref>' if rid else escape(text))
        parts.append("<p>" + "".join(rendered).strip() + "</p>")
    parts.append("</sec></body><back><ref-list><title>References</title>")
    for reference in article.references:
        parts.append(f'<ref id="{reference.rid}"><mixed-citation publication-type="journal">'
                     f"{escape(format_reference(reference, article.reference_style))}</mixed-citation></ref>")
    parts.append("</ref-list></back></article>")
    return "\n".join(parts) + "\n"


@dataclass
class Gold:
    paragraph: int
    start: int
    end: int
    rid: str


def jats_to_markdown(jats: str) -> tuple[str, list[Gold], list[str]]:
    """Strip the markup: prose keeps the printed citation text, references become a list."""

    root = ET.fromstring(jats.encode("utf-8"))
    lines = ["# Introduction", ""]
    gold: list[Gold] = []
    for index, paragraph in enumerate(root.iter("p")):
        text = paragraph.text or ""
        for child in paragraph:
            content = "".join(child.itertext())
            if child.tag == "xref":
                gold.append(Gold(index, len(text), len(text) + len(content), child.get("rid", "")))
            text += content + (child.tail or "")
        # Leading whitespace was stripped when the JATS was written; offsets already match.
        lines += [text, ""]
    lines += ["## References", ""]
    rids = []
    for reference in root.iter("ref"):
        rid = reference.get("id", "")
        rids.append(rid)
        printed = "".join(reference.find("mixed-citation").itertext()).strip()
        # Deterministic wrapping keyed by the target id: the draft breaks some entries over lines.
        if int(hashlib.sha256(rid.encode()).hexdigest(), 16) % 1000 < NOISE["wrap_reference"] * 1000:
            middle = printed.find(" ", len(printed) // 2)
            if middle > 0:
                printed = printed[:middle] + "\n" + printed[middle + 1:]
        lines += [printed, ""]
    return "\n".join(lines), gold, rids


def corpus(seeds: range) -> list[Article]:
    return [build_article(seed) for seed in seeds]


def corpus_digest(seeds: range) -> str:
    digest = hashlib.sha256()
    for article in corpus(seeds):
        digest.update(to_jats(article).encode("utf-8"))
    return digest.hexdigest()


def evaluate(seeds: range, directory: Path) -> dict[str, Any]:
    """Score resolution link by link against the xref targets of the generated JATS."""

    from ocsl_review.ingest import ingest_manuscript

    totals = {"gold": 0, "predicted": 0, "correct": 0, "wrong": 0, "unmatched": 0, "missed": 0,
              "documents": 0, "styles": set()}
    errors: list[dict[str, Any]] = []
    for article in corpus(seeds):
        markdown, gold, rids = jats_to_markdown(to_jats(article))
        path = directory / f"article-{article.seed}.md"
        path.write_text(markdown, encoding="utf-8")
        manuscript = ingest_manuscript(path)
        entries = manuscript["bibliographyEntries"]
        if len(entries) != len(rids):
            raise AssertionError(f"reference list was not split exactly: {len(entries)} vs {len(rids)}")
        entry_to_rid = {entry["id"]: rid for entry, rid in zip(entries, rids)}
        body = [paragraph for paragraph in manuscript["paragraphs"] if paragraph["kind"] == "body"]
        paragraph_index = {paragraph["id"]: number for number, paragraph in enumerate(body)}
        predicted: list[tuple[int, int, int, str]] = []
        for cluster in manuscript["citationClusters"]:
            marker = cluster.get("authorYear")
            if not marker or cluster["paragraphId"] not in paragraph_index:
                continue
            number = paragraph_index[cluster["paragraphId"]]
            for item in marker["items"]:
                if item["status"] == "resolved":
                    predicted.append((number, item["start"], item["end"], entry_to_rid[item["referenceId"]]))
        matched_predictions: set[int] = set()
        for link in gold:
            overlapping = [position for position, (number, start, end, _) in enumerate(predicted)
                           if number == link.paragraph and start < link.end and link.start < end]
            correct = [position for position in overlapping if predicted[position][3] == link.rid]
            totals["gold"] += 1
            if correct:
                totals["correct"] += 1
                matched_predictions.add(correct[0])
            else:
                totals["missed"] += 1
                errors.append({"seed": article.seed, "paragraph": link.paragraph, "rid": link.rid,
                               "text": body[link.paragraph]["text"][max(0, link.start - 30):link.end + 10]})
        totals["predicted"] += len(predicted)
        for position, (number, start, end, rid) in enumerate(predicted):
            if position in matched_predictions:
                continue
            if any(link.paragraph == number and start < link.end and link.start < end for link in gold):
                totals["wrong"] += 1
            else:
                totals["unmatched"] += 1
            errors.append({"seed": article.seed, "paragraph": number, "predicted": rid,
                           "text": body[number]["text"][max(0, start - 30):end + 10]})
        totals["documents"] += 1
        totals["styles"].add((article.reference_style, article.citation_style))
    precision = totals["correct"] / totals["predicted"] if totals["predicted"] else 0.0
    recall = totals["correct"] / totals["gold"] if totals["gold"] else 0.0
    totals["styles"] = sorted(totals["styles"])
    return {**totals, "precision": precision, "recall": recall, "errors": errors}
