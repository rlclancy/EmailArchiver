import unittest
from datetime import date

from email_archiver.folders import Folder
from email_archiver.preview import run_preview
from email_archiver.report import render_preview
from email_archiver.safe_imap import check_command


def hdr(date_value, sender, subject, mid=None):
    lines = [f"Date: {date_value}", f"From: {sender}", f"Subject: {subject}"]
    if mid:
        lines.append(f"Message-ID: {mid}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class FakeIMAP:
    """Minimal stand-in for ReadOnlyIMAP. SEARCH FROM is a substring match, like Yahoo."""

    def __init__(self, mailboxes):
        self.mailboxes = mailboxes  # quoted name -> (uidvalidity, {uid: (size, idate, header)})
        self.current = None
        self.commands = []

    def select(self, name, readonly=True):
        assert readonly
        self.commands.append(("EXAMINE", (name,)))
        check_command("EXAMINE", (name,))
        if name not in self.mailboxes:
            return "NO", [b"no such mailbox"]
        self.current = name
        return "OK", [str(len(self.mailboxes[name][1])).encode()]

    def response(self, code):
        return code, [str(self.mailboxes[self.current][0]).encode()]

    def uid(self, sub, *args):
        check_command("UID", (sub, *args))
        self.commands.append(("UID", (sub, *args)))
        msgs = self.mailboxes[self.current][1]
        if sub == "SEARCH":
            needle = args[1].strip('"').lower().encode()
            hits = [u for u, (_, _, h) in msgs.items() if needle in h.lower()]
            return "OK", [" ".join(map(str, hits)).encode()]
        if sub == "FETCH":
            data = []
            for u in map(int, args[0].split(",")):
                size, idate, h = msgs[u]
                data.append(((f'{u} (UID {u} RFC822.SIZE {size} INTERNALDATE "{idate}" '
                             f'BODY[HEADER.FIELDS (DATE FROM SUBJECT MESSAGE-ID)] {{{len(h)}}}').encode(), h))
                data.append(b")")
            return "OK", data
        raise AssertionError(sub)


ID = "01-Jan-2024 00:00:00 +0000"
INBOX = {
    1: (1000, ID, hdr("Sun, 31 Dec 2023 23:30:00 -0800", "Pat <pat@example.com>", "NYE", "<m1@x>")),
    2: (2000, ID, hdr("Mon, 15 Jan 2024 09:00:00 +0000", "pat@example.com", "Jan", "<m2@x>")),
    3: (500, ID, hdr("Mon, 15 Jan 2024 09:00:00 +0000", "notpat@example.com", "Other", "<m3@x>")),
    4: (300, "05-Mar-2022 12:00:00 +0000", hdr("garbage", "PAT@EXAMPLE.COM", "No date", "<m4@x>")),
}
ARCHIVE = {
    10: (2000, ID, hdr("Mon, 15 Jan 2024 09:00:00 +0000", "pat@example.com", "Jan", "<m2@x>")),
    11: (700, ID, hdr("Tue, 20 Jun 2023 09:00:00 +0200", "alt@example.org", "Alt", "<m5@x>")),
}


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeIMAP({'"Inbox"': (111, INBOX), '"Archive"': (222, ARCHIVE)})
        self.folders = [Folder("Inbox", "Inbox", frozenset(), "/"),
                        Folder("Archive", "Archive", frozenset(), "/")]

    def run_it(self, **kw):
        return run_preview(self.conn, self.folders, [], ["pat@example.com", "alt@example.org"], **kw)

    def test_counts_dedup_and_rejection(self):
        r = self.run_it()
        self.assertEqual(len(r.messages), 4)  # m1, m2, m4, m5
        self.assertEqual(r.duplicates, 1)
        self.assertEqual(r.scans[0].rejected, 1)  # notpat@ substring hit
        years = {y: s.count for y, s in r.by_year.items()}
        self.assertEqual(years, {2022: 1, 2023: 2, 2024: 1})
        self.assertEqual(r.by_year[2022].fallback, 1)
        m2 = next(m for m in r.messages if m.message_id == "<m2@x>")
        self.assertEqual((m2.folder, m2.also_in), ("Inbox", ["Archive"]))
        self.assertEqual(m2.uidvalidity, 111)
        self.assertEqual([m.message_id for m in r.messages], ["<m4@x>", "<m5@x>", "<m1@x>", "<m2@x>"])
        self.assertEqual([m.message_id for m in r.boundary_messages], ["<m1@x>"])

    def test_date_range(self):
        r = self.run_it(start=date(2023, 1, 1), end=date(2023, 12, 31))
        self.assertEqual({m.message_id for m in r.messages}, {"<m1@x>", "<m5@x>"})
        self.assertEqual(sum(s.out_of_range for s in r.scans), 3)

    def test_oversized_years(self):
        r = self.run_it(max_messages=1, max_bytes=10**9)
        self.assertEqual([y for y, _ in r.oversized_years], [2023])

    def test_only_read_only_commands_used(self):
        self.run_it()
        names = {c[0] for c in self.conn.commands} | {c[1][0] for c in self.conn.commands if c[0] == "UID"}
        self.assertEqual(names, {"EXAMINE", "UID", "SEARCH", "FETCH"})

    def test_missing_folder_reported(self):
        self.folders.append(Folder("Gone", "Gone", frozenset(), "/"))
        r = self.run_it()
        self.assertIn("could not open", r.scans[2].error)
        self.assertIn("FOLDER ERRORS", render_preview(r, "me@cox.net", "imap.mail.yahoo.com"))

    def test_report_renders(self):
        text = render_preview(self.run_it(), "me@cox.net", "imap.mail.yahoo.com")
        self.assertIn("Unique matching messages:", text)
        self.assertIn("2023", text)
        self.assertIn("No files were written", text)


if __name__ == "__main__":
    unittest.main()
