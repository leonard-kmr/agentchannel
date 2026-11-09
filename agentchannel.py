"""A small GitHub Issues transport for the AgentChannel protocol."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

import agentchannel_protocol as protocol


class ChannelError(RuntimeError):
    """A local safety or transport check could not be completed."""


class UncertainSend(ChannelError):
    """A post may have reached GitHub. Only reconcile its original UUID."""


# Per-channel configuration and private local ledger.

@dataclass(frozen=True)
class Config:
    repo: str
    repository_id: int
    issue: int
    self_agent: str
    self_user_id: int
    peer_agent: str
    peer_user_id: int

    def __post_init__(self):
        if not isinstance(self.repo, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", self.repo
        ) or self.repo.split("/")[1] in {".", ".."}:
            raise ChannelError("repo must be a GitHub owner/name")
        for name in ("repository_id", "issue", "self_user_id", "peer_user_id"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ChannelError(f"{name} must be a positive integer")
        if self.self_user_id == self.peer_user_id or self.self_agent == self.peer_agent:
            raise ChannelError("the two participants need distinct account IDs and agent labels")
        for label in (self.self_agent, self.peer_agent):
            if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", label):
                raise ChannelError("agent labels must be 1–64 simple ASCII characters")

    @property
    def issue_endpoint(self):
        return f"repos/{self.repo}/issues/{self.issue}"

    def author_for(self, envelope):
        pair = (envelope["from"], envelope["to"])
        if pair == (self.self_agent, self.peer_agent):
            return self.self_user_id
        if pair == (self.peer_agent, self.self_agent):
            return self.peer_user_id
        return None


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, value):
    """Replace a private local file only after its new bytes reach disk."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        Path(tmp).unlink(missing_ok=True)


