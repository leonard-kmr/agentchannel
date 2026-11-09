"""Build a read-only task digest from locally verified AgentChannel ledgers."""

import hashlib
import html
import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone

from agentchannel_protocol import parse_envelope


_REPO = r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+"
_ISSUE = re.compile(rf"https://github\.com/(?P<repo>{_REPO})/issues/(?P<issue>[1-9][0-9]*)")
_COMMENT = re.compile(_ISSUE.pattern + r"#issuecomment-(?P<comment>[1-9][0-9]*)")
_PR = re.compile(rf"https://github\.com/(?P<repo>{_REPO})/pull/(?P<pr>[1-9][0-9]*)")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_HEAD = re.compile(r"[0-9a-f]{40}")
_VIEWS = {"pending", "autonomous", "all"}
_FORWARD_SUMMARY = re.compile(
    r"(?:\b(?:app|tool|chat) accepted (?:the )?(?:forward|handoff)\b"
    r"|(?:^|[.!?]\s+)Forwarded (?:the |this |to\b))", re.IGNORECASE,
)


# Local receipts determine which records appear. Peer subject and metadata are display-only.

def _mapping(value):
    return value if isinstance(value, dict) else {}


def _sha(value):
    if not isinstance(value, str):
        return None
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None


def _is_digest(value):
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _canonical_uuid(value):
    if not isinstance(value, str):
        return None
    try:
        return value if str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


def _positive_int(value):
    return type(value) is int and value > 0


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if stamp.tzinfo is not None and stamp.utcoffset() is not None:
            return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        pass
    return None


def _match_url(pattern, value):
    return pattern.fullmatch(value) if isinstance(value, str) else None


def _display(value, limit):
    if not isinstance(value, str):
        return ""
    value = "".join(char if not unicodedata.category(char).startswith("C") else " " for char in value)
    value = " ".join(value.split())
    return value[:limit - 1] + "…" if len(value) > limit else value


def _summary(resolution, *, utf8_bom=False):
    text = resolution.get("summary")
    expected = resolution.get("summary_sha256")
    if not isinstance(text, str) or not text.strip() or not _is_digest(expected):
        return None
    if _sha(text) != expected and (not utf8_bom or _sha("\ufeff" + text) != expected):
        return None
    return _display(text, 2000)


def _source_state(capture, incoming):
    if any(row.get("disposition") in {"review_required", "rejected", "deleted", "source_changed"}
           or row.get("source_changed") is True for row in (capture, incoming)):
        return "changed_since_capture"
    return "captured"


def _wire_matches(row, envelope):
    try:
        return parse_envelope(row.get("wire_body")) == envelope
    except (ValueError, TypeError, UnicodeError):
        return False


def _verified_channel_source(state, task_id):
    inbox = _mapping(state.get("inbox"))
    row = _mapping(inbox.get(task_id))
    comment_id = row.get("comment_id")
    if not _positive_int(comment_id):
        return None
    cap = _mapping(_mapping(state.get("comments")).get(str(comment_id)))
    envelope = _mapping(row.get("envelope"))
    source = row.get("body_sha256")
    url = row.get("comment_url")
    parsed = _match_url(_COMMENT, url)
    if (not parsed or int(parsed["comment"]) != comment_id
            or cap.get("comment_id") != comment_id or cap.get("url") != url
            or not _positive_int(row.get("author_id")) or cap.get("author_id") != row["author_id"]
            or not _is_digest(source) or cap.get("body_sha256") != source
            or _sha(row.get("wire_body")) != source or cap.get("wire_body") != row.get("wire_body")
            or envelope.get("message_id") != task_id or not _wire_matches(row, envelope)
            or (row.get("message_id") is not None and row["message_id"] != task_id)):
        return None
    return {
        "source_url": url,
        "source_sha256": source,
        "source_state": _source_state(cap, row),
        "title": _display(envelope.get("subject"), 180) or f"Channel comment #{comment_id}",
        "envelope": envelope,
        "received_at": _stamp(row.get("received_at")),
        "source_kind": "channel_message",
    }


