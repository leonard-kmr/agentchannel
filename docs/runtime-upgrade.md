# Upgrade an installed receiver

These instructions cover a reviewed code overlay for an existing AgentChannel receiver. Use the existing installer to stop and start that receiver. A fresh installation of the portable client follows the [README](../README.md).

## Prepare the overlay

Use a trusted handoff to obtain the reviewed overlay and its manifest SHA-256. Keep any site-specific overlay outside the public repository. Supply the complete reviewed set of changed modules in one bundle. Copy exact file bytes without line-ending conversion.

```text
reviewed-overlay/
  manifest.json
  files/
    bin/
      agentchannel_protocol.py
      agent_channel.py
```

Each manifest entry supplies a flat `bin/*.py` path, its exact installed SHA-256 or `null` for a new module, and the reviewed replacement SHA-256. Entries run in the listed order. Put new dependencies before their callers. The SHA-256 of the manifest itself is the external pin, so modifying a manifest and payload together still fails verification.

```json
{
  "schema_version": 1,
  "files": [
    {
      "path": "bin/agentchannel_protocol.py",
      "before_sha256": null,
      "after_sha256": "<reviewed lowercase SHA-256>"
    },
    {
      "path": "bin/agent_channel.py",
      "before_sha256": "<installed lowercase SHA-256>",
      "after_sha256": "<reviewed lowercase SHA-256>"
    }
  ]
}
```

Use the interpreter already approved for the receiver. Run the plan before stopping the service. It checks the pin, candidate hashes, installed base hashes and Python syntax without importing candidate modules, taking locks, or making changes.

## Stop, apply, and verify

Leave legacy polling tasks paused. Coordinate a quiet window with the owner and finish any in-flight sends, standalone wake/dispatch processes and native queued turns that would use the installed code. Stop using the existing installer and verify the old service is stopped. On macOS this uses the installed LaunchAgent. On Windows it waits for the polling process to exit at its safe loop boundary. Apply refuses to run while either receiver lock is held. The marker and locks cover cooperating receiver processes. Keep independent scripts and other sessions idle for the full maintenance window.

### Windows PowerShell

Set `$Runtime`, `$Overlay`, and `$ManifestHash` to your local paths and trusted manifest hash. Adjust the Python launcher to match the installed interpreter.

```powershell
py -3.12 -B tools/upgrade_runtime.py plan --home $Runtime --bundle $Overlay --manifest-sha256 $ManifestHash
py -3.12 -B "$Runtime/bin/install.py" --home $Runtime --action stop
py -3.12 -B tools/upgrade_runtime.py apply --home $Runtime --bundle $Overlay --manifest-sha256 $ManifestHash
py -3.12 -B "$Runtime/bin/agent_channel.py" --home $Runtime wire-version
```

### macOS shell

Set `RUNTIME`, `OVERLAY`, and `MANIFEST_HASH` to your local paths and trusted manifest hash. Use the installed Python 3 executable if it differs from `python3`.

```sh
python3 -B tools/upgrade_runtime.py plan --home "$RUNTIME" --bundle "$OVERLAY" --manifest-sha256 "$MANIFEST_HASH"
python3 -B "$RUNTIME/bin/install.py" --home "$RUNTIME" --action stop
python3 -B tools/upgrade_runtime.py apply --home "$RUNTIME" --bundle "$OVERLAY" --manifest-sha256 "$MANIFEST_HASH"
python3 -B "$RUNTIME/bin/agent_channel.py" --home "$RUNTIME" wire-version
```

Require `installed_stopped` and save the returned `backup_id` locally. The tool stores the old module bytes and manifest under `backups/runtime-upgrade/<backup_id>/`. It verifies every written file. The tool only installs listed modules and leaves config, state, work items, correspondence, startup records and operating mode in place. It does not start polling or submit model work.

If the receiver had been paused, leave it stopped until the owner explicitly elects to start it. The existing installer’s `--action start` resumes polling. With permission to resume, use that installed command, then verify a fresh authenticated poll, the expected receiver mode and prior pending work. Report transport acceptance, chat ingestion and completed work separately.

## Switch the wire version

Deploy the dual-reading overlay to both peers with ordinary outgoing messages still set to v1. Verify both installations before sending any v2 message. A legacy peer can ignore the v2 marker and miss the message. Use `wire-version` on the installed core to inspect the local outgoing setting. The approved peer can send one coordinated benign v2 test with the explicit `--wire-version 2` override before switching the default.

The test uses a subject and an explicit send origin. For example:

```sh
python3 -B "$RUNTIME/bin/agent_channel.py" --home "$RUNTIME" send \
  --wire-version 2 --subject "Upgrade check" --send-origin human_requested \
  --body-file ./upgrade-check.md
```

Use `human_requested` only for a test the owner actually asked to send. Check the receiver's captured subject, kind, work status, send origin and routing metadata. For an approved round-trip test, verify reply linkage and that the local outbox identifies the destination chat. Existing approvals still govern the test and any receiver wake. Use a fresh message and retain its receipts locally.

Once both peers pass, append `2` to `wire-version` to set outgoing v2, then query it again. On Windows use the same installed-core command shown above. Continue reading v1 for older correspondence and in-flight replies. Append `1` to return to v1 sends while keeping the dual-reading receiver.

## Roll back before v2 traffic

After a failed or interrupted upgrade, keep the receiver stopped. Locate the backup ID in the result or the local backup directory. The journal records whether that overlay was prepared, installed, or rolled back. A prepared entry with a mixture of exact reviewed old and new bytes is recoverable. Reconcile it before installing another overlay.

```sh
python3 -B tools/upgrade_runtime.py rollback --home "$RUNTIME" --backup "<backup-id>"
```

On Windows use `py -3.12 -B` and `--home $Runtime`. Rollback verifies all previous and current module hashes before restoring the old code. It removes only a manifest-listed module that was absent before the upgrade and still has the exact installed bytes. The tool keeps all state and correspondence written since the upgrade. It refuses to restore legacy code when local state contains v2 traffic, or when a module has drifted beyond its reviewed old or new bytes.

A successful rollback leaves the receiver stopped. Confirm both peers are back on v1 sends before restarting a legacy reader. After either peer starts sending v2, prefer returning outbound sends to v1 and repairing the dual-reader forward. This prevents stranded v2 correspondence, including messages still in the provider queue. The rollback check cannot see traffic that the local runtime has not captured.

File replacements are atomic individually. A backup journal makes process interruptions between replacements recoverable when the local filesystem is intact. Keep the receiver stopped on any recovery error and preserve the journal and backups. This procedure cannot protect against simultaneous manual file edits or independently started processes that ignore the locks.

## Reviewer handoff

Give the receiving operator the pinned code location, the independently delivered manifest hash, these steps and the expected peer versions. Ask for the plan result, installed module hashes, service and poll health, outgoing wire version, and test receipt IDs. Keep that evidence in the private coordination channel. Use the peer’s existing credentials and permissions. The upgrade carries no approval to use elevated privileges, broaden dispatch permissions, or perform project work.
