"""Phase 3: render parsed messages into one searchable PDF with WeasyPrint.

Safety rules (design doc section 6):
- Untrusted HTML is sanitized with nh3 before rendering; scripts, <style>
  blocks, forms, frames and positioning CSS are removed.
- No network access: remote images are dropped during sanitizing, and the
  URL fetcher refuses everything except images carried inside the message
  itself (cid: parts and small data: URIs).
"""

from __future__ import annotations

import hashlib
import html as htmlmod
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import nh3

from .mime import ParsedMessage, _normalize_cid

_SAFE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}  # no SVG
_DATA_IMAGE = re.compile(r"^data:(image/(?:png|jpeg|gif|webp));base64,", re.IGNORECASE)
MAX_INLINE_IMAGE_BYTES = 15 * 1024 * 1024
INTERNAL_SCHEME = "x-mail"

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "cite", "code", "col",
    "colgroup", "dd", "del", "div", "dl", "dt", "em", "font", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "i", "img", "ins", "li", "ol", "p", "pre", "q", "s", "small",
    "span", "strike", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "tt", "u", "ul",
}
_GENERIC = {"style", "align", "valign", "dir", "title", "lang"}
ALLOWED_ATTRIBUTES = {
    "*": _GENERIC,
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "table": {"width", "border", "cellpadding", "cellspacing", "bgcolor"},
    "td": {"colspan", "rowspan", "width", "bgcolor", "nowrap"},
    "th": {"colspan", "rowspan", "width", "bgcolor"},
    "tr": {"bgcolor"},
    "col": {"span", "width"},
    "font": {"color", "size", "face"},
    "ol": {"start", "type"},
    "ul": {"type"},
    "li": {"value"},
}
# CSS properties kept from inline styles. Positioning, display, sizing and
# backgrounds are dropped so a message cannot overlay or hide other content.
ALLOWED_STYLE_PROPERTIES = {
    "color", "background-color", "font", "font-family", "font-size", "font-style",
    "font-weight", "font-variant", "text-align", "text-decoration", "text-indent",
    "text-transform", "line-height", "letter-spacing", "word-spacing", "white-space",
    "vertical-align", "direction", "list-style-type",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border", "border-top", "border-right", "border-bottom", "border-left",
    "border-color", "border-style", "border-width", "border-collapse", "border-spacing",
}
CLEAN_CONTENT_TAGS = {"script", "style", "title", "noscript", "template", "textarea",
                      "select", "button", "iframe", "object", "embed", "svg", "math"}


@dataclass
class SanitizeStats:
    remote_images: int = 0
    inline_images: int = 0
    missing_inline: int = 0
    used_cids: set[str] = field(default_factory=set)


