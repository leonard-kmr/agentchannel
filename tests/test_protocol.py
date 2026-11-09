"""Protocol vectors use fictional participants and fixed UTC timestamps."""

import copy
import datetime as dt
import json
import unittest

import agentchannel_protocol as protocol


REQUEST = "11111111-1111-4111-8111-111111111111"
CONVERSATION = "22222222-2222-4222-8222-222222222222"
REPLY = "33333333-3333-4333-8333-333333333333"
CHAT_A = "44444444-4444-4444-8444-444444444444"
CHAT_B = "55555555-5555-4555-8555-555555555555"
OTHER_CHAT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOW = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)


def envelope(version=protocol.PROTOCOL_V2):
    row = {
        "protocol": version, "message_id": REQUEST, "conversation_id": CONVERSATION,
        "from": "agent-a", "to": "agent-b", "kind": "request", "reply_to": None,
        "created_at": "2026-01-01T11:55:00Z", "expires_at": "2026-01-02T11:55:00Z",
        "response_required": True, "hop": 0, "max_hops": 4,
        "body": "Please check the **example**.",
    }
    if version == protocol.PROTOCOL_V2:
        row.update(
            subject="Example check",
            metadata={
                "work_status": "human_signoff_required", "send_origin": "human_requested",
                "chat_id": CHAT_A, "reply_chat_id": None,
            },
        )
    return row


def reply(parent, version=protocol.PROTOCOL_V2, kind="response"):
    row = envelope(version)
    row.update(
        message_id=REPLY, conversation_id=parent["conversation_id"],
        reply_to=parent["message_id"], hop=parent["hop"] + 1,
        kind=kind, response_required=False,
        created_at="2026-01-01T11:59:00Z",
        **{"from": parent["to"], "to": parent["from"]},
    )
    if version == protocol.PROTOCOL_V2:
        row["metadata"].update(
            work_status="autonomous", send_origin="autonomous", chat_id=CHAT_B,
            reply_chat_id=protocol.chat_id(parent),
        )
    return row


def framed(marker, payload):
    return f"{marker}\n```json\n{payload}\n```\n"


