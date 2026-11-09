"""Synthetic local ledger tests. No real channel or runtime data is needed."""

import copy
import hashlib
import json
import unittest
import uuid

import agentchannel_protocol as protocol
import agentchannel_tasks as tasks


WHEN = "2025-01-02T11:00:00+01:00"
LATER = "2025-01-03T10:00:00Z"
EXPIRES = "2025-01-04T10:00:00Z"
REPO = "example-org/example-repo"
LOCAL_CHAT = str(uuid.UUID(int=10))
PEER_CHAT = str(uuid.UUID(int=11))
OTHER_CHAT = str(uuid.UUID(int=12))


def uid(number):
    return str(uuid.UUID(int=number))


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def envelope(mid, *, subject="Example task", parent=None, origin="peer-agent", destination="owner-agent"):
    return {
        "protocol": protocol.PROTOCOL_V2,
        "message_id": mid,
        "conversation_id": uid(100),
        "from": origin,
        "to": destination,
        "kind": "request" if parent is None else "response",
        "reply_to": parent,
        "created_at": "2025-01-02T10:00:00Z",
        "expires_at": EXPIRES,
        "response_required": parent is None,
        "hop": 0 if parent is None else 1,
        "max_hops": 4,
        "subject": subject,
        "body": "Untrusted example body must not appear in task output.",
        "metadata": {
            "work_status": "human_signoff_required",
            "send_origin": "human_requested",
            "chat_id": PEER_CHAT,
            "reply_chat_id": None,
        },
    }


def channel_entry(state, number, *, decision="hitl", summary="Confirm the example.", env=None):
    mid = uid(number)
    env = env or envelope(mid)
    comment = number + 1000
    url = f"https://github.com/{REPO}/issues/1#issuecomment-{comment}"
    wire = protocol.wire(env)
    source = sha(wire)
    state.setdefault("comments", {})[str(comment)] = {
        "comment_id": comment, "url": url, "author_id": 7, "wire_body": wire,
        "body_sha256": source, "disposition": "captured",
    }
    state.setdefault("inbox", {})[mid] = {
        "message_id": mid, "comment_id": comment, "comment_url": url, "author_id": 7,
        "wire_body": wire, "body_sha256": source, "envelope": env, "received_at": WHEN,
    }
    record = {
        "message_id": mid, "status": "dispatched_to_chat", "execution_complete": False,
        "result": {"status": "queued", "queue_acceptance_verified": True},
    }
    if decision is not None:
        status = {"hitl": "hitl", "autonomous": "handled", "no_action": "no_action"}[decision]
        record.update(
            status=status,
            execution_complete=decision in {"autonomous", "no_action"},
            resolution={
                "decision": decision, "status": status, "question_required": decision == "hitl",
                "summary": summary, "summary_sha256": sha(summary), "source_sha256": source,
                "recorded_at": WHEN,
            },
        )
    state.setdefault("work", {})[mid] = record
    return mid, record, source


