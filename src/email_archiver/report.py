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


# --------------------------------------------------------------------------
# Phase 2
# --------------------------------------------------------------------------

def _when(m) -> str:
    d = m.effective_date
    return f"{d:%Y-%m-%d %H:%M %z}" if d else "undated"


def render_message_list(messages) -> str:
    """All matches with FOLDER:UID handles for --uid. Terminal only."""
    lines = [f"{len(messages)} matching message(s). Pass any FOLDER:UID to --uid.", ""]
    lines.append(f"  {'FOLDER:UID':<24}{'DATE':<24}{'SIZE':>9}  SUBJECT")
    for m in messages:
        handle = f"{m.folder}:{m.uid}"
        lines.append(f"  {handle:<24}{_when(m):<24}{human_size(m.size):>9}  {_truncate(m.subject, 50)}")
    return "\n".join(lines)


def render_sample_plan(picks) -> str:
    lines = [f"Sample to fetch ({len(picks)}):"]
    for p in picks:
        m = p.meta
        lines.append(f"  {m.folder}:{m.uid:<10} {_when(m):<24}{human_size(m.size):>9}  "
                     f"[{p.reason}]  {_truncate(m.subject, 40)}")
    return "\n".join(lines)


def render_sample_result(outcomes, picks, run_dir) -> str:
    subjects = {(p.meta.folder, p.meta.uid): p.meta.subject for p in picks}
    saved = [o for o in outcomes if o.ok]
    failed = [o for o in outcomes if not o.ok]
    flags_same = sum(o.flags_before is not None and o.flags_before == o.flags_after
                     for o in outcomes)
    out = ["", "=" * 64, "SAMPLE FETCH (Phase 2)", "=" * 64]
    out.append(f"Saved to:  {run_dir}")
    out.append("")
    for o in outcomes:
        mark = "OK  " if o.ok else "FAIL"
        flags = " ".join(o.flags_before or []) or "(none)"
        out.append(f"  {mark} {o.folder}:{o.uid}  {o.header_date or 'undated'}")
        out.append(f"       subject: {_truncate(subjects.get((o.folder, o.uid), ''), 60)}")
        if o.file:
            out.append(f"       file:    {o.file}  ({human_size(o.bytes_written or 0)}, "
                       f"sha256 {o.sha256[:12]}…)")
        out.append(f"       flags:   {flags}"
                   + ("  (unchanged)" if o.flags_before is not None and o.flags_before == o.flags_after
                      else ""))
        for w in o.warnings:
            out.append(f"       note:    {w}")
        for pr in o.problems:
            out.append(f"       PROBLEM: {pr}")
    out.append("")
    out.append(f"Saved {len(saved)} of {len(outcomes)}; flags unchanged on "
               f"{flags_same} of {len(outcomes)}; failed {len(failed)}.")
    out.append("Mailbox unchanged: folders opened read-only (EXAMINE), messages read with "
               "BODY.PEEK, flags compared before and after.")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Phase 3
# --------------------------------------------------------------------------

def render_render_result(sections, output, errors, report=None) -> str:
    from pypdf import PdfReader

    try:
        pages = len(PdfReader(str(output)).pages)
    except Exception:
        pages = None
    out = ["", "=" * 64, "PDF RENDER", "=" * 64]
    out.append(f"Wrote:  {output}" + (f"  ({pages} pages)" if pages else ""))
    if report:
        out.append(f"Report: {report}")
    out.append("")
    out.append(f"  {'#':>2}  {'DATE':<17}{'BODY':<6}{'INLINE':>7}{'BLOCKED':>8}{'IMG':>5}{'PDF':>5}{'TXT':>5}{'SAVED':>7}  SUBJECT")
    for i, s in enumerate(sections, 1):
        m = s.message
        when = f"{m.date:%Y-%m-%d %H:%M}" if m.date else "undated"
        text = s.count("embedded-text") + s.count("embedded-html")
        out.append(f"  {i:>2}  {when:<17}{m.body_kind:<6}{s.inline_images:>7}{s.remote_images:>8}"
                   f"{s.count('embedded-image'):>5}{s.count('embedded-pdf'):>5}{text:>5}"
                   f"{s.count('saved'):>7}  {_truncate(m.subject, 34)}")
    out.append("")
    out.append("  INLINE = images shown inside the message body; BLOCKED = remote images not loaded;")
    out.append("  IMG/PDF/TXT = attachments embedded (TXT includes HTML); SAVED = saved separately.")
    saved = [(i, a) for i, s in enumerate(sections, 1) for a in s.attachments
             if a.action == "saved" or (a.saved_as and a.action != "saved")]
    if saved:
        out.append("")
        out.append("Saved separately (under attachments/ next to the PDF):")
        for i, a in saved:
            why = a.reason or "full copy of a long text attachment"
            out.append(f"  #{i}  {_truncate(a.filename, 38):<40}{why}")
            if a.saved_as:
                out.append(f"       -> {a.saved_as}")
    if errors:
        out.append("")
        out.append(f"  {len(errors)} file(s) could not be read; see messages above.")
    out.append("")
    out.append("Rendered offline: no network access; HTML sanitized before rendering.")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Phase 5
