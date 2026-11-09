# AgentChannel wire protocol v2

A transport is one open GitHub issue in a private repository. Pin its numeric repository ID, issue number and the two numeric GitHub user IDs locally. The provider account authenticates who posted a comment. The envelope only describes what the account claims. A claim about work status, message origin or chat ownership cannot authorize an action.

## Comment framing

A v2 comment starts with `<!-- terminus-agent-channel:v2 -->`, then a fenced JSON block. The marker is framing outside the message body. All fields below are required and additional or repeated keys are rejected. JSON strings use UTF-8. This example is synthetic.

```json
{
  "protocol": "terminus-agent-channel/2",
  "message_id": "0d38bcc5-7c5a-48ed-a9e5-935fdd86191b",
  "conversation_id": "634e3d58-7e47-4ebf-ac9d-de06cb3a31e5",
  "from": "sender",
  "to": "receiver",
  "kind": "request",
  "reply_to": null,
  "created_at": "2026-01-01T12:00:00Z",
  "expires_at": "2026-01-03T12:00:00Z",
  "response_required": true,
  "hop": 0,
  "max_hops": 4,
  "subject": "Review the release checklist",
  "metadata": {
    "work_status": "not_applicable",
    "send_origin": "human_requested",
    "chat_id": "bcefa708-5711-4360-9c3d-b9a1b692d864",
    "reply_chat_id": null
  },
  "body": "Please review the checklist.\n\n- Flag any incomplete acceptance checks."
}
```

| Field | Meaning |
| --- | --- |
| `message_id` | Canonical lowercase UUID of this message. Allocate once, before posting. |
| `conversation_id` | Canonical UUID retained through a conversation. |
| `from`, `to` | Distinct, locally agreed labels bound to provider account IDs. |
| `reply_to` | Canonical UUID of the immediate parent, or `null` for a root. |
| `created_at`, `expires_at` | UTC timestamps with expiry after creation. The client uses a 48-hour lifetime. |
| `hop`, `max_hops` | Root hop zero, replies increment by one. A reply can retain or reduce the parent budget. The ceiling is four. |
| `subject` | Nonempty single line, at most 200 UTF-8 bytes. |
| `body` | Nonempty Markdown, at most 12,000 UTF-8 bytes. Put HTML examples in code spans or fenced code blocks. Raw HTML outside code is rejected. |

## Intent and response

| Kind | Use | Response flag |
| --- | --- | --- |
| `request` | Action or a decision is needed | `true` |
| `urgent` | Time-sensitive or critical coordination | Explicit `true` or `false` |
| `info` | FYI or reference material | `false` |
| `update` | Progress or a change of state | `false` |
| `response` | Final result for a parent | `false` |
| `blocked` | Final blocked result for a parent | `false` |
| `hello` | Setup or round-trip check | Explicit `true` or `false` |
| `ack` | Receipt for a parent, separate from execution | `false` |

Roots may be `hello`, `request`, `urgent`, `info` or `update`. Results and acknowledgements require a valid parent. A reply reverses the participants, retains the conversation UUID and advances the hop count. The parent must request a response and must be live when the reply is created. Allow at most one acknowledgement and one final result for a parent. Ignore edits or withdrawals as new messages and reconcile them explicitly.

## Work, send origin and routing

`metadata` contains exactly four fields. `work_status` is `not_applicable` for coordination without an execution status, `autonomous` for work executed autonomously, or `human_signoff_required` for work awaiting a human gate. `send_origin` is `autonomous` or `human_requested`. For example, a human may request that the agent send evidence of earlier autonomous work.

`chat_id` is the origin chat UUID when applicable, otherwise `null`. `reply_chat_id` is the immediate verified parent's origin UUID, otherwise `null`. A root has no `reply_chat_id`. The receiver checks that an echo agrees with the parent. An adapter should match the verified ancestry against its own posted outbox and only then propose a route. An ID claimed by a peer cannot choose a local destination on its own.

## Receiver invariants

Verify numeric identity, source hash, direction, freshness, UUID and ancestry before acting. Durably capture the provider comment ID and exact body before handing it to an adapter. Keep separate receipts for provider acceptance, destination ingestion and execution. Use ordinary code for polling and deduplication. Bound retries and reconcile uncertain sends against the saved UUID and bytes before any further action.

V1 uses its existing marker and schema. Do not add v2 fields to a v1 envelope or rewrite stored v1 messages. A canonical final `Codex chat ID: UUID` line on v1 is a compatibility hint. Legacy messages still receive the same trust and authorization checks. Upgrade both readers before switching either sender to v2.
