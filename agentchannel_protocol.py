"""The AgentChannel wire format, independent of transport and local routing."""

from bisect import bisect_left
import datetime as dt
import hashlib
import json
import re
import unicodedata
import uuid


PROTOCOL_V1 = "terminus-agent-channel/1"
PROTOCOL_V2 = "terminus-agent-channel/2"
MARKER_V1 = "<!-- terminus-agent-channel:v1 -->"
MARKER_V2 = "<!-- terminus-agent-channel:v2 -->"
PROTOCOL = PROTOCOL_V2
MARKER = MARKER_V2

MAX_HOPS = 4
MAX_BODY_BYTES = 12000
MAX_SUBJECT_BYTES = 200
MAX_COMMENT_BYTES = 80000
FIELDS_V1 = frozenset({
    "protocol", "message_id", "conversation_id", "from", "to", "kind",
    "reply_to", "created_at", "expires_at", "response_required", "hop",
    "max_hops", "body",
})
FIELDS = FIELDS_V1 | {"subject", "metadata"}
METADATA_FIELDS = frozenset({"work_status", "send_origin", "chat_id", "reply_chat_id"})
KINDS_V1 = frozenset({"hello", "request", "response", "ack", "blocked"})
KINDS = KINDS_V1 | {"urgent", "info", "update"}
WORK_STATUSES = frozenset({"not_applicable", "autonomous", "human_signoff_required"})
SEND_ORIGINS = frozenset({"autonomous", "human_requested"})

_MARKERS = {PROTOCOL_V1: MARKER_V1, PROTOCOL_V2: MARKER_V2}
_COMMENT_PATTERNS = {
    version: re.compile(re.escape(marker) + r"\s*```json\s*\n(.*?)\n```\s*", re.S)
    for version, marker in _MARKERS.items()
}
_AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_LEGACY_ROUTE = "Codex chat ID: "
_ROOT_KINDS_V2 = frozenset({"hello", "request", "urgent", "info", "update"})
_REPLY_KINDS_V2 = frozenset({"response", "ack", "blocked"})

_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_BACKTICKS = re.compile(r"`+")
_MARKDOWN_TOKEN = re.compile(r"""\\[!"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~]|`+|<(?=[A-Za-z/!?])""")
_AUTOLINK = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*|"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9]"
    r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)>"
)


# Shared timestamps, fingerprints and route hints.

def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def utcstamp(value=None):
    return (value or utcnow()).isoformat().replace("+00:00", "Z")


def parsedate(value):
    if not isinstance(value, str):
        raise ValueError("timestamp is not text")
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() != dt.timedelta(0):
        raise ValueError("timestamp must explicitly use UTC")
    return result


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_uuid(value, label):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError(f"{label} must be a canonical UUID")
    return value


def chat_id(envelope):
    """Return the sender's route hint. The caller must verify ownership locally."""
    if envelope.get("protocol") == PROTOCOL_V2:
        return envelope["metadata"]["chat_id"]
    if envelope.get("protocol") != PROTOCOL_V1:
        return None
    lines = envelope["body"].rstrip("\r\n").split("\n")
    last = lines[-1]
    if not last.startswith(_LEGACY_ROUTE) or sum(line.startswith(_LEGACY_ROUTE) for line in lines) != 1:
        return None
    try:
        return _canonical_uuid(last[len(_LEGACY_ROUTE):], "legacy chat ID")
    except ValueError:
        return None


# Markdown validation. The wire JSON and the body are separate layers.

def _check_markdown_line(line, allow_inline_code=True):
    # Code-span delimiters match a raw run of exactly the opening length. Search
    # those runs by position so a long line of unmatched delimiters stays cheap.
    runs = {}
    for match in _BACKTICKS.finditer(line):
        runs.setdefault(len(match[0]), []).append(match.start())
    cursor = 0
    # Tables can split a line before inline parsing. Link titles can carry
    # literal backticks across lines, so retain their ambiguity until a break.
    possible_link = "](" in line or "]:" in line
    inline_code = allow_inline_code and not possible_link and "|" not in line
    dangling = False
    while match := _MARKDOWN_TOKEN.search(line, cursor):
        token = match[0]
        cursor = match.end()
        if token[0] == "`":
            if inline_code:
                choices = runs.get(len(token), ())
                following = bisect_left(choices, cursor)
                if following < len(choices):
                    cursor = choices[following] + len(token)
                    continue
            dangling = True
        elif token == "<":
            autolink = _AUTOLINK.match(line, match.start())
            if autolink is None:
                raise ValueError("raw HTML is not allowed in Markdown")
            cursor = autolink.end()
    return dangling or possible_link


