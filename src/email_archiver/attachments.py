"""Phase 4: decide how each attachment is shown, convert safely, save the rest.

Attachment names and bytes are untrusted. Types are detected from the bytes
(magic numbers) first; the declared MIME type and file extension are hints.
Nothing is executed, no archive is extracted, and nothing is fetched.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

MAX_PDF_PAGES = 100            # per attachment; longer PDFs are saved separately
MAX_IMAGE_PIXELS = 60_000_000  # ~60 MP; larger images are saved separately
MAX_TEXT_CHARS = 200_000       # longer text attachments are truncated in the PDF
SIPS = "/usr/bin/sips"         # macOS built-in image converter (for HEIC)

OFFICE_EXTS = {"doc", "docx", "dot", "dotx", "rtf", "odt", "xls", "xlsx", "xlsm",
               "ods", "ppt", "pptx", "pps", "ppsx", "pot", "potx", "odp", "pages", "numbers", "key"}
ARCHIVE_EXTS = {"zip", "rar", "7z", "gz", "tgz", "bz2", "xz", "tar", "dmg", "iso"}
TEXT_EXTS = {"txt", "text", "log", "csv", "md", "ics", "vcf"}
HTML_EXTS = {"htm", "html"}


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def sniff(data: bytes) -> str | None:
    """Detect a type from leading bytes. Returns a MIME type or None."""
    head = data[:64]
    if head.startswith(b"%PDF-") or b"%PDF-" in data[:1024]:
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis",
                                                 b"mif1", b"msf1"):
        return "image/heic"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if head[:2] == b"BM" and len(data) > 26:
        return "image/bmp"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/x-ole-storage"   # legacy Office (doc/xls/ppt/pps)
    if head.startswith(b"PK\x03\x04"):
        return "application/zip"             # also docx/xlsx/pptx
    if head.startswith(b"0&\xb2u\x8ef\xcf\x11"):
        return "video/x-ms-asf"              # wmv/wma
    text_head = data[:512].lstrip().lower()
    if text_head.startswith(b"<svg") or (text_head.startswith(b"<?xml") and b"<svg" in data[:2048].lower()):
        return "image/svg+xml"
    return None


_EXT_FOR_TYPE = {
    "application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/heic": ".heic", "image/tiff": ".tif", "image/bmp": ".bmp",
    "image/svg+xml": ".svg", "application/zip": ".zip", "video/mp4": ".mp4",
    "video/x-ms-asf": ".wmv", "text/plain": ".txt", "text/html": ".html",
    "application/vnd.ms-powerpoint": ".pps", "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
}


def ole_kind(data: bytes) -> str | None:
    """Which legacy Office format an OLE file holds, from its stream names."""
    names = (("PowerPoint Document", "application/vnd.ms-powerpoint"),
             ("WordDocument", "application/msword"),
             ("Workbook", "application/vnd.ms-excel"), ("Book", "application/vnd.ms-excel"))
    for name, mime in names:
        if name.encode("utf-16-le") in data:
            return mime
    return None


def _ext(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


@dataclass
class Plan:
    action: str                # image | pdf | text | html | save | skip
    reason: str = ""           # why saved separately (shown in the PDF)
    detected: str | None = None
    image_type: str | None = None
    image_bytes: bytes | None = None
    pdf_pages: int = 0
    text: str | None = None
    also_save: bool = False    # shown (partly) in the PDF and also saved in full
    suggested_ext: str = ""    # used when a saved file's name has no extension
    notes: list[str] = field(default_factory=list)


def _validate_image(data: bytes, mime: str) -> Plan:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            frames = getattr(im, "n_frames", 1)
            if w * h > MAX_IMAGE_PIXELS:
                return Plan("save", f"image too large to embed ({w}×{h} pixels)", mime)
            im.verify()
    except Exception as exc:
        return Plan("save", f"image could not be read ({exc.__class__.__name__})", mime)
    plan = Plan("image", detected=mime, image_type=mime, image_bytes=data)
    if frames > 1:
        plan.notes.append(f"animated image ({frames} frames); first frame shown")
    return plan


def _convert_with_pillow(data: bytes, mime: str) -> Plan:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if w * h > MAX_IMAGE_PIXELS:
                return Plan("save", f"image too large to embed ({w}×{h} pixels)", mime)
            out = io.BytesIO()
            im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(out, "PNG")
    except Exception as exc:
        return Plan("save", f"image could not be converted ({exc.__class__.__name__})", mime)
    plan = _validate_image(out.getvalue(), "image/png")
    plan.detected = mime
    plan.notes.append(f"converted from {mime.split('/')[1].upper()} to PNG for display")
    return plan


def _convert_heic(data: bytes) -> Plan:
    if not os.path.exists(SIPS):
        return Plan("save", "HEIC photo (no local converter available)", "image/heic")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp, "in.heic"), Path(tmp, "out.jpg")
        src.write_bytes(data)
        try:
            subprocess.run([SIPS, "-s", "format", "jpeg", str(src), "--out", str(dst)],
                           check=True, capture_output=True, timeout=120)
            converted = dst.read_bytes()
        except (subprocess.SubprocessError, OSError) as exc:
            return Plan("save", f"HEIC photo could not be converted ({exc.__class__.__name__})",
                        "image/heic")
    plan = _validate_image(converted, "image/jpeg")
    plan.detected = "image/heic"
    plan.notes.append("converted from HEIC to JPEG for display")
    return plan


def _plan_pdf(data: bytes) -> Plan:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return Plan("save", "encrypted PDF (not embedded)", "application/pdf")
        n = len(reader.pages)
        for page in reader.pages[: min(n, 3)]:   # touch a few pages to catch damage early
            _ = page.mediabox
    except Exception as exc:
        return Plan("save", f"PDF could not be read ({exc.__class__.__name__})", "application/pdf")
    if n == 0:
        return Plan("save", "PDF has no pages", "application/pdf")
    if n > MAX_PDF_PAGES:
        return Plan("save", f"PDF too long to embed ({n} pages; limit {MAX_PDF_PAGES})",
                    "application/pdf")
    return Plan("pdf", detected="application/pdf", pdf_pages=n)


def _decode_text(data: bytes, charset: str | None) -> tuple[str, list[str]]:
    notes = []
    for enc in [charset, "utf-8"] if charset else ["utf-8"]:
        try:
            return data.decode(enc), notes
        except (LookupError, UnicodeDecodeError):
            continue
    notes.append("text encoding unknown; shown as Latin-1")
    return data.decode("latin-1"), notes


def plan_attachment(filename: str, declared: str, data: bytes, charset: str | None = None) -> Plan:
    declared = (declared or "application/octet-stream").lower()
    ext = _ext(filename)
    if not data:
        return Plan("skip", "empty attachment")
    detected = sniff(data)
    if detected == "application/x-ole-storage":
        detected = ole_kind(data) or detected
    plan = _plan(filename, declared, ext, data, detected, charset)
    if not plan.suggested_ext:
        plan.suggested_ext = _EXT_FOR_TYPE.get(plan.detected or detected or declared, "")
    return plan


def _plan(filename: str, declared: str, ext: str, data: bytes, detected: str | None,
          charset: str | None) -> Plan:
    if detected == "application/pdf":
        return _plan_pdf(data)
    if detected in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        return _validate_image(data, detected)
    if detected in ("image/tiff", "image/bmp"):
        return _convert_with_pillow(data, detected)
    if detected == "image/heic":
        return _convert_heic(data)
    if detected == "image/svg+xml" or declared == "image/svg+xml" or ext == "svg":
        return Plan("save", "SVG image (not rendered: can contain scripts and remote links)",
                    "image/svg+xml")

    office = (ext in OFFICE_EXTS or "officedocument" in declared or declared.startswith(
        ("application/msword", "application/vnd.ms-", "application/vnd.oasis.opendocument"))
        or detected in ("application/x-ole-storage", "application/msword",
                        "application/vnd.ms-powerpoint", "application/vnd.ms-excel"))
    if office:
        return Plan("save", "Office document (not converted in this version)", detected or declared)
    if detected == "application/zip" or ext in ARCHIVE_EXTS or declared in (
            "application/zip", "application/x-zip-compressed", "application/x-rar-compressed",
            "application/x-7z-compressed", "application/gzip"):
        return Plan("save", "archive (not extracted)", detected or declared)
    if declared.startswith("video/") or (detected or "").startswith("video/"):
        return Plan("save", "video (cannot be shown in a PDF)", detected or declared)
    if declared.startswith("audio/"):
        return Plan("save", "audio (cannot be shown in a PDF)", declared)
    if declared == "message/rfc822":
        return Plan("save", "attached email (saved as .eml)", declared)

    binary = b"\x00" in data[:4096]
    if not binary and (declared == "text/html" or ext in HTML_EXTS):
        text, notes = _decode_text(data, charset)
        return Plan("html", detected="text/html", text=text, notes=notes)
    if not binary and (declared.startswith("text/") or ext in TEXT_EXTS):
        text, notes = _decode_text(data, charset)
        if len(text) > MAX_TEXT_CHARS:
            notes.append(f"long text: first {MAX_TEXT_CHARS:,} of {len(text):,} characters shown; "
                         "full file saved separately")
            return Plan("text", detected="text/plain", text=text[:MAX_TEXT_CHARS],
                        also_save=True, notes=notes)
        return Plan("text", detected="text/plain", text=text, notes=notes)

    return Plan("save", f"unsupported type ({detected or declared})", detected or declared)


# --------------------------------------------------------------------------
# Saving separately
# --------------------------------------------------------------------------

_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


def safe_filename(name: str, fallback: str = "attachment") -> str:
    """A single path component safe on macOS: no separators, controls or dot-files."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _BAD_CHARS.sub("_", name).strip(" .") or fallback
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext = ext[:10]
    stem = stem.encode("utf-8")[:150].decode("utf-8", "ignore").strip(" .") or fallback
    return f"{stem}.{ext}" if ext else stem


