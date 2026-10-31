# SPDX-License-Identifier: Apache-2.0
"""Evidence commands of ``ocsl-review followup``: status observations, support and function judging, dependency,
cited-by traversal, facts and exports.

Each command dispatches to its implementing module (``VERBS``); the argument lists here are the command contract
that Study Run steps build against.
"""
from __future__ import annotations

from typing import Any

from .followup_cli import dispatch, judge_arguments

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
}
READ_ONLY = frozenset({"verify-facts"})


def add_commands(commands: Any, register: Any) -> None:
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
    parsers["status-resolve"] = commands.add_parser(
        "status-resolve", help="one status row (source and date) for every cited work of the parent census")
    parsers["support"] = commands.add_parser(
        "support", help="citation-support@2 verdicts for the frozen strata at one evidence tier")
    parsers["support"].add_argument("--tier", required=True, choices=("abstract", "full-text"))
    parsers["support"].add_argument("--store", help="literature-evidence@2 projection read as a byte cache")
    parsers["support"].add_argument("--store-root", help="Merkle root the store must have")
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
    parsers["citedby"] = commands.add_parser(
        "citedby", help="works citing the affected reviews (Europe PMC citations, OpenAlex when available)")
    parsers["citedby"].add_argument("--source", action="append", choices=("europe-pmc", "openalex"),
                                    help="sources to ask (default: the protocol's second-generation sources)")
    parsers["citedby"].add_argument("--limit", type=int, help="at most this many targets (a smoke test)")
    parsers["citedby"].add_argument("--workspace", help="workspace whose egress ledger records the requests")
    parsers["facts"] = commands.add_parser("facts", help="facts v1 with per-fact SQL over a closed snapshot")
    parsers["facts"].add_argument("--output", required=True, help="new directory for facts.sqlite3 and facts.json")
    parsers["verify-facts"] = commands.add_parser("verify-facts", help="postflight of a follow-up facts export")
    parsers["verify-facts"].add_argument("--facts", required=True)
    parsers["export"] = commands.add_parser(
        "export", help="compromised-claim database, per-article alert records and the propagation graph")
    parsers["export"].add_argument("--output", required=True, help="new directory for the exports")
    for name, parser in parsers.items():
        if name != "verify-facts":
            parser.add_argument("--database", required=True, help="follow-up branch database")
        if name in ("support", "function"):
            judge_arguments(parser)
        register(parser, "followup_cli_a", "command", "read-only" if name in READ_ONLY else "workspace",
                 state_required=False)


def command(args: Any) -> Any:
    return dispatch(VERBS, args)
