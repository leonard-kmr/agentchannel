"""Offline transport tests with synthetic comments and accounts."""

import copy
import datetime as dt
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from agentchannel import ChannelError, Client, Config, GitHub, UncertainSend, load_json, local_lock
import agentchannel_protocol as protocol


CONFIG = Config("example-owner/example-private-channel", 30001, 7, "sender", 40001, "peer", 40002)
ISSUE = "https://api.github.com/" + CONFIG.issue_endpoint
CHAT = "ed4fda6c-8aef-4550-8d2e-5048a70442f5"
OTHER_CHAT = "18c39152-855e-4186-a15c-ecf2a12d57f9"


class SyntheticGitHub(GitHub):
    """Simulate real provider shapes without sockets or credentials."""

    PAGE_SIZE = 2
    MAX_PAGES = 4

    def __init__(self):
        self.rows = []
        self.user_id = CONFIG.self_user_id
        self.repo_id = CONFIG.repository_id
        self.private = True
        self.pull_request = False
        self.post_calls = 0
        self.endpoints = []
        self.timeout_after_post = False
        self.timeout_before_post = False
        self.omit_last = False
        self.duplicate_pages = False

    def get(self, endpoint):
        self.endpoints.append(endpoint)
        if endpoint == "user":
            return {"id": self.user_id}
        if endpoint == f"repos/{CONFIG.repo}":
            return {"id": self.repo_id, "full_name": CONFIG.repo, "private": self.private}
        if endpoint == CONFIG.issue_endpoint:
            issue = {"number": CONFIG.issue, "comments": len(self.rows), "url": ISSUE}
            if self.pull_request:
                issue["pull_request"] = {}
            return issue
        if endpoint.startswith(CONFIG.issue_endpoint + "/comments?"):
            page = int(endpoint.rsplit("=", 1)[1])
            start = 0 if self.duplicate_pages else (page - 1) * self.PAGE_SIZE
            rows = self.rows[:-1] if self.omit_last else self.rows
            return copy.deepcopy(rows[start:start + self.PAGE_SIZE])
        if endpoint.startswith(f"repos/{CONFIG.repo}/issues/comments/"):
            cid = int(endpoint.rsplit("/", 1)[1])
            return copy.deepcopy(next(row for row in self.rows if row["id"] == cid))
        raise AssertionError(f"unexpected endpoint {endpoint}")

    def add(self, raw, author=CONFIG.peer_user_id):
        now = protocol.utcstamp()
        row = {
            "id": len(self.rows) + 3000, "body": raw, "user": {"id": author},
            "issue_url": ISSUE, "created_at": now, "updated_at": now,
        }
        self.rows.append(row)
        return row

    def post(self, config, wire_body):
        self.post_calls += 1
        if self.timeout_before_post:
            raise subprocess.TimeoutExpired("synthetic", 0.1)
        row = self.add(wire_body, CONFIG.self_user_id)
        if self.timeout_after_post:
            raise subprocess.TimeoutExpired("synthetic", 0.1)
        return copy.deepcopy(row)


def peer_message(*, parent=None, kind=None, version=2, chat_id=None):
    now = protocol.utcnow()
    mid = str(uuid.uuid4())
    kind = kind or ("response" if parent else "request")
    envelope = {
        "protocol": protocol.PROTOCOL_V2 if version == 2 else protocol.PROTOCOL_V1,
        "message_id": mid, "conversation_id": parent["conversation_id"] if parent else mid,
        "from": CONFIG.peer_agent, "to": CONFIG.self_agent, "kind": kind,
        "reply_to": parent["message_id"] if parent else None, "created_at": protocol.utcstamp(now),
        "expires_at": protocol.utcstamp(now + dt.timedelta(hours=1)),
        "response_required": kind in {"request", "hello"},
        "hop": parent["hop"] + 1 if parent else 0, "max_hops": protocol.MAX_HOPS,
        "body": "Synthetic Markdown body.",
    }
    if version == 2:
        envelope.update(subject="Synthetic subject", metadata={
            "work_status": "not_applicable", "send_origin": "human_requested",
            "chat_id": chat_id, "reply_chat_id": protocol.chat_id(parent) if parent else None,
        })
    elif chat_id:
        envelope["body"] += f"\n\nCodex chat ID: {chat_id}"
    return envelope


class ClientTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.github = SyntheticGitHub()
        self.client = Client(Path(tmp.name) / "channel", self.github)
        self.assertEqual(self.client.initialize(CONFIG), {"status": "configured"})

    def send(self, **options):
        return self.client.send(
            **dict(subject="Synthetic request", body="**Check** the fixture.", send_origin="human_requested", **options)
        )

    def insert(self, envelope, author=CONFIG.peer_user_id):
        return self.github.add(protocol.wire(envelope), author)

    def test_v2_send_readback_and_reply_route(self):
        result = self.send(chat_id=CHAT, work_status="human_signoff_required")
        sent = protocol.parse_envelope(self.github.rows[0]["body"])
        self.assertEqual(result["message_id"], sent["message_id"])
        self.assertEqual(sent["protocol"], protocol.PROTOCOL_V2)
        self.assertEqual(sent["subject"], "Synthetic request")
        self.assertTrue(sent["response_required"])
        self.assertEqual(sent["metadata"]["work_status"], "human_signoff_required")
        self.assertEqual(sent["metadata"]["send_origin"], "human_requested")
        incoming = peer_message(parent=sent, chat_id=OTHER_CHAT)
        self.insert(incoming)
        mid = incoming["message_id"]
        self.assertEqual(self.client.poll()["new_message_ids"], [mid])
        self.assertEqual(self.client.poll()["new_message_ids"], [])
        captured = self.client.inbox(mid)
        self.assertEqual(captured["verified_return_chat_id"], CHAT)
        self.assertEqual(captured["status"], "received")

    def test_new_kinds_and_explicit_response_flag(self):
        for kind, required in (("info", False), ("update", False), ("urgent", True), ("urgent", False)):
            self.send(kind=kind, response_required=required)
            sent = protocol.parse_envelope(self.github.rows[-1]["body"])
            self.assertEqual(sent["kind"], kind)
            self.assertIs(sent["response_required"], required)
        with self.assertRaises(ValueError):
            self.send(kind="request", response_required=False)

    def test_legacy_compatibility_and_reply_echo(self):
        legacy = peer_message(version=1, chat_id=CHAT)
        self.insert(legacy)
        parent = legacy["message_id"]
        self.assertEqual(self.client.poll()["new_message_ids"], [parent])
        self.send(parent_id=parent, chat_id=OTHER_CHAT)
        reply = protocol.parse_envelope(self.github.rows[-1]["body"])
        self.assertEqual(reply["metadata"]["reply_chat_id"], CHAT)
        self.assertEqual(reply["metadata"]["chat_id"], OTHER_CHAT)
        self.assertEqual(reply["conversation_id"], legacy["conversation_id"])
        with self.assertRaises(ChannelError):
            self.send(parent_id=parent)
        self.send(wire_version=1, chat_id=CHAT)
        old = protocol.parse_envelope(self.github.rows[-1]["body"])
        self.assertEqual(old["protocol"], protocol.PROTOCOL_V1)
        self.assertTrue(old["body"].startswith("## Synthetic request"))
        self.assertEqual(protocol.chat_id(old), CHAT)
        with self.assertRaises(ChannelError):
            self.send(wire_version=1, work_status="human_signoff_required")

    def test_capture_rejects_spoof_and_invalid_ancestry(self):
        spoof = peer_message()
        self.insert(spoof, CONFIG.self_user_id)
        own = self.send(chat_id=CHAT)
        parent = protocol.parse_envelope(self.github.rows[-1]["body"])
        bad = peer_message(parent=parent)
        bad["metadata"]["reply_chat_id"] = OTHER_CHAT
        self.insert(bad)
        self.assertEqual(self.client.poll()["new_message_ids"], [])
        self.assertEqual(self.client.inbox(), {})
        self.assertEqual(own["status"], "posted")

    def test_manually_posted_parent_never_establishes_local_return_route(self):
        peer_request = peer_message(chat_id=CHAT)
        peer_request["from"], peer_request["to"] = CONFIG.self_agent, CONFIG.peer_agent
        self.insert(peer_request, CONFIG.self_user_id)
        reply = peer_message(parent=peer_request)
        self.insert(reply)
        self.client.poll()
        self.assertNotIn("verified_return_chat_id", self.client.inbox(reply["message_id"]))

    def test_edited_deleted_and_reused_sources_require_review(self):
        envelope = peer_message()
        original = self.insert(envelope)
        mid = envelope["message_id"]
        self.assertEqual(self.client.poll()["new_message_ids"], [mid])
        original["body"] += "\nchanged"
        self.client.poll()
        self.assertEqual(self.client.inbox(mid)["status"], "review_required")
        self.github.rows.clear()
        self.client.poll()
        self.github.add(protocol.wire(envelope))["id"] = 3999
        self.assertEqual(self.client.poll()["new_message_ids"], [])
        self.assertEqual(self.client.inbox(mid)["status"], "review_required")
        self.assertEqual(self.client.inbox(mid)["envelope"], envelope)

    def test_wrong_identity_public_repo_and_pull_request_fail_before_post(self):
        for field, value in (("user_id", 45000), ("repo_id", 31000), ("private", False), ("pull_request", True)):
            previous = getattr(self.github, field)
            setattr(self.github, field, value)
            with self.assertRaises(ChannelError):
                self.send()
            setattr(self.github, field, previous)
        self.assertEqual(self.github.post_calls, 0)

    def test_incomplete_and_duplicate_pages_fail_closed(self):
        for _ in range(4):
            self.github.add("Synthetic ordinary comment")
        for mode in ("omit_last", "duplicate_pages"):
            setattr(self.github, mode, True)
            with self.assertRaises(ChannelError):
                self.send()
            setattr(self.github, mode, False)
        self.assertFalse(self.client.state_path.exists())
        self.assertEqual(self.github.post_calls, 0)
        self.client.poll()
        self.assertIn("page=2", " ".join(self.github.endpoints))

    def test_no_post_on_reconcile_after_provider_timeout(self):
        self.github.timeout_after_post = True
        with self.assertRaises(UncertainSend):
            self.send()
        state = load_json(self.client.state_path)
        mid, row = next(iter(state["outbox"].items()))
        self.assertEqual(row["status"], "uncertain")
        with self.assertRaises(ChannelError):
            self.send()
        self.assertEqual(self.github.post_calls, 1)
        self.assertEqual(self.client.reconcile()["messages"], {mid: "posted"})
        self.assertEqual(self.github.post_calls, 1)
        self.assertEqual(self.client.reconcile()["messages"], {})

    def test_missing_uncertain_comment_is_not_retried(self):
        self.github.timeout_before_post = True
        with self.assertRaises(UncertainSend):
            self.send()
        mid = next(iter(load_json(self.client.state_path)["outbox"]))
        self.assertEqual(self.client.reconcile()["messages"], {mid: "uncertain"})
        self.assertEqual(self.client.reconcile()["messages"], {mid: "uncertain"})
        self.assertEqual(self.github.post_calls, 1)

    def test_conflicting_uncertain_comment_requires_review(self):
        self.github.timeout_after_post = True
        with self.assertRaises(UncertainSend):
            self.send()
        mid = next(iter(load_json(self.client.state_path)["outbox"]))
        self.github.add(self.github.rows[0]["body"], CONFIG.self_user_id)
        self.assertEqual(self.client.reconcile()["messages"], {mid: "review_required"})
        self.assertEqual(self.github.post_calls, 1)

    def test_reply_refuses_parent_edited_since_capture(self):
        peer = peer_message()
        row = self.insert(peer)
        self.client.poll()
        row["body"] += "\nextra"
        with self.assertRaises(ChannelError):
            self.send(parent_id=peer["message_id"])
        self.assertEqual(self.github.post_calls, 0)

    def test_second_writer_cannot_enter_local_state_lock(self):
        with local_lock(self.client.home):
            with self.assertRaisesRegex(ChannelError, "Channel state is locked"):
                self.client.poll()
        self.assertEqual(self.client.poll()["status"], "polled")


    def test_no_raw_html_on_new_messages_in_either_version(self):
        for version in (1, 2):
            with self.assertRaises(ValueError):
                self.client.send(subject="Synthetic test", body="<b>raw</b>",
                                 send_origin="human_requested", wire_version=version)
        self.assertEqual(self.github.post_calls, 0)


if __name__ == "__main__":
    unittest.main()
