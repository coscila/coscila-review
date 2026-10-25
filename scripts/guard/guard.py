#!/usr/bin/env python3
"""Commit and push guard for the coscila repositories (stdlib only).

Hooks (installed through core.hooksPath):
  guard.py commit-msg FILE        one line, no trailers, no tool attribution
  guard.py pre-commit             identity, staged file names and sizes, added lines
  guard.py pre-push REMOTE URL    refuses main, checks every new commit including merges (refs on stdin)
CI:
  guard.py ci --range A..B        the same checks for each commit in the range, merges included

Excluded-project names and the publisher name are matched by length:sha256 over word-start windows, so this file does not
contain them. Findings print rule ids and locations only, never the matched text.

Waiver: a comment on the same line containing "guard: allow <rule>[,<rule>...]" lets that reviewed line pass the named
content rules. Excluded-project findings, forbidden files and file sizes cannot be waived.
"""

import hashlib
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
PUBLISHER = {8: "c1f35ffd9e5268dec53413e3d2d88b306788f5b95b747812f2a479241f742bf9"}

# Checker and integrity feature paths: only here, and in commit messages touching them, the publisher-framing and
# correspondence rules apply. Publisher and journal names elsewhere (writing skills, vendored skills, the journal-policy
# skill, bio lines) are fine. The paths are repository-relative; test_guard.py checks a sample against the real checkouts.
CHECKER_SKILLS = (  # coscila-review skills, whole directories (assets, prompts, references, scripts)
    "ocsl-review-reference-integrity",
    "ocsl-review-claim-source-verification",
    "ocsl-review-citation-census",
    "ocsl-review-citation-completeness",
    "ocsl-review-reference-analysis",
)
CHECKER_MODULES = ("citations", "census", "claim_inventory", r"corpus_\w+", "literature", "scholarly")  # src/ocsl_review
PLANNED_MODULES = ("claim_check", "integrity", "screen", "sanitize", "author_year", "retrieval")
CHECKER_TESTS = ("citations", "census", "claim_inventory", "corpus", "literature", "scholarly") + PLANNED_MODULES
CHECKER_PATHS = re.compile(
    "|".join(
        [
            r"^skills/(?:%s)/" % "|".join(CHECKER_SKILLS),
            r"^skills/ocsl-review-(?:claim-check|integrity|screen|sanitize|author-year|retrieval)[^/]*/",
            r"^src/ocsl_review/(?:%s)\.py$" % "|".join(CHECKER_MODULES),
            r"^src/ocsl_review/(?:%s)(?:\.[^/]+|/)" % "|".join(PLANNED_MODULES),  # any extension, or a package
            r"^tests/test_(?:%s)(?:_\w+)?\.py$" % "|".join(CHECKER_TESTS),
            r"^tests/fixtures/citation_census",
            r"^docs/(?:citation-census|corpus-review)\.md$",
            r"^docs/(?:claim[-_]check|integrity|screen|sanitize|author[-_]year|retrieval)[^/]*$",
            r"^prompts/",
            r"^scripts/rollout-meta-literature-preset",  # coscila
        ]
    )
)
PUBLISHER_FRAMING = re.compile(
    r"\bfor (a |the )?publishers?\b|\bpublisher[- ](service|facing|partner|partnership|integration|pilot|customer|use[- ]case)s?\b"
    r"|\b(editorial|submission)[- ]system integration\b|\bsold to publishers\b",
    re.I,
)
CORRESPONDENCE = re.compile(
    r"\b(e-?mail|mail) (exchange|thread|conversation|correspondence)\b|\bas discussed (by|via|over) (e-?)?mail\b"
    r"|\bour correspondence\b|\bper (your|their|our) (e-?)?mail\b",
    re.I,
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


def message_problems(message):
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


def line_problems(path, line):
    problems = [name for name, pattern in SECRETS if _secret_hit(name, pattern, line)]
    if kv_secret(line):
        problems.append("kv-secret")
    if NPMRC.search(path) and NPMRC_AUTH.search(line):
        problems.append("npmrc-auth")
    if hashed_hit(line, EXCLUDED):
        problems.append("excluded-project")
    if CHECKER_PATHS.search(path):
        if hashed_hit(line, PUBLISHER) or PUBLISHER_FRAMING.search(line):
            problems.append("publisher-framing")
        if CORRESPONDENCE.search(line):
            problems.append("correspondence")
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


def change_problems(entries, diff_command, prefix=""):
    """File, path and added-line checks for the entries of one change; each file is diffed on its own (literal pathspec)."""
    found = []
    sizes = blob_sizes(blob for _, blob, gitlink in entries if not gitlink)
    for path, blob, gitlink in entries:
        for rule in file_problems(path, None if gitlink else sizes.get(blob)):
            found.append((rule, f"{prefix}{path}"))
        for number, line in added_lines([*diff_command, "--", literal(path)]):
            for rule in line_problems(path, line):
                found.append((rule, f"{prefix}{path}:{number}"))
    return found


def checker_message_problems(message, paths):
    if any(CHECKER_PATHS.search(p) for p in paths):
        if hashed_hit(message, PUBLISHER) or PUBLISHER_FRAMING.search(message) or CORRESPONDENCE.search(message):
            return ["msg-publisher-framing"]
    return []


def report(found):
    for rule, where in found:
        print(f"guard: {rule}\t{where}", file=sys.stderr)
    return 1 if found else 0


def staged_paths():
    return [os.fsdecode(p) for p in git("diff", "--cached", "--name-only", "-z", "--no-renames").split(b"\0") if p]


def cmd_commit_msg(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        message = editor_message(handle.read())
    found = [(rule, "commit message") for rule in message_problems(message)]
    found += [(rule, "commit message") for rule in checker_message_problems(message, staged_paths())]
    if found:
        print("guard: commit messages are exactly one line, without trailers or attribution.", file=sys.stderr)
    return report(found)


def ident(var):
    value = text(git("var", var))
    return re.sub(r"\s+\d+\s+[+-]\d{4}\s*$", "", value.strip())


def cmd_pre_commit():
    found = []
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        if ident(var) != IDENTITY:
            found.append(("identity", var))
    entries = parse_raw(git("diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames"))
    found += change_problems(entries, ["diff", "--cached", *DIFF_OPTIONS])
    return report(found)


def commit_problems(sha):
    short = sha[:12]
    found = []
    header = text(git("show", "-s", "--format=%an <%ae>%n%cn <%ce>%n%P", sha)).split("\n")
    if header[0] != IDENTITY:
        found.append(("identity-author", short))
    if header[1] != IDENTITY:
        found.append(("identity-committer", short))
    message = text(git("show", "-s", "--format=%B", sha))
    found += [(rule, short) for rule in message_problems(message)]
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
    found += [(rule, short) for rule in checker_message_problems(message, touched)]
    found += change_problems(entries, diff_command, prefix=f"{short} ")
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
            found += commit_problems(sha)
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
