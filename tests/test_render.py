import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from email_archiver.mime import load_messages, parse_eml
from email_archiver.render import (
    InlineImages, OfflineFetcher, build_document, chronological, render_pdf, sanitize_html,
    text_to_html,
)

PNG = base64.b64decode(  # 1x1 transparent PNG
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def eml(subject="Hi", date="Tue, 3 Nov 2015 21:00:46 -0300", text=None, html=None,
        attach=None, inline_png_cid=None, sender="Pat <pat@example.com>", charset="utf-8"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "me@cox.net", subject
    if date:
        m["Date"] = date
    m["Message-ID"] = "<x@y>"
    if text is not None:
        m.set_content(text, charset=charset)
    if html is not None:
        if text is None:
            m.set_content(html, subtype="html")
        else:
            m.add_alternative(html, subtype="html")
    if inline_png_cid:
        m.get_body(("html",)).add_related(PNG, "image", "png", cid=f"<{inline_png_cid}>")
    if attach:
        name, data, maintype, subtype = attach
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m.as_bytes()


class ParseTests(unittest.TestCase):
    def test_keeps_header_timezone_for_year(self):
        p = parse_eml(eml(date="Thu, 31 Dec 2015 22:30:00 -0300"), "a.eml")
        self.assertEqual(p.year, 2015)              # UTC would say 2016
        self.assertEqual(p.utc.year, 2016)

    def test_prefers_html_over_text(self):
        p = parse_eml(eml(text="plain", html="<p>rich</p>"), "a.eml")
        self.assertEqual(p.body_kind, "html")
        self.assertEqual(p.attachments, [])         # the plain alternative is not an attachment

    def test_text_only_and_html_only(self):
        self.assertEqual(parse_eml(eml(text="hola ñ"), "a").body_kind, "text")
        self.assertEqual(parse_eml(eml(html="<b>x</b>"), "a").body_kind, "html")

    def test_decodes_encoded_headers_and_charsets(self):
        p = parse_eml(eml(subject="Café con leche ☕", text="mañana", charset="iso-8859-1"), "a")
        self.assertEqual(p.subject, "Café con leche ☕")
        self.assertIn("mañana", p.body)

    def test_attachments_and_inline_images(self):
        p = parse_eml(eml(text="t", html='<img src="cid:pic1">', inline_png_cid="pic1",
                          attach=("clip.wmv", b"x" * 300, "video", "x-ms-wmv")), "a")
        self.assertIn("pic1", p.inline_parts)
        names = {a.filename: a for a in p.attachments}
        self.assertEqual(names["clip.wmv"].size, 300)

    def test_missing_date_and_unknown_charset_do_not_crash(self):
        raw = (b"From: a@b.com\r\nSubject: x\r\nContent-Type: text/plain; charset=x-bogus\r\n"
               b"\r\nhello\r\n")
        p = parse_eml(raw, "a")
        self.assertTrue(p.date_fallback)
        self.assertIn("hello", p.body)
        self.assertTrue(any("encoding" in n for n in p.notes))

    def test_attached_message_not_descended(self):
        inner = EmailMessage(); inner["Subject"] = "Inner"; inner.set_content("secret body")
        outer = EmailMessage(); outer["From"] = "a@b.com"; outer["Subject"] = "Outer"
        outer.set_content("see attached"); outer.add_attachment(inner)
        p = parse_eml(outer.as_bytes(), "a")
        self.assertEqual(p.body.strip(), "see attached")
        self.assertEqual([a.content_type for a in p.attachments], ["message/rfc822"])
        self.assertIn("Inner", p.attachments[0].filename)


class SanitizeTests(unittest.TestCase):
    def clean(self, html, parts=None):
        return sanitize_html(html, parts or {}, InlineImages())

    def test_strips_active_and_dangerous_content(self):
        out, _ = self.clean('<script>alert(1)</script><style>p{}</style><p onclick="x()">ok</p>'
                            '<iframe src="https://e.com"></iframe><form><input></form>'
                            '<a href="javascript:x()">j</a><a href="https://ok.com">k</a>')
        for bad in ("script", "alert", "onclick", "iframe", "<style", "javascript", "<input", "<form"):
            self.assertNotIn(bad, out)
        self.assertIn('href="https://ok.com"', out)

    def test_remote_images_dropped_and_counted(self):
        out, st = self.clean('<img src="https://t.co/p.gif" width=1 height=1><img src="//x/y.png">')
        self.assertNotIn("http", out)
        self.assertNotIn("//x", out)
        self.assertEqual(st.remote_images, 2)

    def test_positioning_css_removed_but_colors_kept(self):
        out, _ = self.clean('<div style="position:fixed;top:0;color:red;'
                            'background-image:url(https://e.com/a.png)">x</div>')
        self.assertIn("color:red", out.replace(" ", ""))
        for bad in ("position", "background-image", "https"):
            self.assertNotIn(bad, out)

    def test_cid_image_mapped_to_internal_token(self):
        images = InlineImages()
        out, st = sanitize_html('<img src="cid:Pic1">', {"pic1": ("image/png", PNG)}, images)
        self.assertIn('src="x-mail:1"', out)
        self.assertEqual(st.inline_images, 1)
        self.assertEqual(images.get("x-mail:1"), ("image/png", PNG))

    def test_svg_and_unknown_cid_not_shown(self):
        out, st = self.clean('<img src="cid:a"><img src="cid:b">', {"a": ("image/svg+xml", b"<svg/>")})
        self.assertNotIn("src", out)
        self.assertEqual(st.missing_inline, 2)

    def test_text_body_is_escaped(self):
        out = text_to_html("<b>not bold</b>\n> quoted")
        self.assertIn("&lt;b&gt;", out)
        self.assertIn('class="quote"', out)


class FetcherTests(unittest.TestCase):
    def test_refuses_everything_but_message_images(self):
        images = InlineImages()
        token = images.add("image/png", PNG)
        f = OfflineFetcher(images)
        self.assertEqual(f(token).read(), PNG)
        self.assertEqual(f("data:image/png;base64," + base64.b64encode(PNG).decode()).read(), PNG)
        for url in ("https://example.com/a.png", "http://x/y", "file:///etc/passwd",
                    "data:text/html;base64,PGI+", "ftp://x/y"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                f(url)


class DocumentTests(unittest.TestCase):
    def test_chronological_by_utc_and_toc(self):
        # Local wall-clock order says b (Jan 1, 23:00) first; absolute time says a.
        a = parse_eml(eml(subject="First", date="Sat, 2 Jan 2016 01:00:00 +0000"), "a")
        b = parse_eml(eml(subject="Second", date="Fri, 1 Jan 2016 23:00:00 -0300"), "b")  # 02:00 UTC
        self.assertEqual([m.subject for m in chronological([b, a])], ["First", "Second"])
        doc, _, sections = build_document([b, a], "T")
        self.assertLess(doc.index("First"), doc.index("Second"))
        self.assertIn('href="#m1"', doc)
        self.assertEqual(len(sections), 2)


class RenderPdfTests(unittest.TestCase):
    def test_renders_searchable_pdf_offline(self):
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "one.eml").write_bytes(eml(subject="Plain one", text="Hola mañana\n> quoted"))
            (d / "two.eml").write_bytes(eml(
                subject="Rich two", date="Wed, 4 Nov 2015 10:00:00 +0000", text="alt",
                html='<p>Receta <b>café</b></p><img src="https://track.example/p.gif">'
                     '<img src="cid:pic">', inline_png_cid="pic",
                attach=("doc.docx", b"z" * 50, "application", "octet-stream")))
            msgs, errors = load_messages([d])
            self.assertEqual(errors, [])
            out = d / "out.pdf"
            sections = render_pdf(msgs, out, "Test Archive")
            reader = PdfReader(str(out))
            self.assertEqual(len(reader.pages), 3)  # TOC + one page per message
            text = " ".join(p.extract_text() for p in reader.pages)
            for needle in ("Test Archive", "Plain one", "Hola mañana", "Rich two", "café",
                           "doc.docx", "remote image"):
                self.assertIn(needle, text)
            rich = next(s for s in sections if s.message.subject == "Rich two")
            self.assertEqual((rich.inline_images, rich.remote_images, rich.listed_attachments), (1, 1, 1))
            self.assertEqual([p.name for p in d.iterdir() if p.name.startswith(".partial")], [])


if __name__ == "__main__":
    unittest.main()


class RobustnessTests(unittest.TestCase):
    """Regressions from the full export: one bad message must not sink a PDF."""

    def test_css_functions_stripped(self):
        from email_archiver.render import strip_css_functions
        self.assertEqual(strip_css_functions("margin-left:calc(( + 80px);color:red"), "color:red")
        self.assertEqual(strip_css_functions("color: rgb(1,2,3); width: var(--x)"), "color: rgb(1,2,3)")
        out, _ = sanitize_html('<p style="margin-left:calc(( + 80px);color:red">x</p>', {}, InlineImages())
        self.assertNotIn("calc", out)
        self.assertIn("color:red", out.replace(" ", ""))

    def test_deep_nesting_flattened_and_text_kept(self):
        from email_archiver.render import MAX_NESTING, limit_depth
        deep = "<blockquote>" * 200 + "deep text &amp; more" + "</blockquote>" * 200
        out, flattened = limit_depth(deep, MAX_NESTING)
        self.assertTrue(flattened)
        self.assertEqual(out.count("<blockquote>"), MAX_NESTING)
        self.assertEqual(out.count("</blockquote>"), MAX_NESTING)
        self.assertIn("deep text &amp; more", out)
        shallow = "<p>a</p>"
        self.assertEqual(limit_depth(shallow, MAX_NESTING), (shallow, False))

    def test_html_to_text(self):
        from email_archiver.render import html_to_text
        self.assertEqual(html_to_text("<p>Hola</p><style>x{}</style><div>mundo &amp; más</div>"),
                         "Hola\n\nmundo & más")

    def test_unrenderable_message_falls_back_to_plain_text(self):
        from pypdf import PdfReader
        from email_archiver import render as R
        deep = "<blockquote>" * 400 + "<p>Very deep reply</p>" + "</blockquote>" * 400
        bad = parse_eml(eml(subject="Deep", text="t", html=deep), "bad")
        good = parse_eml(eml(subject="Fine", date="Wed, 4 Nov 2015 10:00:00 +0000", text="ok body"), "good")
        old = R.MAX_NESTING
        R.MAX_NESTING = 100000          # disable flattening to reproduce the real failure
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "x.pdf"
                sections, issues = R.render_pdf_resilient([bad, good], out, "T")
                self.assertEqual([m.source for m in issues.plain], ["bad"])
                self.assertEqual(issues.excluded, [])
                self.assertEqual(len(sections), 2)
                text = " ".join(p.extract_text() for p in PdfReader(str(out)).pages)
                self.assertIn("Very deep reply", text)
                self.assertIn("could not be laid out", text)
                self.assertIn("ok body", text)
        finally:
            R.MAX_NESTING = old
