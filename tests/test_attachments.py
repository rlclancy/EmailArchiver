import io
import json
import stat
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from PIL import Image

from email_archiver import attachments as A
from email_archiver.attachments import plan_attachment, safe_filename, save_attachment, sniff
from email_archiver.mime import load_messages
from email_archiver.render import render_pdf, write_render_report


def image_bytes(fmt, size=(40, 30), frames=1):
    imgs = [Image.new("RGB", size, (i * 60 % 255, 100, 150)) for i in range(frames)]
    out = io.BytesIO()
    if frames > 1:
        imgs[0].save(out, fmt, save_all=True, append_images=imgs[1:])
    else:
        imgs[0].save(out, fmt)
    return out.getvalue()


def pdf_bytes(pages=2, text="ATTACHED PDF PAGE", encrypt=False):
    import weasyprint
    from pypdf import PdfReader, PdfWriter
    html = "".join(f'<p style="break-before:page">{text} {i + 1}</p>' for i in range(pages))
    data = weasyprint.HTML(string=html).write_pdf()
    if encrypt:
        w = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
        w.encrypt("secret")
        out = io.BytesIO(); w.write(out); data = out.getvalue()
    return data


class SniffTests(unittest.TestCase):
    def test_magic_numbers(self):
        self.assertEqual(sniff(image_bytes("PNG")), "image/png")
        self.assertEqual(sniff(image_bytes("JPEG")), "image/jpeg")
        self.assertEqual(sniff(image_bytes("GIF")), "image/gif")
        self.assertEqual(sniff(image_bytes("TIFF")), "image/tiff")
        self.assertEqual(sniff(b"%PDF-1.4 ..."), "application/pdf")
        self.assertEqual(sniff(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest"), "application/x-ole-storage")
        self.assertEqual(sniff(b"PK\x03\x04rest"), "application/zip")
        self.assertEqual(sniff(b'<?xml version="1.0"?><svg xmlns="x"/>'), "image/svg+xml")
        self.assertEqual(sniff(b"\x00\x00\x00\x18ftypheic...."), "image/heic")
        self.assertIsNone(sniff(b"hello world"))


class PlanTests(unittest.TestCase):
    def test_images_embedded_even_with_generic_type(self):
        p = plan_attachment("Mail Attachment", "application/octet-stream", image_bytes("JPEG"))
        self.assertEqual((p.action, p.image_type), ("image", "image/jpeg"))

    def test_animated_gif_noted(self):
        p = plan_attachment("a.gif", "image/gif", image_bytes("GIF", frames=3))
        self.assertEqual(p.action, "image")
        self.assertTrue(any("animated" in n for n in p.notes))

    def test_tiff_converted_to_png(self):
        p = plan_attachment("scan.tif", "image/tiff", image_bytes("TIFF"))
        self.assertEqual((p.action, p.image_type, p.detected), ("image", "image/png", "image/tiff"))
        self.assertEqual(sniff(p.image_bytes), "image/png")

    def test_broken_image_saved(self):
        p = plan_attachment("x.jpg", "image/jpeg", b"\xff\xd8\xff" + b"garbage" * 10)
        self.assertEqual(p.action, "save")
        self.assertIn("could not be read", p.reason)

    def test_huge_image_saved(self):
        old = A.MAX_IMAGE_PIXELS
        A.MAX_IMAGE_PIXELS = 100
        try:
            p = plan_attachment("big.png", "image/png", image_bytes("PNG", (20, 20)))
        finally:
            A.MAX_IMAGE_PIXELS = old
        self.assertEqual(p.action, "save")
        self.assertIn("too large", p.reason)

    def test_pdf_ok_encrypted_broken_and_too_long(self):
        self.assertEqual(plan_attachment("a.pdf", "application/pdf", pdf_bytes(2)).pdf_pages, 2)
        enc = plan_attachment("e.pdf", "application/pdf", pdf_bytes(1, encrypt=True))
        self.assertEqual((enc.action, enc.reason), ("save", "encrypted PDF (not embedded)"))
        bad = plan_attachment("b.pdf", "application/pdf", b"%PDF-1.4\nnot really a pdf")
        self.assertEqual(bad.action, "save")
        old = A.MAX_PDF_PAGES
        A.MAX_PDF_PAGES = 1
        try:
            long = plan_attachment("l.pdf", "application/pdf", pdf_bytes(2))
        finally:
            A.MAX_PDF_PAGES = old
        self.assertIn("too long", long.reason)

    def test_unsupported_types_saved_with_reasons(self):
        cases = [
            ("pic.svg", "image/svg+xml", b"<svg xmlns='x'><script>x</script></svg>", "SVG"),
            ("deck.pps", "application/vnd.ms-powerpoint", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1x", "Office"),
            ("show.ppsx", "application/octet-stream", b"PK\x03\x04x", "Office"),
            ("files.zip", "application/zip", b"PK\x03\x04x", "archive"),
            ("clip.mp4", "video/mp4", b"\x00\x00\x00\x18ftypmp42xxxx", "video"),
            ("thing.bin", "application/octet-stream", b"\x01\x02\x00\x03", "unsupported"),
            ("x.php", "application/octet-stream", b"<?php echo 1; ?>", "unsupported"),
        ]
        for name, ctype, data, word in cases:
            with self.subTest(name=name):
                p = plan_attachment(name, ctype, data)
                self.assertEqual(p.action, "save")
                self.assertIn(word, p.reason)

    def test_text_and_html(self):
        t = plan_attachment("notes.txt", "text/plain", "mañana".encode("latin-1"), "iso-8859-1")
        self.assertEqual((t.action, t.text), ("text", "mañana"))
        h = plan_attachment("page.htm", "application/octet-stream", b"<p>hi</p>")
        self.assertEqual(h.action, "html")
        old = A.MAX_TEXT_CHARS
        A.MAX_TEXT_CHARS = 5
        try:
            long = plan_attachment("log.txt", "text/plain", b"0123456789")
        finally:
            A.MAX_TEXT_CHARS = old
        self.assertEqual((long.action, long.text, long.also_save), ("text", "01234", True))

    def test_unnamed_ole_identified_as_office_with_extension(self):
        ppt = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100 + "PowerPoint Document".encode("utf-16-le")
        p = plan_attachment("Mail Attachment", "application/octet-stream", ppt)
        self.assertEqual((p.action, p.detected, p.suggested_ext),
                         ("save", "application/vnd.ms-powerpoint", ".pps"))
        self.assertIn("Office", p.reason)
        doc = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + "WordDocument".encode("utf-16-le")
        self.assertEqual(plan_attachment("x", "application/octet-stream", doc).suggested_ext, ".doc")

    def test_empty_skipped(self):
        self.assertEqual(plan_attachment("e.txt", "text/plain", b"").action, "skip")


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_safe_filename(self):
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(safe_filename("..\\..\\boot.ini"), "boot.ini")
        self.assertEqual(safe_filename(".hidden"), "hidden")
        self.assertEqual(safe_filename('a<b>:c"d|e?.pdf'), "a_b__c_d_e_.pdf")
        self.assertEqual(safe_filename(""), "attachment")
        self.assertEqual(safe_filename("Theo's Blessing.wmv"), "Theo's Blessing.wmv")
        self.assertLessEqual(len(safe_filename("x" * 500 + ".pdf").encode()), 160)

    def test_save_idempotent_and_collisions_tagged(self):
        a = save_attachment(self.root, 2016, "deck.pps", b"one", "Inbox:1")
        again = save_attachment(self.root, 2016, "deck.pps", b"one", "Inbox:1")
        other = save_attachment(self.root, 2016, "deck.pps", b"two", "Inbox:2")
        self.assertEqual(a, again)
        self.assertEqual(a, self.root / "attachments" / "2016" / "deck.pps")
        self.assertNotEqual(a, other)
        self.assertRegex(other.name, r"^deck-[0-9a-f]{8}\.pps$")
        self.assertEqual(other.read_bytes(), b"two")
        self.assertEqual(stat.S_IMODE(a.stat().st_mode), 0o600)
        for d in (self.root / "attachments", self.root / "attachments" / "2016"):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700, d)

    def test_traversal_name_stays_inside(self):
        noext = save_attachment(self.root, 2015, "Mail Attachment", b"x", "k", ".pps")
        self.assertEqual(noext.name, "Mail Attachment.pps")
        p = save_attachment(self.root, None, "../../../evil.sh", b"x", "k")
        self.assertEqual(p.parent.resolve(), (self.root / "attachments" / "undated").resolve())


def message_with(*atts, subject="With attachments", html='<p>See attached</p>'):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Pat <pat@example.com>", "me@cox.net", subject
    m["Date"], m["Message-ID"] = "Tue, 3 Nov 2015 21:00:46 -0300", "<a@b>"
    m.set_content("See attached")
    m.add_alternative(html, subtype="html")
    for name, data, maintype, subtype in atts:
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m.as_bytes()


class RenderWithAttachmentsTests(unittest.TestCase):
    def test_full_render(self):
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "a.eml").write_bytes(message_with(
                ("photo.jpg", image_bytes("JPEG"), "image", "jpeg"),
                ("Mail Attachment", image_bytes("PNG"), "application", "octet-stream"),
                ("report.pdf", pdf_bytes(2), "application", "pdf"),
                ("notes.txt", "línea uno".encode(), "text", "plain"),
                ("page.html", b"<p>HTML ATTACHMENT</p><script>evil()</script>", "text", "html"),
                ("deck.pps", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1x", "application", "vnd.ms-powerpoint"),
                ("../../escape.mp4", b"\x00\x00\x00\x18ftypmp42xx", "video", "mp4"),
            ))
            (d / "b.eml").write_bytes(message_with(subject="Later message"))
            # b is later in time than a? give it a later date
            raw = (d / "b.eml").read_bytes().replace(b"Tue, 3 Nov 2015", b"Wed, 4 Nov 2015")
            (d / "b.eml").write_bytes(raw)
            msgs, errors = load_messages([d])
            self.assertEqual(errors, [])
            out = d / "out.pdf"
            sections = render_pdf(msgs, out, "Attachment Test")
            report = write_render_report(out, sections)

            reader = PdfReader(str(out))
            texts = [p.extract_text() for p in reader.pages]
            # TOC, message page(s), 2 placeholder pages, saved-box page, then message b
            pdf_pages = [i for i, t in enumerate(texts) if "ATTACHED PDF PAGE" in t]
            self.assertEqual(len(pdf_pages), 2)
            self.assertEqual(pdf_pages[1], pdf_pages[0] + 1)
            for i in pdf_pages:   # merged pages keep the archive's header and page numbers
                self.assertIn(f"Page {i + 1} of {len(reader.pages)}", texts[i])
                self.assertIn("report.pdf — page", texts[i])
            whole = " ".join(texts)
            for needle in ("línea uno", "HTML ATTACHMENT", "photo.jpg", "Mail Attachment",
                           "Saved separately (2)", "Office document", "video"):
                self.assertIn(needle, whole)
            self.assertNotIn("evil()", whole)

            # TOC page number for message b points at its real page after the inserted pages
            b_page = next(i for i, t in enumerate(texts) if "Later message" in t and i > 0) + 1
            self.assertRegex(texts[0], rf"Later message\s+{b_page}")

            sec = sections[0]
            actions = {a.filename: a.action for a in sec.attachments}
            self.assertEqual(actions, {
                "photo.jpg": "embedded-image", "Mail Attachment": "embedded-image",
                "report.pdf": "embedded-pdf", "notes.txt": "embedded-text",
                "page.html": "embedded-html", "deck.pps": "saved", "../../escape.mp4": "saved"})
            saved = sorted(p.relative_to(d).as_posix() for p in (d / "attachments").rglob("*") if p.is_file())
            self.assertEqual(saved, ["attachments/2015/deck.pps", "attachments/2015/escape.mp4"])

            doc = json.loads(report.read_text())
            self.assertNotIn("With attachments", report.read_text())   # no subjects
            atts = doc["messages"][0]["attachments"]
            self.assertEqual({a["saved_as"] for a in atts if a["saved_as"]},
                             {"attachments/2015/deck.pps", "attachments/2015/escape.mp4"})
            self.assertTrue(all(len(a["sha256"]) == 64 for a in atts))

            # Saved files are clickable relative links that resolve to the real file.
            from urllib.parse import unquote
            uris = {a.get_object()["/A"]["/URI"] for p in reader.pages for a in (p.get("/Annots") or [])
                    if "/URI" in a.get_object().get("/A", {})}
            file_uris = {u for u in uris if u.startswith("attachments/")}
            self.assertEqual({unquote(u) for u in file_uris},
                             {"attachments/2015/deck.pps", "attachments/2015/escape.mp4"})
            for u in file_uris:
                self.assertTrue((d / unquote(u)).is_file(), u)

    def test_link_encodes_spaces_accents_and_quotes(self):
        from email_archiver.render import file_link
        html = file_link("attachments/2016/despedida Arturo a mamá's.doc")
        self.assertIn('href="attachments/2016/despedida%20Arturo%20a%20mam%C3%A1%27s.doc"', html)
        self.assertIn(">attachments/2016/despedida Arturo a mamá&#x27;s.doc<", html)


if __name__ == "__main__":
    unittest.main()