def validate_markdown(body):
    """Require Markdown text without raw HTML outside code.

    Accept unindented top-level fences, simple same-line code spans, escaped text and
    CommonMark-style autolinks. Use an unindented fence when showing HTML in
    table cells, link syntax, nested blocks or multiline code spans.
    This syntax check is not a sanitizer for downstream HTML renderers.
    """
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError(f"Markdown body must be nonempty and at most {MAX_BODY_BYTES} UTF-8 bytes")
    if "\x00" in body:
        raise ValueError("Markdown body contains a NUL")
    opening = None
    ambiguous_inline = False
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        fence = _FENCE.fullmatch(line)
        if opening is not None:
            if fence and fence[1][0] == opening[0] and len(fence[1]) >= len(opening) and not fence[2].strip(" \t"):
                opening = None
        # A dedent may end a nested list fence before its closing delimiter.
        # Only an unindented opening fence can exempt later lines from checks.
        elif fence and fence.start(1) == 0 and (fence[1][0] == "~" or "`" not in fence[2]):
            _check_markdown_line(fence[2])
            opening = fence[1]
            ambiguous_inline = False
        elif not line.strip(" \t"):
            ambiguous_inline = False
        else:
            # A delimiter may close an earlier span or belong to a link title.
            # Resume inline exemptions at a clear paragraph boundary.
            uncertain = _check_markdown_line(line, allow_inline_code=not ambiguous_inline)
            ambiguous_inline = ambiguous_inline or uncertain


# Envelope validation and framing.

def _validate_envelope(envelope, version, allowed_agents=None):
    fields = FIELDS_V1 if version == PROTOCOL_V1 else FIELDS
    kinds = KINDS_V1 if version == PROTOCOL_V1 else KINDS
    if not isinstance(envelope, dict) or set(envelope) != fields:
        raise ValueError("unexpected envelope fields")
    kind = envelope["kind"]
    if envelope["protocol"] != version or not isinstance(kind, str) or kind not in kinds:
        raise ValueError("unknown protocol or message kind")
    for key in ("message_id", "conversation_id"):
        _canonical_uuid(envelope[key], key)
    parent_id = envelope["reply_to"]
    if parent_id is not None:
        _canonical_uuid(parent_id, "reply_to")
    if type(envelope["response_required"]) is not bool:
        raise ValueError("response_required is not a boolean")
    hop, max_hops = envelope["hop"], envelope["max_hops"]
    if type(hop) is not int or type(max_hops) is not int:
        raise ValueError("hop fields are not integers")
    if not (1 <= max_hops <= MAX_HOPS and 0 <= hop <= max_hops):
        raise ValueError("hop budget exceeded")
    body = envelope["body"]
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError(f"body exceeds {MAX_BODY_BYTES} UTF-8 bytes or is not text")
    for key in ("from", "to"):
        name = envelope[key]
        if not isinstance(name, str) or not name or (version == PROTOCOL_V2 and not _AGENT_NAME.fullmatch(name)):
            raise ValueError("invalid agent name")
        if allowed_agents is not None and name not in allowed_agents:
            raise ValueError("unknown agent")
    if envelope["from"] == envelope["to"]:
        raise ValueError("sender and recipient are identical")
    created, expires = parsedate(envelope["created_at"]), parsedate(envelope["expires_at"])
    if expires <= created:
        raise ValueError("expiry does not follow creation")
    if parent_id is None:
        roots = {"hello", "request"} if version == PROTOCOL_V1 else _ROOT_KINDS_V2
        if hop != 0 or kind not in roots:
            raise ValueError("invalid root kind or hop")
    elif hop == 0:
        raise ValueError("reply must advance hop")
    if kind in {"ack", "blocked"} and envelope["response_required"]:
        raise ValueError("ack/blocked cannot require another response")
    if version == PROTOCOL_V2:
        _validate_v2_details(envelope)
    return envelope


