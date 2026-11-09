"""Offline coverage for pinned upgrades and state-preserving rollback."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import upgrade_runtime as upgrade


class RuntimeUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "runtime"
        (self.home / "bin").mkdir(parents=True)
        self.config = self.home / "config.json"
        self.config.write_bytes(b'{"mode":"example","enabled":true}')
        self.state = self.home / "state.json"
        self.state.write_bytes(b'{"inbox":{},"outbox":{},"comments":{},"position":1}')
        self.stop = self.home / "service.stop"
        self.stop.write_bytes(b"{}")
        self.bundle = self.root / "staged"
        (self.bundle / "files" / "bin").mkdir(parents=True)
        self.installed = self.home / "bin" / "agent_channel.py"
        self.before = b"VERSION = 1\n"
        self.after = b"VERSION = 2\n"
        self.installed.write_bytes(self.before)
        self.extension = self.home / "bin" / "other_extension.py"
        self.extension.write_bytes(b"EXTENSION = True\n")
        self.protocol = self.home / "bin" / "agentchannel_protocol.py"
        self.write_bundle()

    def write_bundle(self, entries=None):
        if entries is None:
            entries = (
                ("bin/agentchannel_protocol.py", None, b"PROTOCOL = 2\n"),
                ("bin/agent_channel.py", self.before, self.after),
            )
        files = []
        for name, before, after in entries:
            (self.bundle / "files" / name).write_bytes(after)
            files.append({
                "path": name,
                "before_sha256": upgrade.sha(before),
                "after_sha256": upgrade.sha(after),
            })
        manifest = {"schema_version": 1, "files": files}
        self.manifest = self.bundle / "manifest.json"
        self.manifest.write_bytes(json.dumps(manifest).encode("utf-8"))
        self.pin = upgrade.sha(self.manifest.read_bytes())

    def run_cli(self, action, *, pin=None, backup=None):
        args = [action, "--home", str(self.home)]
        if action == "rollback":
            args += ["--backup", backup]
        else:
            args += ["--bundle", str(self.bundle), "--manifest-sha256", pin or self.pin]
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = upgrade.main(args)
        return code, json.loads(stdout.getvalue())

    def do_apply(self):
        code, result = self.run_cli("apply")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "installed_stopped")
        return result["backup_id"]

    def test_plan_is_read_only_and_does_not_require_stopping(self):
        self.stop.unlink()
        before = sorted(self.home.rglob("*"))
        code, result = self.run_cli("plan")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "planned")
        self.assertFalse(result["stop_marker_present"])
        self.assertEqual(sorted(self.home.rglob("*")), before)
        self.assertEqual(self.installed.read_bytes(), self.before)

    def test_apply_rollback_preserves_newer_non_v2_state_and_other_files(self):
        initial_config = self.config.read_bytes()
        initial_state = self.state.read_bytes()
        initial_extension = self.extension.read_bytes()
        backup = self.do_apply()
        self.assertEqual(self.config.read_bytes(), initial_config)
        self.assertEqual(self.state.read_bytes(), initial_state)
        self.assertEqual(self.extension.read_bytes(), initial_extension)
        self.assertEqual(self.installed.read_bytes(), self.after)
        self.assertEqual(self.protocol.read_bytes(), b"PROTOCOL = 2\n")
        self.assertTrue(self.stop.exists())

        updated = b'{"inbox":{},"outbox":{},"comments":{},"position":2}'
        self.state.write_bytes(updated)
        code, result = self.run_cli("rollback", backup=backup)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "rolled_back_stopped")
        self.assertEqual(self.state.read_bytes(), updated)
        self.assertEqual(self.config.read_bytes(), initial_config)
        self.assertEqual(self.extension.read_bytes(), initial_extension)
        self.assertEqual(self.installed.read_bytes(), self.before)
        self.assertFalse(self.protocol.exists())
        self.assertTrue(self.stop.exists())
        code, result = self.run_cli("rollback", backup=backup)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "already_rolled_back")

    def test_apply_requires_service_stop_and_both_idle_locks(self):
        self.stop.unlink()
        self.assertIn("Stop the receiver", self.run_cli("apply")[1]["error"])
        self.stop.write_bytes(b"{}")
        for lock in upgrade.LOCKS:
            with self.subTest(lock=lock), upgrade.runtime_lock(self.home, lock):
                code, result = self.run_cli("apply")
                self.assertEqual(code, 2, result)
                self.assertIn("Receiver is busy", result["error"])
        self.assertEqual(self.installed.read_bytes(), self.before)
        self.assertFalse((self.home / "backups").exists())

    def test_changed_base_or_payload_refuses_before_mutation(self):
        self.installed.write_bytes(b"VERSION = 'local edits'\n")
        code, result = self.run_cli("apply")
        self.assertEqual(code, 2, result)
        self.assertIn("differs from the reviewed base", result["error"])
        self.installed.write_bytes(self.before)
        (self.bundle / "files" / "bin" / "agent_channel.py").write_bytes(b"modified\n")
        code, result = self.run_cli("apply")
        self.assertEqual(code, 2, result)
        self.assertIn("payload hash mismatch", result["error"])
        self.assertEqual(self.installed.read_bytes(), self.before)
        self.assertFalse((self.home / "backups").exists())

    def test_bad_manifest_hash_or_python_syntax_refused(self):
        code, result = self.run_cli("plan", pin="0" * 64)
        self.assertEqual(code, 2, result)
        self.assertIn("does not match its pin", result["error"])
        self.write_bundle((("bin/agent_channel.py", self.before, b"invalid =\n"),))
        code, result = self.run_cli("plan")
        self.assertEqual(code, 2, result)
        self.assertIn("Payload syntax error", result["error"])

    def test_path_traversal_unexpected_fields_and_duplicates_are_rejected(self):
        for file_rows in (
            [{"path": "bin/../state.json", "before_sha256": None, "after_sha256": "1" * 64}],
            [{"path": "bin/foo.py", "before_sha256": None, "after_sha256": "1" * 64, "extra": 1}],
            [{"path": "bin/foo.py", "before_sha256": None, "after_sha256": "1" * 64}] * 2,
        ):
            with self.subTest(file_rows=file_rows):
                data = json.dumps({"schema_version": 1, "files": file_rows}).encode("utf-8")
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.load_manifest(data)
        with self.assertRaisesRegex(upgrade.UpgradeError, "Duplicate JSON field"):
            upgrade.load_manifest(b'{"schema_version":1,"schema_version":1,"files":[]}')

    def test_bad_backup_id_and_tampered_backup_are_refused(self):
        code, result = self.run_cli("rollback", backup="../outside")
        self.assertEqual(code, 2, result)
        self.assertIn("canonical UUID", result["error"])
        backup_id = self.do_apply()
        saved = self.home / "backups" / "runtime-upgrade" / backup_id / "files" / "bin"
        (saved / self.installed.name).write_bytes(b"tampered")
        code, result = self.run_cli("rollback", backup=backup_id)
        self.assertEqual(code, 2, result)
        self.assertIn("Backup module hash mismatch", result["error"])
        self.assertEqual(self.installed.read_bytes(), self.after)
        self.assertTrue(self.protocol.exists())

    def test_rollback_refuses_independent_module_drift_or_v2_traffic(self):
        backup = self.do_apply()
        self.state.write_bytes(b'{"outbox":{"x":{"envelope":{"protocol":"terminus-agent-channel/2"}}}}')
        code, result = self.run_cli("rollback", backup=backup)
        self.assertEqual(code, 2, result)
        self.assertIn("Version 2 traffic exists", result["error"])
        self.state.write_bytes(b'{"inbox":{},"outbox":{},"comments":{}}')
        self.installed.write_bytes(b"VERSION = 99\n")
        code, result = self.run_cli("rollback", backup=backup)
        self.assertEqual(code, 2, result)
        self.assertIn("independently changed module", result["error"])
        self.assertEqual(self.installed.read_bytes(), b"VERSION = 99\n")
        self.assertTrue(self.protocol.exists())

    def test_interrupted_multi_file_apply_can_recover_from_prepared_backup(self):
        original = upgrade.atomic_bytes

        def interrupt(path, content):
            if path == self.installed:
                raise KeyboardInterrupt
            original(path, content)

        with mock.patch.object(upgrade, "atomic_bytes", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cli("apply")
        self.assertEqual(self.installed.read_bytes(), self.before)
        self.assertTrue(self.protocol.exists())
        root = self.home / "backups" / "runtime-upgrade"
        backup, = list(root.iterdir())
        self.assertEqual(upgrade.read_journal(backup)["status"], "prepared")
        code, result = self.run_cli("rollback", backup=backup.name)
        self.assertEqual(code, 0, result)
        self.assertEqual(self.installed.read_bytes(), self.before)
        self.assertFalse(self.protocol.exists())

    def test_write_error_reverts_prior_module_replacements(self):
        original = upgrade.atomic_bytes

        def denied(path, content):
            if path == self.installed and content == self.after:
                raise OSError("synthetic refusal")
            original(path, content)

        with mock.patch.object(upgrade, "atomic_bytes", side_effect=denied):
            code, result = self.run_cli("apply")
        self.assertEqual(code, 2, result)
        self.assertIn("prior modules were restored", result["error"])
        self.assertEqual(self.installed.read_bytes(), self.before)
        self.assertFalse(self.protocol.exists())
        backup, = list((self.home / "backups" / "runtime-upgrade").iterdir())
        self.assertEqual(upgrade.read_journal(backup)["status"], "rolled_back")

    def test_interrupted_rollback_keeps_a_recoverable_journal(self):
        backup_id = self.do_apply()
        backup = self.home / "backups" / "runtime-upgrade" / backup_id
        original = upgrade.atomic_bytes

        def interrupt(path, content):
            original(path, content)
            if path == self.installed:
                raise KeyboardInterrupt

        with mock.patch.object(upgrade, "atomic_bytes", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cli("rollback", backup=backup_id)
        self.assertEqual(upgrade.read_journal(backup)["status"], "prepared")
        self.assertEqual(self.installed.read_bytes(), self.before)
        self.assertTrue(self.protocol.exists())
        code, result = self.run_cli("rollback", backup=backup_id)
        self.assertEqual(code, 0, result)
        self.assertFalse(self.protocol.exists())

    def test_invalid_state_refuses_rollback_without_overwriting_code(self):
        backup_id = self.do_apply()
        self.state.write_bytes(b'{"inbox":{"x":{"envelope":null}}}')
        code, result = self.run_cli("rollback", backup=backup_id)
        self.assertEqual(code, 2, result)
        self.assertIn("Unexpected runtime record", result["error"])
        self.assertEqual(self.installed.read_bytes(), self.after)

    def test_symlink_payload_is_refused(self):
        payload = self.bundle / "files" / "bin" / "agent_channel.py"
        payload.unlink()
        real = self.root / "alias-target.py"
        real.write_bytes(self.after)
        try:
            payload.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable for this test account")
        code, result = self.run_cli("apply")
        self.assertEqual(code, 2, result)
        self.assertIn("Expected a regular file", result["error"])
        self.assertEqual(self.installed.read_bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