@contextmanager
def local_lock(home):
    """Lock a stable file that the OS unlocks when this process exits."""
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(home / ".client.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ChannelError("Channel state is locked. Try again later.") from exc
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ChannelError("Channel state is locked. Try again later.") from exc
        yield
    finally:
        os.close(fd)


# The GitHub boundary. All operations use the caller's existing gh login.

class GitHub:
    PAGE_SIZE = 100
    MAX_PAGES = 50

    def __init__(self, executable="gh"):
        self.executable = executable

    def _api(self, method, endpoint, payload=None):
        exe = shutil.which(self.executable)
        if not exe:
            raise ChannelError("Install the GitHub CLI and sign in with gh.")
        args = [exe, "api", "--hostname", "github.com", "--method", method, endpoint]
        if payload is not None:
            args.extend(("--input", "-"))
        env = dict(os.environ, GH_PROMPT_DISABLED="1")
        result = subprocess.run(
            args, input=None if payload is None else json.dumps(payload, ensure_ascii=False),
            encoding="utf-8", capture_output=True, timeout=30, check=False, env=env,
        )
        if result.returncode:
            # Provider diagnostics can include credentials or message content.
            raise ChannelError(f"GitHub {method} failed. Check gh auth and repository access.")
        return json.loads(result.stdout)

    def get(self, endpoint):
        return self._api("GET", endpoint)

    def _issue(self, config):
        issue = self.get(config.issue_endpoint)
        expected = "https://api.github.com/" + config.issue_endpoint
        if (
            not isinstance(issue, dict) or issue.get("number") != config.issue
            or not isinstance(issue.get("url"), str)
            or issue["url"].casefold() != expected.casefold()
            or "pull_request" in issue
            or type(issue.get("comments")) is not int or issue["comments"] < 0
        ):
            raise ChannelError("GitHub issue did not match the configured channel")
        return issue

    def verify(self, config):
        user = self.get("user")
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] != config.self_user_id:
            raise ChannelError("authenticated GitHub account ID does not match self_user_id")
        repo = self.get(f"repos/{config.repo}")
        if (
            not isinstance(repo, dict) or type(repo.get("id")) is not int or repo["id"] != config.repository_id
            or not isinstance(repo.get("full_name"), str)
            or repo["full_name"].casefold() != config.repo.casefold()
        ):
            raise ChannelError("GitHub repository identity does not match the configured channel")
        if repo.get("private") is not True or repo.get("visibility", "private") != "private":
            raise ChannelError("AgentChannel requires a private correspondence repository")
        return self._issue(config)

    def comments(self, config):
        before = self.verify(config)["comments"]
        if before > self.PAGE_SIZE * self.MAX_PAGES:
            raise ChannelError("The issue exceeds the comment scan limit. Start a new channel.")
        comments = []
        ids = set()
        pages = max(1, (before + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        for page in range(1, pages + 1):
            items = self.get(f"{config.issue_endpoint}/comments?per_page={self.PAGE_SIZE}&page={page}")
            expected = min(self.PAGE_SIZE, before - (page - 1) * self.PAGE_SIZE)
            if not isinstance(items, list) or len(items) != expected:
                raise ChannelError("The GitHub comment scan was incomplete. Retry the read.")
            for item in items:
                if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] in ids:
                    raise ChannelError("GitHub issue scan returned a duplicate or invalid comment")
                ids.add(item["id"])
                comments.append(item)
        if self._issue(config)["comments"] != before:
            raise ChannelError("Issue comments changed during the scan. Retry the read.")
        return comments

    def post(self, config, wire_body):
        return self._api("POST", config.issue_endpoint + "/comments", {"body": wire_body})

    def comment(self, config, comment_id):
        return self.get(f"repos/{config.repo}/issues/comments/{comment_id}")


def comment_matches_channel(config, comment):
    issue_url = comment.get("issue_url")
    return (
        type(comment.get("id")) is int and comment["id"] > 0
        and isinstance(comment.get("user"), dict)
        and type(comment["user"].get("id")) is int
        and isinstance(comment.get("created_at"), str)
        and isinstance(comment.get("updated_at"), str)
        and isinstance(issue_url, str)
        and issue_url.casefold() == ("https://api.github.com/" + config.issue_endpoint).casefold()
    )


# Source-bound capture, including the reply graph and local return-route proof.

def reply_key(envelope):
    parent = envelope["reply_to"]
    if parent is None:
        return None
    return parent, "ack" if envelope["kind"] == "ack" else "final"


class Client:
    def __init__(self, home, github=None):
        self.home = Path(home)
        self.config_path = self.home / "config.json"
        self.state_path = self.home / "state.json"
        self.github = github or GitHub()

    def initialize(self, config):
        with local_lock(self.home):
            if self.config_path.exists() or self.state_path.exists():
                raise ChannelError("this state directory already belongs to a channel")
            self.github.verify(config)
            save_json(self.config_path, asdict(config))
        return {"status": "configured"}

    def _load(self):
        if not self.config_path.is_file():
            raise ChannelError("Channel config is missing. Run init first.")
        config = Config(**load_json(self.config_path))
        state = {
            "version": 1, "channel": asdict(config), "comments": {},
            "inbox": {}, "outbox": {}, "review": {},
        }
        if self.state_path.exists():
            state = load_json(self.state_path)
            if state.get("version") != 1 or state.get("channel") != asdict(config):
                raise ChannelError("local state does not match the current channel config")
            if not all(isinstance(state.get(key), dict) for key in ("comments", "inbox", "outbox", "review")):
                raise ChannelError("local state has an invalid structure")
        return config, state

    def _review(self, state, comment_id, reason):
        state["review"].setdefault(str(comment_id), reason)
        snapshot = state["comments"].get(str(comment_id), {})
        message_id = snapshot.get("message_id")
        for ledger in ("inbox", "outbox"):
            row = state[ledger].get(message_id)
            if row is not None and row.get("comment_id") == comment_id:
                row["status"] = "review_required"
                row.pop("verified_return_chat_id", None)

    def _index(self, config, state, comments):
        by_message = {}
        seen = set()
        for comment in comments:
            comment_id = comment["id"]
            seen.add(str(comment_id))
            raw = comment.get("body")
            author = (comment.get("user") or {}).get("id") if isinstance(comment.get("user"), dict) else None
            snapshot = state["comments"].get(str(comment_id))
            if snapshot and (
                not isinstance(raw, str) or protocol.sha(raw) != snapshot["body_sha256"]
                or author != snapshot["author_id"] or not comment_matches_channel(config, comment)
                or comment.get("updated_at") != snapshot["updated_at"]
            ):
                self._review(state, comment_id, "captured comment changed")
                continue
            if not isinstance(raw, str) or not comment_matches_channel(config, comment):
                continue
            try:
                envelope = protocol.parse_envelope(raw)
            except (TypeError, ValueError):
                continue
            if type(author) is not int or author != config.author_for(envelope):
                continue
            message_id = envelope["message_id"]
            state["comments"].setdefault(str(comment_id), {
                "message_id": message_id, "body_sha256": protocol.sha(raw),
                "author_id": author, "updated_at": comment.get("updated_at"),
                "reply_key": reply_key(envelope),
            })
            if comment.get("created_at") != comment.get("updated_at"):
                self._review(state, comment_id, "comment was edited before capture")
            by_message.setdefault(message_id, []).append((envelope, comment))
        known = {}
        for cid, snapshot in state["comments"].items():
            known.setdefault(snapshot["message_id"], []).append(int(cid))
            if cid not in seen:
                self._review(state, int(cid), "captured comment is no longer present")
        candidates = {}
        for message_id, rows in by_message.items():
            if len(known[message_id]) != 1:
                for cid in known[message_id]:
                    self._review(state, cid, "message UUID occurs on multiple comments")
            elif str(rows[0][1]["id"]) not in state["review"]:
                candidates[message_id] = rows[0]
        return self._valid_ancestry(state, candidates)

    def _valid_ancestry(self, state, candidates):
        ordered = sorted(candidates.items(), key=lambda item: item[1][0]["hop"])
        valid = {}
        parents = {}
        groups = {}
        for message_id, (envelope, comment) in ordered:
            try:
                protocol.validate_link(envelope, parents)
            except (TypeError, ValueError):
                self._review(state, comment["id"], "reply ancestry or timing did not validate")
                continue
            valid[message_id] = (envelope, comment)
            parents[message_id] = envelope
            key = reply_key(envelope)
            if key is not None:
                groups.setdefault(key, []).append(message_id)
        for group in groups.values():
            if len(group) > 1:
                for message_id in group:
                    self._review(state, valid.pop(message_id)[1]["id"], "conflicting replies")
        return {
            mid: (env, comment) for mid, (env, comment) in valid.items()
            if self._parent_chain_present(env, valid)
        }

    @staticmethod
    def _parent_chain_present(envelope, records):
        while envelope["reply_to"] is not None:
            parent = records.get(envelope["reply_to"])
            if parent is None:
                return False
            envelope = parent[0]
        return True

    def _capture(self, config, state, records):
        new = []
        for mid, (env, comment) in records.items():
            if env["from"] != config.peer_agent:
                continue
            fresh = protocol.parsedate(env["expires_at"]) > protocol.utcnow()
            if mid not in state["inbox"]:
                state["inbox"][mid] = {
                    "envelope": env, "comment_id": comment["id"],
                    "body_sha256": protocol.sha(comment["body"]),
                    "status": "received" if fresh else "expired",
                }
                if fresh:
                    new.append(mid)
            row = state["inbox"][mid]
            if row["status"] == "review_required":
                continue
            row["status"] = "received" if fresh else "expired"
            parent_id = env["reply_to"]
            parent = state["outbox"].get(parent_id, {})
            live = records.get(parent_id)
            if fresh and parent.get("status") == "posted" and live and (
                parent.get("envelope") == live[0]
                and parent.get("wire_body") == live[1]["body"]
                and parent.get("comment_id") == live[1]["id"]
            ):
                local_chat = protocol.chat_id(parent["envelope"])
                if local_chat is not None:
                    row["verified_return_chat_id"] = local_chat
            else:
                row.pop("verified_return_chat_id", None)
        for mid, row in state["inbox"].items():
            if mid not in records:
                self._review(state, row["comment_id"], "source or reply ancestry no longer verified")
        return new

    def poll(self):
        with local_lock(self.home):
            config, state = self._load()
            comments = self.github.comments(config)
            records = self._index(config, state, comments)
            new = self._capture(config, state, records)
            save_json(self.state_path, state)
        return {"status": "polled", "new_message_ids": new, "review_required": len(state["review"])}

    def inbox(self, message_id=None):
        with local_lock(self.home):
            _, state = self._load()
        if message_id is None:
            return state["inbox"]
        if message_id not in state["inbox"]:
            raise ChannelError("message is not captured in the local inbox")
        return state["inbox"][message_id]

    # Explicit outgoing operations. A saved posting state always precedes POST.

    def send(self, *, subject, body, send_origin, work_status="not_applicable", chat_id=None,
             kind=None, response_required=None, parent_id=None, wire_version=2):
        with local_lock(self.home):
            config, state = self._load()
            comments = self.github.comments(config)
            records = self._index(config, state, comments)
            self._capture(config, state, records)
            save_json(self.state_path, state)
            parent, parent_comment = (None, None) if parent_id is None else records.get(parent_id, (None, None))
            kind = kind or ("request" if parent_id is None else "response")
            if parent_id is not None:
                captured = state["inbox"].get(parent_id, {})
                if parent is None or captured.get("status") != "received":
                    raise ChannelError("reply requires a current verified inbound parent")
                if parent["kind"] == "hello" and kind != "ack":
                    raise ChannelError("a hello permits only an acknowledgement")
                self._verify_live_chain(config, records, parent, parent_comment)
            envelope = self._outgoing(
                config, subject, body, send_origin, work_status, chat_id,
                kind, response_required, parent, wire_version,
            )
            raw = protocol.wire(envelope)
            protocol.validate_link(envelope, {mid: row[0] for mid, row in records.items()})
            self._reject_repeat(state, envelope)
            existing_ids = {row[0]["message_id"] for row in records.values()}
            if envelope["message_id"] in existing_ids | set(state["outbox"]) | set(state["inbox"]):
                raise ChannelError("new UUID unexpectedly collides with existing evidence")
            self.github.verify(config)
            mid = envelope["message_id"]
            outgoing = {"envelope": envelope, "wire_body": raw, "status": "posting"}
            state["outbox"][mid] = outgoing
            save_json(self.state_path, state)
            try:
                posted = self.github.post(config, raw)
                if not isinstance(posted, dict) or type(posted.get("id")) is not int:
                    raise ChannelError("GitHub POST did not return a comment identity")
                saved = self.github.comment(config, posted["id"])
                if not self._matches_post(config, outgoing, saved, posted["id"]):
                    raise ChannelError("GitHub comment read-back did not match the prepared send")
            except (ChannelError, OSError, ValueError, subprocess.SubprocessError) as exc:
                outgoing["status"] = "uncertain"
                save_json(self.state_path, state)
                raise UncertainSend(f"Send {mid} is uncertain. Run reconcile to check it.") from exc
            self._record_post(state, outgoing, saved)
            save_json(self.state_path, state)
        return {"status": "posted", "message_id": mid, "comment_id": saved["id"]}

    def _verify_live_chain(self, config, records, envelope, comment):
        while True:
            live = self.github.comment(config, comment["id"])
            if not isinstance(live, dict) or any(
                live.get(key) != comment.get(key) for key in ("id", "body", "user", "issue_url", "updated_at")
            ):
                raise ChannelError("the reply parent or an ancestor changed before sending")
            parent_id = envelope["reply_to"]
            if parent_id is None:
                return
            envelope, comment = records[parent_id]

    @staticmethod
    def _outgoing(config, subject, body, send_origin, work_status, chat, kind,
                  response_required, parent, version):
        now = protocol.utcnow()
        mid = str(uuid.uuid4())
        needs_response = kind in {"request", "hello"} if response_required is None else response_required
        envelope = {
            "protocol": protocol.PROTOCOL_V2, "message_id": mid,
            "conversation_id": mid if parent is None else parent["conversation_id"],
            "from": config.self_agent, "to": config.peer_agent, "kind": kind,
            "reply_to": None if parent is None else parent["message_id"],
            "created_at": protocol.utcstamp(now), "expires_at": protocol.utcstamp(now + dt.timedelta(days=2)),
            "response_required": needs_response, "hop": 0 if parent is None else parent["hop"] + 1,
            "max_hops": protocol.MAX_HOPS if parent is None else parent["max_hops"],
            "subject": subject, "body": body,
            "metadata": {
                "work_status": work_status, "send_origin": send_origin,
                "chat_id": chat, "reply_chat_id": protocol.chat_id(parent) if parent is not None else None,
            },
        }
        if version == 1:
            if work_status != "not_applicable" or kind in {"info", "update", "urgent"}:
                raise ChannelError("v1 cannot carry this kind or work status")
            if parent is not None and parent["protocol"] != protocol.PROTOCOL_V1:
                raise ChannelError("v1 reply requires a v1 parent")
            # Validate new content against v2 rules before dropping legacy-incompatible metadata.
            protocol.wire(envelope)
            envelope["protocol"] = protocol.PROTOCOL_V1
            del envelope["subject"], envelope["metadata"]
            envelope["body"] = f"## {subject}\n\n{body}"
            if chat is not None:
                envelope["body"] += f"\n\nCodex chat ID: {chat}"
        elif version != 2:
            raise ChannelError("unsupported wire version")
        return envelope

    @staticmethod
    def _reject_repeat(state, envelope):
        key = reply_key(envelope)
        for row in state["outbox"].values():
            prior = row["envelope"]
            if key is not None and reply_key(prior) == key:
                raise ChannelError("a reply of this category is already saved for that parent")
            if row["status"] in {"posting", "uncertain", "review_required"} and (
                envelope["body"], envelope.get("subject"), envelope["kind"], envelope["reply_to"]
            ) == (prior["body"], prior.get("subject"), prior["kind"], prior["reply_to"]):
                raise ChannelError("a matching outgoing message needs reconciliation before another send")
        if key is not None and any(tuple(row.get("reply_key") or ()) == key for row in state["comments"].values()):
            raise ChannelError("the provider already has a reply of this category for that parent")

    @staticmethod
    def _matches_post(config, outgoing, comment, comment_id):
        return (
            isinstance(comment, dict) and comment_matches_channel(config, comment)
            and comment.get("id") == comment_id and comment["user"].get("id") == config.self_user_id
            and comment.get("body") == outgoing["wire_body"]
            and isinstance(comment.get("created_at"), str)
            and comment["created_at"] == comment.get("updated_at")
        )

    @staticmethod
    def _record_post(state, outgoing, comment):
        outgoing.update(status="posted", comment_id=comment["id"])
        state["comments"][str(comment["id"])] = {
            "message_id": outgoing["envelope"]["message_id"],
            "body_sha256": protocol.sha(comment["body"]),
            "author_id": comment["user"]["id"], "updated_at": comment["updated_at"],
            "reply_key": reply_key(outgoing["envelope"]),
        }

    def reconcile(self):
        """Read only. A missing or conflicting provider record never causes a POST."""
        with local_lock(self.home):
            config, state = self._load()
            records = self._index(config, state, self.github.comments(config))
            conflicting = {
                snapshot["message_id"] for cid, snapshot in state["comments"].items()
                if cid in state["review"]
            }
            pending = {}
            for mid, row in state["outbox"].items():
                if row["status"] not in {"posting", "uncertain"}:
                    continue
                match = records.get(mid)
                if mid in conflicting:
                    row["status"] = "review_required"
                elif match:
                    cid = match[1]["id"]
                    live = self.github.comment(config, cid)
                    if self._matches_post(config, row, live, cid):
                        self._record_post(state, row, live)
                    else:
                        row["status"] = "review_required"
                else:
                    row["status"] = "uncertain"
                pending[mid] = row["status"]
            save_json(self.state_path, state)
        return {"status": "reconciled", "messages": pending}


# CLI. Commands never read or post anything on behalf of a background agent.

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".agentchannel",
                        help="private local directory for channel config and state")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="configure one private GitHub issue channel")
    for key in ("repo", "repository-id", "issue", "self-agent", "self-user-id", "peer-agent", "peer-user-id"):
        init.add_argument("--" + key, required=True, type=int if key.endswith("-id") or key == "issue" else str)
    sub.add_parser("poll", help="validate and capture issue comments without model execution")
    inbox = sub.add_parser("inbox", help="print captured local messages")
    inbox.add_argument("--message-id")
    sub.add_parser("reconcile", help="check uncertain outgoing UUIDs without reposting")
    for command, choices in (
        ("send", ("hello", "request", "urgent", "info", "update")),
        ("reply", ("response", "ack", "blocked")),
    ):
        send = sub.add_parser(command)
        if command == "reply":
            send.add_argument("parent_id", metavar="PARENT_UUID")
        send.add_argument("--kind", choices=choices)
        send.add_argument("--subject", required=True)
        send.add_argument("--body-file", required=True, type=Path)
        send.add_argument("--response-required", action=argparse.BooleanOptionalAction)
        send.add_argument("--send-origin", required=True, choices=("autonomous", "human_requested"))
        send.add_argument("--work-status", default="not_applicable",
                          choices=("not_applicable", "autonomous", "human_signoff_required"))
        send.add_argument("--chat-id")
        send.add_argument("--wire-version", type=int, choices=(1, 2), default=2)
    args = vars(parser.parse_args(argv))
    client = Client(args.pop("home"))
    command = args.pop("command")
    try:
        if command == "init":
            result = client.initialize(Config(**args))
        elif command in {"send", "reply"}:
            body_file = args.pop("body_file")
            if body_file.stat().st_size > 12_000:
                raise ChannelError("body file exceeds 12,000 bytes")
            result = client.send(body=body_file.read_text(encoding="utf-8"), **args)
        elif command == "inbox":
            result = client.inbox(**args)
        else:
            result = getattr(client, command)()
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (ChannelError, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        print(f"AgentChannel: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