def _verified_bug_source(state, task_id, work):
    issue_id = work.get("issue_id")
    number = work.get("issue_number")
    if not _positive_int(issue_id) or not _positive_int(number):
        return None
    issue = _mapping(_mapping(state.get("issues")).get(str(issue_id)))
    snapshot = _mapping(issue.get("captured_snapshot"))
    source = issue.get("source_sha256")
    url = snapshot.get("html_url")
    parsed = _match_url(_ISSUE, url)
    if (not parsed or int(parsed["issue"]) != number
            or issue.get("work_id") != task_id or issue.get("issue_id") != issue_id
            or issue.get("issue_number") != number
            or snapshot.get("id") != issue_id or snapshot.get("number") != number
            or not _is_digest(source) or work.get("source_sha256") != source
            or _sha(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) != source
            or snapshot.get("repository_url") != "https://api.github.com/repos/" + parsed["repo"]):
        return None
    current = _mapping(issue.get("current_snapshot"))
    return {
        "source_url": url,
        "source_sha256": source,
        "source_state": "changed_since_capture" if work.get("source_changed") or issue.get("source_changed") else "captured",
        "title": _display(snapshot.get("title"), 180) or f"GitHub issue #{number}",
        "received_at": _stamp(issue.get("captured_at")),
        "source_kind": "assigned_github_bug",
        "repo": parsed["repo"],
        "closed_at_last_capture": current.get("state") == "closed",
    }


# Route links derive from local dispatch, a checked outbox parent or an accepted forward.

def _outbox_route(state, envelope):
    parent_id = _canonical_uuid(envelope.get("reply_to"))
    if parent_id is None:
        return None
    parent = _mapping(_mapping(state.get("outbox")).get(parent_id))
    sent = _mapping(parent.get("envelope"))
    hop = sent.get("hop")
    convo = _canonical_uuid(sent.get("conversation_id"))
    cid = parent.get("comment_id")
    capture = _mapping(_mapping(state.get("comments")).get(str(cid))) if _positive_int(cid) else {}
    wire = parent.get("wire_body")
    saved_wire = capture.get("wire_body")
    if (parent.get("status") == "posted" and parent.get("message_id") == sent.get("message_id") == parent_id
            and convo and convo == envelope.get("conversation_id")
            and sent.get("from") == envelope.get("to") and sent.get("to") == envelope.get("from")
            and type(hop) is int and type(envelope.get("hop")) is int and envelope["hop"] == hop + 1
            and _wire_matches(parent, sent) and capture.get("url") == parent.get("comment_url")
            and _is_digest(capture.get("body_sha256")) and _sha(saved_wire) == capture["body_sha256"]
            and isinstance(wire, str) and isinstance(saved_wire, str) and wire.strip() == saved_wire.strip()
            and capture.get("disposition") != "review_required"):
        return _canonical_uuid(parent.get("chat_id"))
    return None


def _forward_receipt(receipts, task_id, source):
    receipt = _mapping(receipts.get(task_id))
    target = _canonical_uuid(receipt.get("target_thread_id"))
    if (not target or receipt.get("status") != "accepted" or receipt.get("source_message_id") != task_id
            or receipt.get("source_sha256") != source or not _is_digest(receipt.get("prompt_sha256"))):
        return None
    result = {"target_thread_id": target, "prompt_sha256": receipt["prompt_sha256"]}
    accepted = _stamp(receipt.get("accepted_at"))
    if accepted:
        result["accepted_at"] = accepted
    result["destination_prompt_ingestion_verified"] = receipt.get("destination_prompt_ingestion_verified") is True
    return result


def _dispatch_route(work):
    target = _canonical_uuid(work.get("target_thread_id"))
    result_target = _canonical_uuid(_mapping(work.get("result")).get("target_thread_id"))
    if target and result_target and target != result_target:
        return None
    return target or result_target


