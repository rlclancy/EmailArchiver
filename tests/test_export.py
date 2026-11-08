import contextlib
import imaplib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from email_archiver import cli
from email_archiver.export import (
    CHECKPOINT_NAME, MANIFEST_NAME, Checkpoint, ExportError, Session, plan_parts, run_export,
)
from email_archiver.folders import Folder
from email_archiver.preview import run_preview
from email_archiver.safe_imap import check_command

SENDER = "pat@example.com"


def raw(uid, date, subject=None, sender=SENDER):
    return (f"Date: {date}\r\nFrom: Pat <{sender}>\r\nTo: me@cox.net\r\n"
            f"Subject: {subject or f'Message {uid}'}\r\nMessage-ID: <m{uid}@x>\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\nBody of message {uid}\r\n").encode()


def mailbox():
    """Two folders; 2015/2016 messages incl. a late-Dec-31 one that is 2016 in UTC."""
    inbox = {
        1: raw(1, "Mon, 2 Feb 2015 10:00:00 +0000"),
        2: raw(2, "Thu, 31 Dec 2015 22:30:00 -0300"),     # 2015 by its own timezone
        3: raw(3, "Fri, 15 Jan 2016 09:00:00 +0000"),
        4: raw(4, "Tue, 3 May 2016 09:00:00 +0000"),
        5: raw(5, "Tue, 3 May 2016 09:00:00 +0000", sender="other@example.com"),
    }
    archive = {10: raw(10, "Wed, 7 Sep 2016 12:00:00 +0000")}
    return {"INBOX": [7, inbox, {u: [] for u in inbox}], "Archive": [9, archive, {10: ["\\Seen"]}]}


class Server:
    """Shared server state so reconnects see the same mailbox."""

    def __init__(self):
        self.boxes = mailbox()
        self.fail_peek_once: set[int] = set()   # raise IMAP4.abort on first BODY.PEEK[] of these
        self.interrupt_on: int | None = None     # raise KeyboardInterrupt on BODY.PEEK[] of this uid
        self.seen_on_fetch: set[int] = set()     # simulate a server that marks these read
        self.connections = 0
        self.peeks: list[int] = []

    def connect(self, *a, **k):
        self.connections += 1
        return FakeConn(self)


class FakeConn:
    def __init__(self, server):
        self.s = server
        self.current = None

    def login(self, u, p):
        return "OK", [b""]

    def logout(self):
        pass

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Archive"']

    def select(self, name, readonly=True):
        assert readonly
        check_command("EXAMINE", (name,))
        self.current = name.strip('"')
        return "OK", [str(len(self.s.boxes[self.current][1])).encode()]

    def response(self, code):
        return code, [str(self.s.boxes[self.current][0]).encode()]

    def uid(self, sub, *a):
        check_command("UID", (sub, *a))
        uv, msgs, flags = self.s.boxes[self.current]
        if sub == "SEARCH":
            needle = a[1].strip('"').encode()
            return "OK", [" ".join(str(u) for u, r in msgs.items() if needle in r).encode()]
        items = a[-1]
        if items == "(UID FLAGS)":
            u = int(a[0])
            return "OK", [f"1 (UID {u} FLAGS ({' '.join(flags[u])}))".encode()]
        if items == "(UID BODY.PEEK[])":
            u = int(a[0])
            if u in self.s.fail_peek_once:
                self.s.fail_peek_once.discard(u)
                raise imaplib.IMAP4.abort("socket error: EOF")
            if self.s.interrupt_on == u:
                raise KeyboardInterrupt
            self.s.peeks.append(u)
            if u in self.s.seen_on_fetch and "\\Seen" not in flags[u]:
                flags[u].append("\\Seen")
            return "OK", [(f"1 (UID {u} BODY[] {{{len(msgs[u])}}}".encode(), msgs[u]), b")"]
        data = []  # header fetch for the search
        for u in map(int, a[0].split(",")):
            r = msgs[u]
            h = r.split(b"\r\n\r\n")[0] + b"\r\n\r\n"
            data += [((f'{u} (UID {u} RFC822.SIZE {len(r)} INTERNALDATE "01-Feb-2016 10:00:00 +0000" '
                      f'BODY[HEADER] {{{len(h)}}}').encode(), h), b")"]
        return "OK", data


