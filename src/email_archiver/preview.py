"""Phase 1: read-only search and preview of matching messages."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from .folders import Folder
from .headers import MessageMeta, build_meta, parse_fetch_response
from .safe_imap import imap_quote

FETCH_ITEMS = (
    "(UID RFC822.SIZE INTERNALDATE "
    "BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT MESSAGE-ID)])"
)
FETCH_BATCH = 100


@dataclass
class FolderScan:
    folder: Folder
    uidvalidity: int | None = None
    exists: int = 0
    server_hits: int = 0
    rejected: int = 0  # server hit, but From is not an exact match
    out_of_range: int = 0
    unparsed: int = 0  # UIDs the server returned no usable FETCH data for
    matched: list[MessageMeta] = field(default_factory=list)
    error: str | None = None


@dataclass
class YearStats:
    count: int = 0
    size: int = 0
    fallback: int = 0


@dataclass
class PreviewResult:
    senders: list[str]
    start: date | None
    end: date | None
    scans: list[FolderScan]
    excluded: list[tuple[Folder, str]]
    messages: list[MessageMeta]  # unique, sorted chronologically
    duplicates: int
    max_messages: int
    max_bytes: int

    @property
    def by_year(self) -> dict[int | None, YearStats]:
        stats: dict[int | None, YearStats] = defaultdict(YearStats)
        for m in self.messages:
            s = stats[m.year]
            s.count += 1
            s.size += m.size
            s.fallback += m.used_fallback
        return dict(sorted(stats.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)))

    @property
    def by_folder_year(self) -> dict[str, dict[int | None, int]]:
        table: dict[str, dict[int | None, int]] = defaultdict(lambda: defaultdict(int))
        for m in self.messages:
            table[m.folder][m.year] += 1
        return table

    @property
    def oversized_years(self) -> list[tuple[int | None, YearStats]]:
        return [(y, s) for y, s in self.by_year.items()
                if s.count > self.max_messages or s.size > self.max_bytes]

    @property
    def boundary_messages(self) -> list[MessageMeta]:
        return [m for m in self.messages if m.near_year_boundary()]


def _chunks(items: list[int], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _text(data) -> str:
    parts = [d.decode("utf-8", "replace") if isinstance(d, bytes) else str(d)
             for d in (data or []) if d is not None]
    return " ".join(parts) or "no detail"


def scan_folder(conn, folder: Folder, senders: list[str],
                start: date | None, end: date | None) -> FolderScan:
    """Search one folder read-only and fetch header metadata for server hits.

    The date range is applied locally to the header date: server-side SINCE/
    BEFORE use INTERNALDATE, which may not match the Date header (for example
    on mail migrated from Cox to Yahoo), and could silently drop messages.
    """
    scan = FolderScan(folder)
    typ, data = conn.select(imap_quote(folder.raw), readonly=True)
    if typ != "OK":
        scan.error = f"could not open folder: {_text(data)}"
        return scan
    _, uv = conn.response("UIDVALIDITY")
    try:
        scan.uidvalidity = int(uv[0]) if uv and uv[0] else None
    except (TypeError, ValueError):
        scan.uidvalidity = None
    try:
        scan.exists = int(data[0]) if data and data[0] else 0
    except (TypeError, ValueError):
        scan.exists = 0
    if scan.exists == 0:
        return scan

    uids: set[int] = set()
    for sender in senders:
        typ, data = conn.uid("SEARCH", "FROM", imap_quote(sender))
        if typ != "OK":
            scan.error = f"search failed: {_text(data)}"
            return scan
        for chunk in data:
            if chunk:
                uids.update(int(x) for x in chunk.split())
    scan.server_hits = len(uids)

    sender_set = set(senders)
    for batch in _chunks(sorted(uids), FETCH_BATCH):
        typ, data = conn.uid("FETCH", ",".join(map(str, batch)), FETCH_ITEMS)
        if typ != "OK":
            scan.unparsed += len(batch)
            scan.error = f"fetch failed for {len(batch)} message(s): {_text(data)}"
            continue
        seen: set[int] = set()
        for record in parse_fetch_response(data):
            if record.uid is None or record.uid not in batch or record.uid in seen:
                continue
            seen.add(record.uid)
            meta = build_meta(record, folder.name, scan.uidvalidity)
            if not meta.matches(sender_set):
                scan.rejected += 1
            elif not meta.in_range(start, end):
                scan.out_of_range += 1
            else:
                scan.matched.append(meta)
        scan.unparsed += len(set(batch) - seen)
    return scan


def dedupe(messages: list[MessageMeta]) -> tuple[list[MessageMeta], int]:
    """Keep the first occurrence (input order: folder order, then UID)."""
    kept: dict[tuple, MessageMeta] = {}
    duplicates = 0
    for m in messages:
        first = kept.get(m.dedup_key)
        if first is None:
            kept[m.dedup_key] = m
        else:
            duplicates += 1
            if m.folder != first.folder and m.folder not in first.also_in:
                first.also_in.append(m.folder)
    return list(kept.values()), duplicates


def chronological_key(m: MessageMeta):
    utc = m.utc_date
    return (utc is None, utc.timestamp() if utc else 0.0, m.folder, m.uid)


def run_preview(
    conn,
    folders: list[Folder],
    excluded: list[tuple[Folder, str]],
    senders: list[str],
    start: date | None = None,
    end: date | None = None,
    max_messages: int = 1500,
    max_bytes: int = 200 * 1024 * 1024,
    progress: Callable[[str], None] = lambda _msg: None,
) -> PreviewResult:
    scans: list[FolderScan] = []
    for i, folder in enumerate(folders, 1):
        progress(f"[{i}/{len(folders)}] Searching {folder.name} ...")
        scan = scan_folder(conn, folder, senders, start, end)
        scan.matched.sort(key=lambda m: m.uid)
        scans.append(scan)
        note = f" (error: {scan.error})" if scan.error else ""
        progress(f"[{i}/{len(folders)}] {folder.name}: {len(scan.matched)} match(es){note}")

    unique, duplicates = dedupe([m for s in scans for m in s.matched])
    unique.sort(key=chronological_key)
    return PreviewResult(
        senders=senders, start=start, end=end, scans=scans, excluded=excluded,
        messages=unique, duplicates=duplicates,
        max_messages=max_messages, max_bytes=max_bytes,
    )