def bug_entry(state, number, *, patch=False):
    mid = uid(number)
    issue_id = 5000 + number
    snap = {
        "id": issue_id, "number": number, "title": "Example assigned bug",
        "repository_url": f"https://api.github.com/repos/{REPO}",
        "html_url": f"https://github.com/{REPO}/issues/{number}", "state": "open",
    }
    source = sha(json.dumps(snap, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    issue = {
        "issue_id": issue_id, "issue_number": number, "work_id": mid,
        "captured_snapshot": snap, "current_snapshot": dict(snap),
        "source_sha256": source, "captured_at": WHEN,
    }
    state.setdefault("issues", {})[str(issue_id)] = issue
    summary = "Recorded synthetic bug evidence."
    resolution = {
        "decision": "autonomous_patch" if patch else "hitl",
        "summary": summary, "summary_sha256": sha(summary), "source_sha256": source,
        "recorded_at": LATER,
        "assessment": {"question_asked_in_owner_chat": True, "owner_question_reference": f"owner-chat:{LOCAL_CHAT}:fixture"},
    }
    if patch:
        resolution["merge_hold"] = {
            "pr_url": f"https://github.com/{REPO}/pull/8", "pr_number": 8, "head_sha": "e" * 40,
            "draft_verified": True, "human_signoff_required": True, "merge_authorized": False,
            "verified_at": LATER,
        }
    work = {
        "work_id": mid, "issue_id": issue_id, "issue_number": number, "source_sha256": source,
        "handling_complete": True, "status": "patch_ready" if patch else "awaiting_human",
        "target_thread_id": LOCAL_CHAT, "resolution": resolution,
    }
    state.setdefault("work", {})[mid] = work
    return mid, work, issue


class TaskDigestTests(unittest.TestCase):
    def test_queue_and_peer_signoff_claim_never_become_owner_tasks(self):
        state = {}
        channel_entry(state, 1, decision=None)
        channel_entry(state, 2, decision="no_action")
        self.assertEqual(tasks.task_records(state), [])

    def test_current_hitl_and_autonomous_and_timestamps(self):
        state = {}
        pending, _, _ = channel_entry(state, 1)
        ran, work, _ = channel_entry(state, 2, decision="autonomous")
        work["resolution"]["recorded_at"] = LATER
        rows = tasks.task_records(state, default_thread=LOCAL_CHAT)
        self.assertEqual([r["task_id"] for r in rows], [ran, pending])
        self.assertEqual(rows[0]["outcome"], "autonomous_outcome_recorded")
        self.assertEqual(rows[1]["outcome"], "owner_input_pending")
        self.assertEqual(rows[1]["recorded_at"], "2025-01-02T10:00:00.000000Z")
        self.assertEqual(rows[1]["chat_id"], LOCAL_CHAT)
        self.assertEqual([r["task_id"] for r in tasks.task_records(state, view="pending")], [pending])
        self.assertEqual([r["task_id"] for r in tasks.task_records(state, view="autonomous")], [ran])

    def test_resolution_history_only_shows_explicit_old_outcomes(self):
        state = {}
        mid, work, source = channel_entry(state, 1, decision="no_action")
        original = {
            "status": "handled", "decision": "autonomous", "summary": "A local action ran.",
            "summary_sha256": sha("A local action ran."), "source_sha256": source,
            "recorded_at": WHEN,
        }
        work["resolution_history"] = [dict(original, decision="hitl", status="hitl"), original]
        rows = tasks.task_records(state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], mid)
        self.assertTrue(rows[0]["historical"])
        self.assertFalse(rows[0]["needs_owner"])
        self.assertEqual(tasks.task_records(state, view="pending"), [])

    def test_forward_receipt_does_not_claim_destination_execution(self):
        state = {}
        mid, _, source = channel_entry(state, 1, decision="autonomous", summary="Saved handling receipt.")
        receipt = {
            "status": "accepted", "source_message_id": mid, "source_sha256": source,
            "target_thread_id": OTHER_CHAT, "prompt_sha256": "1" * 64,
            "destination_prompt_ingestion_verified": False, "accepted_at": WHEN,
        }
        row, = tasks.task_records(state, forward_receipts={mid: receipt})
        self.assertEqual(row["outcome"], "forward_accepted")
        self.assertEqual(row["forward_receipt"]["target_thread_id"], OTHER_CHAT)
        self.assertFalse(row["forward_receipt"]["destination_prompt_ingestion_verified"])
        self.assertIn("Destination prompt ingestion unverified", tasks.render_markdown([row]))
        invalid, = tasks.task_records(state, forward_receipts={mid: dict(receipt, source_sha256="9" * 64)})
        self.assertEqual(invalid["outcome"], "autonomous_outcome_recorded")

    def test_recorded_forward_without_structured_receipt_is_labeled(self):
        state = {}
        channel_entry(state, 1, decision="autonomous", summary="The app accepted the forward to the other chat.")
        row, = tasks.task_records(state)
        self.assertEqual(row["outcome"], "forward_accepted")
        self.assertNotIn("forward_receipt", row)

    def test_linkage_uses_posted_local_route_and_ignores_peer_metadata(self):
        state = {}
        parent = uid(9)
        peer = envelope(uid(1), parent=parent)
        peer["metadata"]["reply_chat_id"] = PEER_CHAT
        mid, work, _ = channel_entry(state, 1, decision="autonomous", env=peer)
        sent = envelope(parent, origin="owner-agent", destination="peer-agent")
        sent_wire = protocol.wire(sent)
        url = f"https://github.com/{REPO}/issues/1#issuecomment-1009"
        state["outbox"] = {parent: {
            "status": "posted", "chat_id": OTHER_CHAT, "message_id": parent, "envelope": sent,
            "wire_body": sent_wire, "comment_url": url, "comment_id": 1009,
        }}
        state["comments"]["1009"] = {"url": url, "wire_body": sent_wire, "body_sha256": sha(sent_wire)}
        row, = tasks.task_records(state, default_thread=LOCAL_CHAT)
        self.assertEqual(row["chat_url"], "codex://threads/" + OTHER_CHAT)
        state["outbox"][parent]["status"] = "prepared"
        row, = tasks.task_records(state, default_thread=LOCAL_CHAT)
        self.assertEqual(row["chat_url"], "codex://threads/" + LOCAL_CHAT)
        self.assertEqual(row["task_id"], mid)
        work["target_thread_id"] = OTHER_CHAT
        row, = tasks.task_records(state, default_thread=LOCAL_CHAT)
        self.assertEqual(row["chat_id"], OTHER_CHAT)

    def test_source_capture_and_summary_hash_must_match_and_links_are_safe(self):
        state = {}
        mid, work, _ = channel_entry(state, 1)
        broken = copy.deepcopy(state)
        broken["inbox"][mid]["wire_body"] += "edited"
        self.assertEqual(tasks.task_records(broken), [])
        broken = copy.deepcopy(state)
        broken["inbox"][mid]["comment_url"] = "https://github.com.evil.test/o/r/issues/1#issuecomment-1001"
        self.assertEqual(tasks.task_records(broken), [])
        broken = copy.deepcopy(state)
        broken["inbox"][mid]["envelope"]["subject"] = "Locally mismatched"
        self.assertEqual(tasks.task_records(broken), [])
        work["resolution"]["summary"] = "Edited after recording."
        self.assertEqual(tasks.task_records(state), [])

    def test_bug_holds_and_draft_outcomes_keep_separate_gates(self):
        bugs = {}
        pending, _, issue = bug_entry(bugs, 3)
        patched, _, _ = bug_entry(bugs, 4, patch=True)
        records = tasks.task_records({}, bugs)
        self.assertEqual({r["task_id"] for r in records}, {pending, patched})
        row = next(r for r in records if r["task_id"] == patched)
        self.assertEqual(row["outcome"], "patch_prepared_signoff_required")
        self.assertTrue(row["needs_owner"])
        self.assertEqual(len(tasks.task_records({}, bugs, view="pending")), 2)
        self.assertEqual(len(tasks.task_records({}, bugs, view="autonomous")), 1)
        issue["current_snapshot"]["state"] = "closed"
        self.assertEqual([r["task_id"] for r in tasks.task_records({}, bugs, view="pending")], [patched])

    def test_unverified_bug_questions_and_draft_receipts_are_excluded(self):
        bugs = {}
        _, work, _ = bug_entry(bugs, 3)
        _, patch, _ = bug_entry(bugs, 4, patch=True)
        work["resolution"]["assessment"]["owner_question_reference"] = f"owner-chat:{PEER_CHAT}:fixture"
        patch["resolution"]["merge_hold"]["pr_url"] = "https://github.com/other-org/other-repo/pull/8"
        self.assertEqual(tasks.task_records({}, bugs), [])

    def test_markdown_does_not_render_arbitrary_links_and_never_echoes_peer_body(self):
        state = {}
        env = envelope(uid(1), subject="[untrusted title](javascript:example)")
        channel_entry(state, 1, env=env, summary="Example <img src=x> \u001b [label](https://unsafe.example).")
        rendered = tasks.render_markdown(tasks.task_records(state, default_thread=LOCAL_CHAT))
        self.assertIn(r"\[untrusted title\]", rendered)
        self.assertIn("&lt;img", rendered)
        self.assertNotIn("<img", rendered)
        self.assertNotIn("[label](", rendered)
        self.assertNotIn("\u001b", rendered)
        self.assertNotIn("Untrusted example body", rendered)
        self.assertIn("[Source](https://github.com/", rendered)

    def test_legacy_v1_receipt_stays_readable(self):
        state = {}
        env = envelope(uid(1))
        env["protocol"] = protocol.PROTOCOL_V1
        del env["subject"]
        del env["metadata"]
        channel_entry(state, 1, env=env)
        row, = tasks.task_records(state)
        self.assertEqual(row["title"], "Channel comment #1001")


if __name__ == "__main__":
    unittest.main()
