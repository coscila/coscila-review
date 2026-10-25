#!/usr/bin/env python3
"""Commit and push guard for the coscila repositories (stdlib only).

Hooks (installed through core.hooksPath):
  guard.py commit-msg FILE        one line, no trailers, no attribution phrases
  guard.py pre-commit             identity, staged files and added lines
  guard.py pre-push REMOTE URL    refuses main, checks every new commit (refs on stdin)
CI:
  guard.py ci --range A..B        the same checks for each commit in the range

Excluded-project names and the publisher name are matched by length:sha256 over word-start windows, so this file does not
contain them. Findings print rule ids and locations only, never the matched text.
"""

import hashlib
import os
import re
import subprocess
import sys

IDENTITY = "Philipp Flotho <PhilippFlotho@gmail.com>"
MAX_BYTES = 5 * 1024 * 1024

EXCLUDED = {
    7: "0fd19fb4647c9b55ee7f950c035f33d888b7b10d40deb5eb1317d170da89965a",
    10: "5ac4a1bd0b4a831a5fcd76d0dbe03e2b33038f8058d41faec7cc296d7706d070",
    15: "aac427a207938b0684e5c8945e509ccb6d0c076f997421258a3456c8b6eb2f9d",
}
PUBLISHER = {8: "c1f35ffd9e5268dec53413e3d2d88b306788f5b95b747812f2a479241f742bf9"}

# Paths of the checker features: only here (and in commit messages touching them) the publisher-framing rule applies.
CHECKER_PATHS = re.compile(
    r"(^|/)(claim_check|integrity|screen|sanitize|author_year|retrieval)(\.py|/|_test\.py|\.md)"
    r"|(^|/)test_(claim_check|integrity|screen|sanitize|author_year|retrieval)\.py$"
    r"|(^|/)docs?/.*(claim[-_]?check|integrity|screen|reference[-_]?check|reproduction)"
    r"|(^|/)(prompts?|presets?)/.*(reproduction|citation[-_]census|claim[-_]?check)"
    r"|citation-census-reproduction|reference-census",
    re.I,
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
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("url-credentials", re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|mysql|redis|amqp)://([^\s/:@'\"`]+):([^\s/@'\"`]{6,})@")),
]
# Well-known development placeholders (compose files, CI services, test fixtures) are not secrets.
PLACEHOLDER_PASSWORDS = {"postgres", "password", "example", "changeme", "test", "testtest", "minio123"}
FORBIDDEN_FILES = re.compile(
    r"(^|/)\.env($|\.)|(^|/)auth\.json$|\.api_key$|(^|/)puppenclaw\.config\.json$|(^|/)\.credentials\.json$"
    r"|\.(pem|key|p12|pfx)$|(^|/)id_(rsa|ed25519|ecdsa)(\.pub)?$|(^|/)cookies?[^/]*\.(txt|json)$"
    r"|\.sqlite3?(-wal|-shm|-journal)?$|\.db-(wal|shm)$|\.dump$",
    re.I,
)
ALLOWED_FILES = re.compile(r"(^|/)\.env\.(example|sample|template)$", re.I)
ATTRIBUTION = re.compile(
    r"co-authored-by|signed-off-by|generated (with|by)|🤖|noreply@anthropic\.com|\bclaude\.(ai|com)/code\b", re.I
)
TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")
WORD_START = re.compile(r"(?<![a-z0-9])[a-z0-9]")


def hashed_hit(text, table):
    low = text.lower().replace("_", "-")
    for match in WORD_START.finditer(low):
        start = match.start()
        for length, digest in table.items():
            window = low[start : start + length]
            if len(window) == length and hashlib.sha256(window.encode()).hexdigest() == digest:
                return True
    return False