# --------------------------------------------------------------------------

def render_export_plan(result, parts, out, ckpt, split) -> str:
    from .export import _reusable, message_key

    total = len(result.messages)
    size = sum(m.size for m in result.messages)
    reused = sum(_reusable(ckpt.messages.get(message_key(m)), out) for m in result.messages)
    lines = ["", "=" * 64, "EXPORT PLAN", "=" * 64]
    lines.append(f"Output folder: {out}")
    if result.start or result.end:
        lines.append(f"Date range:    {result.start or 'beginning'} to {result.end or 'end'} (limited run)")
    lines.append(f"Messages:      {total:,} ({human_size(size)}) into {len(parts)} PDF(s)")
    if reused:
        lines.append(f"Already downloaded by an earlier run: {reused:,} (verified by checksum; not fetched again)")
    if result.oversized_years:
        lines.append(f"Oversized years: {split} "
                     f"({'split into calendar-month parts' if split == 'monthly' else 'kept as one PDF'})")
    lines.append("")
    for p in parts:
        lines.append(f"  {p.filename:<40}{len(p.messages):>6} messages {human_size(p.size):>10}"
                     + (f"   {p.label.split('(')[-1].rstrip(')')}" if p.index else ""))
    lines.append("")
    lines.append("Originals are kept as .eml under messages/YYYY/; attachments that cannot be")
    lines.append("shown in a PDF go under attachments/YYYY/. The mailbox is only read, never changed.")
    return "\n".join(lines)


def render_export_result(res, out, reconnects: int = 0) -> str:
    lines = ["", "=" * 64, "EXPORT RESULT", "=" * 64]
    lines.append(f"Output folder: {out}")
    lines.append("")
    lines.append(f"  {'PDF':<40}{'STATUS':<12}{'MESSAGES':>9}{'PAGES':>7}")
    for p in res.parts:
        lines.append(f"  {p.part.filename:<40}{p.status:<12}{p.included:>9}"
                     f"{(p.pages if p.pages is not None else '-'):>7}")
        if p.error:
            lines.append(f"      ERROR: {p.error}")
        for m in p.plain_text:
            lines.append(f"      note: {m.folder}:{m.uid} shown as plain text (its HTML could not be laid out)")
    lines.append("")
    lines.append("Reconciliation")
    lines.append(f"  Found by the search:             {res.total:>6,}")
    lines.append(f"  Exported into PDFs:              {res.exported:>6,}")
    lines.append(f"  Not exported:                    {len(res.not_exported):>6,}")
    lines.append(f"  Downloaded this run:             {res.fetch.fetched:>6,}")
    lines.append(f"  Reused from an earlier run:      {res.fetch.reused:>6,}")
    if reconnects:
        lines.append(f"  Network reconnects:              {reconnects:>6,}")
    if res.not_exported:
        lines.append("")
        lines.append("NOT EXPORTED — these messages are not in any PDF:")
        for m, why in res.not_exported:
            when = f"{m.effective_date:%Y-%m-%d}" if m.effective_date else "undated"
            lines.append(f"  {m.folder}:{m.uid}  {when}  {_truncate(m.subject, 36)!r}")
            lines.append(f"      reason: {why}")
        lines.append("Re-run the same command to retry them; finished files are kept.")
    if res.manifest:
        lines.append("")
        lines.append(f"Manifest: {res.manifest}")
    lines.append("")
    lines.append("Mailbox unchanged: folders opened read-only (EXAMINE), messages read with")
    lines.append("BODY.PEEK, flags compared before and after every download.")
    return "\n".join(lines)