class InlineImages:
    """Registry of message-internal images, served to WeasyPrint by token."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, bytes]] = {}

    def add(self, ctype: str, data: bytes) -> str:
        token = f"{INTERNAL_SCHEME}:{len(self._items) + 1}"
        self._items[token] = (ctype, data)
        return token

    def get(self, url: str) -> tuple[str, bytes] | None:
        return self._items.get(url)


def sanitize_html(body: str, inline_parts: dict[str, tuple[str, bytes]],
                  images: InlineImages) -> tuple[str, SanitizeStats]:
    stats = SanitizeStats()

    def attribute_filter(tag: str, attr: str, value: str):
        v = value.strip()
        if tag == "img" and attr == "src":
            low = v.lower()
            if low.startswith("cid:"):
                cid = _normalize_cid(v)
                part = inline_parts.get(cid) if cid else None
                if part and part[0] in _SAFE_IMAGE_TYPES and 0 < len(part[1]) <= MAX_INLINE_IMAGE_BYTES:
                    stats.inline_images += 1
                    stats.used_cids.add(cid)
                    return images.add(*part)
                stats.missing_inline += 1
                return None
            if _DATA_IMAGE.match(v) and len(v) <= MAX_INLINE_IMAGE_BYTES * 4 // 3 + 100:
                stats.inline_images += 1
                return v
            if low.startswith(("http:", "https:", "//")):
                stats.remote_images += 1
            return None
        if tag == "a" and attr == "href":
            return v if v.lower().startswith(("http:", "https:", "mailto:")) else None
        return value

    clean = nh3.clean(
        body,
        tags=ALLOWED_TAGS,
        clean_content_tags=CLEAN_CONTENT_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=attribute_filter,
        url_schemes={"http", "https", "mailto", "cid", "data", INTERNAL_SCHEME},
        filter_style_properties=ALLOWED_STYLE_PROPERTIES,
        link_rel="noopener noreferrer",
    )
    return clean, stats


def text_to_html(text: str) -> str:
    """Plain text body: escaped, line breaks kept, quoted lines dimmed."""
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        esc = htmlmod.escape(line)
        lines.append(f'<span class="quote">{esc}</span>' if line.lstrip().startswith(">") else esc)
    return '<div class="plain">' + "\n".join(lines) + "</div>"


class OfflineFetcher:
    """WeasyPrint URL fetcher that never touches the network or disk."""

    def __init__(self, images: InlineImages):
        self.images = images
        self.refused: list[str] = []

    def __call__(self, url: str, *args, **kwargs):
        from weasyprint.urls import URLFetcherResponse

        item = self.images.get(url)
        if item is not None:
            ctype, data = item
            return URLFetcherResponse(url, body=data, headers={"Content-Type": ctype})
        m = _DATA_IMAGE.match(url)
        if m:
            import base64
            data = base64.b64decode(url[m.end():], validate=False)
            return URLFetcherResponse(url, body=data, headers={"Content-Type": m.group(1)})
        self.refused.append(url.split(":", 1)[0])
        raise ValueError("external resources are disabled")


# --------------------------------------------------------------------------
# Document assembly
# --------------------------------------------------------------------------

CSS = """
@page {
  size: letter;
  background: #fff;  /* opaque full page (html background stops at the margins) */
  margin: 0.75in 0.7in 0.8in 0.7in;
  @top-left { content: string(doctitle); font-size: 8pt; color: #777; }
  @top-right { content: string(msgdate); font-size: 8pt; color: #777; }
  @bottom-center { content: "Page " counter(page) " of " counter(pages); font-size: 8pt; color: #777; }
}
html { font-family: "Helvetica Neue", Helvetica, Arial, "PingFang SC", "Hiragino Sans",
       "Apple Color Emoji", sans-serif; font-size: 10.5pt; line-height: 1.4; color: #111; }
h1.doctitle { string-set: doctitle content(); font-size: 18pt; margin: 0 0 4pt; }
.meta { color: #555; margin: 0 0 14pt; }
table.toc { width: 100%; border-collapse: collapse; font-size: 9pt; }
table.toc th { text-align: left; border-bottom: 1px solid #999; padding: 3pt 4pt; }
table.toc td { border-bottom: 1px solid #e3e3e3; padding: 3pt 4pt; vertical-align: top; }
table.toc td.date { white-space: nowrap; width: 1%; }
table.toc td.pg { text-align: right; white-space: nowrap; width: 1%; }
table.toc a { color: inherit; text-decoration: none; }
table.toc a.pg::after { content: target-counter(attr(href), page); }

section.msg { break-before: page; }
h2.subject { font-size: 13pt; margin: 0 0 6pt; bookmark-level: 1; bookmark-label: content(); }
table.hdr td.date { string-set: msgdate content(); }  /* running header date */
table.hdr { width: 100%; border-collapse: collapse; background: #f3f4f6; border: 1px solid #d5d8dd;
            border-left: 4px solid #4a6fa5; font-size: 9.5pt; margin-bottom: 12pt; }
table.hdr th { text-align: right; vertical-align: top; color: #555; font-weight: 600;
               padding: 3pt 6pt; width: 1%; white-space: nowrap; }
table.hdr td { padding: 3pt 6pt; overflow-wrap: anywhere; }
.body { overflow-wrap: break-word; }
.body img { max-width: 100%; height: auto; }
.body table { max-width: 100%; }
.body pre { white-space: pre-wrap; }
.plain { white-space: pre-wrap; font-family: inherit; }
.plain .quote { color: #666; }
.box { margin-top: 12pt; padding: 6pt 8pt; border: 1px solid #ddd; font-size: 9pt; background: #fafafa; }
.box h3 { font-size: 9pt; margin: 0 0 3pt; }
.box ul { margin: 0; padding-left: 14pt; }
.notes { border-color: #e2c27a; background: #fdf8ec; }
.end { margin-top: 10pt; text-align: center; color: #999; font-size: 8pt; }
"""


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def format_date(m: ParsedMessage) -> str:
    if m.date is None:
        return "Unknown date"
    return m.date.strftime("%a, %d %b %Y %H:%M ") + m.date.strftime("%z")


def chronological(messages: list[ParsedMessage]) -> list[ParsedMessage]:
    """Oldest first by absolute (UTC) time; ties by folder, UID, then file name."""
    return sorted(messages, key=lambda m: (
        m.utc is None, m.utc.timestamp() if m.utc else 0.0,
        m.folder or "", m.uid or 0, m.source))


# Page geometry (points). Must agree with the @page margins in CSS.
PAGE_W, PAGE_H = 612.0, 792.0                     # US Letter
MARGIN_TOP, MARGIN_RIGHT, MARGIN_BOTTOM, MARGIN_LEFT = 54.0, 50.4, 57.6, 50.4
PDF_CAPTION_H = 18.0
# Area of a placeholder page that an attachment page is drawn into.
PDF_BOX = (MARGIN_LEFT, MARGIN_BOTTOM, PAGE_W - MARGIN_RIGHT, PAGE_H - MARGIN_TOP - PDF_CAPTION_H)

ATTACHMENT_CSS = """
.atts h3.atthead { font-size: 10pt; margin: 14pt 0 6pt; border-top: 1px solid #ccc; padding-top: 6pt; }
figure.attimg { margin: 8pt 0 12pt; break-inside: avoid; text-align: center; }
figure.attimg img { max-width: 100%; max-height: 8.4in; }
figure.attimg figcaption, .attcap { font-size: 8.5pt; color: #555; margin-top: 3pt; }
.atttext { border: 1px solid #ddd; padding: 6pt 8pt; margin: 6pt 0 12pt; font-size: 9pt; }
.atthtml { border: 1px solid #ddd; padding: 6pt 8pt; margin: 6pt 0 12pt; }
.pdfintro { margin: 8pt 0; font-size: 9.5pt; }
div.pdfpage { break-before: page; break-after: page; height: 640pt; }
div.pdfpage .pdfcap { font-size: 8pt; color: #777; height: 14pt; }
.saved li { margin-bottom: 2pt; }
.saved .why { color: #7a5b12; }
.saved .path { color: #777; font-family: Menlo, monospace; font-size: 8pt; }
a.file { color: #1f5fbf; text-decoration: underline; }
"""


@dataclass
class AttachmentRecord:
    """Per-attachment outcome; written to render-report.json (no message content)."""
    filename: str
    declared_type: str
    detected_type: str | None
    size: int
    sha256: str
    action: str                 # embedded-image | embedded-pdf | embedded-text | embedded-html | saved | skipped
    saved_as: str | None = None  # relative to the PDF's folder
    reason: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class RenderedSection:
    message: ParsedMessage
    remote_images: int = 0
    inline_images: int = 0
    attachments: list[AttachmentRecord] = field(default_factory=list)

    @property
    def listed_attachments(self) -> int:
        return len(self.attachments)

    def count(self, action: str) -> int:
        return sum(a.action == action for a in self.attachments)


@dataclass
class _Assembly:
    images: InlineImages = field(default_factory=InlineImages)
    pdf_jobs: list[tuple[str, bytes, int]] = field(default_factory=list)  # (anchor id, pdf, page)
    save_root: Path | None = None


def _msg_key(m: ParsedMessage) -> str:
    return f"{m.folder}:{m.uid}" if m.uid is not None else m.source


def file_link(rel_path: str) -> str:
    """Clickable link to a saved file, relative to the PDF's folder.

    Relative links keep working when the whole archive folder is moved. The
    path is percent-encoded (spaces, accents, apostrophes) as URIs require.
    """
    e = htmlmod.escape
    return f'<a class="file" href="{e(quote(rel_path))}">{e(rel_path)}</a>'


def _attachment_html(i: int, m: ParsedMessage, atts, asm: _Assembly,
                     sec: RenderedSection) -> str:
    from .attachments import plan_attachment, save_attachment

    e = htmlmod.escape
    shown, saved = [], []
    for k, a in enumerate(atts, 1):
        rec = AttachmentRecord(filename=a.filename, declared_type=a.content_type, detected_type=None,
                               size=a.size, sha256=hashlib.sha256(a.data).hexdigest(), action="saved")
        try:
            plan = plan_attachment(a.filename, a.content_type, a.data, a.charset)
        except Exception as exc:  # never let one attachment stop the render
            from .attachments import Plan
            plan = Plan("save", f"could not be processed ({exc.__class__.__name__})")
        rec.detected_type, rec.notes = plan.detected, list(plan.notes)
        cap = (f"{e(a.filename)} ({e(plan.detected or a.content_type)}, {_human(a.size)})"
               + "".join(f" — {e(n)}" for n in plan.notes))

        # Save first, so the PDF can link to the saved copy.
        link = ""
        if plan.action == "save" or plan.also_save:
            if plan.action == "save":
                rec.reason = plan.reason
            if asm.save_root is not None:
                default_ext = ".eml" if a.content_type == "message/rfc822" else plan.suggested_ext
                path = save_attachment(asm.save_root, m.year, a.filename, a.data, _msg_key(m),
                                       default_ext)
                rec.saved_as = path.relative_to(asm.save_root).as_posix()
                link = file_link(rec.saved_as)

        if plan.action == "image":
            token = asm.images.add(plan.image_type, plan.image_bytes)
            shown.append(f'<figure class="attimg"><img src="{token}" alt="{e(a.filename)}">'
                         f"<figcaption>Attachment: {cap}</figcaption></figure>")
            rec.action = "embedded-image"
        elif plan.action == "pdf":
            anchor = f"pdf-{i}-{k}"
            pages = "".join(
                f'<div class="pdfpage" id="{anchor}-{j}"><div class="pdfcap">Attachment: '
                f"{e(a.filename)} — page {j + 1} of {plan.pdf_pages}</div></div>"
                for j in range(plan.pdf_pages))
            shown.append(f'<p class="pdfintro">PDF attachment: {cap} — {plan.pdf_pages} page(s) follow.</p>'
                         + pages)
            asm.pdf_jobs += [(f"{anchor}-{j}", a.data, j) for j in range(plan.pdf_pages)]
            rec.action = "embedded-pdf"
        elif plan.action == "text":
            full = f" — full file: {link}" if link else ""
            shown.append(f'<div class="attcap">Attachment: {cap}{full}</div>'
                         f'<div class="atttext">{text_to_html(plan.text)}</div>')
            rec.action = "embedded-text"
        elif plan.action == "html":
            body, stats = sanitize_html(plan.text, {}, asm.images)
            extra = (f" — {stats.remote_images} remote image(s) not loaded" if stats.remote_images else "")
            shown.append(f'<div class="attcap">Attachment: {cap}{extra}</div>'
                         f'<div class="atthtml body">{body}</div>')
            rec.action = "embedded-html"
        elif plan.action == "skip":
            rec.action, rec.reason = "skipped", plan.reason

        if plan.action == "save":
            name = (f'<a class="file" href="{e(quote(rec.saved_as))}">{e(a.filename)}</a>'
                    if rec.saved_as else e(a.filename))
            where = f'<br><span class="path">saved as {link}</span>' if link else ""
            saved.append(f"<li>{name} ({e(plan.detected or a.content_type)}, "
                         f"{_human(a.size)}) — <span class='why'>{e(plan.reason)}</span>{where}</li>")
        sec.attachments.append(rec)

    if not shown and not saved:
        return ""
    out = ['<div class="atts">']
    if shown:
        out.append(f'<h3 class="atthead">Attachments shown below ({len(shown)})</h3>')
        out += shown
    if saved:
        # A PDF placeholder ends with a page break, so this box starts cleanly after it.
        out.append(f'<div class="box saved"><h3>Saved separately ({len(saved)})</h3><ul>'
                   + "".join(saved) + "</ul></div>")
    out.append("</div>")
    return "".join(out)


def _message_html(i: int, m: ParsedMessage, asm: _Assembly) -> tuple[str, RenderedSection]:
    e = htmlmod.escape
    sec = RenderedSection(m)
    notes = list(m.notes)
    if m.body_kind == "html":
        body, stats = sanitize_html(m.body, m.inline_parts, asm.images)
        sec.remote_images, sec.inline_images = stats.remote_images, stats.inline_images
        if stats.remote_images:
            notes.append(f"{stats.remote_images} remote image(s) were not loaded "
                         "(blocked for privacy; no network access while rendering).")
        if stats.missing_inline:
            notes.append(f"{stats.missing_inline} embedded image(s) could not be shown "
                         "(missing or unsupported format).")
        used = stats.used_cids
    elif m.body_kind == "text":
        body, used = text_to_html(m.body), set()
    else:
        body, used = "", set()
    if m.date_fallback:
        notes.append("The Date header was missing or invalid; this message is placed "
                     "using the date the server received it." if m.date else
                     "The Date header was missing or invalid.")

    rows = [("Date", format_date(m)), ("From", m.sender)]
    if m.to:
        rows.append(("To", m.to))
    if m.cc:
        rows.append(("Cc", m.cc))
    rows.append(("Subject", m.subject))
    hdr = "".join(f"<tr><th>{e(k)}</th><td{' class=date' if k == 'Date' else ''}>{e(v)}</td></tr>"
                  for k, v in rows)

    # Images already shown inside the HTML body are not repeated as attachments.
    atts = [a for a in m.attachments if not (a.content_id and a.content_id in used)]
    att_html = _attachment_html(i, m, atts, asm, sec)
    notes_html = ""
    if notes:
        notes_html = ('<div class="box notes"><h3>Notes</h3><ul>'
                      + "".join(f"<li>{e(n)}</li>" for n in notes) + "</ul></div>")
    html = (f'<section class="msg" id="m{i}">'
            f'<h2 class="subject">{e(m.subject)}</h2>'
            f'<table class="hdr">{hdr}</table>'
            f'<div class="body">{body}</div>{att_html}{notes_html}'
            f'<div class="end">— end of message {i} —</div></section>')
    return html, sec


def build_document(messages: list[ParsedMessage], title: str, save_root: Path | None = None,
                   generated: datetime | None = None) -> tuple[str, _Assembly, list[RenderedSection]]:
    """Assemble the HTML. With ``save_root``, unsupported attachments are saved
    under save_root/attachments/<year>/; without it they are only listed."""
    e = htmlmod.escape
    ordered = chronological(messages)
    asm = _Assembly(save_root=save_root)
    parts, sections = [], []
    for i, m in enumerate(ordered, 1):
        html, sec = _message_html(i, m, asm)
        parts.append(html)
        sections.append(sec)

    dated = [m.date for m in ordered if m.date]
    span = (f"{dated[0]:%Y-%m-%d} to {dated[-1]:%Y-%m-%d}" if dated else "no dates")
    gen = (generated or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    toc_rows = "".join(
        f'<tr><td class="date">{e(m.date.strftime("%Y-%m-%d") if m.date else "undated")}</td>'
        f"<td>{e(m.sender)}</td>"
        f'<td><a href="#m{i}">{e(m.subject)}</a></td>'
        f'<td class="pg"><a class="pg" href="#m{i}"></a></td></tr>'
        for i, m in enumerate(ordered, 1))
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{e(title)}</title><style>{CSS}{ATTACHMENT_CSS}</style></head><body>"
        f'<h1 class="doctitle">{e(title)}</h1>'
        f'<p class="meta">{len(ordered)} message(s), {e(span)}. Generated {e(gen)}. '
        "Messages are in chronological order; each starts on a new page.</p>"
        '<table class="toc"><thead><tr><th>Date</th><th>From</th><th>Subject</th>'
        f"<th>Page</th></tr></thead><tbody>{toc_rows}</tbody></table>"
        + "".join(parts) + "</body></html>"
    )
    return doc, asm, sections


def render_pdf(messages: list[ParsedMessage], output: Path, title: str,
               verbose: bool = False) -> list[RenderedSection]:
    """Render to ``output`` atomically (temp file + rename, mode 0600).

    PDF attachments are drawn into placeholder pages after rendering, so page
    numbers and the table of contents already account for them.
    """
    import weasyprint

    from .attachments import merge_pdf_pages

    if not verbose:
        for name in ("weasyprint", "fontTools", "pypdf"):
            logging.getLogger(name).setLevel(logging.ERROR)
    output = output.resolve()
    doc, asm, sections = build_document(messages, title, save_root=output.parent)
    fetcher = OfflineFetcher(asm.images)
    document = weasyprint.HTML(string=doc, base_url=None, url_fetcher=fetcher).render()

    pdf_bytes = document.write_pdf()
    if asm.pdf_jobs:
        where = {}
        for index, page in enumerate(document.pages):
            for anchor in page.anchors:
                where.setdefault(anchor, index)
        jobs = []
        for anchor, data, page_no in asm.pdf_jobs:
            if anchor not in where:
                raise RuntimeError(f"internal error: placeholder {anchor} was not laid out")
            jobs.append((where[anchor], data, page_no))
        pdf_bytes = merge_pdf_pages(pdf_bytes, jobs, PDF_BOX)

    fd, tmp = tempfile.mkstemp(dir=output.parent, prefix=".partial-", suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(pdf_bytes)
        os.chmod(tmp, 0o600)
        os.replace(tmp, output)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return sections


def write_render_report(output: Path, sections: list[RenderedSection]) -> Path:
    """render-report.json beside the PDF: attachment outcomes, no message content."""
    import json
    from dataclasses import asdict

    doc = {
        "pdf": output.name,
        "generated_utc": datetime.now().astimezone().isoformat(timespec="seconds"),
        "messages": [{
            "source": s.message.source, "folder": s.message.folder, "uid": s.message.uid,
            "year": s.message.year, "remote_images_blocked": s.remote_images,
            "inline_images_shown": s.inline_images,
            "attachments": [asdict(a) for a in s.attachments],
        } for s in sections],
    }
    path = output.parent / (output.stem + "-report.json")
    fd, tmp = tempfile.mkstemp(dir=output.parent, prefix=".partial-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path