def _patch_receipt(resolution, repo):
    proof = _mapping(resolution.get("merge_hold"))
    parsed = _match_url(_PR, proof.get("pr_url"))
    head = proof.get("head_sha")
    if (parsed and parsed["repo"] == repo and _positive_int(proof.get("pr_number"))
            and int(parsed["pr"]) == proof["pr_number"] and isinstance(head, str) and _HEAD.fullmatch(head)
            and proof.get("draft_verified") is True and proof.get("human_signoff_required") is True
            and proof.get("merge_authorized") is False):
        return {"pr_url": proof["pr_url"], "head_sha": head, "verified_at": _stamp(proof.get("verified_at"))}
    return None


def _record(task_id, source, resolution, summary, outcome, chat, *, historical=False, needs_owner=False):
    recorded_at = _stamp(resolution.get("recorded_at"))
    if recorded_at is None or summary is None or resolution.get("source_sha256") != source["source_sha256"]:
        return None
    return {
        "task_id": task_id,
        "record_id": source["source_kind"] + ":" + task_id + ":" + resolution["summary_sha256"] + ":" + recorded_at,
        "source_kind": source["source_kind"],
        "source_url": source["source_url"],
        "source_state": source["source_state"],
        "source_sha256": source["source_sha256"],
        "title": source["title"],
        "decision": resolution["decision"],
        "outcome": outcome,
        "historical": historical,
        "needs_owner": needs_owner,
        "recorded_at": recorded_at,
        "received_at": source["received_at"],
        "chat_id": chat,
        "chat_url": "codex://threads/" + chat if chat else None,
        "summary": summary,
        "summary_sha256": resolution["summary_sha256"],
    }


# Classify only ledger outcomes. Queue submissions and superseded owner asks never make pending tasks.

def _channel_records(state, default_thread, receipts):
    for task_id, raw_work in _mapping(state.get("work")).items():
        work = _mapping(raw_work)
        if not _canonical_uuid(task_id) or work.get("message_id") != task_id:
            continue
        source = _verified_channel_source(state, task_id)
        if source is None:
            continue
        resolution = _mapping(work.get("resolution"))
        decision = resolution.get("decision")
        pending = (decision == "hitl" and work.get("status") == resolution.get("status") == "hitl"
                   and work.get("execution_complete") is False and resolution.get("question_required") is True)
        executed = (decision == "autonomous" and work.get("status") == resolution.get("status") == "handled"
                    and work.get("execution_complete") is True)
        receipt = _forward_receipt(receipts, task_id, source["source_sha256"])
        dispatched = _dispatch_route(work)
        origin = _outbox_route(state, source["envelope"])
        chat = (dispatched or origin or default_thread) if pending else (origin or dispatched or default_thread)
        if pending or executed:
            summary = _summary(resolution)
            forwarded = executed and (receipt is not None or (
                summary is not None and _FORWARD_SUMMARY.search(resolution["summary"])
            ))
            outcome = "owner_input_pending" if pending else "forward_accepted" if forwarded else "autonomous_outcome_recorded"
            row = _record(task_id, source, resolution, summary, outcome, chat, needs_owner=pending)
            if row is not None:
                if receipt is not None and forwarded:
                    row["forward_receipt"] = receipt
                yield row
        history = work.get("resolution_history")
        for older in history if isinstance(history, list) else ():
            older = _mapping(older)
            if older.get("decision") == "autonomous" and older.get("status") == "handled":
                summary = _summary(older)
                forwarded = summary is not None and _FORWARD_SUMMARY.search(older["summary"])
                outcome = "forward_accepted" if forwarded else "autonomous_outcome_recorded"
                row = _record(task_id, source, older, summary, outcome, chat, historical=True)
                if row is not None:
                    yield row


