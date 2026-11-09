#!/usr/bin/env python3
"""Apply a pinned, reviewed Python overlay to a stopped local receiver."""

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path


MANIFEST_LIMIT = 64 * 1024
MODULE_LIMIT = 2 * 1024 * 1024
STATE_LIMIT = 128 * 1024 * 1024
MODULE_PATH = re.compile(r"bin/[a-z][a-z0-9_]*\.py")
DIGEST = re.compile(r"[0-9a-f]{64}")
LOCKS = (".receiver.lock", ".service.lock")


class UpgradeError(RuntimeError):
    """A precondition failed or a local install requires reconciliation."""


# Validate paths and reviewed bytes without executing any payload.

def regular_path(path, kind, *, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise UpgradeError(f"Missing {path.name}") from None
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    test = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if reparse or not test(info.st_mode):
        raise UpgradeError(f"Expected a regular {kind}: {path.name}")
    return info


def root_directory(path):
    path = Path(path).expanduser()
    regular_path(path, "directory")
    return path.resolve(strict=True)


def read_bytes(path, limit, *, missing=False):
    info = regular_path(path, "file", missing=missing)
    if info is None:
        return None
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise UpgradeError(f"File exceeds size limit: {path.name}")
    return data


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpgradeError("Duplicate JSON field")
        result[key] = value
    return result


def parse_json(data):
    return json.loads(data.decode("utf-8"), object_pairs_hook=unique_fields)


def sha(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def is_digest(value):
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def load_manifest(data):
    manifest = parse_json(data)
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "files"}:
        raise UpgradeError("Unexpected manifest fields")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise UpgradeError("Unsupported manifest version")
    rows = manifest["files"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 24:
        raise UpgradeError("Expected between 1 and 24 reviewed modules")
    names = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "before_sha256", "after_sha256"}:
            raise UpgradeError("Unexpected manifest file entry")
        name, before, after = row["path"], row["before_sha256"], row["after_sha256"]
        if not isinstance(name, str) or not MODULE_PATH.fullmatch(name) or name in names:
            raise UpgradeError("Unsafe or duplicate module path")
        if (before is not None and not is_digest(before)) or not is_digest(after) or before == after:
            raise UpgradeError(f"Invalid before/after hashes: {name}")
        names.add(name)
    return rows


def load_bundle(home, bundle, manifest_pin):
    bundle = root_directory(bundle)
    if bundle == home or home in bundle.parents or bundle in home.parents:
        raise UpgradeError("Keep the staged overlay outside the runtime tree")
    manifest = read_bytes(bundle / "manifest.json", MANIFEST_LIMIT)
    pin = manifest_pin.lower()
    if not is_digest(pin) or sha(manifest) != pin:
        raise UpgradeError("Manifest SHA-256 does not match its pin")
    rows = load_manifest(manifest)
    regular_path(bundle / "files", "directory")
    regular_path(bundle / "files" / "bin", "directory")
    payload = {}
    for row in rows:
        name = row["path"]
        data = read_bytes(bundle / "files" / name, MODULE_LIMIT)
        if sha(data) != row["after_sha256"]:
            raise UpgradeError(f"Reviewed payload hash mismatch: {name}")
        compile(data, name, "exec")
        payload[name] = data
    return manifest, rows, payload


def read_originals(home, rows):
    originals = {}
    for row in rows:
        name = row["path"]
        original = read_bytes(home / name, MODULE_LIMIT, missing=True)
        if sha(original) != row["before_sha256"]:
            raise UpgradeError(f"Installed module differs from the reviewed base: {name}")
        originals[name] = original
    return originals


# Share the receiver's nonblocking OS locks. A busy process is never killed.

@contextlib.contextmanager
def runtime_lock(home, name):
    path = home / name
    regular_path(path, "file", missing=True)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            def acquire():
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise UpgradeError(f"Receiver is busy ({name}); finish its work before proceeding") from exc
        try:
            yield
        finally:
            handle.seek(0)
            release()


@contextlib.contextmanager
def stopped_runtime(home):
    if regular_path(home / "service.stop", "file", missing=True) is None:
        raise UpgradeError("Stop the receiver with its existing installer first")
    # Match the established upgrade lock order. Neither acquisition can wait.
    with runtime_lock(home, LOCKS[0]), runtime_lock(home, LOCKS[1]):
        regular_path(home / "service.stop", "file")
        yield


# A single replacement is atomic. A backup journal makes interrupted batches recoverable.

def atomic_bytes(path, content):
    original = regular_path(path, "file", missing=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".upgrade-", dir=path.parent, delete=False) as file:
            temp = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if original is not None and path.suffix == ".py":
            os.chmod(temp, stat.S_IMODE(original.st_mode) & 0o777)
            # Prevent an equal-size rewrite in the same second from reusing a stale .pyc.
            mtime = max(time.time_ns(), original.st_mtime_ns + 2_000_000_000)
            os.utime(temp, ns=(mtime, mtime))
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def backup_root(home, *, create=False):
    parent = home / "backups"
    root = parent / "runtime-upgrade"
    for path in (parent, root):
        if regular_path(path, "directory", missing=True) is None:
            if not create:
                return None
            path.mkdir(mode=0o700)
    return root


def journal(path, manifest_pin, status):
    atomic_bytes(path / "journal.json", json.dumps({
        "schema_version": 1,
        "manifest_sha256": manifest_pin,
        "status": status,
    }, sort_keys=True).encode("utf-8"))


def read_journal(path):
    data = parse_json(read_bytes(path / "journal.json", MANIFEST_LIMIT))
    if (not isinstance(data, dict) or set(data) != {"schema_version", "manifest_sha256", "status"}
            or type(data["schema_version"]) is not int or data["schema_version"] != 1
            or not is_digest(data["manifest_sha256"])
            or data["status"] not in ("prepared", "installed", "rolled_back")):
        raise UpgradeError("Invalid upgrade journal")
    return data


def verify_restore_targets(home, rows):
    # A mix of exact old/new bytes is recoverable. Refuse independently edited files.
    for row in rows:
        name = row["path"]
        digest = sha(read_bytes(home / name, MODULE_LIMIT, missing=True))
        if digest not in (row["before_sha256"], row["after_sha256"]):
            raise UpgradeError(f"Rollback would overwrite an independently changed module: {name}")


def restore(home, rows, originals):
    for name, data in reversed(list(originals.items())):
        path = home / name
        if sha(read_bytes(path, MODULE_LIMIT, missing=True)) == sha(data):
            continue
        if data is None:
            path.unlink()
        else:
            atomic_bytes(path, data)
    read_originals(home, rows)


def apply(home, manifest, rows, payload):
    with stopped_runtime(home):
        originals = read_originals(home, rows)
        root = backup_root(home, create=True)
        for existing in root.iterdir():
            if regular_path(existing, "directory", missing=True) and (
                existing / "journal.json"
            ).exists() and read_journal(existing)["status"] == "prepared":
                raise UpgradeError(f"Recover the interrupted overlay first: {existing.name}")
        backup = root / str(uuid.uuid4())
        backup.mkdir(mode=0o700)
        files = backup / "files" / "bin"
        files.mkdir(mode=0o700, parents=True)
        for name, data in originals.items():
            if data is not None:
                atomic_bytes(backup / "files" / name, data)
                if sha(read_bytes(backup / "files" / name, MODULE_LIMIT)) != sha(data):
                    raise UpgradeError(f"Could not verify backup: {name}")
        pin = sha(manifest)
        atomic_bytes(backup / "manifest.json", manifest)
        journal(backup, pin, "prepared")
        try:
            for name, data in payload.items():
                atomic_bytes(home / name, data)
                if sha(read_bytes(home / name, MODULE_LIMIT)) != sha(data):
                    raise UpgradeError(f"Could not verify installed module: {name}")
            journal(backup, pin, "installed")
        except (OSError, UpgradeError) as exc:
            try:
                verify_restore_targets(home, rows)
                restore(home, rows, originals)
                journal(backup, pin, "rolled_back")
            except (OSError, UpgradeError) as recovery:
                raise UpgradeError(f"Overlay recovery needs attention; keep stopped, backup {backup.name}") from recovery
            raise UpgradeError(f"Overlay failed and prior modules were restored; backup {backup.name}") from exc
    return {"status": "installed_stopped", "backup_id": backup.name, "modules": list(payload)}


def ensure_legacy_rollback_safe(home):
    state = parse_json(read_bytes(home / "state.json", STATE_LIMIT))
    if not isinstance(state, dict):
        raise UpgradeError("Unexpected runtime state while checking rollback")
    for section in ("inbox", "outbox", "comments"):
        records = state.get(section, {})
        if not isinstance(records, dict):
            raise UpgradeError("Unexpected runtime state while checking rollback")
        for record in records.values():
            if not isinstance(record, dict):
                raise UpgradeError("Unexpected runtime record while checking rollback")
            envelope = record.get("envelope", {})
            wire = record.get("wire_body", "")
            if not isinstance(envelope, dict) or not isinstance(wire, str):
                raise UpgradeError("Unexpected runtime record while checking rollback")
            if envelope.get("protocol") == "terminus-agent-channel/2" or wire.startswith(
                "<!-- terminus-agent-channel:v2 -->"
            ):
                raise UpgradeError("Version 2 traffic exists; keep dual-read support and repair forward")


def rollback(home, backup_id):
    try:
        valid = str(uuid.UUID(backup_id)) == backup_id
    except ValueError:
        valid = False
    if not valid:
        raise UpgradeError("Backup ID must be a canonical UUID")
    with stopped_runtime(home):
        root = backup_root(home)
        if root is None:
            raise UpgradeError("No local overlay backups")
        backup = root / backup_id
        regular_path(backup, "directory")
        record = read_journal(backup)
        manifest = read_bytes(backup / "manifest.json", MANIFEST_LIMIT)
        if sha(manifest) != record["manifest_sha256"]:
            raise UpgradeError("Backup manifest hash mismatch")
        rows = load_manifest(manifest)
        if record["status"] == "rolled_back":
            read_originals(home, rows)
            return {"status": "already_rolled_back", "backup_id": backup_id}
        ensure_legacy_rollback_safe(home)
        regular_path(backup / "files", "directory")
        regular_path(backup / "files" / "bin", "directory")
        originals = {}
        for row in rows:
            name = row["path"]
            data = read_bytes(backup / "files" / name, MODULE_LIMIT, missing=True)
            if sha(data) != row["before_sha256"]:
                raise UpgradeError(f"Backup module hash mismatch: {name}")
            originals[name] = data
        verify_restore_targets(home, rows)
        # An interruption during rollback is the same recoverable old/new mix as apply.
        journal(backup, record["manifest_sha256"], "prepared")
        restore(home, rows, originals)
        journal(backup, record["manifest_sha256"], "rolled_back")
    return {"status": "rolled_back_stopped", "backup_id": backup_id}


# CLI. No provider calls, config writes, service starts or message replay.

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "apply", "rollback"):
        sub = commands.add_parser(action)
        sub.add_argument("--home", required=True, type=Path)
        if action == "rollback":
            sub.add_argument("--backup", required=True)
        else:
            sub.add_argument("--bundle", required=True, type=Path)
            sub.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        home = root_directory(args.home)
        regular_path(home / "bin", "directory")
        for name in ("config.json", "state.json"):
            regular_path(home / name, "file")
        if args.action == "rollback":
            result = rollback(home, args.backup)
        else:
            manifest, rows, payload = load_bundle(home, args.bundle, args.manifest_sha256)
            if args.action == "plan":
                read_originals(home, rows)
                result = {
                    "status": "planned",
                    "modules": rows,
                    "stop_marker_present": regular_path(home / "service.stop", "file", missing=True) is not None,
                }
            else:
                result = apply(home, manifest, rows, payload)
        print(json.dumps(result, sort_keys=True))
        return 0
    except SyntaxError as exc:
        print(json.dumps({"status": "refused", "error": f"Payload syntax error: {exc.filename}:{exc.lineno}"}))
        return 2
    except (OSError, ValueError, UpgradeError) as exc:
        print(json.dumps({"status": "refused", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