class WireTests(unittest.TestCase):
    def test_round_trip_preserves_markdown_and_cannot_end_the_json_fence(self):
        row = envelope()
        row["body"] = "# Details\n\n```html\n<div>Example</div>\n```\n\n**Done**."
        comment = protocol.wire(row)
        self.assertTrue(protocol.is_protocol_comment(comment))
        self.assertEqual(comment.count("\n```"), 2)
        self.assertEqual(protocol.parse_envelope(comment, {"agent-a", "agent-b"}), row)

    def test_legacy_without_subject_or_metadata_stays_readable(self):
        row = envelope(protocol.PROTOCOL_V1)
        row.update(body="<div>Legacy archive content</div>", response_required=False)
        comment = protocol.wire(row)
        self.assertTrue(comment.startswith(protocol.MARKER_V1))
        self.assertEqual(protocol.parse_envelope(comment), row)
        self.assertIsNone(protocol.chat_id(row))

    def test_marker_must_match_the_protocol_and_the_entire_comment(self):
        valid = protocol.wire(envelope())
        for invalid in (
            valid.replace(protocol.MARKER_V2, protocol.MARKER_V1, 1),
            valid + "\nA second payload",
            "preamble\n" + valid,
            valid.replace(protocol.MARKER_V2, "<!-- terminus-agent-channel:v3 -->", 1),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                protocol.parse_envelope(invalid)
        self.assertFalse(protocol.is_protocol_comment("ordinary issue comment"))

    def test_duplicate_keys_and_oversized_or_deep_comments_fail(self):
        payload = json.dumps(envelope())
        for invalid in (
            framed(protocol.MARKER_V2, payload.replace('"metadata": {', '"metadata": {"chat_id":null,', 1)),
            framed(protocol.MARKER_V2, payload.replace('"kind":', '"kind":"info","kind":', 1)),
            framed(protocol.MARKER_V2, "[" * 2500 + "0" + "]" * 2500),
            framed(protocol.MARKER_V2, " " * protocol.MAX_COMMENT_BYTES),
        ):
            with self.subTest(size=len(invalid)), self.assertRaises(ValueError):
                protocol.parse_envelope(invalid)

    def test_agent_allowlist_and_identity_shape(self):
        valid = protocol.wire(envelope())
        with self.assertRaisesRegex(ValueError, "unknown agent"):
            protocol.parse_envelope(valid, {"agent-a", "agent-c"})
        row = envelope()
        row["from"] = "agent-a\n"
        with self.assertRaisesRegex(ValueError, "invalid agent name"):
            protocol.wire(row)

    def test_v2_requires_all_metadata_and_subject_fields(self):
        malformed = []
        for field in ("subject", "metadata"):
            row = envelope()
            del row[field]
            malformed.append(row)
        for field in protocol.METADATA_FIELDS:
            row = envelope()
            del row["metadata"][field]
            malformed.append(row)
        for field, value in (
            ("send_origin", "operator"),
            ("work_status", "approved"),
            ("chat_id", OTHER_CHAT.upper()),
            ("reply_chat_id", CHAT_B),
        ):
            row = envelope()
            row["metadata"][field] = value
            malformed.append(row)
        for subject in (" ", "two\nlines", "bad\u2028line", "x\tb", "é" * 101, "<b>HTML</b>"):
            row = envelope()
            row["subject"] = subject
            malformed.append(row)
        for row in malformed:
            with self.subTest(row=row), self.assertRaises(ValueError):
                protocol.parse_envelope(framed(protocol.MARKER_V2, json.dumps(row)))

    def test_subject_and_body_limits_measure_utf8(self):
        row = envelope()
        row.update(subject="é" * 100, body="é" * 6000)
        self.assertEqual(protocol.parse_envelope(protocol.wire(row)), row)
        row["body"] += "é"
        with self.assertRaises(ValueError):
            protocol.wire(row)

    def test_v2_kinds_and_response_required_have_independent_rules(self):
        for kind, allowed in (
            ("request", (True,)), ("hello", (True, False)), ("urgent", (True, False)),
            ("info", (False,)), ("update", (False,)),
        ):
            for response_required in (True, False):
                row = envelope()
                row.update(kind=kind, response_required=response_required)
                with self.subTest(kind=kind, response_required=response_required):
                    if response_required in allowed:
                        self.assertEqual(protocol.parse_envelope(protocol.wire(row)), row)
                    else:
                        with self.assertRaises(ValueError):
                            protocol.wire(row)
        for kind in ("ack", "response", "blocked"):
            row = envelope()
            row.update(kind=kind, response_required=False)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                protocol.wire(row)

    def test_common_invalid_types_and_bounds(self):
        for field, value in (
            ("hop", True), ("max_hops", 5), ("response_required", 1),
            ("message_id", OTHER_CHAT.upper()), ("expires_at", "2026-01-02T11:55:00"),
            ("created_at", "2026-01-03T11:55:00Z"), ("kind", ["request"]),
        ):
            row = envelope()
            row[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                protocol.wire(row)


class MarkdownTests(unittest.TestCase):
    def test_accepts_markdown_plain_text_autolinks_and_code(self):
        for body in (
            "# Heading\n\n- **Bold** and [link](https://example.invalid).",
            "Use `<div>` to illustrate the tag.",
            "Use ``<div> and ``` delimiters``.",
            "Use 1 < 2, \\<b>, or &lt;b&gt;.",
            "See <https://example.invalid/page> or <help@example.invalid>.",
            "[Link](https://example.invalid)\n\nUse `<b>` in a separate paragraph.",
            "~~~html\n<div>literal</div>\n~~~~\n**After**",
            "```html\n<!-- literal -->\n```",
        ):
            with self.subTest(body=body):
                protocol.validate_markdown(body)

    def test_rejects_html_and_contextual_backtick_bypasses(self):
        for body in (
            "<b>bold</b>", "x<br/>y", "text <!-- hidden -->",
            "<?xml version='1.0'?>", "<!DOCTYPE html>", "<![CDATA[data]]>",
            "<script\n src=x></script>", "\\\\<b>even escapes</b>",
            "`unclosed <b>", "```\n```\n<b>outside fence</b>",
            "fake separator\u2028```\n<b>outside fence</b>",
            "- item\n  ```\n<b>after dedent</b>",
            "| a | b |\n| --- | --- |\n| `a | <b>outside code</b> ` |",
            "[link](path`) <b>outside code</b> `",
            "`open span\n` <b>outside code</b> `",
            '[link](path "\nmultiline title `") <b>outside code</b> `',
            "ordinary text\x00", "  ",
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                protocol.validate_markdown(body)


class LinkTests(unittest.TestCase):
    def test_v2_echoes_parent_origin_separately_from_own_origin(self):
        parent = envelope()
        row = reply(parent)
        self.assertEqual(protocol.chat_id(row), CHAT_B)
        self.assertEqual(row["metadata"]["reply_chat_id"], CHAT_A)
        self.assertTrue(protocol.validate_link(row, {REQUEST: parent}, NOW))
        for wrong in (OTHER_CHAT, None):
            bad = copy.deepcopy(row)
            bad["metadata"]["reply_chat_id"] = wrong
            with self.assertRaisesRegex(ValueError, "authenticated parent"):
                protocol.validate_link(bad, {REQUEST: parent}, NOW)

    def test_mixed_version_and_legacy_footer_hint(self):
        parent = envelope(protocol.PROTOCOL_V1)
        parent["body"] += f"\n\nCodex chat ID: {CHAT_A}\n"
        self.assertEqual(protocol.chat_id(parent), CHAT_A)
        row = reply(parent)
        self.assertTrue(protocol.validate_link(row, {REQUEST: parent}, NOW))
        modern = envelope()
        old_reply = reply(modern, protocol.PROTOCOL_V1)
        self.assertTrue(protocol.validate_link(old_reply, {REQUEST: modern}, NOW))
        self.assertIsNone(protocol.chat_id(old_reply))
        parent["body"] += "Following text"
        self.assertIsNone(protocol.chat_id(parent))

    def test_reply_link_rejects_forgery_and_no_reply_parent(self):
        parent = envelope()
        row = reply(parent)
        with self.assertRaisesRegex(ValueError, "unknown or ambiguous parent"):
            protocol.validate_link(row, {}, NOW)
        for field, value in (
            ("conversation_id", OTHER_CHAT), ("from", "other-agent"), ("hop", 2),
            ("max_hops", 5), ("created_at", "2026-01-02T11:55:01Z"),
        ):
            malformed = dict(row, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                protocol.validate_link(malformed, {REQUEST: parent}, NOW)
        parent.update(kind="info", response_required=False)
        with self.assertRaisesRegex(ValueError, "does not permit a reply"):
            protocol.validate_link(row, {REQUEST: parent}, NOW)

    def test_v2_hello_only_accepts_ack_and_ack_cannot_ask_for_a_reply(self):
        parent = envelope()
        parent["kind"] = "hello"
        with self.assertRaisesRegex(ValueError, "acknowledgement"):
            protocol.validate_link(reply(parent), {REQUEST: parent}, NOW)
        ack = reply(parent, kind="ack")
        self.assertTrue(protocol.validate_link(protocol.parse_envelope(protocol.wire(ack)), {REQUEST: parent}, NOW))
        ack["response_required"] = True
        with self.assertRaises(ValueError):
            protocol.wire(ack)

    def test_link_freshness_does_not_reject_valid_archived_shape(self):
        row = envelope()
        row["expires_at"] = "2026-01-01T11:58:00Z"
        self.assertEqual(protocol.parse_envelope(protocol.wire(row)), row)
        self.assertFalse(protocol.validate_link(row, {}, NOW))
        row.update(created_at="2026-01-01T12:11:00Z", expires_at="2026-01-01T13:00:00Z")
        with self.assertRaisesRegex(ValueError, "too far in the future"):
            protocol.validate_link(row, {}, NOW)


if __name__ == "__main__":
    unittest.main()
