"""Phase 5: full export — every match fetched, one PDF per year, resumable.

Layout under the output folder (design doc section 5):

    Cox-Sender-Archive-2016.pdf            (or -2016-part-01.pdf when split by month)
    messages/2016/*.eml                    original messages, byte for byte
    attachments/2016/*                     attachments not embedded in the PDF
    export-manifest.json                   identifiers, checksums, paths (no content)
    export.checkpoint                      resume state (safe to delete after a full run)

Every message the search found ends up either inside a PDF or listed as a
failure with its reason; nothing is dropped silently.
"""

from __future__ import annotations

import hashlib
import imaplib
import json
import os
import socket
import ssl
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from .fetch import Pick, fetch_one, inside_git_repo
from .headers import MessageMeta
from .safe_imap import imap_quote

CHECKPOINT_NAME = "export.checkpoint"
MANIFEST_NAME = "export-manifest.json"
DEFAULT_PREFIX = "Cox-Sender-Archive"
RETRY_DELAYS = (2, 4, 8, 16, 32)  # seconds between reconnect attempts
# Network failures worth a reconnect-and-retry. File-system errors are not here:
# a full disk must stop the run, not be retried.
TRANSIENT = (imaplib.IMAP4.abort, ConnectionError, TimeoutError, socket.timeout, ssl.SSLError)

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class ExportError(RuntimeError):
    """A problem that stops the export before or between steps (shown to the user)."""


class FolderError(ExportError):
    """A problem with one folder; its messages fail, the rest of the export continues."""


def message_key(m: MessageMeta) -> str:
    return f"{m.folder}|{m.uidvalidity}|{m.uid}"


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".partial-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _private_dir(path: Path) -> Path:
    if not path.exists():
        path.mkdir(parents=True)
        os.chmod(path, 0o700)
    return path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Checkpoint
# --------------------------------------------------------------------------

