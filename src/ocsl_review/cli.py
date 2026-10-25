"""Command-line surface with explicit operator and workspace trust classes."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .config import resolve_paths
from .errors import (
    EXIT_POLICY,
    EXIT_RUNTIME,
    EXIT_TRUST,
    EXIT_USAGE,
    EXIT_VALIDATION,
    ConfidentialityError,
    ReviewError,
    TrustBoundaryError,
    ValidationError,
)
from .util import CommandResult

OPERATOR = "operator"
READ_ONLY = "read-only"
WORKSPACE = "workspace"


def _chat_context() -> bool:
    if os.environ.get("OCSL_REVIEW_SANDBOX"):
        return True
    current = Path.cwd().resolve()
    declared = os.environ.get("LAB_WORKSPACE_ROOT")
    if declared:
        workspace_root = Path(declared).expanduser().resolve()
        if current == workspace_root or workspace_root in current.parents:
            return True
    for candidate in (current, *current.parents):
        if (candidate / ".ocsl" / "chat.json").is_file():
            return True
        if (candidate / "WORKSPACE.md").is_file() and (
            candidate / "skills" / "oc-science-lab"
        ).exists():
            return True
    return False


def _require_operator(command: str) -> None:
    if _chat_context():
        raise TrustBoundaryError(
            f"operator command cannot run from a materialized review workspace: {command}"
        )


def _handler(module: str, function: str) -> Callable[[argparse.Namespace], Any]:
    def invoke(args: argparse.Namespace) -> Any:
        imported = __import__(f"ocsl_review.{module}", fromlist=[function])
        return getattr(imported, function)(args)

    return invoke


def _set(
    parser: argparse.ArgumentParser,
    module: str,
    function: str,
    trust: str,
    *,
    state_required: bool,
) -> None:
    parser.set_defaults(
        _handler=_handler(module, function),
        _trust=trust,
        _state_required=state_required,
    )


def _subcommands(parser: argparse.ArgumentParser, destination: str = "command") -> Any:
    return parser.add_subparsers(dest=destination, required=True)


def _environment_runtime(parser: argparse.ArgumentParser) -> None:
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--environment", help="environment identifier")
    choice.add_argument("--skill", help="skill whose primary environment should be used")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--name", help="logical environment name")
    parser.add_argument("--chat-root")
    parser.add_argument("--conda-executable")
    parser.add_argument("--platform")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--probe-timeout", type=float, default=120)


def _review_input(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", help="manuscript inside the selected workspace")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--output", default=".ocsl-review/manuscript.json")
    parser.add_argument(
        "--public", action="store_true", help="mark the local artifact non-confidential"
    )
    parser.add_argument("--pdftotext", default="pdftotext")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-input-bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument("--max-extracted-bytes", type=int, default=64 * 1024 * 1024)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocsl-review",
        description="Evidence-chain manuscript review companion for OC Science Lab",
    )
    parser.add_argument("--version", action="version", version=f"ocsl-review {__version__}")
    parser.add_argument("--home", help="companion source checkout")
    parser.add_argument("--state", help="operator-owned mutable state root")
    parser.add_argument("--ocsl-repo", help="OC Science Lab deployment checkout")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    families = _subcommands(parser, "family")

    catalog = families.add_parser("catalog", help="validate tracked catalogs and schemas")
    catalog_commands = _subcommands(catalog)
    catalog_validate = catalog_commands.add_parser("validate")
    _set(catalog_validate, "catalogs", "cmd_validate", READ_ONLY, state_required=False)

    bootstrap = families.add_parser("bootstrap", help="build and snapshot operator dependencies")
    bootstrap.add_argument(
        "--component", action="append", choices=["environments", "repositories", "standards", "mcp"]
    )
    bootstrap.add_argument("--conda-executable")
    bootstrap.add_argument("--platform")
    bootstrap.add_argument("--timeout", type=float, default=3600)
    bootstrap.add_argument("--probe-timeout", type=float, default=120)
    bootstrap.add_argument("--standard-timeout", type=float, default=30)
    bootstrap.add_argument("--refresh-repositories", action="store_true")
    _set(bootstrap, "bootstrap", "cmd_bootstrap", OPERATOR, state_required=True)

    doctor = families.add_parser("doctor", help="aggregate deployment readiness")
    doctor.add_argument("--strict", action="store_true")
    doctor.add_argument("--live", action="store_true")
    doctor.add_argument("--staged-config")
    doctor.add_argument("--platform")
    _set(doctor, "doctor", "cmd_doctor", READ_ONLY, state_required=True)

    integrate = families.add_parser("integrate", help="manage deployment extension points")
    integrate_commands = _subcommands(integrate)
    install = integrate_commands.add_parser("install")
    install.add_argument("--native-skills-root")
    install.add_argument("--codex-skills-root")
    install.add_argument("--mcp-config")
    install.add_argument("--mcp-entries-file")
    install.add_argument("--stage-command")
    install.add_argument("--stage-cwd")
    install.add_argument("--stage-timeout", type=float, default=None)
    install.add_argument(
        "--staged-config",
        "--acceptance-config",
        dest="acceptance_config",
        help="generated deployment MCP config to probe (distinct from --mcp-config)",
    )
    _set(install, "lifecycle", "cmd_install", OPERATOR, state_required=True)
    status = integrate_commands.add_parser("status")
    _set(status, "lifecycle", "cmd_status", READ_ONLY, state_required=True)
    verify = integrate_commands.add_parser("verify")
    verify.add_argument("--live", action="store_true")
    verify.add_argument("--strict", action="store_true")
    verify.add_argument(
        "--staged-config",
        help="generated deployment MCP config; required for legacy install records",
    )
    _set(verify, "lifecycle", "cmd_verify", READ_ONLY, state_required=True)
    uninstall = integrate_commands.add_parser("uninstall")
    uninstall.add_argument("--stage-timeout", type=float, default=None)
    _set(uninstall, "lifecycle", "cmd_uninstall", OPERATOR, state_required=True)

    skill_family = families.add_parser("skills", help="inspect or mirror review skills")
    skill_commands = _subcommands(skill_family)
    skill_list = skill_commands.add_parser("list")
    _set(skill_list, "skills", "cmd_list", READ_ONLY, state_required=False)
    skill_show = skill_commands.add_parser("show")
    skill_show.add_argument("skill")
    _set(skill_show, "skills", "cmd_show", READ_ONLY, state_required=False)
    skill_validate = skill_commands.add_parser("validate")
    _set(skill_validate, "skills", "cmd_validate", READ_ONLY, state_required=False)
    for command, function in (("install", "cmd_install"), ("uninstall", "cmd_uninstall")):
        selected = skill_commands.add_parser(command)
        if command == "install":
            selected.add_argument("--native-skills-root")
            selected.add_argument("--codex-skills-root")
        _set(selected, "skills", function, OPERATOR, state_required=True)

    repo_family = families.add_parser("repos", help="manage immutable upstream archives")
    repo_commands = _subcommands(repo_family)
    repo_list = repo_commands.add_parser("list")
    _set(repo_list, "repositories", "cmd_list", READ_ONLY, state_required=False)
    repo_path = repo_commands.add_parser("path")
    repo_path.add_argument("repository")
    _set(repo_path, "repositories", "cmd_path", READ_ONLY, state_required=True)
    repo_verify = repo_commands.add_parser("verify")
    repo_verify.add_argument("repository", nargs="?")
    repo_verify.add_argument("--strict", action="store_true")
    _set(repo_verify, "repositories", "cmd_verify", READ_ONLY, state_required=True)
    repo_sync = repo_commands.add_parser("sync")
    repo_sync.add_argument("--refresh", action="store_true")
    _set(repo_sync, "repositories", "cmd_sync", OPERATOR, state_required=True)
    repo_update = repo_commands.add_parser("update")
    _set(repo_update, "repositories", "cmd_update", OPERATOR, state_required=True)

    standard_family = families.add_parser("standards", help="snapshot official guidance")
    standard_commands = _subcommands(standard_family)
    standard_list = standard_commands.add_parser("list")
    _set(standard_list, "standards", "cmd_list", READ_ONLY, state_required=False)
    standard_path = standard_commands.add_parser("path")
    standard_path.add_argument("standard")
    _set(standard_path, "standards", "cmd_path", READ_ONLY, state_required=True)
    standard_verify = standard_commands.add_parser("verify")
    standard_verify.add_argument("standard", nargs="?")
    standard_verify.add_argument("--strict", action="store_true")
    _set(standard_verify, "standards", "cmd_verify", READ_ONLY, state_required=True)
    standard_sync = standard_commands.add_parser("sync")
    standard_sync.add_argument("standard", nargs="?")
    _set(standard_sync, "standards", "cmd_sync", OPERATOR, state_required=True)

    environment_family = families.add_parser("env", help="manage immutable and chat environments")
    environment_commands = _subcommands(environment_family)
    environment_list = environment_commands.add_parser("list")
    environment_list.add_argument("--platform")
    _set(environment_list, "environments", "cmd_list", READ_ONLY, state_required=True)
    environment_build = environment_commands.add_parser("build")
    build_choice = environment_build.add_mutually_exclusive_group(required=True)
    build_choice.add_argument("--environment")
    build_choice.add_argument("--all", action="store_true")
    environment_build.add_argument("--conda-executable")
    environment_build.add_argument("--platform")
    environment_build.add_argument("--timeout", type=float, default=3600)
    environment_build.add_argument("--probe-timeout", type=float, default=120)
    _set(environment_build, "environments", "cmd_build", OPERATOR, state_required=True)
    environment_ensure = environment_commands.add_parser("ensure")
    _environment_runtime(environment_ensure)
    _set(environment_ensure, "environments", "cmd_ensure", WORKSPACE, state_required=True)
    environment_run = environment_commands.add_parser("run")
    _environment_runtime(environment_run)
    environment_run.add_argument("--command-timeout", type=float)
    environment_run.add_argument("argv", nargs=argparse.REMAINDER)
    _set(environment_run, "environments", "cmd_run", WORKSPACE, state_required=True)
    environment_fork = environment_commands.add_parser("fork")
    environment_fork.add_argument("--source-workspace", required=True)
    environment_fork.add_argument("--target-workspace", required=True)
    environment_fork.add_argument("--environment")
    environment_fork.add_argument("--name")
    environment_fork.add_argument("--source-name")
    environment_fork.add_argument("--all", action="store_true")
    environment_fork.add_argument(
        "--mode", choices=["clone", "rebuild", "reproducible-rebuild"], default="clone"
    )
    environment_fork.add_argument("--add-conda", action="append", default=[])
    environment_fork.add_argument("--add-pip", action="append", default=[])
    environment_fork.add_argument("--chat-root")
    environment_fork.add_argument("--conda-executable")
    environment_fork.add_argument("--platform")
    environment_fork.add_argument("--timeout", type=float, default=1800)
    environment_fork.add_argument("--probe-timeout", type=float, default=120)
    _set(environment_fork, "environments", "cmd_fork", WORKSPACE, state_required=True)
    for command, function in (("describe", "cmd_describe"), ("verify", "cmd_verify")):
        selected = environment_commands.add_parser(command)
        selected.add_argument("name")
        selected.add_argument("--workspace", default=".")
        selected.add_argument("--chat-root")
        selected.add_argument("--conda-executable")
        selected.add_argument("--probe-timeout", type=float, default=120)
        _set(selected, "environments", function, READ_ONLY, state_required=True)

    mcp_family = families.add_parser("mcp", help="inspect and exercise review MCP servers")
    mcp_commands = _subcommands(mcp_family)
    mcp_list = mcp_commands.add_parser("list")
    _set(mcp_list, "mcps", "cmd_list", READ_ONLY, state_required=False)
    mcp_render = mcp_commands.add_parser("render")
    _set(mcp_render, "mcps", "cmd_render", READ_ONLY, state_required=True)
    mcp_doctor = mcp_commands.add_parser("doctor")
    mcp_doctor.add_argument("--live", action="store_true")
    mcp_doctor.add_argument("--strict", action="store_true")
    mcp_doctor.add_argument("--staged-config")
    _set(mcp_doctor, "mcps", "cmd_doctor", READ_ONLY, state_required=True)
    mcp_call = mcp_commands.add_parser("call")
    mcp_call.add_argument("server")
    mcp_call.add_argument("tool")
    mcp_call.add_argument("--arguments", default="{}")
    _set(mcp_call, "mcps", "cmd_call", READ_ONLY, state_required=True)

    review_family = families.add_parser("review", help="create local review artifacts")
    review_commands = _subcommands(review_family)
    ingest = review_commands.add_parser("ingest")
    _review_input(ingest)
    _set(ingest, "workflow", "cmd_ingest", WORKSPACE, state_required=False)
    inventory = review_commands.add_parser("inventory")
    inventory.add_argument("--workspace", default=".")
    inventory.add_argument("--manuscript", default=".ocsl-review/manuscript.json")
    inventory.add_argument("--output", default=".ocsl-review/audit-graph.json")
    _set(inventory, "workflow", "cmd_inventory", WORKSPACE, state_required=False)
    plan = review_commands.add_parser("plan")
    plan.add_argument("--workspace", default=".")
    plan.add_argument("--manuscript", default=".ocsl-review/manuscript.json")
    plan.add_argument("--output", default=".ocsl-review/review-plan.json")
    _set(plan, "workflow", "cmd_plan", WORKSPACE, state_required=False)
    package = review_commands.add_parser("package")
    package.add_argument("--workspace", default=".")
    package.add_argument("--manuscript", default=".ocsl-review/manuscript.json")
    package.add_argument("--plan", default=".ocsl-review/review-plan.json")
    package.add_argument("--audit-graph", default=".ocsl-review/audit-graph.json")
    package.add_argument("--audit-summary")
    package.add_argument("--title")
    package.add_argument("--output", default=".ocsl-review/review-package.json")
    package.add_argument("--summary-output", default=".ocsl-review/audit-summary.json")
    _set(package, "workflow", "cmd_package", WORKSPACE, state_required=False)
    render = review_commands.add_parser("render")
    render.add_argument("--workspace", default=".")
    render.add_argument("--package", default=".ocsl-review/review-package.json")
    render.add_argument("--output-stem", default=".ocsl-review/review-report")
    _set(render, "workflow", "cmd_render", WORKSPACE, state_required=False)
    prepare = review_commands.add_parser("prepare")
    _review_input(prepare)
    prepare.add_argument("--audit-output", default=".ocsl-review/audit-graph.json")
    prepare.add_argument("--plan-output", default=".ocsl-review/review-plan.json")
    prepare.add_argument("--package-output", default=".ocsl-review/review-package.json")
    prepare.add_argument("--summary-output", default=".ocsl-review/audit-summary.json")
    prepare.add_argument("--title")
    _set(prepare, "workflow", "cmd_prepare", WORKSPACE, state_required=False)

    literature = review_commands.add_parser(
        "literature", help="prepare, acquire, and record an extensive reference audit"
    )
    literature_commands = _subcommands(literature, "literature_command")
    for name in ("prepare", "confirm", "fetch", "attach", "record", "status"):
        command = literature_commands.add_parser(name)
        command.add_argument("--workspace", default=".")
        command.add_argument("--manuscript", default=".ocsl-review/manuscript.json")
        command.add_argument("--audit-graph", default=".ocsl-review/audit-graph.json")
        if name == "confirm":
            decision = command.add_mutually_exclusive_group(required=True)
            decision.add_argument("--accept", action="store_true")
            decision.add_argument("--decline", action="store_true")
            command.add_argument("--scope-sha256", required=True,
                                 help="scope digest returned by prepare")
            command.add_argument("--basis", required=True,
                                 help="record the user's actual confirmation or explicit request")
        if name in {"fetch", "attach"}:
            command.add_argument("--reference-id", action="append", default=[])
        if name == "fetch":
            command.add_argument("--retry", action="store_true",
                                 help="retry recorded failures or refresh successful acquisitions")
            command.add_argument("--additional-doi", action="append", default=[],
                                 help="acquire a targeted gap source without changing bibliography counts")
            command.add_argument("--claim-id", action="append", default=[],
                                 help="claim motivating an additional literature source")
        if name == "attach":
            command.add_argument("input", help="source document inside this workspace")
            command.add_argument("--media-type", help="override a document's detected media type")
            command.add_argument("--access-level", choices=("full-text", "abstract-only"),
                                 default="full-text")
        if name == "record":
            command.add_argument("input", help="assessment JSON inside this workspace")
        _set(command, "literature", f"cmd_{name}",
             READ_ONLY if name == "status" else WORKSPACE, state_required=False)

    census = review_commands.add_parser("census", help="run resumable per-locus worker measurements")
    census_commands = _subcommands(census, "census_command")
    for name in ("prepare", "run", "list", "status", "wait", "resume", "cancel", "import", "export"):
        command = census_commands.add_parser(name)
        command.add_argument("--workspace", default=".")
        if name == "prepare":
            command.add_argument("--mode", choices=("abstract", "full-text", "panel"), required=True)
            command.add_argument("--worker-config", required=True)
            command.add_argument("--limit", type=int, help="explicitly labelled pilot subset")
            command.add_argument("--reference-id", action="append", default=[])
        elif name != "list":
            command.add_argument("--run", required=True)
        if name in {"status", "wait", "resume", "cancel", "import"}:
            command.add_argument("--job", help="select a recorded worker job within this run")
        if name == "wait":
            command.add_argument("--timeout-ms", type=int, default=60000,
                                 help="bounded wait, at most 60000 ms; results are written inside the workspace")
        if name == "run":
            command.add_argument("--retry", action="store_true", help="new attempt for still-unaccepted tasks")
            command.add_argument("--concurrency", type=int,
                                 help="positive safe-integer bulk concurrency request; broker capacity limits still apply")
        if name == "import":
            command.add_argument("input", help="worker result JSONL inside the workspace")
        if name == "export":
            command.add_argument("--output", default="results/census")
        _set(command, "census", f"cmd_{name}", WORKSPACE, state_required=False)

    audit_family = families.add_parser("audit", help="validate or summarize an audit graph")
    audit_commands = _subcommands(audit_family)
    audit_validate = audit_commands.add_parser("validate")
    audit_validate.add_argument("--workspace", default=".")
    audit_validate.add_argument("--audit-graph", default=".ocsl-review/audit-graph.json")
    _set(audit_validate, "workflow", "cmd_audit_validate", READ_ONLY, state_required=False)
    audit_summarize = audit_commands.add_parser("summarize")
    audit_summarize.add_argument("--workspace", default=".")
    audit_summarize.add_argument("--audit-graph", default=".ocsl-review/audit-graph.json")
    audit_summarize.add_argument("--output", default=".ocsl-review/audit-summary.json")
    _set(audit_summarize, "workflow", "cmd_audit_summarize", WORKSPACE, state_required=False)

    calibrate = families.add_parser("calibrate", help="score predictions on adversarial fixtures")
    calibrate.add_argument("predictions")
    calibrate.add_argument("--gold")
    calibrate.add_argument("--first-predictions", help="first-pass local JSONL for repair comparison")
    calibrate.add_argument("--output")
    calibrate.add_argument("--workspace", default=".")
    _set(calibrate, "calibration", "cmd_calibrate", WORKSPACE, state_required=False)
    from .corpus_cli import add_commands
    add_commands(families, _set)
    from .screen import add_commands as add_screen_commands
    add_screen_commands(families, _set)
    return parser


def _normalize_globals(argv: Sequence[str]) -> list[str]:
    """Permit global options before or after a command without touching pass-through argv."""

    extracted: list[str] = []
    remainder: list[str] = []
    index = 0
    options_with_values = {"--home", "--state", "--ocsl-repo"}
    while index < len(argv):
        token = argv[index]
        if token == "--":
            remainder.extend(argv[index:])
            break
        if token == "--json":
            extracted.append(token)
            index += 1
            continue
        if token in options_with_values:
            if index + 1 >= len(argv):
                remainder.append(token)
                index += 1
                continue
            extracted.extend((token, argv[index + 1]))
            index += 2
            continue
        if any(token.startswith(option + "=") for option in options_with_values):
            extracted.append(token)
            index += 1
            continue
        remainder.append(token)
        index += 1
    return [*extracted, *remainder]


def _emit(value: Any, *, as_json: bool) -> None:
    if isinstance(value, str):
        print(value)
        return
    rendered = json.dumps(
        value,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        default=lambda item: str(item),
    )
    print(rendered)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(_normalize_globals(raw))
        trust = getattr(args, "_trust", READ_ONLY)
        if trust == OPERATOR:
            command = " ".join(
                item for item in (getattr(args, "family", ""), getattr(args, "command", "")) if item
            )
            _require_operator(command)
        args.paths = resolve_paths(
            home=getattr(args, "home", None),
            state=getattr(args, "state", None),
            lab_repo=getattr(args, "ocsl_repo", None),
            state_required=bool(getattr(args, "_state_required", True)),
        )
        if trust == OPERATOR:
            from .catalogs import validate_path_boundaries

            validate_path_boundaries(args.paths)
        if getattr(args, "family", None) == "env" and getattr(args, "command", None) == "run":
            command_argv = list(getattr(args, "argv", []) or [])
            if command_argv and command_argv[0] == "--":
                command_argv = command_argv[1:]
            args.argv = command_argv
        result = args._handler(args)
        if isinstance(result, CommandResult):
            if result.stdout:
                sys.stdout.write(result.stdout)
            if result.stderr:
                sys.stderr.write(result.stderr)
            return result.returncode
        if result is not None:
            _emit(result, as_json=bool(getattr(args, "json", False)))
        return 0
    except TrustBoundaryError as exc:
        print(f"trust boundary: {exc}", file=sys.stderr)
        return EXIT_TRUST
    except ConfidentialityError as exc:
        print(f"confidentiality policy: {exc}", file=sys.stderr)
        return EXIT_POLICY
    except ValidationError as exc:
        print(f"validation: {exc}", file=sys.stderr)
        return EXIT_VALIDATION
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - stable CLI boundary
        print(f"runtime failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME


if __name__ == "__main__":
    raise SystemExit(main())
