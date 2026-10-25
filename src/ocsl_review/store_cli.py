# SPDX-License-Identifier: Apache-2.0
"""``ocsl-review store``: sealed literature stores and the label-free projection."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from .errors import ValidationError


def add_commands(families, register):
    family = families.add_parser("store", help="sealed literature stores: project, verify and attach read-only")
    commands = family.add_subparsers(dest="store_command", required=True)
    project = commands.add_parser(
        "project", help="build the label-free projection literature-evidence@2 from a sealed store (operator)")
    project.add_argument("--source", required=True, help="sealed source store (a snapshot directory); never modified")
    project.add_argument("--output", required=True, help="new directory for the projection")
    project.add_argument("--workers", type=int, default=8, help="threads that read, verify and compress blobs")
    project.add_argument("--compression-level", type=int, default=6, choices=range(0, 10))
    register(project, "store_cli", "command", "operator", state_required=False)
    verify = commands.add_parser(
        "verify", help="verify a sealed store; a projection must also pass the zero-row and vocabulary gate")
    verify.add_argument("--store", required=True)
    verify.add_argument("--level", choices=("light", "standard", "deep"), default="standard",
                        help="deep also scans every kept blob for legacy verdict vocabulary")
    register(verify, "store_cli", "command", "read-only", state_required=False)
    attach = commands.add_parser("attach", help="pin a sealed store as a read-only byte cache of a study library")
    attach.add_argument("--workspace", default=".")
    attach.add_argument("--library", default="work/literature")
    attach.add_argument("--store", required=True, help="absolute path of the mounted store")
    register(attach, "store_cli", "command", "workspace", state_required=False)


def _progress(event):
    print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)


def command(args):
    name = args.store_command
    if name == "project":
        from .store_projection import build_projection
        if args.workers < 1:
            raise ValidationError("workers must be positive")
        return build_projection(Path(args.source), Path(args.output), workers=args.workers,
                                level=args.compression_level, progress=_progress)
    if name == "verify":
        from .store_client import Store
        from .store_projection import FORMAT, verify_projection
        path = Path(args.store).absolute()
        manifest = path / "manifest.json"
        declared = json.loads(manifest.read_text()).get("store") if manifest.is_file() else None
        if isinstance(declared, dict) and declared.get("id") == FORMAT:
            return verify_projection(path, level=args.level)
        return {"verified": True, "level": args.level, **Store(path, level=args.level).describe()}
    if name == "attach":
        from .corpus_store import CorpusRepository
        from .store_client import attach_store
        repo = CorpusRepository(Path(args.workspace).absolute(), args.library)
        return attach_store(repo, Path(args.store))
    raise ValidationError("unknown store command")