class Checkpoint:
    """Resume state, rewritten atomically after every message.

    identity ties the folder to one mailbox and sender list, so two different
    exports cannot be mixed in one output folder.
    """

    def __init__(self, path: Path, identity: dict):
        self.path = path
        self.data = {"version": 1, "identity": identity, "split": None,
                     "messages": {}, "rendered": {}}
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ExportError(f"{path} is unreadable ({exc.__class__.__name__}); "
                                  "move it aside to start over") from exc
            if stored.get("identity") != identity:
                raise ExportError(
                    f"{path.parent} already holds an export for a different account or "
                    f"sender list ({', '.join(stored.get('identity', {}).get('senders', []))}). "
                    "Use a different --output-dir.")
            self.data.update(stored)

    @property
    def messages(self) -> dict:
        return self.data["messages"]

    @property
    def rendered(self) -> dict:
        return self.data["rendered"]

    def save(self) -> None:
        _atomic_write(self.path, (json.dumps(self.data, indent=1) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------
# Planning PDFs (one per year, or month-based parts for oversized years)
# --------------------------------------------------------------------------

@dataclass
class Part:
    year: int | None
    messages: list[MessageMeta]
    index: int | None = None      # 1-based when the year is split
    total: int | None = None
    months: tuple[int, int] | None = None
    filename: str = ""

    @property
    def label(self) -> str:
        y = str(self.year) if self.year is not None else "undated"
        if self.index is None:
            return y
        a, b = self.months
        span = _MONTHS[a - 1] if a == b else f"{_MONTHS[a - 1]}–{_MONTHS[b - 1]}"
        return f"{y} part {self.index} of {self.total} ({span})"

    @property
    def size(self) -> int:
        return sum(m.size for m in self.messages)


def plan_parts(messages: list[MessageMeta], split_years: set, max_messages: int,
               max_bytes: int, prefix: str = DEFAULT_PREFIX) -> list[Part]:
    """Group chronologically ordered messages into PDFs.

    Years in ``split_years`` are packed greedily by calendar month so each part
    stays within the limits where possible; a part is always whole months, so
    every part covers a contiguous date range and no month is divided.
    """
    by_year: dict[int | None, list[MessageMeta]] = {}
    for m in messages:
        by_year.setdefault(m.year, []).append(m)
    parts: list[Part] = []
    for year in sorted(by_year, key=lambda y: (y is None, y or 0)):
        msgs = by_year[year]
        ystr = str(year) if year is not None else "undated"
        if year not in split_years or year is None:
            parts.append(Part(year, msgs, filename=f"{prefix}-{ystr}.pdf"))
            continue
        months: dict[int, list[MessageMeta]] = {}
        for m in msgs:
            months.setdefault(m.effective_date.month, []).append(m)
        groups: list[list[int]] = []
        count = size = 0
        for mo in sorted(months):
            mc, ms = len(months[mo]), sum(x.size for x in months[mo])
            if groups and (count + mc > max_messages or size + ms > max_bytes):
                groups.append([mo]); count, size = mc, ms
            elif groups:
                groups[-1].append(mo); count += mc; size += ms
            else:
                groups.append([mo]); count, size = mc, ms
        if len(groups) == 1:
            parts.append(Part(year, msgs, filename=f"{prefix}-{ystr}.pdf"))
            continue
        for i, g in enumerate(groups, 1):
            gm = [m for mo in g for m in months[mo]]
            parts.append(Part(year, gm, index=i, total=len(groups), months=(g[0], g[-1]),
                              filename=f"{prefix}-{ystr}-part-{i:02d}.pdf"))
    return parts


# --------------------------------------------------------------------------
# Fetching with reconnect/retry
# --------------------------------------------------------------------------

class Session:
    """Holds the IMAP connection; reconnects (with a fresh login) on network errors."""

    def __init__(self, conn, reconnect: Callable[[], object], folder_raw: dict[str, str],
                 progress: Callable[[str], None], sleep: Callable[[float], None] = time.sleep):
        self.conn = conn
        self._reconnect = reconnect
        self.folder_raw = folder_raw
        self.progress = progress
        self.sleep = sleep
        self.current: str | None = None
        self.uidvalidity: int | None = None
        self.reconnects = 0

    def examine(self, folder: str) -> int | None:
        if self.current != folder:
            typ, data = self.conn.select(imap_quote(self.folder_raw.get(folder, folder)), readonly=True)
            if typ != "OK":
                raise FolderError(f"could not open folder {folder!r} read-only")
            _, uv = self.conn.response("UIDVALIDITY")
            try:
                self.uidvalidity = int(uv[0]) if uv and uv[0] else None
            except (TypeError, ValueError):
                self.uidvalidity = None
            self.current = folder
        return self.uidvalidity

    def with_retry(self, fn):
        """Run fn(); on a network error, wait, reconnect and try again."""
        last: Exception | None = None
        for attempt, delay in enumerate((0,) + RETRY_DELAYS):
            if delay:
                self.progress(f"    network problem; reconnecting in {delay}s "
                              f"(attempt {attempt} of {len(RETRY_DELAYS)}) ...")
                self.sleep(delay)
                try:
                    try:
                        self.conn.logout()
                    except Exception:
                        pass
                    self.conn = self._reconnect()
                    self.current = None
                    self.reconnects += 1
                except TRANSIENT + (OSError,) as exc:
                    last = exc
                    continue
            try:
                return fn()
            except TRANSIENT as exc:
                last = exc
                continue
        raise ExportError(f"network kept failing after {len(RETRY_DELAYS)} reconnect attempts "
                          f"({type(last).__name__}); progress is saved — re-run to resume")


@dataclass
class FetchSummary:
    fetched: int = 0
    reused: int = 0
    failed: list[tuple[MessageMeta, str]] = field(default_factory=list)
    stopped_folders: dict[str, str] = field(default_factory=dict)


def _reusable(entry: dict | None, out: Path) -> bool:
    if not entry or entry.get("status") != "ok":
        return False
    path = out / entry["file"]
    return path.is_file() and path.stat().st_size == entry["size"] and _sha256_file(path) == entry["sha256"]


def fetch_all(session: Session, messages: list[MessageMeta], out: Path, ckpt: Checkpoint,
              progress: Callable[[str], None]) -> FetchSummary:
    summary = FetchSummary()
    total = len(messages)
    for n, meta in enumerate(messages, 1):
        key = message_key(meta)
        if _reusable(ckpt.messages.get(key), out):
            summary.reused += 1
            continue
        if meta.folder in summary.stopped_folders:
            reason = summary.stopped_folders[meta.folder]
            summary.failed.append((meta, reason))
            ckpt.messages[key] = {"status": "failed", "problems": [reason], "year": meta.year}
            continue
        year_dir = _private_dir(_private_dir(out / "messages") / (str(meta.year) if meta.year else "undated"))
        progress(f"[{n}/{total}] {meta.year or 'undated'}  {meta.folder} UID {meta.uid}  "
                 f"({meta.size / 1048576:.1f} MB)")

        def attempt():
            uv = session.examine(meta.folder)
            return fetch_one(session.conn, Pick(meta, "export"), year_dir, uv)

        try:
            outcome = session.with_retry(attempt)
        except FolderError as exc:
            summary.stopped_folders[meta.folder] = str(exc)
            summary.failed.append((meta, str(exc)))
            ckpt.messages[key] = {"status": "failed", "problems": [str(exc)], "year": meta.year}
            ckpt.save()
            continue
        if outcome.ok:
            summary.fetched += 1
            ckpt.messages[key] = {
                "status": "ok", "year": meta.year,
                "file": (year_dir / outcome.file).relative_to(out).as_posix(),
                "size": outcome.bytes_written, "sha256": outcome.sha256,
                "flags": outcome.flags_before, "warnings": outcome.warnings,
            }
        else:
            reason = "; ".join(outcome.problems)
            if any("UIDVALIDITY" in p for p in outcome.problems):
                reason = ("folder UIDVALIDITY changed since the search (the server renumbered "
                          "this folder); re-run the export to search it again")
                summary.stopped_folders[meta.folder] = reason
            summary.failed.append((meta, reason))
            ckpt.messages[key] = {"status": "failed", "problems": outcome.problems, "year": meta.year}
        ckpt.save()
    return summary


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

@dataclass
class PartResult:
    part: Part
    status: str                    # rendered | up-to-date | failed | empty
    included: int = 0
    pages: int | None = None
    error: str | None = None
    excluded: list[tuple[MessageMeta, str]] = field(default_factory=list)
    plain_text: list[MessageMeta] = field(default_factory=list)  # HTML shown as text (fallback)


def _part_hash(part: Part, ckpt: Checkpoint, title: str) -> str:
    h = hashlib.sha256(title.encode())
    for m in part.messages:
        e = ckpt.messages.get(message_key(m), {})
        h.update(f"{message_key(m)}={e.get('sha256')}\n".encode())
    return h.hexdigest()


def render_part(part: Part, out: Path, ckpt: Checkpoint, title: str, force: bool = False,
                progress: Callable[[str], None] = lambda _m: None) -> PartResult:
    from pypdf import PdfReader

    from .mime import parse_eml
    from .render import render_pdf_resilient

    ok = [m for m in part.messages if ckpt.messages.get(message_key(m), {}).get("status") == "ok"]
    excluded = [(m, "not fetched (see failures)") for m in part.messages if m not in ok]
    result = PartResult(part, "failed", excluded=excluded)
    if not ok:
        result.status = "empty"
        return result

    digest = _part_hash(part, ckpt, title)
    pdf = out / part.filename
    previous = ckpt.rendered.get(part.filename, {})
    if not force and pdf.is_file() and previous.get("hash") == digest:
        result.status, result.included, result.pages = "up-to-date", previous["included"], previous["pages"]
        by_key = {message_key(m): m for m in part.messages}
        result.excluded += [(by_key[k], why) for k, why in previous.get("excluded", []) if k in by_key]
        result.plain_text = [by_key[k] for k in previous.get("plain_text", []) if k in by_key]
        return result

    parsed = []
    meta_of: dict[int, MessageMeta] = {}
    for m in ok:
        entry = ckpt.messages[message_key(m)]
        try:
            pm = parse_eml((out / entry["file"]).read_bytes(), Path(entry["file"]).name)
        except Exception as exc:
            result.excluded.append((m, f"could not be parsed ({exc.__class__.__name__})"))
            continue
        pm.folder, pm.uid = m.folder, m.uid
        if pm.date is None and m.internaldate is not None:
            pm.date = m.internaldate          # same fallback the search used for the year
        parsed.append(pm)
        meta_of[id(pm)] = m
    try:
        sections, issues = render_pdf_resilient(parsed, pdf, title, progress=progress)
    except (Exception, RecursionError) as exc:  # record and continue with other years
        result.error = f"{exc.__class__.__name__}: {exc}"
        return result
    result.excluded += [(meta_of[id(pm)], why) for pm, why in issues.excluded]
    result.plain_text = [meta_of[id(pm)] for pm in issues.plain]
    if len(sections) != len(parsed) - len(issues.excluded):
        result.error = f"rendered {len(sections)} of {len(parsed) - len(issues.excluded)} messages"
        return result
    result.status, result.included = "rendered", len(sections)
    result.pages = len(PdfReader(str(pdf)).pages)
    ckpt.rendered[part.filename] = {
        "hash": digest, "year": part.year, "included": result.included, "pages": result.pages,
        "plain_text": [message_key(m) for m in result.plain_text],
        "excluded": [[message_key(m), why] for m, why in result.excluded],
        "rendered_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attachments": [
            {"message": f"{s.message.folder}|{s.message.uid}", "filename": a.filename,
             "action": a.action, "saved_as": a.saved_as, "reason": a.reason, "sha256": a.sha256}
            for s in sections for a in s.attachments],
    }
    ckpt.save()
    return result


def part_title(part: Part, start: date | None, end: date | None) -> str:
    t = f"Email Archive — {part.label}"
    if start or end:
        t += f" (limited run: {start or 'beginning'} to {end or 'end'})"
    return t


# --------------------------------------------------------------------------
# Whole run
# --------------------------------------------------------------------------

@dataclass
class ExportResult:
    parts: list[PartResult]
    fetch: FetchSummary
    messages: list[MessageMeta]
    manifest: Path | None
    started: datetime
    finished: datetime | None = None

    @property
    def not_exported(self) -> list[tuple[MessageMeta, str]]:
        seen, out = set(), []
        failed_keys = {message_key(m) for m, _ in self.fetch.failed}
        for meta, why in self.fetch.failed + [x for p in self.parts for x in p.excluded]:
            if why.startswith("not fetched") and message_key(meta) in failed_keys:
                continue
            if message_key(meta) not in seen:
                seen.add(message_key(meta))
                out.append((meta, why))
        for p in self.parts:
            if p.status == "failed" and p.error:
                for m in p.part.messages:
                    if message_key(m) not in seen:
                        seen.add(message_key(m))
                        out.append((m, f"PDF {p.part.filename} failed: {p.error}"))
        return out

    @property
    def total(self) -> int:
        return len(self.messages)

    @property
    def exported(self) -> int:
        return sum(p.included for p in self.parts if p.status in ("rendered", "up-to-date"))


def check_output_dir(out: Path) -> Path:
    out = out.expanduser().resolve()
    repo = inside_git_repo(out)
    if repo is not None:
        raise ExportError(f"{out} is inside the git repository {repo}. "
                          "Choose an output folder outside any repository (mail is private).")
    if out.exists() and not out.is_dir():
        raise ExportError(f"{out} exists and is not a folder")
    return out


def run_export(session: Session, messages: list[MessageMeta], parts: list[Part], out: Path,
               ckpt: Checkpoint, *, start: date | None, end: date | None,
               manifest_info: dict | None, progress: Callable[[str], None]) -> ExportResult:
    """Fetch everything, render each part, write the manifest (even if stopped early)."""
    result = ExportResult(parts=[], fetch=FetchSummary(), messages=messages, manifest=None,
                          started=datetime.now(timezone.utc))
    try:
        result.fetch = fetch_all(session, messages, out, ckpt, progress)
        for i, part in enumerate(parts, 1):
            title = part_title(part, start, end)
            progress(f"Rendering PDF {i}/{len(parts)}: {part.filename} ({len(part.messages)} messages) ...")
            result.parts.append(render_part(part, out, ckpt, title, progress=progress))
        result.finished = datetime.now(timezone.utc)
    finally:
        if manifest_info is not None:
            result.manifest = write_manifest(out, result, ckpt, manifest_info)
    return result


def write_manifest(out: Path, result: ExportResult, ckpt: Checkpoint, info: dict) -> Path:
    """export-manifest.json: identifiers, checksums, paths, counts. No subjects,
    bodies, Message-IDs or credentials."""
    pdf_of = {message_key(m): p.part.filename for p in result.parts for m in p.part.messages
              if p.status in ("rendered", "up-to-date")}
    failed = {message_key(m): why for m, why in result.not_exported}
    plain = {message_key(m) for p in result.parts for m in p.plain_text}
    msgs = []
    for m in result.messages:
        k = message_key(m)
        e = ckpt.messages.get(k, {})
        msgs.append({
            "folder": m.folder, "uidvalidity": m.uidvalidity, "uid": m.uid, "year": m.year,
            "date_fallback": m.used_fallback, "eml": e.get("file"), "sha256": e.get("sha256"),
            "size": e.get("size"), "pdf": None if k in failed else pdf_of.get(k),
            "status": "failed" if k in failed else ("exported" if k in pdf_of else "pending"),
            "problem": failed.get(k),
            "shown_as_plain_text": k in plain,
        })
    years: dict[str, dict] = {}
    for m in result.messages:
        years.setdefault(str(m.year or "undated"), {"found": 0, "exported": 0, "pdfs": []})["found"] += 1
    for p in result.parts:
        y = years.setdefault(str(p.part.year or "undated"), {"found": 0, "exported": 0, "pdfs": []})
        y["exported"] += p.included if p.status in ("rendered", "up-to-date") else 0
        y["pdfs"].append(p.part.filename)
    doc = {
        "tool": "email-archiver", **info,
        "started_utc": result.started.isoformat(timespec="seconds"),
        "finished_utc": result.finished.isoformat(timespec="seconds") if result.finished else None,
        "complete": result.finished is not None and not failed,
        "split": ckpt.data.get("split"),
        "totals": {"found": result.total, "exported": result.exported, "not_exported": len(failed),
                   "fetched_this_run": result.fetch.fetched, "reused_from_earlier_run": result.fetch.reused},
        "years": years,
        "pdfs": [{"file": p.part.filename, "year": p.part.year, "part": p.part.index,
                  "months": list(p.part.months) if p.part.months else None, "status": p.status,
                  "messages": p.included, "pages": p.pages, "error": p.error,
                  "attachments": ckpt.rendered.get(p.part.filename, {}).get("attachments", [])}
                 for p in result.parts],
        "messages": msgs,
    }
    path = out / MANIFEST_NAME
    _atomic_write(path, (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return path