def _validate_v2_details(envelope):
    kind, parent_id = envelope["kind"], envelope["reply_to"]
    if parent_id is not None and (parent_id == envelope["message_id"] or kind not in _REPLY_KINDS_V2):
        raise ValueError("invalid reply kind or parent")
    wants_response = envelope["response_required"]
    if kind == "request" and not wants_response:
        raise ValueError("request requires a response")
    if kind not in {"hello", "request", "urgent"} and wants_response:
        raise ValueError("only hello, request and urgent may require a response")
    subject = envelope["subject"]
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or len(subject.encode("utf-8")) > MAX_SUBJECT_BYTES
        or any(unicodedata.category(c) in {"Cc", "Zl", "Zp"} for c in subject)
    ):
        raise ValueError(f"subject must be one nonempty line of at most {MAX_SUBJECT_BYTES} UTF-8 bytes")
    _check_markdown_line(subject)
    metadata = envelope["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != METADATA_FIELDS:
        raise ValueError("unexpected metadata fields")
    for field, values in (("work_status", WORK_STATUSES), ("send_origin", SEND_ORIGINS)):
        if not isinstance(metadata[field], str) or metadata[field] not in values:
            raise ValueError(f"unknown {field}")
    for field in ("chat_id", "reply_chat_id"):
        if metadata[field] is not None:
            _canonical_uuid(metadata[field], field)
    if parent_id is None and metadata["reply_chat_id"] is not None:
        raise ValueError("root message has a reply chat ID")
    validate_markdown(envelope["body"])


def is_protocol_comment(body):
    return isinstance(body, str) and body.startswith((MARKER_V1, MARKER_V2))


def wire(envelope):
    """Serialize a validated envelope. The JSON fence is transport framing."""
    version = envelope.get("protocol") if isinstance(envelope, dict) else None
    if not isinstance(version, str) or version not in _MARKERS:
        raise ValueError("unknown protocol")
    _validate_envelope(envelope, version)
    framed = _MARKERS[version] + "\n```json\n" + json.dumps(
        envelope, indent=2, ensure_ascii=False,
    ) + "\n```\n"
    if len(framed.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise ValueError("comment exceeds the wire limit")
    return framed


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_envelope(body, allowed_agents=None):
    """Parse a strictly framed v1 or v2 comment without granting trust."""
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise ValueError("comment is not a bounded text envelope")
    for version, pattern in _COMMENT_PATTERNS.items():
        if body.startswith(_MARKERS[version]):
            match = pattern.fullmatch(body)
            if match is None:
                break
            try:
                envelope = json.loads(match[1], object_pairs_hook=_unique_pairs)
            except RecursionError as exc:
                raise ValueError("envelope JSON nesting is too deep") from exc
            return _validate_envelope(envelope, version, allowed_agents)
    raise ValueError("not a recognized protocol wire comment")


# Authenticated ancestry is supplied by the transport.

def validate_link(envelope, parents, now=None):
    """Validate linkage against authenticated parents and return freshness."""
    now = now or utcnow()
    created = parsedate(envelope["created_at"])
    if created > now + dt.timedelta(minutes=10):
        raise ValueError("message creation is too far in the future")
    parent_id = envelope["reply_to"]
    if parent_id is not None:
        parent = parents.get(parent_id)
        if parent is None or parent["message_id"] != parent_id:
            raise ValueError("unknown or ambiguous parent")
        if envelope["conversation_id"] != parent["conversation_id"]:
            raise ValueError("reply belongs to a different conversation")
        if envelope["from"] != parent["to"] or envelope["to"] != parent["from"]:
            raise ValueError("reply direction does not reverse parent")
        if envelope["hop"] != parent["hop"] + 1:
            raise ValueError("reply does not increment hop")
        if envelope["max_hops"] > parent["max_hops"]:
            raise ValueError("reply increases hop budget")
        if created < parsedate(parent["created_at"]) - dt.timedelta(minutes=10):
            raise ValueError("reply precedes parent")
        if created >= parsedate(parent["expires_at"]):
            raise ValueError("reply was created after its parent expired")
        if parent["kind"] == "ack" or not parent["response_required"]:
            raise ValueError("parent does not permit a reply")
        if envelope["protocol"] == PROTOCOL_V2:
            # In mixed conversations a v1 terminal footer is only the hint to echo.
            # Routing authority still comes from the sender's authenticated outbox.
            if envelope["metadata"]["reply_chat_id"] != chat_id(parent):
                raise ValueError("reply chat ID does not match the authenticated parent")
            if parent["kind"] == "hello" and envelope["kind"] != "ack":
                raise ValueError("hello may only receive an acknowledgement")
    return parsedate(envelope["expires_at"]) > now
