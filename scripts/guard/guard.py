#!/usr/bin/env python3
"""Commit and push guard for the coscila repositories (stdlib only).

Hooks (installed through core.hooksPath):
  guard.py commit-msg FILE        one line, no trailers, no tool attribution
  guard.py pre-commit             identity, staged file names and sizes, added lines
  guard.py pre-push REMOTE URL    refuses main, checks every new commit including merges (refs on stdin)
CI:
  guard.py ci --range A..B        the same checks for each commit in the range, merges included

Excluded-project names are matched by length:sha256 over word-start windows, so this file does not contain them. Findings
print rule ids and locations only, never the matched text.

Local rules: guard_private.py next to this file, when present, adds content rules that only the local hooks apply. The
repository mirrors of this file do not carry that module, so CI runs without those rules.

Process notes: repository text describes the system in technical terms. Added lines (pre-commit, ci) and commit messages
(commit-msg, ci) must not cite the development process: plan step ids and phases, review rounds, workflow ids, dated
internal decisions and incidents, or the private knowledge base. Vendored, licence, data-fixture, migration and
generated paths are exempt. In a merge the rule skips added lines that one of the merged revisions already contains,
since its author did not write them, and checks the rest: conflict resolutions and extra edits. pre-push does not apply
this rule: it re-checks commits that commit-msg and pre-commit already passed.

Waiver: a comment on the same line containing "guard: allow <rule>[,<rule>...]" lets that reviewed line pass the named
content rules. Excluded-project findings, forbidden files and file sizes cannot be waived.
"""

import hashlib
import importlib.util
import os
import re
import subprocess
import sys

IDENTITY = "Philipp Flotho <PhilippFlotho@gmail.com>"
MAX_BYTES = 5 * 1024 * 1024
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ZERO_SHA = "0" * 40

EXCLUDED = {
    7: "0fd19fb4647c9b55ee7f950c035f33d888b7b10d40deb5eb1317d170da89965a",
    10: "5ac4a1bd0b4a831a5fcd76d0dbe03e2b33038f8058d41faec7cc296d7706d070",
    15: "aac427a207938b0684e5c8945e509ccb6d0c076f997421258a3456c8b6eb2f9d",
}
# Process notes. Every alternative needs a form that ordinary prose, identifiers and numbers do not take: a step id only
# after "plan", a round only with its number and a review context, a decision or an incident only with its date (a
# decision also in parentheses).
PLAN_STEP = r"(?:[HSPRETM]\d[a-z]?|UI-[A-Z]|AUTH(?:-\d)?|PRIV-\d[a-z]?|Phase\s+\d+)"
PROCESS_NOTE = re.compile(
    "|".join(
        [
            r"\b[Pp]lans?\s+(?:(?:items?|steps?|tracks?)\s+)?%s(?![\w-])" % PLAN_STEP,
            r"\b[Pp]hase\s+\d+[a-z]?\s+(?:review|readiness|prep|rehearsal|runbook)\b",
            r"\b[Rr]eadiness\s+rounds?\b|\b(?:[Rr]eview|[Rr]eadiness)\s+round[- ]?\d",
            r"\b(?:review|readiness)\s*,\s*round[- ]?\d+[a-z]?\s*\)",
            r"\b[Rr]ound[- ](?:\d+[a-z]?|[A-Za-z])"
            r"(?:['\u2019]s\b|\s+(?:fix(?:es)?|findings?|finds|verifier|blockers?)\b)",
            r"(?<![\w.~/-])kb/(?!(?:\d+(?:\.\d+)?|(?i:s|sec|ms|min|h|bp|kbp|kb|mb|gb|tb))(?![\w-]|\.\w))[\w.-]",
            r"\bPROGRESS\.md\b|\bDESIGN_\w*\.md\b|\bDESIGN_[a-z]\w*",
            r"\bISSUES\s+[A-Z]{2,5}-\d+\b|\brunbook\s+[A-Z]\d+(?:\.\d+)*\b",
            r"\bwf_[0-9a-f]{8,}\b",
            r"\b(?:operator|author|user|owner)\s+decision\s+(?:(?:of|on)\s+)?(?:\d{4}-)?\d\d-\d\d\b",
            r"\bdecision\s+(?:(?:of|on)\s+)?\d{4}-\d\d-\d\d\b|\b\d{4}-\d\d-\d\d\s+(?:operator|author|user)\s+decision\b",
            r"\(\s*(?:operator|user)\s+decision\s*[:;)]|\bbinding\s+(?:operator|user)\s+decision\b",
            r"\b(?:\d{4}-)?\d\d-\d\d\s+incident\b",
            r"\b[Ii]ncident\s+(?:regression|repair)\b[^\n]{0,40}?\b\d{4}-\d\d-\d\d\b",
        ]
    )
)
# Text the repositories carry but do not author: vendored trees, licences, data fixtures and snapshots, verbatim copies of
# earlier sources that rollback tests load (fixtures/legacy), migrations (their checksums are recorded once applied) and
# generated code.
PROCESS_NOTE_EXEMPT = re.compile(
    r"^(?:upstream|sources|translations|licenses|LICENSES)/"
    r"|(?:^|/)(?:LICEN[CS]E|COPYING|NOTICE)(?:[.-][^/]*)?$"
    r"|(?:^|/)fixtures/legacy/"
    r"|(?:^|/)(?:fixtures?|__fixtures__|testdata|golden)/(?:[^/]+/)*[^/]+\."
    r"(?:json|jsonl|ndjson|xml|html?|tsv|csv|txt|md|ya?ml|bib|ris|nbib|tex|svg|snap)$"
    r"|\.snap$"
    r"|^packages/db/prisma/migrations/|^packages/db/src/generated/"
)
PROCESS_NOTE_HINT = (
    "guard: describe the system in technical terms; plan ids, rounds, kb references are not allowed in repository text."
)

