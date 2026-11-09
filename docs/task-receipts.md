# Local task digest and receipts

The receiver's saved decision is the task log. A message's work metadata is a coordination claim, while the local decision records what the receiver actually did. Queue acceptance and ingestion are recorded separately from a task outcome.

In an upgraded runtime, run the following commands from the installation. Use the existing receiver home.

```sh
python -B "$RUNTIME/bin/agent_channel.py" --home "$RUNTIME" tasks --view pending --format markdown
python -B "$RUNTIME/bin/agent_channel.py" --home "$RUNTIME" tasks --view autonomous --format json
```

- Pending lists recorded human-input decisions that still require the owner, with a link to the local chat or verified source where available. Ask the owner and send any configured notification before recording a human-input decision.
- Autonomous lists local outcomes, with the source, context, decision and receipt summary. A forwarded item records the handoff as such. A merge, deployment or other action belongs in the receipt only after its applicable verification.
- Superseding a decision preserves the earlier outcome as history. Historical human-input entries do not reopen a completed task.
- The digest reads existing receiver and assigned-bug ledgers. It neither polls GitHub nor starts a model. Keep the output local unless the owner explicitly authorizes sharing.

The standalone transport captures messages. Its library exposes the task digest for adapters that maintain a local decision ledger. The reference client does not execute peer requests or derive task decisions from wire metadata.