FOLDERS = [Folder(name="INBOX", raw="INBOX", flags=frozenset(), delimiter="/"),
           Folder(name="Archive", raw="Archive", flags=frozenset(), delimiter="/")]


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "archive"
        self.server = Server()
        self.log: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, split_years=frozenset(), max_messages=1500, max_bytes=200 << 20):
        conn = self.server.connect()
        result = run_preview(conn, FOLDERS, [], [SENDER])
        ckpt = Checkpoint(self.out / CHECKPOINT_NAME, {"host": "h", "user": "u", "senders": [SENDER]})
        self.out.mkdir(exist_ok=True)
        parts = plan_parts(result.messages, set(split_years), max_messages, max_bytes)
        session = Session(conn, self.server.connect, {"INBOX": "INBOX", "Archive": "Archive"},
                          self.log.append, sleep=lambda s: None)
        res = run_export(session, result.messages, parts, self.out, ckpt, start=None, end=None,
                         manifest_info={"senders": [SENDER]}, progress=self.log.append)
        return res, session

    def test_full_export(self):
        from pypdf import PdfReader
        res, _ = self.run_once()
        self.assertEqual((res.total, res.exported, res.not_exported), (5, 5, []))
        self.assertEqual(sorted(p.part.filename for p in res.parts),
                         ["Cox-Sender-Archive-2015.pdf", "Cox-Sender-Archive-2016.pdf"])
        y2015 = next(p for p in res.parts if p.part.year == 2015)
        self.assertEqual(sorted(m.uid for m in y2015.part.messages), [1, 2])  # Dec 31 -0300 stays 2015
        # originals byte-for-byte, under messages/YYYY/
        for u, data in mailbox()["INBOX"][1].items():
            if u == 5:
                continue
            files = list((self.out / "messages").rglob(f"*uid{u}.eml"))
            self.assertEqual(len(files), 1, u)
            self.assertEqual(files[0].read_bytes(), data)
        self.assertTrue((self.out / "messages" / "2016").is_dir())
        text = " ".join(p.extract_text() for p in PdfReader(str(self.out / "Cox-Sender-Archive-2016.pdf")).pages)
        for n in (3, 4, 10):
            self.assertIn(f"Body of message {n}", text)
        man = json.loads((self.out / MANIFEST_NAME).read_text())
        self.assertTrue(man["complete"])
        self.assertEqual(man["totals"]["exported"], 5)
        self.assertEqual({m["status"] for m in man["messages"]}, {"exported"})
        self.assertNotIn("Message 1", json.dumps(man))       # no subjects
        self.assertNotIn("Body of message", json.dumps(man))  # no bodies

    def test_resume_after_interrupt_and_rerun_is_noop(self):
        self.server.interrupt_on = 3
        with self.assertRaises(KeyboardInterrupt):
            self.run_once()
        man = json.loads((self.out / MANIFEST_NAME).read_text())
        self.assertFalse(man["complete"])                    # manifest still written
        done = len(self.server.peeks)
        self.assertGreaterEqual(done, 1)

        self.server.interrupt_on = None
        self.server.peeks.clear()
        res, _ = self.run_once()
        self.assertEqual(res.fetch.reused, done)
        self.assertEqual(res.fetch.fetched, 5 - done)
        self.assertEqual(len(self.server.peeks), 5 - done)   # nothing downloaded twice
        self.assertEqual(res.exported, 5)

        self.server.peeks.clear()
        res, _ = self.run_once()
        self.assertEqual(self.server.peeks, [])
        self.assertEqual({p.status for p in res.parts}, {"up-to-date"})

    def test_tampered_eml_is_fetched_again(self):
        self.run_once()
        f = next((self.out / "messages").rglob("*uid1.eml"))
        f.write_bytes(b"corrupted")
        self.server.peeks.clear()
        res, _ = self.run_once()
        self.assertEqual(self.server.peeks, [1])
        self.assertEqual(f.read_bytes(), mailbox()["INBOX"][1][1])
        rendered = {p.part.filename: p.status for p in res.parts}
        self.assertEqual(rendered["Cox-Sender-Archive-2015.pdf"], "up-to-date")  # same bytes again

    def test_network_drop_reconnects_and_continues(self):
        self.server.fail_peek_once = {3}
        res, session = self.run_once()
        self.assertEqual(res.exported, 5)
        self.assertEqual(session.reconnects, 1)
        self.assertTrue(any("reconnecting" in line for line in self.log))

    def test_uidvalidity_change_stops_that_folder_only(self):
        conn = self.server.connect()
        result = run_preview(conn, FOLDERS, [], [SENDER])
        self.server.boxes["INBOX"][0] = 8   # server renumbered INBOX after the search
        ckpt = Checkpoint(self.out / CHECKPOINT_NAME, {"host": "h", "user": "u", "senders": [SENDER]})
        self.out.mkdir()
        parts = plan_parts(result.messages, set(), 1500, 200 << 20)
        session = Session(conn, self.server.connect, {"INBOX": "INBOX", "Archive": "Archive"},
                          self.log.append, sleep=lambda s: None)
        res = run_export(session, result.messages, parts, self.out, ckpt, start=None, end=None,
                         manifest_info=None, progress=self.log.append)
        self.assertEqual(res.exported, 1)                      # the Archive message
        self.assertEqual(len(res.not_exported), 4)
        self.assertTrue(all("UIDVALIDITY" in why for _, why in res.not_exported))
        self.assertEqual([u for u in self.server.peeks], [10])  # no stale INBOX UID was fetched

    def test_flag_change_is_reported_not_exported(self):
        self.server.seen_on_fetch = {4}
        res, _ = self.run_once()
        self.assertEqual(res.exported, 4)
        [(meta, why)] = res.not_exported
        self.assertEqual(meta.uid, 4)
        self.assertIn("flags changed", why)
        man = json.loads((self.out / MANIFEST_NAME).read_text())
        self.assertFalse(man["complete"])
        self.assertEqual([m["uid"] for m in man["messages"] if m["status"] == "failed"], [4])

    def test_monthly_split(self):
        res, _ = self.run_once(split_years={2016}, max_messages=1)
        names = sorted(p.part.filename for p in res.parts)
        self.assertEqual(names, ["Cox-Sender-Archive-2015.pdf", "Cox-Sender-Archive-2016-part-01.pdf",
                                 "Cox-Sender-Archive-2016-part-02.pdf", "Cox-Sender-Archive-2016-part-03.pdf"])
        p1 = next(p for p in res.parts if p.part.filename.endswith("part-01.pdf"))
        self.assertEqual((p1.part.months, p1.part.label), ((1, 1), "2016 part 1 of 3 (Jan)"))
        self.assertEqual(res.exported, 5)

    def test_checkpoint_refuses_other_sender_list(self):
        self.run_once()
        with self.assertRaises(ExportError):
            Checkpoint(self.out / CHECKPOINT_NAME, {"host": "h", "user": "u", "senders": ["x@y.com"]})


class ExportCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "archive"
        self.server = Server()
        self._connect = cli.connect
        cli.connect = self.server.connect
        os.environ[cli.PASSWORD_ENV_VAR] = "pw"

    def tearDown(self):
        cli.connect = self._connect
        os.environ.pop(cli.PASSWORD_ENV_VAR, None)
        self.tmp.cleanup()

    def run_cli(self, *extra):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = cli.main(["export", "--user", "me@cox.net", "--sender", SENDER,
                           "--output-dir", str(self.out), *extra])
        return rc, o.getvalue(), e.getvalue()

    def test_requires_yes_without_terminal(self):
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("--yes", err)
        self.assertFalse(self.out.exists())

    def test_limited_year_then_full_run(self):
        rc, out, err = self.run_cli("--yes", "--year", "2015")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("EXPORT RESULT", out)
        self.assertTrue((self.out / "Cox-Sender-Archive-2015.pdf").is_file())
        self.assertFalse((self.out / "Cox-Sender-Archive-2016.pdf").exists())

        rc, out, err = self.run_cli("--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Reused from an earlier run:           2", out)
        self.assertTrue((self.out / "Cox-Sender-Archive-2016.pdf").is_file())
        # the 2015 PDF is re-rendered once (its title changes from "limited run")
        man = json.loads((self.out / MANIFEST_NAME).read_text())
        self.assertEqual((man["totals"]["found"], man["totals"]["exported"]), (5, 5))

    def test_refuses_output_inside_git_repo(self):
        (Path(self.tmp.name) / ".git").mkdir()
        rc, _, err = self.run_cli("--yes")
        self.assertEqual(rc, 2)
        self.assertIn("git repository", err)


if __name__ == "__main__":
    unittest.main()