SECRETS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{16,}")),
    ("openrouter-key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("stripe-key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{16,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    (
        "url-credentials",
        re.compile(
            r"(?i)\b(?:https?|postgres(?:ql)?|mysql|redis|rediss|amqps?|mongodb(?:\+srv)?|smtps?|ftps?)://"
            r"([^\s/:@'\"`]+):([^\s/@'\"`]{6,})@([^\s/:?#'\"`]*)"
        ),
    ),
]
# Well-known development passwords (compose files, CI services, test fixtures) and credentials for reserved example hosts
# (RFC 2606: example.com/net/org, .example, .test, .invalid, .localhost) are not secrets.
PLACEHOLDER_PASSWORDS = {"postgres", "password", "example", "changeme", "secret", "test", "testtest", "minio123"}
PLACEHOLDER_PASSWORD = re.compile(r"(?i)secret[-_]?[a-z]*|pass(?:word)?[-_]?[a-z]*")
RESERVED_HOST = re.compile(r"(?i)(?:^|\.)(?:example\.(?:com|net|org)|example|test|invalid)$|\.localhost$")
# Obvious placeholders: fixture tokens, documentation values and templates.
PLACEHOLDER = re.compile(
    r"(?i)replace[-_.]?(?:this|me)|change[-_.]?me|example|dummy|placeholder|synthetic|fake|redacted|fixture|sample"
    r"|x{4,}|\*{3,}|(?:^|[-_.])(?:your|test|e2e|mock)(?:[-_.]|$)"
)
TEMPLATED = re.compile(r"^[$<%{*]|\$\{|\$\(|\{\{|<[A-Za-z_][^>]*>")
# KEY=value lines (env files, shell, Dockerfile ENV/ARG, compose lists) with a literal value of at least 16 characters.
KV_LINE = re.compile(r"^\s*(?:-\s+)?(?:export\s+|ENV\s+|ARG\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)$")
KV_NAME = re.compile(r"SECRET|TOKEN(?![A-Z])|PASSWORD|(?<![A-Z])PASS(?![A-Z])|API_KEY|HMAC|PRIVATE_KEY")
KV_NOT_SECRET = re.compile(  # names that carry locations, sizes or identifiers rather than the secret itself
    r"_(?:URL|URI|PATH|FILE|DIR|ROOT|NAME|ID|HASH|SHA\d*|DIGEST|HEADER|TTL|MS|SECONDS|LENGTH|BYTES|COUNT|LIMIT|ENABLED"
    r"|MODE|TYPE|VERSION|HELPER|COMMAND|CMD|BIN|PREFIX|ENDPOINT|HOST|PORT|USER|USERNAME|EMAIL|SCOPES?)$"
)
KV_EXPRESSION = re.compile(  # code expressions, URLs, dotted names, other variable names and file paths
    r"[\s()\[\]{}`]|://|^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$|^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$|^(?:~|\.{1,2})?/[\w.\-/]*$"
)
KV_MIN_LENGTH = 16
NPMRC = re.compile(r"(?:^|/)\.(?:npmrc|yarnrc(?:\.yml)?)$")
NPMRC_AUTH = re.compile(r"(?i)(?:^|[:\s])_?(?:authToken|auth|password|npmAuthToken|npmAuthIdent)\s*[=:]\s*(?![\"']?\$\{)\S")

FORBIDDEN_FILES = re.compile(
    r"(?:^|/)(?:auth\.json|puppenclaw\.config\.json|\.credentials\.json|\.pgpass|\.netrc|_netrc|\.git-credentials|\.pypirc"
    r"|credentials|[^/]*credentials\.(?:json|ya?ml|toml|ini|cfg|conf|csv|xml|txt)|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?)$"
    r"|\.api_key$|\.(?:pem|key|p12|pfx|ppk|keystore|jks)$|(?:^|/)cookies?[^/]*\.(?:txt|json)$"
    r"|\.sqlite3?(?:-wal|-shm|-journal)?$|\.db-(?:wal|shm)$|\.dump$",
    re.I,
)
# Env files: names starting with .env, ending in .env, or NAME.env.<suffix>; templates (*.example, *.sample, *.template) and
# source files such as vite.env.ts are fine.
ENV_FILE = re.compile(r"^\.env|\.env$|\.env\.(?!(?:[cm]?[jt]sx?|d\.[cm]?ts|py|go|rs|rb|java|kt|swift|php)$)[^/]+$", re.I)
ENV_TEMPLATE = re.compile(r"\.(?:example|sample|template)$", re.I)

# Tool attribution only; ordinary words such as "generated by" or "Claude" are fine.
ATTRIBUTION = re.compile(
    r"co-authored-by:|signed-off-by:|generated with \[|\U0001F916|noreply@anthropic\.com"
    r"|\bclaude\.com/claude-code\b|\bclaude\.ai/code\b",
    re.I,
)
TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")
SCISSORS = re.compile(r"^\S+ -{24} >8 -{24}\s*$")
WORD_START = re.compile(r"(?<![a-z0-9])[a-z0-9]")
WAIVER = re.compile(r"(?:#|//|/\*|<!--|--|;|%)[^\n]*?\bguard:\s*allow\s+([a-z0-9-]+(?:\s*,\s*[a-z0-9-]+)*)", re.I)
UNWAIVABLE = {"excluded-project"}
DIFF_OPTIONS = ["--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--submodule=short", "-U0"]


def load_local_rules():
    """The module guard_private.py next to this file, or None where it is absent (the repository mirrors)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guard_private.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("guard_private", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LOCAL = load_local_rules()


def hashed_hit(text, table):
    low = text.lower().replace("_", "-")
    for match in WORD_START.finditer(low):
        start = match.start()
        for length, digest in table.items():
            window = low[start : start + length]
            if len(window) == length and hashlib.sha256(window.encode()).hexdigest() == digest:
                return True
    return False


def run_git(args, stdin=None):
    """Runs git with unquoted paths; returns (returncode, stdout bytes, stderr text)."""
    result = subprocess.run(["git", "-c", "core.quotePath=false", *args], input=stdin, capture_output=True)
    return result.returncode, result.stdout, result.stderr.decode("utf-8", "replace").strip()


def git(*args):
    code, out, err = run_git(args)
    if code != 0:
        raise SystemExit(f"guard: git {' '.join(args)} failed: {err}")
    return out


def text(data):
    return data.decode("utf-8", "replace")


def literal(path):
    return ":(literal)" + path


def editor_message(message):
    """The message git keeps from the editor file: comment lines and the diff below the commit -v scissors line dropped."""
    lines = message.split("\n")
    for index, line in enumerate(lines):
        if SCISSORS.match(line):
            lines = lines[:index]
            break
    lines = [line for line in lines if not line.startswith("#")]
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def message_problems(message, notes=True):
    lines = message.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    body = "\n".join(lines)
    problems = []
    if not lines or not lines[0].strip():
        problems.append("msg-empty")
    if len(lines) > 1:
        problems.append("msg-not-single-line")
    if any(TRAILER.match(line) for line in lines[1:]):
        problems.append("msg-trailer")
    if ATTRIBUTION.search(body):
        problems.append("msg-attribution")
    if hashed_hit(body, EXCLUDED):
        problems.append("msg-excluded-project")
    if notes and PROCESS_NOTE.search(body):
        problems.append("msg-process-note")
    return problems


def is_placeholder(value):
    return bool(PLACEHOLDER.search(value) or TEMPLATED.search(value))


def _secret_hit(name, pattern, line):
    for match in pattern.finditer(line):
        if name == "url-credentials":
            user, password, host = match.group(1), match.group(2), match.group(3)
            if (
                password.lower() in PLACEHOLDER_PASSWORDS
                or password == user
                or PLACEHOLDER_PASSWORD.fullmatch(password)
                or is_placeholder(password)
                or RESERVED_HOST.search(host)
            ):
                continue
        elif name != "private-key" and is_placeholder(match.group(0)):
            continue
        return True
    return False


def kv_secret(line):
    match = KV_LINE.match(line)
    if not match:
        return False
    name, rest = match.group(1), match.group(2).strip()
    if not KV_NAME.search(name) or KV_NOT_SECRET.search(name):
        return False
    if rest[:1] in ('"', "'"):
        end = rest.find(rest[0], 1)
        value = rest[1:end] if end > 0 else rest[1:]
    else:
        value = rest.split(None, 1)[0].rstrip(";,") if rest else ""
    if len(value) < KV_MIN_LENGTH or KV_EXPRESSION.search(value) or is_placeholder(value):
        return False
    return True


def waived(line):
    rules = set()
    for match in WAIVER.finditer(line):
        rules.update(rule.strip().lower() for rule in match.group(1).split(","))
    return rules


def line_problems(path, line, notes=True):
    problems = [name for name, pattern in SECRETS if _secret_hit(name, pattern, line)]
    if kv_secret(line):
        problems.append("kv-secret")
    if NPMRC.search(path) and NPMRC_AUTH.search(line):
        problems.append("npmrc-auth")
    if hashed_hit(line, EXCLUDED):
        problems.append("excluded-project")
    if LOCAL is not None:
        problems += LOCAL.line_problems(path, line, hashed_hit)
    if notes and not PROCESS_NOTE_EXEMPT.search(path) and PROCESS_NOTE.search(line):
        problems.append("process-note")
    if problems and "guard:" in line:
        allowed = waived(line)
        problems = [rule for rule in problems if rule in UNWAIVABLE or rule not in allowed]
    return problems


def forbidden_file(path):
    base = path.rsplit("/", 1)[-1]
    if ENV_FILE.search(base):
        return not ENV_TEMPLATE.search(base)
    return bool(FORBIDDEN_FILES.search(path))


def file_problems(path, size):
    problems = []
    if forbidden_file(path):
        problems.append("forbidden-file")
    if hashed_hit(path, EXCLUDED):
        problems.append("excluded-project-path")
    if size is not None and size > MAX_BYTES:
        problems.append("file-too-large")
    return problems


def parse_raw(output):
    """Entries (path, new blob, is_gitlink) of `git diff --raw -z --no-renames` output, deletions skipped."""
    fields = output.split(b"\0")
    entries = []
    index = 0
    while index < len(fields) - 1:
        meta = fields[index]
        if not meta.startswith(b":"):
            index += 1
            continue
        parts = meta[1:].split()
        path = os.fsdecode(fields[index + 1])
        if len(parts) >= 5 and not parts[4].startswith(b"D"):
            entries.append((path, parts[3].decode(), parts[1] == b"160000"))
        index += 2
    return entries


def blob_sizes(blobs):
    wanted = sorted({blob for blob in blobs if blob and blob != ZERO_SHA})
    if not wanted:
        return {}
    _, out, _ = run_git(["cat-file", "--batch-check"], stdin=("\n".join(wanted) + "\n").encode())
    sizes = {}
    for line in text(out).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].isdigit():
            sizes[parts[0]] = int(parts[2])
    return sizes


def added_lines(diff_args):
    """(line number, text) of every added line of a single-file diff: every '+' line after the first hunk header."""
    started = False
    number = 0
    for raw in git(*diff_args).split(b"\n"):
        if raw.startswith(b"@@"):
            started = True
            match = re.match(rb"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            number = int(match.group(1)) if match else 0
        elif started and raw.startswith(b"+"):
            yield number, raw[1:].decode("utf-8", "replace")
            number += 1


def revision_lines(revisions, path):
    """Every line of path in the given revisions; a revision without the file contributes none."""
    lines = set()
    for revision in revisions:
        code, out, _ = run_git(["cat-file", "blob", f"{revision}:{path}"])
        if code == 0:
            lines.update(text(out).split("\n"))
    return lines


def change_problems(entries, diff_command, prefix="", notes=True, merged=()):
    """File, path and added-line checks for the entries of one change; each file is diffed on its own (literal pathspec).
    notes=False leaves out the process-note rule. merged names the revisions a merge brings together: an added line that
    one of them already contains is merged text, which the process-note rule skips."""
    found = []
    sizes = blob_sizes(blob for _, blob, gitlink in entries if not gitlink)
    for path, blob, gitlink in entries:
        for rule in file_problems(path, None if gitlink else sizes.get(blob)):
            found.append((rule, f"{prefix}{path}"))
        merged_lines = None
        for number, line in added_lines([*diff_command, "--", literal(path)]):
            rules = line_problems(path, line, notes)
            if merged and "process-note" in rules:
                if merged_lines is None:
                    merged_lines = revision_lines(merged, path)
                if line in merged_lines:
                    rules = [rule for rule in rules if rule != "process-note"]
            for rule in rules:
                found.append((rule, f"{prefix}{path}:{number}"))
    return found


def local_message_problems(message, paths):
    return LOCAL.message_problems(message, paths, hashed_hit) if LOCAL is not None else []


def report(found):
    for rule, where in found:
        print(f"guard: {rule}\t{where}", file=sys.stderr)
    if any(rule in ("process-note", "msg-process-note") for rule, _ in found):
        print(PROCESS_NOTE_HINT, file=sys.stderr)
    return 1 if found else 0


def staged_paths():
    return [os.fsdecode(p) for p in git("diff", "--cached", "--name-only", "-z", "--no-renames").split(b"\0") if p]


def cmd_commit_msg(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        message = editor_message(handle.read())
    found = [(rule, "commit message") for rule in message_problems(message)]
    found += [(rule, "commit message") for rule in local_message_problems(message, staged_paths())]
    if any(rule != "msg-process-note" for rule, _ in found):
        print("guard: commit messages are exactly one line, without trailers or attribution.", file=sys.stderr)
    return report(found)


def ident(var):
    value = text(git("var", var))
    return re.sub(r"\s+\d+\s+[+-]\d{4}\s*$", "", value.strip())


def merge_heads():
    """The revisions a merge being concluded brings in (MERGE_HEAD, several for an octopus merge); empty otherwise."""
    code, out, _ = run_git(["rev-parse", "--git-path", "MERGE_HEAD"])
    if code != 0:
        return []
    try:
        with open(text(out).strip(), encoding="utf-8") as handle:
            return [line.split()[0] for line in handle if line.strip()]
    except OSError:
        return []


def cmd_pre_commit():
    found = []
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        if ident(var) != IDENTITY:
            found.append(("identity", var))
    entries = parse_raw(git("diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames"))
    heads = merge_heads()
    found += change_problems(entries, ["diff", "--cached", *DIFF_OPTIONS], merged=["HEAD", *heads] if heads else ())
    return report(found)


def commit_problems(sha, notes=True):
    """Every check for one commit; notes=False leaves out the process-note rule. In a merge that rule checks only the
    added lines that none of its parents contains."""
    short = sha[:12]
    found = []
    header = text(git("show", "-s", "--format=%an <%ae>%n%cn <%ce>%n%P", sha)).split("\n")
    if header[0] != IDENTITY:
        found.append(("identity-author", short))
    if header[1] != IDENTITY:
        found.append(("identity-committer", short))
    message = text(git("show", "-s", "--format=%B", sha))
    found += [(rule, short) for rule in message_problems(message, notes)]
    parents = header[2].split() if len(header) > 2 else []
    first = parents[0] if parents else EMPTY_TREE
    entries = None
    if len(parents) == 2:
        # A merge: scan what the merge itself changed against the automatic merge of its parents (conflict resolutions,
        # extra edits). The merged commits are checked on their own.
        code, out, _ = run_git(["show", "--remerge-diff", "--format=", "--raw", "-z", "--no-abbrev", "--no-renames", sha])
        if code == 0:
            entries = parse_raw(out)
            diff_command = ["show", "--remerge-diff", "--format=", *DIFF_OPTIONS, sha]
    if entries is None:  # ordinary commit, octopus merge, or a git without --remerge-diff (first-parent diff)
        entries = parse_raw(git("diff", "--raw", "-z", "--no-abbrev", "--no-renames", first, sha))
        diff_command = ["diff", *DIFF_OPTIONS, first, sha]
    touched = [os.fsdecode(p) for p in git("diff", "--name-only", "-z", "--no-renames", first, sha).split(b"\0") if p]
    found += [(rule, short) for rule in local_message_problems(message, touched)]
    found += change_problems(entries, diff_command, prefix=f"{short} ", notes=notes,
                             merged=parents if len(parents) >= 2 else ())
    return found


def cmd_pre_push(lines):
    found = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, _ = parts
        if local_sha == ZERO_SHA:
            continue
        if remote_ref == "refs/heads/main" and os.environ.get("COSCILA_ALLOW_MAIN_PUSH") != "1":
            found.append(("push-main", remote_ref))
            continue
        for sha in text(git("rev-list", local_sha, "--not", "--remotes")).split():
            found += commit_problems(sha, notes=False)
    return report(found)


def cmd_ci(rng):
    found = []
    for sha in text(git("rev-list", rng)).split():
        found += commit_problems(sha)
    return report(found)


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    command = argv[1]
    if command == "commit-msg":
        return cmd_commit_msg(argv[2])
    if command == "pre-commit":
        return cmd_pre_commit()
    if command == "pre-push":
        return cmd_pre_push(sys.stdin.read().splitlines())
    if command == "ci":
        if len(argv) < 4 or argv[2] != "--range":
            raise SystemExit("usage: guard.py ci --range A..B")
        return cmd_ci(argv[3])
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
