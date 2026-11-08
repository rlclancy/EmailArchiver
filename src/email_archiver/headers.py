"""Parsing of FETCH responses, message headers, sender addresses and dates."""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

_NEW_RESPONSE = re.compile(rb"^\d+ \(")
_UID = re.compile(rb"\bUID (\d+)")
_SIZE = re.compile(rb"\bRFC822\.SIZE (\d+)")
_INTERNALDATE = re.compile(rb'\bINTERNALDATE "([^"]+)"')
_INTERNALDATE_PARTS = re.compile(
    r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d\d):(\d\d):(\d\d) ([+-])(\d\d)(\d\d)$"
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MSGID = re.compile(r"<[^<>]+>")

MIN_VALID_YEAR = 1980


def normalize_address(value: str) -> str:
    return value.strip().lower()


def is_valid_address(value: str) -> bool:
    return bool(_ADDRESS.match(value.strip()))


@dataclass
class FetchRecord:
    uid: int | None
    size: int | None
    internaldate: str | None
    header: bytes


def parse_fetch_response(data: list) -> list[FetchRecord]:
    """Parse imaplib ``uid('FETCH', ...)`` data into one record per message.

    imaplib returns each message as a (meta, literal) tuple, sometimes followed
    by a bytes element carrying items the server sent after the literal
    (e.g. ``b' UID 42)'``). A message without a literal arrives as plain bytes.
    """
    chunks: list[list] = []  # [meta_bytes, header_bytes]
    for part in data:
        if part is None:
            continue
        if isinstance(part, tuple):
            chunks.append([part[0], part[1] or b""])
        elif isinstance(part, bytes):
            if _NEW_RESPONSE.match(part) or not chunks:
                chunks.append([part, b""])
            else:
                chunks[-1][0] += part
    records = []
    for meta, header in chunks:
        uid = _UID.search(meta)
        size = _SIZE.search(meta)
        idate = _INTERNALDATE.search(meta)
        records.append(FetchRecord(
            uid=int(uid.group(1)) if uid else None,
            size=int(size.group(1)) if size else None,
            internaldate=idate.group(1).decode("ascii", "replace") if idate else None,
            header=header,
        ))
    return records


def parse_internaldate(value: str | None) -> datetime | None:
    """Parse IMAP INTERNALDATE (e.g. ' 7-Jul-2023 14:02:11 -0700'), locale-independent."""
    if not value:
        return None
    m = _INTERNALDATE_PARTS.match(value)
    if not m:
        return None
    day, mon, year, hh, mm, ss, sign, tzh, tzm = m.groups()
    month = _MONTHS.get(mon.lower())
    if not month:
        return None
    offset = timedelta(hours=int(tzh), minutes=int(tzm)) * (1 if sign == "+" else -1)
    try:
        return datetime(int(year), month, int(day), int(hh), int(mm), int(ss),
                        tzinfo=timezone(offset))
    except ValueError:
        return None


def parse_date_header(value: str | None, now: datetime | None = None) -> datetime | None:
    """Parse a Date header, keeping the timezone written in the header.

    Returns None when missing, unparseable or implausible (before 1980 or more
    than a day in the future), so the caller falls back to INTERNALDATE.
    """
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:  # "-0000": time is UTC, source zone unknown
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if dt.year < MIN_VALID_YEAR or dt > now + timedelta(days=1):
        return None
    return dt


def _decode_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # malformed encoded-words: show raw text
        return value


@dataclass
class MessageMeta:
    folder: str
    uid: int
    uidvalidity: int | None
    size: int
    internaldate: datetime | None
    header_date: datetime | None
    date_raw: str
    from_addresses: list[str]
    subject: str
    message_id: str | None
    also_in: list[str] = field(default_factory=list)

    @property
    def used_fallback(self) -> bool:
        return self.header_date is None

    @property
    def effective_date(self) -> datetime | None:
        """Header date in its own timezone, else INTERNALDATE."""
        return self.header_date or self.internaldate

    @property
    def year(self) -> int | None:
        d = self.effective_date
        return d.year if d else None

    @property
    def utc_date(self) -> datetime | None:
        d = self.effective_date
        return d.astimezone(timezone.utc) if d else None

    @property
    def dedup_key(self) -> tuple:
        if self.message_id:
            return ("message-id", self.message_id)
        return ("provisional", self.date_raw, tuple(self.from_addresses), self.subject, self.size)

    def matches(self, senders: set[str]) -> bool:
        return any(a in senders for a in self.from_addresses)

    def in_range(self, start: date | None, end: date | None) -> bool:
        d = self.effective_date
        if d is None:
            return start is None and end is None
        local = d.date()  # calendar date in the message's own timezone
        return (start is None or local >= start) and (end is None or local <= end)

    def near_year_boundary(self) -> bool:
        d = self.effective_date
        if d is None:
            return False
        own = (d.month, d.day)
        return own in {(12, 31), (1, 1)} or d.year != d.astimezone(timezone.utc).year


def build_meta(record: FetchRecord, folder: str, uidvalidity: int | None) -> MessageMeta:
    msg = email.message_from_bytes(record.header, policy=email.policy.compat32)
    date_raw = (msg.get("Date") or "").strip()
    from_values = [str(v) for v in msg.get_all("From", [])]
    addresses = [normalize_address(addr) for _, addr in getaddresses(from_values) if addr]
    mid_raw = (msg.get("Message-ID") or "").strip()
    mid_match = _MSGID.search(mid_raw)
    message_id = mid_match.group(0) if mid_match else (mid_raw or None)
    return MessageMeta(
        folder=folder,
        uid=record.uid or 0,
        uidvalidity=uidvalidity,
        size=record.size or 0,
        internaldate=parse_internaldate(record.internaldate),
        header_date=parse_date_header(date_raw),
        date_raw=date_raw,
        from_addresses=addresses,
        subject=_decode_text(msg.get("Subject")),
        message_id=message_id,
    )
