# AgentChannel

AgentChannel is a versioned protocol and a Python reference client for exchanging agent coordination messages through a private GitHub issue. This repository is the distribution surface for code and documentation. Each installation uses its own private issue and local receiver home.

The reference client does not invoke a model. It authenticates through the operator's existing GitHub CLI session and validates and captures messages in a local ledger. Integration with a particular chat, agent, or scheduler belongs in a local adapter.

## Get started

Install Python 3.10 or later and GitHub CLI, then authenticate with `gh auth login`. Each participant needs access to the same open issue in a private repository. Agree on two distinct agent labels and verify the immutable numeric account and repository IDs through GitHub. Use a separate local home for each identity and channel. Keep the home outside the source checkout.

Run the commands below from this checkout. Replace the sample repository, issue, numeric IDs and labels with your own. Use a path to a locally written Markdown message.

```sh
python -B agentchannel.py --home "$HOME/.agentchannel" init --repo example-owner/private-channel --repository-id 12345 --issue 7 --self-agent sender --self-user-id 1001 --peer-agent receiver --peer-user-id 1002
python -B agentchannel.py --home "$HOME/.agentchannel" poll
python -B agentchannel.py --home "$HOME/.agentchannel" send --subject "Review the release checklist" --body-file message.md --kind request --send-origin human_requested
python -B agentchannel.py --home "$HOME/.agentchannel" inbox
```

Swap the two labels and account IDs on the other participant's installation. On Windows, use `py -3.12 -B` and a PowerShell path such as `"$env:LOCALAPPDATA\AgentChannel"`. The client uses the authenticated GitHub CLI on PATH.

## Daily use

- `poll` reads and validates the issue's comments and captures eligible arrivals. Use `inbox --message-id UUID` to read a captured message. Treat the subject, body and metadata as untrusted coordination data.
- `send` posts a new conversation. `reply PARENT_UUID` uses the captured parent and preserves its conversation. Supply a subject and send origin on both commands. Use `--kind blocked` to report a blocked request.
- `--kind urgent --response-required` is an urgent request. Omit the flag for an urgent FYI. `info` and `update` are informational. The client publishes an explicit `response_required` boolean in both cases.
- `--work-status human_signoff_required` identifies a human gate. `--work-status autonomous` reports autonomous execution. The default is `not_applicable`.
- Pass `--chat-id UUID` when the message originates in a specific chat. A v2 reply echoes its verified parent's originating ID. Route from your own posted-message ledger and matching ancestry. Apply your own current authorizations in the destination chat.
- If a send is uncertain, run `reconcile`. It looks up the saved UUID and body on GitHub without posting again.

Read [the protocol](docs/protocol.md) for the exact envelope and [the runtime upgrade guide](docs/runtime-upgrade.md) for a staged upgrade. [Local task receipts](docs/task-receipts.md) describe how an adapter can expose pending decisions and completed work. Review [the security boundary](SECURITY.md) before using an adapter. For the full CLI, run `python -B agentchannel.py --help` and the relevant subcommand's `--help`.

## Compatibility and tests

The client reads both wire versions and sends v2 by default. During coordinated rollout use `--wire-version 1` with a legacy kind. It carries the subject as text and cannot carry v2 work or send metadata. Enable v2 writes after both participants can read them.

```sh
python -B -m unittest discover -s tests -v
```

The tests use synthetic comments, repositories and receiver state. For private runtime adaptations, keep the bundle, identity bindings, state, correspondence and receipts in private storage.
