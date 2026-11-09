"""Scan the isolated publication tree and the exact Git bytes selected for release."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

# Files are admitted one by one. This prevents runtime directories, logs, archives,
# generated credentials and unknown additions from becoming publication inputs.
ALLOWED = frozenset({
    ".gitattributes", ".gitignore", "README.md", "SECURITY.md",
    "agentchannel.py", "agentchannel_protocol.py", "agentchannel_tasks.py",
    "docs/protocol.md", "docs/runtime-upgrade.md", "docs/task-receipts.md",
    "tests/test_client.py", "tests/test_protocol.py", "tests/test_tasks.py",
    "tests/test_upgrade_runtime.py", "tools/check_public_tree.py", "tools/upgrade_runtime.py",
})
MAX_FILE_BYTES = 250_000
_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    "GitHub credential": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{35,})\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}\b"),
    "Slack credential": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{15,}\b"),
    "OpenAI credential": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}\b"),
    "URL userinfo": re.compile(r"\b(?:https?|postgres(?:ql)?|mysql)://[^/\s:@]+:[^/\s@]+@"),
    "quoted credential assignment": re.compile(
        r"""(?im)\b(?:password|client_secret|api_key|access_token|secret_key)\s*[:=]\s*["'][^"'\r\n]{16,}["']"""
    ),
    "local absolute user path": re.compile(
        r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/Users/|/home/)[A-Za-z0-9_][^/\\\s]*"
    ),
}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)


def entries(raw):
    return [row for row in raw.decode("utf-8").split("\x00") if row]


def check_files(blobs, deny):
    errors = []
    for path, raw in blobs.items():
        if path not in ALLOWED:
            errors.append(f"Unexpected publication path: {path}")
            continue
        if len(raw) > MAX_FILE_BYTES or b"\x00" in raw:
            errors.append(f"Non-text or oversized publication input: {path}")
            continue
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"Publication input is not UTF-8: {path}")
            continue
        for kind, pattern in _PATTERNS.items():
            match = pattern.search(content)
            if match:
                errors.append(f"{path}:{content.count(chr(10), 0, match.start()) + 1}: {kind} pattern")
        lowered = content.casefold()
        if any(term.casefold() in lowered or term.casefold() in path.casefold() for term in deny):
            errors.append(f"{path}: private identifier from local denylist")
    if not blobs:
        errors.append("No publication files")
    return errors


def worktree(root):
    files = {}
    for directory, dirs, names in os.walk(root):
        base = Path(directory)
        dirs[:] = [name for name in dirs if not (base == root and name == ".git")]
        for name in list(dirs):
            path = base / name
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError("Symbolic directory in publication tree")
            if path.relative_to(root).as_posix() not in {"docs", "tests", "tools"}:
                raise ValueError("Unexpected directory in publication tree: " + path.relative_to(root).as_posix())
        for name in names:
            path = base / name
            if path.is_symlink():
                raise ValueError("Symbolic file in publication tree")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Oversized file in publication tree: " + path.relative_to(root).as_posix())
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def staged(root):
    files = {}
    for row in entries(git(root, "ls-files", "--stage", "-z")):
        meta, path = row.split("\t", 1)
        mode, sha, stage = meta.split()
        if stage != "0" or mode != "100644":
            raise ValueError(f"Unexpected staged Git mode or stage for {path}")
        files[path] = git(root, "cat-file", "blob", sha)
    return files


def committed(root, ref):
    files = {}
    for row in entries(git(root, "ls-tree", "-rz", ref)):
        meta, path = row.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind != "blob" or mode != "100644":
            raise ValueError(f"Unexpected committed Git mode or object for {path}")
        files[path] = git(root, "cat-file", "blob", sha)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--deny-file", type=Path, help="private path with one deny term per line")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="audit the Git index")
    group.add_argument("--commit", action="store_true", help="audit HEAD and require a single root commit across all refs")
    args = parser.parse_args()
    root = args.root.resolve()
    deny = []
    if args.deny_file:
        if args.deny_file.resolve().is_relative_to(root):
            parser.error("denylist must be outside the publication tree")
        deny = [line.strip() for line in args.deny_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    try:
        tree = worktree(root)
        source = tree
        if args.staged:
            source = staged(root)
        elif args.commit:
            head = git(root, "rev-parse", "HEAD").decode().strip()
            refs = set(git(root, "rev-list", "--all").decode().splitlines())
            if refs != {head} or len(git(root, "rev-list", "HEAD").decode().splitlines()) != 1:
                raise ValueError("Publication candidate must have one root commit across all refs")
            metadata = git(root, "show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", head).decode("utf-8")
            if any(term.casefold() in metadata.casefold() for term in deny):
                raise ValueError("Private identifier in commit metadata")
            source = committed(root, head)
        errors = check_files(tree, deny)
        if source is not tree:
            errors.extend(check_files(source, deny))
            if set(source) != set(tree):
                errors.append("Publication tree and selected Git file lists differ")
            if source != tree and set(source) == set(tree):
                errors.append("Publication file bytes differ from selected Git bytes")
        if errors:
            print("\n".join(dict.fromkeys(errors)), file=sys.stderr)
            return 1
        print(f"Publication scan passed for {len(source)} allowlisted UTF-8 files")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Publication scan stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