def _bug_records(state, default_thread):
    for task_id, raw_work in _mapping(state.get("work")).items():
        work = _mapping(raw_work)
        if not _canonical_uuid(task_id) or work.get("work_id") != task_id or work.get("handling_complete") is not True:
            continue
        source = _verified_bug_source(state, task_id, work)
        if source is None:
            continue
        resolution = _mapping(work.get("resolution"))
        decision = resolution.get("decision")
        chat = _dispatch_route(work) or default_thread
        assessment = _mapping(resolution.get("assessment"))
        question_ref = assessment.get("owner_question_reference")
        patch = None
        if (decision == "hitl" and work.get("status") == "awaiting_human"
                and not source["closed_at_last_capture"]
                and assessment.get("question_asked_in_owner_chat") is True
                and chat and isinstance(question_ref, str) and question_ref.startswith(f"owner-chat:{chat}:")
                and question_ref[len(f"owner-chat:{chat}:"):].strip()):
            outcome = "owner_input_pending"
        elif decision == "autonomous_patch" and work.get("status") == "patch_ready":
            patch = _patch_receipt(resolution, source["repo"])
            if patch is None:
                continue
            outcome = "patch_prepared_signoff_required"
        else:
            continue
        summary = _summary(resolution, utf8_bom=True)
        row = _record(task_id, source, resolution, summary, outcome, chat,
                      needs_owner=decision == "hitl" or not source["closed_at_last_capture"])
        if row is not None:
            if patch:
                row["patch_receipt"] = patch
            yield row


def task_records(channel_state, bug_state=None, default_thread=None, view="all", *, forward_receipts=None):
    """Return stable task rows from local captures and resolutions, with no network or filesystem work."""
    if view not in _VIEWS:
        raise ValueError("Expected task view pending, autonomous or all")
    default_thread = _canonical_uuid(default_thread)
    records = [
        *_channel_records(_mapping(channel_state), default_thread, _mapping(forward_receipts)),
        *_bug_records(_mapping(bug_state), default_thread),
    ]
    if view == "pending":
        records = [row for row in records if row["needs_owner"] and not row["historical"]]
    elif view == "autonomous":
        records = [row for row in records if row["decision"] in {"autonomous", "autonomous_patch"}]
    records.sort(key=lambda row: (row["recorded_at"], row["task_id"], row["record_id"]), reverse=True)
    return records


def _markdown(value):
    # Escape display text so peer titles and local summaries cannot create links, images or HTML.
    value = html.escape(value, quote=False)
    return re.sub(r"([\\\x60*_{}\[\]()#+.!|~])", r"\\\1", value)


def render_markdown(records):
    """Render local task records as a compact, inert Markdown digest."""
    labels = {
        "owner_input_pending": "Owner input pending",
        "forward_accepted": "Forward accepted",
        "autonomous_outcome_recorded": "Autonomous outcome recorded",
        "patch_prepared_signoff_required": "Draft patch prepared with human sign-off required",
    }
    lines = ["# AgentChannel tasks", ""]
    if not records:
        return "\n".join(lines + ["No matching tasks.", ""])
    for row in records:
        label = labels[row["outcome"]]
        if row["historical"]:
            label += " (historical)"
        lines += [
            f"- **{label}** · {_markdown(row['title'])} · {row['recorded_at']}",
            f"  {_markdown(_display(row['summary'], 520))}",
        ]
        links = [f"[Source]({row['source_url']})"]
        if row["chat_url"]:
            links.append(f"[Chat]({row['chat_url']})")
        if row.get("forward_receipt"):
            target = row["forward_receipt"]["target_thread_id"]
            links.append(f"[Forward destination](codex://threads/{target})")
        if row.get("patch_receipt"):
            links.append(f"[Draft PR]({row['patch_receipt']['pr_url']})")
        lines += ["  " + " · ".join(links) + f" · `{row['task_id']}`"]
        if row["source_state"] == "changed_since_capture":
            lines += ["  The local source was flagged as changed since capture."]
        if row["outcome"] == "forward_accepted":
            ingestion = _mapping(row.get("forward_receipt")).get("destination_prompt_ingestion_verified")
            lines += ["  Destination prompt ingestion " + ("verified." if ingestion else "unverified.")]
        lines.append("")
    return "\n".join(lines)
