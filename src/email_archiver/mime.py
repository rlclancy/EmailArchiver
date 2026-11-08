"""Phase 3: parse a raw .eml into what the PDF renderer needs.

Everything here is untrusted input: headers may be malformed, charsets
unknown, and structure unusual. Parsing never raises for bad content; it
records a visible note instead.
"""

from __future__ import annotations

import email
import email.policy
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage, Message
from pathlib import Path

from .headers import _decode_text, parse_date_header


@dataclass
class Attachment:
    filename: str
    content_type: str
    size: int
    content_id: str | None = None  # set for parts an HTML body may show inline


@dataclass
class ParsedMessage:
    source: str
    date: datetime | None  # in the header's own timezone
    date_raw: str
    date_fallback: bool
    sender: str
    to: str
    cc: str
    subject: str
    body_kind: str  # "html" | "text" | "none"
    body: str
    attachments: list[Attachment] = field(default_factory=list)
    inline_parts: dict[str, tuple[str, bytes]] = field(default_factory=dict)  # cid -> (type, bytes)
    notes: list[str] = field(default_factory=list)
    folder: str | None = None
    uid: int | None = None

    @property
    def year(self) -> int | None:
        return self.date.year if self.date else None

    @property
    def utc(self) -> datetime | None:
        return self.date.astimezone(timezone.utc) if self.date else None


def _header(msg: Message, name: str) -> str:
    """Decoded header text; falls back to the raw value if decoding fails."""
    try:
        values = msg.get_all(name) or []
        return ", ".join(" ".join(str(v).split()) for v in values)
    except Exception:
        raw = msg.get(name, failobj="") if hasattr(msg, "get") else ""
        return " ".join(_decode_text(str(raw)).split())


def _normalize_cid(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if v.lower().startswith("cid:"):
        v = v[4:]
    return v.strip("<> ").lower() or None


def _decode_part(part: Message, notes: list[str]) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, ValueError, AssertionError) as exc:
        payload = part.get_payload(decode=True) or b""
        notes.append(f"Body text had an unreadable character encoding "
                     f"({exc.__class__.__name__}); shown with replacement characters.")
        return payload.decode("utf-8", "replace")


def _payload_bytes(part: Message) -> bytes:
    try:
        data = part.get_payload(decode=True)
    except Exception:
        data = None
    return data or b""


def _leaves(part: Message):
    """Yield non-multipart parts without descending into attached messages."""
    if part.is_multipart() and part.get_content_maintype() != "message":
        for sub in part.iter_parts() if isinstance(part, EmailMessage) else part.get_payload():
            yield from _leaves(sub)
    else:
        yield part


def parse_eml(raw: bytes, source: str) -> ParsedMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    notes: list[str] = []

    date_raw = " ".join(str(msg.get("Date", "") or "").split())
    date = parse_date_header(date_raw)

    body_part = None
    try:
        body_part = msg.get_body(preferencelist=("html", "plain"))
    except Exception:
        body_part = None
    if body_part is None and not msg.is_multipart() and msg.get_content_maintype() == "text":
        body_part = msg

    if body_part is None:
        body_kind, body = "none", ""
        notes.append("No readable text or HTML body was found in this message.")
    else:
        body_kind = "html" if body_part.get_content_subtype() == "html" else "text"
        body = _decode_part(body_part, notes)

    attachments: list[Attachment] = []
    inline: dict[str, tuple[str, bytes]] = {}
    for part in _leaves(msg):
        if part is body_part:
            continue
        ctype = part.get_content_type()
        disposition = part.get_content_disposition()
        try:
            filename = part.get_filename()
        except Exception:
            filename = None
        cid = _normalize_cid(part.get("Content-ID"))
        if ctype in ("text/plain", "text/html") and not filename and disposition != "attachment":
            continue  # the other alternative of the chosen body
        data = _payload_bytes(part) if not part.is_multipart() else b""
        if cid and part.get_content_maintype() == "image":
            inline[cid] = (ctype, data)
        if ctype == "message/rfc822" and not filename:
            inner = part.get_payload()
            inner_msg = inner[0] if isinstance(inner, list) and inner else None
            subj = _header(inner_msg, "Subject") if inner_msg is not None else ""
            filename = f"Attached message: {subj or '(no subject)'}"
            data = inner_msg.as_bytes() if inner_msg is not None else b""
        attachments.append(Attachment(
            filename=" ".join((filename or "(unnamed)").split()),
            content_type=ctype, size=len(data), content_id=cid,
        ))

    return ParsedMessage(
        source=source, date=date, date_raw=date_raw, date_fallback=date is None,
        sender=_header(msg, "From"), to=_header(msg, "To"), cc=_header(msg, "Cc"),
        subject=_header(msg, "Subject") or "(no subject)",
        body_kind=body_kind, body=body, attachments=attachments,
        inline_parts=inline, notes=notes,
    )


def load_messages(paths: list[Path]) -> tuple[list[ParsedMessage], list[tuple[str, str]]]:
    """Parse .eml files (or folders of them). Uses sample-run.json next to the
    files, when present, for folder/UID and the server-date fallback.

    Returns (messages, [(file, error)]) — unreadable files are reported, not skipped silently.
    """
    files: list[Path] = []
    for p in paths:
        p = p.expanduser()
        if p.is_dir():
            files += sorted(x for x in p.iterdir() if x.suffix.lower() == ".eml" and x.is_file())
        else:
            files.append(p)
    meta_cache: dict[Path, dict] = {}

    def run_meta(folder: Path) -> dict:
        if folder not in meta_cache:
            info = {}
            try:
                doc = json.loads((folder / "sample-run.json").read_text(encoding="utf-8"))
                info = {m["file"]: m for m in doc.get("messages", []) if m.get("file")}
            except (OSError, ValueError, KeyError, TypeError):
                pass
            meta_cache[folder] = info
        return meta_cache[folder]

    messages, errors = [], []
    seen: set[Path] = set()
    for f in files:
        rp = f.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        try:
            raw = f.read_bytes()
            pm = parse_eml(raw, f.name)
        except Exception as exc:  # report, never silently drop
            errors.append((str(f), f"{exc.__class__.__name__}: {exc}"))
            continue
        info = run_meta(rp.parent).get(f.name, {})
        pm.folder = info.get("folder")
        pm.uid = info.get("uid")
        if pm.date is None and info.get("header_date"):
            try:
                pm.date = datetime.fromisoformat(info["header_date"])  # server received date
            except ValueError:
                pass
        messages.append(pm)
    return messages, errors
