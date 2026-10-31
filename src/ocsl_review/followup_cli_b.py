# SPDX-License-Identifier: Apache-2.0
"""Analysis commands of ``ocsl-review followup``: hotspot test, strata, numeric checks, second-generation matching,
retraction lag and estimands.

Each command dispatches to its implementing module (``VERBS``); the argument lists here are the command contract
that Study Run steps build against.
"""
from __future__ import annotations

from typing import Any

from .followup_cli import dispatch, judge_arguments

VERBS = {
    "hotspots": ("followup_stats", "cmd_hotspots"),
    "strata": ("followup_strata", "cmd_strata"),
    "numeric": ("followup_numeric", "cmd_numeric"),
    "second-gen": ("followup_secondgen", "cmd_second_gen"),
    "lag": ("followup_stats", "cmd_lag"),
    "estimands": ("followup_estimands", "cmd_estimands"),
}


def add_commands(commands: Any, register: Any) -> None:
    parsers = {}
    parsers["hotspots"] = commands.add_parser(
        "hotspots", help="works cited without support beyond the journal x year base (beta-binomial, BH-FDR)")
    parsers["strata"] = commands.add_parser(
        "strata", help="build the frame and strata S0-S3 with co-cited loci and inclusion probabilities")
    parsers["strata"].add_argument("--freeze", action="store_true",
                                   help="freeze strata, sizes, estimands and denominators into the protocol digest "
                                        "(required before any follow-up judging)")
    parsers["numeric"] = commands.add_parser(
        "numeric", help="numeric-check@1 parse of S3 claims and the deterministic comparison with a table source")
    parsers["numeric"].add_argument("--tables", required=True,
                                    help="table source (a pinned dataset version; synthetic or licence-verified)")
    parsers["second-gen"] = commands.add_parser(
        "second-gen", help="sample citing articles, acquire them, rank candidate sentences (BM25), confirm (claim-match@1)")
    parsers["second-gen"].add_argument("--stage", choices=("sample", "acquire", "candidates", "judge", "all"),
                                       default="all")
    parsers["second-gen"].add_argument("--workspace", help="workspace whose egress ledger records the requests")
    parsers["lag"] = commands.add_parser("lag", help="retraction-lag rows and the lag model")
    parsers["estimands"] = commands.add_parser(
        "estimands", help="the predeclared estimands with coverage tables (weighted, review-clustered bootstrap)")
    parsers["estimands"].add_argument("--b", type=int, help="bootstrap replicates (default: the protocol's)")
    parsers["estimands"].add_argument("--bootstrap-seed", type=int, help="bootstrap seed (default: the protocol's)")
    for name, parser in parsers.items():
        parser.add_argument("--database", required=True, help="follow-up branch database")
        if name in ("numeric", "second-gen"):
            judge_arguments(parser)
        register(parser, "followup_cli_b", "command", "workspace", state_required=False)


def command(args: Any) -> Any:
    return dispatch(VERBS, args)