def git(*args, check=True):
    result = subprocess.run(["git", *args], capture_output=True, text=True, errors="replace")
    if check and result.returncode != 0:
        raise SystemExit(f"guard: git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def message_problems(message):
    lines = [line for line in message.split("\n") if not line.startswith("#")]
    while lines and not lines[-1].strip():
        lines.pop()
    problems = []
    if not lines or not lines[0].strip():
        problems.append("msg-empty")
    if len(lines) > 1:
        problems.append("msg-not-single-line")
    if any(TRAILER.match(line) for line in lines[1:]):
        problems.append("msg-trailer")
    if ATTRIBUTION.search(message):
        problems.append("msg-attribution")
    if hashed_hit(message, EXCLUDED):
        problems.append("msg-excluded-project")
    return problems


def _secret_hit(name, pattern, line):
    for match in pattern.finditer(line):
        if name == "url-credentials":
            user, password = match.group(1), match.group(2)
            if password.lower() in PLACEHOLDER_PASSWORDS or password == user:
                continue
        return True
    return False


def line_problems(path, line):
    problems = [name for name, pattern in SECRETS if _secret_hit(name, pattern, line)]
    if hashed_hit(line, EXCLUDED):
        problems.append("excluded-project")
    if CHECKER_PATHS.search(path):
        if hashed_hit(line, PUBLISHER) or PUBLISHER_FRAMING.search(line):
            problems.append("publisher-framing")
        if CORRESPONDENCE.search(line):
            problems.append("correspondence")
    return problems


def file_problems(path, size):
    problems = []
    if FORBIDDEN_FILES.search(path) and not ALLOWED_FILES.search(path):
        problems.append("forbidden-file")
    if size is not None and size > MAX_BYTES:
        problems.append("file-too-large")
    return problems


def diff_problems(diff_args):
    """Scan added lines of a diff (unified, zero context); returns (rule, location) pairs."""
    found = []
    path = None
    number = 0
    for raw in git("diff", "--no-color", "--no-ext-diff", "-U0", *diff_args).split("\n"):
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            number = int(match.group(1)) if match else 0
        elif raw.startswith("+") and path is not None:
            for rule in line_problems(path, raw[1:]):
                found.append((rule, f"{path}:{number}"))
            number += 1
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


def cmd_commit_msg(path):
    with open(path, errors="replace") as handle:
        message = handle.read()
    staged = [p for p in git("diff", "--cached", "--name-only").split("\n") if p]
    found = [(rule, "commit message") for rule in message_problems(message)]
    found += [(rule, "commit message") for rule in checker_message_problems(message, staged)]
    if found:
        print("guard: commit messages are exactly one line, without trailers or attribution.", file=sys.stderr)
    return report(found)


def ident(var):
    value = git("var", var)
    return re.sub(r"\s+\d+\s+[+-]\d{4}\s*$", "", value.strip())


def cmd_pre_commit():
    found = []
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        if ident(var) != IDENTITY:
            found.append(("identity", var))
    names =[p for p in git("diff", "--cached", "--name-only", "--diff-filter=ACMR").split("\n") if p]
    for path in names:
        size = None
        blob = git("ls-files", "-s", "--", path).split()
        if len(blob) >= 2:
            size = int(git("cat-file", "-s", blob[1]).strip() or 0)
        for rule in file_problems(path, size):
            found.append((rule, path))
    found += diff_problems(["--cached"])
    return report(found)


def commit_problems(sha):
    found = []
    author = git("show", "-s", "--format=%an <%ae>", sha).strip()
    committer = git("show", "-s", "--format=%cn <%ce>", sha).strip()
    if author != IDENTITY:
        found.append(("identity-author", sha[:12]))
    if committer != IDENTITY:
        found.append(("identity-committer", sha[:12]))
    message = git("show", "-s", "--format=%B", sha)
    found += [(rule, sha[:12]) for rule in message_problems(message)]
    parents = git("show", "-s", "--format=%P", sha).split()
    base = parents[0] if parents else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # empty tree
    paths = [p for p in git("diff", "--name-only", "--diff-filter=ACMR", base, sha).split("\n") if p]
    found += [(rule, sha[:12]) for rule in checker_message_problems(message, paths)]
    for path in paths:
        size_out = git("cat-file", "-s", f"{sha}:{path}", check=False).strip()
        for rule in file_problems(path, int(size_out) if size_out.isdigit() else None):
            found.append((rule, f"{sha[:12]}:{path}"))
    found += [(rule, f"{sha[:12]} {where}") for rule, where in diff_problems([base, sha])]
    return found


def cmd_pre_push(lines):
    found = []
    zero = "0" * 40
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, _ = parts
        if local_sha == zero:
            continue
        if remote_ref == "refs/heads/main" and os.environ.get("COSCILA_ALLOW_MAIN_PUSH") != "1":
            found.append(("push-main", remote_ref))
            continue
        for sha in git("rev-list", local_sha, "--not", "--remotes").split():
            found += commit_problems(sha)
    return report(found)


def cmd_ci(rng):
    found = []
    for sha in git("rev-list", "--no-merges", rng).split():
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
