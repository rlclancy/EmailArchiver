"""Phase 3: render parsed messages into one searchable PDF with WeasyPrint.

Safety rules (design doc section 6):
- Untrusted HTML is sanitized with nh3 before rendering; scripts, <style>
  blocks, forms, frames and positioning CSS are removed.
- No network access: remote images are dropped during sanitizing, and the
  URL fetcher refuses everything except images carried inside the message
  itself (cid: parts and small data: URIs).
"""

from __future__ import annotations

import html as htmlmod
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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


@dataclass
class RenderedSection:
    message: ParsedMessage
    remote_images: int = 0
    inline_images: int = 0
    listed_attachments: int = 0


def _message_html(i: int, m: ParsedMessage, images: InlineImages) -> tuple[str, RenderedSection]:
    e = htmlmod.escape
    sec = RenderedSection(m)
    notes = list(m.notes)
    if m.body_kind == "html":
        body, stats = sanitize_html(m.body, m.inline_parts, images)
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

    atts = [a for a in m.attachments if not (a.content_id and a.content_id in used)]
    sec.listed_attachments = len(atts)
    att_html = ""
    if atts:
        items = "".join(f"<li>{e(a.filename)} <span style='color:#777'>({e(a.content_type)}, "
                        f"{_human(a.size)})</span></li>" for a in atts)
        att_html = (f'<div class="box"><h3>Attachments ({len(atts)}) — not yet included in this PDF</h3>'
                    f"<ul>{items}</ul></div>")
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


def build_document(messages: list[ParsedMessage], title: str,
                   generated: datetime | None = None) -> tuple[str, InlineImages, list[RenderedSection]]:
    e = htmlmod.escape
    ordered = chronological(messages)
    images = InlineImages()
    parts, sections = [], []
    for i, m in enumerate(ordered, 1):
        html, sec = _message_html(i, m, images)
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
        f"<title>{e(title)}</title><style>{CSS}</style></head><body>"
        f'<h1 class="doctitle">{e(title)}</h1>'
        f'<p class="meta">{len(ordered)} message(s), {e(span)}. Generated {e(gen)}. '
        "Messages are in chronological order; each starts on a new page.</p>"
        '<table class="toc"><thead><tr><th>Date</th><th>From</th><th>Subject</th>'
        f"<th>Page</th></tr></thead><tbody>{toc_rows}</tbody></table>"
        + "".join(parts) + "</body></html>"
    )
    return doc, images, sections


def render_pdf(messages: list[ParsedMessage], output: Path, title: str,
               verbose: bool = False) -> list[RenderedSection]:
    """Render to ``output`` atomically (temp file + rename, mode 0600)."""
    import weasyprint

    if not verbose:
        logging.getLogger("weasyprint").setLevel(logging.ERROR)
        logging.getLogger("fontTools").setLevel(logging.ERROR)
    doc, images, sections = build_document(messages, title)
    fetcher = OfflineFetcher(images)
    output = output.resolve()
    fd, tmp = tempfile.mkstemp(dir=output.parent, prefix=".partial-", suffix=".pdf")
    os.close(fd)
    try:
        weasyprint.HTML(string=doc, base_url=None, url_fetcher=fetcher).write_pdf(tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, output)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return sections