def save_attachment(root: Path, year: int | None, filename: str, data: bytes, msg_key: str,
                    default_ext: str = "") -> Path:
    """Save under root/attachments/<year>/ and return the path.

    Same name + same bytes reuses the existing file (re-running is idempotent).
    A different file with the same name gets a short message identifier added.
    """
    base = root / "attachments"
    folder = base / (str(year) if year else "undated")
    for d in (base, folder):          # mkdir's mode applies only to the leaf; set both
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
    name = safe_filename(filename)
    if default_ext and "." not in name:
        name += default_ext
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    tag = hashlib.sha256(msg_key.encode()).hexdigest()[:8]
    candidates = [name, f"{stem}-{tag}{dot}{ext}"] + [f"{stem}-{tag}-{i}{dot}{ext}" for i in range(2, 100)]
    for cand in candidates:
        target = folder / cand
        if target.resolve().parent != folder.resolve():
            raise ValueError(f"unsafe attachment name: {filename!r}")
        if target.exists():
            if target.read_bytes() == data:
                return target
            continue
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".partial-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        return target
    raise OSError(f"too many attachments named {name!r}")


# --------------------------------------------------------------------------
# Merging PDF attachment pages into placeholder pages
# --------------------------------------------------------------------------

def merge_pdf_pages(pdf_bytes: bytes, jobs: list[tuple[int, bytes, int]],
                    box: tuple[float, float, float, float]) -> bytes:
    """Draw attachment pages into the rendered document's placeholder pages.

    jobs: (target page index, attachment PDF bytes, attachment page index).
    box: (x0, y0, x1, y1) area of each placeholder page to fill, in points.
    Only page content is copied — not the attachment's scripts, forms or links.
    """
    from pypdf import PdfReader, PdfWriter, Transformation

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes)))
    # Attachment pages are opened through a writer too: pypdf 7 only allows
    # content changes (like normalizing rotation) on pages owned by a writer.
    sources: dict[int, PdfWriter] = {}
    x0, y0, x1, y1 = box
    for target, data, index in jobs:
        if id(data) not in sources:
            sources[id(data)] = PdfWriter(clone_from=PdfReader(io.BytesIO(data), strict=False))
        page = sources[id(data)].pages[index]
        page.transfer_rotation_to_content()
        src = page.cropbox
        w, h = float(src.width), float(src.height)
        if w <= 0 or h <= 0:
            continue
        scale = min((x1 - x0) / w, (y1 - y0) / h)
        dx = x0 + ((x1 - x0) - w * scale) / 2
        dy = y1 - h * scale  # top-align under the caption
        t = (Transformation().translate(-float(src.left), -float(src.bottom))
             .scale(scale, scale).translate(dx, dy))
        writer.pages[target].merge_transformed_page(page, t)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
