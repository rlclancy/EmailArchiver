"""Phase 2: fetch original messages read-only and save them as byte-faithful .eml files."""

from __future__ import annotations

import email
import email.policy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path

from .headers import MessageMeta, normalize_address, parse_fetch_response
from .safe_imap import imap_quote

_FLAGS = re.compile(rb"\bFLAGS \(([^)]*)\)")
_MSGID = re.compile(r"<[^<>]+>")
_UNSAFE = re.compile(r"[^A-Za-z0-9]+")

SAMPLES_DIRNAME = "_samples"
RUN_METADATA_NAME = "sample-run.json"
MAX_SAMPLE = 25


class OutputDirError(ValueError):
    pass


# --------------------------------------------------------------------------
# Sample selection
# --------------------------------------------------------------------------

@dataclass
class Pick:
    meta: MessageMeta
    reason: str


def _key(m: MessageMeta) -> tuple[str, int]:
    return (m.folder, m.uid)


def choose_sample(messages: list[MessageMeta], count: int) -> list[Pick]:
    """Pick up to ``count`` varied messages, deterministically.

    ``messages`` must be in chronological order (as from run_preview). Picks,
    in priority order: oldest, newest, largest, smallest, a year-boundary
    message, one per uncovered year, then evenly spaced fill.
    """
    if count <= 0 or not messages:
        return []
    picks: list[Pick] = []
    taken: set[tuple[str, int]] = set()

    def add(m: MessageMeta | None, reason: str) -> None:
        if m is not None and len(picks) < count and _key(m) not in taken:
            taken.add(_key(m))
            picks.append(Pick(m, reason))

    add(messages[0], "oldest")
    add(messages[-1], "newest")
    add(max(messages, key=lambda m: (m.size, m.folder, -m.uid)), "largest")
    add(min(messages, key=lambda m: (m.size, m.folder, m.uid)), "smallest")
    add(next((m for m in messages if m.near_year_boundary()), None), "year boundary")

    covered = {p.meta.year for p in picks}
    for m in messages:
        if m.year not in covered:
            covered.add(m.year)
            add(m, f"first of {m.year if m.year is not None else 'undated'}")

    if len(picks) < count:
        remaining = [m for m in messages if _key(m) not in taken]
        need = count - len(picks)
        if need >= len(remaining):
            chosen = remaining
        else:  # evenly spaced; indices strictly increase because len(remaining) > need
            chosen = [remaining[(i * len(remaining)) // need] for i in range(need)]
        for m in chosen:
            add(m, "spread")

    order = {_key(m): i for i, m in enumerate(messages)}
    picks.sort(key=lambda p: order[_key(p.meta)])
    return picks


def parse_uid_spec(spec: str) -> tuple[str, int]:
    """Parse 'Folder:UID' (folder names may themselves contain ':')."""
    folder, sep, uid = spec.rpartition(":")
    if not sep or not folder or not uid.isdigit():
        raise ValueError(f"expected FOLDER:UID (e.g. Inbox:1234), got {spec!r}")
    return folder, int(uid)


def picks_from_specs(messages: list[MessageMeta], specs: list[str]) -> list[Pick]:
    by_key = {_key(m): m for m in messages}
    picks, missing = [], []
    for spec in specs:
        key = parse_uid_spec(spec)
        if key in by_key:
            if all(_key(p.meta) != key for p in picks):
                picks.append(Pick(by_key[key], "selected"))
        else:
            missing.append(spec)
    if missing:
        raise ValueError("Not among the matching messages (check with --list): " + ", ".join(missing))
    order = {_key(m): i for i, m in enumerate(messages)}
    picks.sort(key=lambda p: order[_key(p.meta)])
    return picks


# --------------------------------------------------------------------------
# Output directory
# --------------------------------------------------------------------------

def inside_git_repo(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return None


def prepare_output_dir(output_dir: Path, now: datetime | None = None) -> Path:
    """Create <output_dir>/_samples/<timestamp>/ (mode 0700) and return it.

    Refuses locations inside a git working tree so private mail cannot end up
    in a repository.
    """
    root = output_dir.expanduser().resolve()
    repo = inside_git_repo(root)
    if repo is not None:
        raise OutputDirError(
            f"{root} is inside the git repository {repo}. "
            "Choose an output folder outside any repository (mail is private)."
        )
    if root.exists() and not root.is_dir():
        raise OutputDirError(f"{root} exists and is not a folder")
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    run_dir = root / SAMPLES_DIRNAME / stamp
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = root / SAMPLES_DIRNAME / f"{stamp}-{suffix}"
    run_dir.mkdir(parents=True, mode=0o700)
    os.chmod(run_dir, 0o700)
    return run_dir


def sample_filename(meta: MessageMeta) -> str:
    """Deterministic, path-safe name: no sender names or subjects."""
    d = meta.effective_date
    day = d.strftime("%Y-%m-%d") if d else "undated"
    folder = _UNSAFE.sub("-", meta.folder).strip("-")[:40] or "folder"
    return f"{day}_{folder}_uid{meta.uid}.eml"


def write_atomic(directory: Path, name: str, data: bytes) -> Path:
    target = (directory / name).resolve()
    if target.parent != directory.resolve():
        raise OutputDirError(f"refusing to write outside the output folder: {name!r}")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".partial-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return target


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

@dataclass
class SampleOutcome:
    folder: str
    uid: int
    uidvalidity: int | None
    reason: str
    year: int | None
    header_date: str | None
    date_fallback: bool
    rfc822_size: int
    file: str | None = None
    bytes_written: int | None = None
    sha256: str | None = None
    flags_before: list[str] | None = None
    flags_after: list[str] | None = None
    status: str = "pending"  # ok | failed
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def parse_flags(data: list, uid: int) -> list[str] | None:
    for record_meta in _metas(data):
        if re.search(rb"\bUID %d\b" % uid, record_meta):
            m = _FLAGS.search(record_meta)
            if m:
                return sorted(f.decode("ascii", "replace") for f in m.group(1).split())
    return None


def _metas(data: list) -> list[bytes]:
    out = []
    for part in data or []:
        if isinstance(part, tuple):
            out.append(part[0] + (part[2] if len(part) > 2 else b""))
        elif isinstance(part, bytes):
            out.append(part)
    return out


def _fetch_flags(conn, uid: int) -> list[str] | None:
    typ, data = conn.uid("FETCH", str(uid), "(UID FLAGS)")
    if typ != "OK":
        return None
    return parse_flags(data, uid)


def _fetch_raw(conn, uid: int) -> bytes | None:
    typ, data = conn.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
    if typ != "OK":
        return None
    for record in parse_fetch_response(data):
        if record.uid == uid and record.header:
            return record.header
    return None


def _identity_problems(raw: bytes, meta: MessageMeta) -> list[str]:
    """Confirm the fetched bytes are the message the preview found."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    problems = []
    addresses = {normalize_address(a) for _, a in
                 getaddresses([str(v) for v in msg.get_all("From", [])]) if a}
    if not addresses & set(meta.from_addresses):
        problems.append("From header does not match the preview")
    mid_raw = (msg.get("Message-ID") or "").strip()
    m = _MSGID.search(mid_raw)
    mid = m.group(0) if m else (mid_raw or None)
    if mid != meta.message_id:
        problems.append("Message-ID does not match the preview")
    return problems


def fetch_one(conn, pick: Pick, run_dir: Path, current_uidvalidity: int | None) -> SampleOutcome:
    meta = pick.meta
    out = _blank_outcome(pick)

    def fail(problem: str) -> SampleOutcome:
        out.problems.append(problem)
        out.status = "failed"
        return out

    if current_uidvalidity != meta.uidvalidity:
        return fail("folder UIDVALIDITY changed since the search; re-run")

    out.flags_before = _fetch_flags(conn, meta.uid)
    if out.flags_before is None:
        return fail("could not read flags before fetching")

    raw = _fetch_raw(conn, meta.uid)

    # Always re-check flags, even if the fetch failed.
    out.flags_after = _fetch_flags(conn, meta.uid)
    if out.flags_after is None:
        out.problems.append("could not read flags after fetching")
    elif out.flags_after != out.flags_before:
        out.problems.append(
            f"flags changed: {' '.join(out.flags_before) or '(none)'} -> "
            f"{' '.join(out.flags_after) or '(none)'}")

    if raw is None:
        out.problems.append("server returned no message content")
    else:
        out.problems += _identity_problems(raw, meta)
        if meta.size and len(raw) != meta.size:
            out.warnings.append(
                f"downloaded {len(raw)} bytes; server reported RFC822.SIZE {meta.size}")
        if not out.problems:
            name = sample_filename(meta)
            write_atomic(run_dir, name, raw)
            out.file = name
            out.bytes_written = len(raw)
            out.sha256 = hashlib.sha256(raw).hexdigest()

    out.status = "failed" if out.problems else "ok"
    return out


def _blank_outcome(pick: Pick) -> SampleOutcome:
    meta = pick.meta
    return SampleOutcome(
        folder=meta.folder, uid=meta.uid, uidvalidity=meta.uidvalidity, reason=pick.reason,
        year=meta.year,
        header_date=meta.effective_date.isoformat() if meta.effective_date else None,
        date_fallback=meta.used_fallback, rfc822_size=meta.size,
    )


def fetch_sample(conn, picks: list[Pick], run_dir: Path, folder_raw: dict[str, str],
                 progress=lambda _m: None) -> list[SampleOutcome]:
    """Fetch picks folder by folder, each opened read-only with EXAMINE.

    ``folder_raw`` maps display names (MessageMeta.folder) to the server's raw
    mailbox names.
    """
    outcomes: list[SampleOutcome] = []
    by_folder: dict[str, list[Pick]] = {}
    for p in picks:
        by_folder.setdefault(p.meta.folder, []).append(p)

    done = 0
    for folder, group in by_folder.items():
        typ, _ = conn.select(imap_quote(folder_raw.get(folder, folder)), readonly=True)
        uidvalidity = None
        if typ == "OK":
            _, uv = conn.response("UIDVALIDITY")
            try:
                uidvalidity = int(uv[0]) if uv and uv[0] else None
            except (TypeError, ValueError):
                uidvalidity = None
        for p in group:
            done += 1
            progress(f"[{done}/{len(picks)}] Fetching {folder} UID {p.meta.uid} ...")
            if typ != "OK":
                o = _blank_outcome(p)
                o.status, o.problems = "failed", ["could not open folder read-only"]
                outcomes.append(o)
            else:
                outcomes.append(fetch_one(conn, p, run_dir, uidvalidity))
    order = {(p.meta.folder, p.meta.uid): i for i, p in enumerate(picks)}
    outcomes.sort(key=lambda o: order[(o.folder, o.uid)])
    return outcomes


def write_run_metadata(run_dir: Path, *, version: str, host: str, senders: list[str],
                       folders: list[str], selection: str, started: datetime,
                       outcomes: list[SampleOutcome]) -> Path:
    """Non-sensitive run record: no subjects, bodies, Message-IDs or credentials."""
    doc = {
        "tool": "email-archiver",
        "version": version,
        "phase": 2,
        "started_utc": started.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": host,
        "senders": senders,
        "folders_searched": folders,
        "selection": selection,
        "summary": {
            "requested": len(outcomes),
            "saved": sum(o.ok for o in outcomes),
            "failed": sum(not o.ok for o in outcomes),
            "flags_unchanged": sum(
                o.flags_before is not None and o.flags_before == o.flags_after for o in outcomes),
        },
        "messages": [asdict(o) for o in outcomes],
    }
    return write_atomic(run_dir, RUN_METADATA_NAME,
                        (json.dumps(doc, indent=2) + "\n").encode("utf-8"))
