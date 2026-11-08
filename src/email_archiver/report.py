"""Terminal rendering of the Phase 1 preview."""

from __future__ import annotations

from datetime import timezone

from .folders import Folder
from .preview import PreviewResult


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _year_label(year: int | None) -> str:
    return str(year) if year is not None else "undated"


def _truncate(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def render_folders(selected: list[Folder], excluded: list[tuple[Folder, str]]) -> str:
    lines = [f"Folders to search ({len(selected)}):"]
    lines += [f"  {f.name}" for f in selected] or ["  (none)"]
    lines.append(f"Folders excluded ({len(excluded)}):")
    lines += [f"  {f.name}  —  {reason}" for f, reason in excluded] or ["  (none)"]
    return "\n".join(lines)


def render_preview(result: PreviewResult, account: str, host: str) -> str:
    out: list[str] = []
    add = out.append
    add("")
    add("=" * 64)
    add("PREVIEW (read-only)")
    add("=" * 64)
    add(f"Account:     {account}  ({host})")
    add(f"Senders:     {', '.join(result.senders)}")
    if result.start or result.end:
        add(f"Date range:  {result.start or 'earliest'} to {result.end or 'latest'} "
            "(inclusive, by each message's Date header)")
    else:
        add("Date range:  all dates")
    add("")
    add(render_folders([s.folder for s in result.scans], result.excluded))

    add("")
    add("Matches by year")
    add(f"  {'Year':<8}{'Messages':>10}{'Size':>12}{'Date fallback*':>16}")
    by_year = result.by_year
    for year, s in by_year.items():
        add(f"  {_year_label(year):<8}{s.count:>10,}{human_size(s.size):>12}{s.fallback:>16,}")
    total = len(result.messages)
    total_size = sum(m.size for m in result.messages)
    total_fb = sum(s.fallback for s in by_year.values())
    add(f"  {'Total':<8}{total:>10,}{human_size(total_size):>12}{total_fb:>16,}")
    add("  * Date header missing/invalid; year taken from the server's received date.")

    add("")
    add("Matches by folder (after removing duplicates)")
    table = result.by_folder_year
    for scan in result.scans:
        counts = table.get(scan.folder.name, {})
        n = sum(counts.values())
        detail = ", ".join(f"{_year_label(y)}: {c}" for y, c in
                           sorted(counts.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)))
        add(f"  {scan.folder.name}: {n}" + (f"  ({detail})" if detail else ""))

    add("")
    add("Reconciliation")
    hits = sum(s.server_hits for s in result.scans)
    rejected = sum(s.rejected for s in result.scans)
    out_of_range = sum(s.out_of_range for s in result.scans)
    unparsed = sum(s.unparsed for s in result.scans)
    add(f"  Server search hits (all folders):      {hits:>8,}")
    add(f"  Rejected: From not an exact match:     {rejected:>8,}")
    add(f"  Outside date range:                    {out_of_range:>8,}")
    add(f"  Unreadable server responses:           {unparsed:>8,}")
    add(f"  Duplicates (same message, 2+ folders): {result.duplicates:>8,}")
    add(f"  Unique matching messages:              {total:>8,}")

    boundary = result.boundary_messages
    add("")
    add(f"Year-boundary messages — please verify the year ({len(boundary)})")
    if not boundary:
        add("  (none)")
    for m in boundary:
        d = m.effective_date
        utc = d.astimezone(timezone.utc)
        src = "received date" if m.used_fallback else "Date header"
        add(f"  {d:%Y-%m-%d %H:%M %z}  (UTC {utc:%Y-%m-%d %H:%M})  → {m.year}  "
            f"[{src}, {m.folder}]  {_truncate(m.subject, 40)!r}")

    add("")
    oversized = result.oversized_years
    if oversized:
        add("Large years (over limit of "
            f"{result.max_messages:,} messages or {human_size(result.max_bytes)}):")
        for year, s in oversized:
            add(f"  {_year_label(year)}: {s.count:,} messages, {human_size(s.size)}")
        add("  You will be asked to keep one PDF or split by month at export time.")
    else:
        add("Large years: none over the limit.")

    errors = [s for s in result.scans if s.error]
    if errors:
        add("")
        add("FOLDER ERRORS — counts above may be incomplete:")
        for s in errors:
            add(f"  {s.folder.name}: {s.error}")

    add("")
    add("Mailbox unchanged: folders opened read-only (EXAMINE), headers read with")
    add("BODY.PEEK, no flags changed. No files were written.")
    return "\n".join(out)
