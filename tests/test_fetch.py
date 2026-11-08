import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from email_archiver.fetch import (
    OutputDirError, Pick, choose_sample, fetch_sample, parse_flags, parse_uid_spec,
    picks_from_specs, prepare_output_dir, sample_filename, write_atomic, write_run_metadata,
)
from email_archiver.headers import MessageMeta
from email_archiver.safe_imap import MailboxSafetyError, check_command


def meta(uid, when, size=1000, folder="Inbox", uidvalidity=7, mid=None,
         sender="pat@example.com"):
    return MessageMeta(
        folder=folder, uid=uid, uidvalidity=uidvalidity, size=size, internaldate=None,
        header_date=when, date_raw="", from_addresses=[sender], subject=f"s{uid}",
        message_id=mid or f"<m{uid}@x>",
    )


def raw_message(uid, sender="pat@example.com", mid=None, body="hello"):
    return (f"From: Pat <{sender}>\r\nTo: me@cox.net\r\nSubject: s{uid}\r\n"
            f"Message-ID: {mid or f'<m{uid}@x>'}\r\nDate: Mon, 1 Feb 2016 10:00:00 +0000\r\n"
            f"\r\n{body}\r\n").encode()


def dt(y, m=6, d=15, h=12, tz=0):
    return datetime(y, m, d, h, tzinfo=timezone(timedelta(hours=tz)))


class FakeFetchIMAP:
    """Stand-in for ReadOnlyIMAP covering EXAMINE, UID FETCH (FLAGS) and BODY.PEEK[]."""

    def __init__(self, folders, set_seen_on_fetch=False):
        # folders: raw name -> (uidvalidity, {uid: (flags list, raw bytes)})
        self.folders = folders
        self.set_seen_on_fetch = set_seen_on_fetch
        self.current = None
        self.commands = []

    def select(self, name, readonly=True):
        assert readonly
        check_command("EXAMINE", (name,))
        self.commands.append(("EXAMINE", name))
        key = name.strip('"')
        if key not in self.folders:
            return "NO", [b"no such mailbox"]
        self.current = key
        return "OK", [str(len(self.folders[key][1])).encode()]

    def response(self, code):
        return code, [str(self.folders[self.current][0]).encode()]

    def uid(self, sub, uid, items):
        check_command("UID", (sub, uid, items))
        self.commands.append(("UID", sub, uid, items))
        u = int(uid)
        msgs = self.folders[self.current][1]
        if u not in msgs:
            return "OK", [None]
        flags, raw = msgs[u]
        if items == "(UID FLAGS)":
            return "OK", [f"1 (UID {u} FLAGS ({' '.join(flags)}))".encode()]
        if items == "(UID BODY.PEEK[])":
            if self.set_seen_on_fetch and "\\Seen" not in flags:
                flags.append("\\Seen")
            return "OK", [(f"1 (UID {u} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        raise AssertionError(items)


class GuardTests(unittest.TestCase):
    def test_phase2_fetch_items_allowed(self):
        check_command("UID", ("FETCH", "5", "(UID FLAGS)"))
        check_command("UID", ("FETCH", "5", "(UID BODY.PEEK[])"))

    def test_marking_fetches_still_blocked(self):
        for items in ("(BODY[])", "(RFC822)", "(UID BODY[TEXT])", "(BINARY[1])"):
            with self.subTest(items=items), self.assertRaises(MailboxSafetyError):
                check_command("UID", ("FETCH", "5", items))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.msgs = [
            meta(1, dt(2015), size=500),
            meta(2, dt(2016, 3), size=9000),
            meta(3, dt(2016, 12, 31, 23, tz=-8)),   # year boundary
            meta(4, dt(2017), size=100),
            meta(5, dt(2018, 2)),
            meta(6, dt(2018, 5)),
            meta(7, dt(2018, 9)),
        ]

    def test_picks_varied_messages_in_chronological_order(self):
        picks = choose_sample(self.msgs, 5)
        reasons = {p.meta.uid: p.reason for p in picks}
        self.assertEqual(reasons[1], "oldest")
        self.assertEqual(reasons[7], "newest")
        self.assertEqual(reasons[2], "largest")
        self.assertEqual(reasons[4], "smallest")
        self.assertEqual(reasons[3], "year boundary")
        self.assertEqual([p.meta.uid for p in picks], [1, 2, 3, 4, 7])

    def test_count_limits_and_no_duplicates(self):
        self.assertEqual(len(choose_sample(self.msgs, 2)), 2)
        picks = choose_sample(self.msgs, 50)
        self.assertEqual(len(picks), len(self.msgs))
        self.assertEqual(len({p.meta.uid for p in picks}), len(self.msgs))

    def test_empty(self):
        self.assertEqual(choose_sample([], 5), [])

    def test_uid_specs(self):
        self.assertEqual(parse_uid_spec("Archive/CLUB: X:42"), ("Archive/CLUB: X", 42))
        for bad in ("Inbox", "Inbox:", ":5", "Inbox:abc"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_uid_spec(bad)
        picks = picks_from_specs(self.msgs, ["Inbox:5", "Inbox:2", "Inbox:5"])
        self.assertEqual([p.meta.uid for p in picks], [2, 5])
        with self.assertRaises(ValueError):
            picks_from_specs(self.msgs, ["Inbox:999"])


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_dir_created_private_and_unique(self):
        now = datetime(2026, 11, 8, 12, 0, 0)
        a = prepare_output_dir(self.root / "out", now)
        b = prepare_output_dir(self.root / "out", now)
        self.assertEqual(a, (self.root / "out" / "_samples" / "20261108-120000").resolve())
        self.assertNotEqual(a, b)
        self.assertEqual(stat.S_IMODE(a.stat().st_mode), 0o700)

    def test_refuses_git_repo(self):
        (self.root / "repo" / ".git").mkdir(parents=True)
        with self.assertRaises(OutputDirError):
            prepare_output_dir(self.root / "repo" / "archive")

    def test_filename_is_safe_and_has_no_subject(self):
        m = meta(42, dt(2016, 3, 4), folder="../Archive/CLUB DE LECTURA")
        name = sample_filename(m)
        self.assertEqual(name, "2016-03-04_Archive-CLUB-DE-LECTURA_uid42.eml")
        self.assertNotIn("/", name)

    def test_write_atomic_is_byte_exact_private_and_contained(self):
        data = b"line1\r\nline2\n\x00\xff"
        path = write_atomic(self.root, "x.eml", data)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.root.iterdir()], ["x.eml"])  # no temp left
        with self.assertRaises(OutputDirError):
            write_atomic(self.root, "../escape.eml", data)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, conn, metas, raw_names=None):
        picks = [Pick(m, "test") for m in metas]
        return fetch_sample(conn, picks, self.dir, raw_names or {"Inbox": "INBOX"})

    def test_saves_exact_bytes_and_flags_unchanged(self):
        raw1, raw2 = raw_message(1), raw_message(2, body="caf\xc3\xa9".encode("latin-1").decode("latin-1"))
        conn = FakeFetchIMAP({"INBOX": (7, {1: (["\\Seen"], raw1), 2: ([], raw2)})})
        outs = self._run(conn, [meta(1, dt(2016), size=len(raw1)), meta(2, dt(2017), size=len(raw2))])
        self.assertTrue(all(o.ok for o in outs), [o.problems for o in outs])
        self.assertEqual((self.dir / outs[0].file).read_bytes(), raw1)
        self.assertEqual((self.dir / outs[1].file).read_bytes(), raw2)
        self.assertEqual(outs[0].flags_before, ["\\Seen"])
        self.assertEqual(outs[1].flags_before, [])
        self.assertEqual(outs[1].flags_after, [])
        self.assertEqual(outs[0].warnings, [])
        # one EXAMINE for the folder; only read-only commands issued
        self.assertEqual([c for c in conn.commands if c[0] == "EXAMINE"], [("EXAMINE", '"INBOX"')])

    def test_flag_change_is_a_failure_and_nothing_saved(self):
        raw = raw_message(1)
        conn = FakeFetchIMAP({"INBOX": (7, {1: ([], raw)})}, set_seen_on_fetch=True)
        [o] = self._run(conn, [meta(1, dt(2016), size=len(raw))])
        self.assertFalse(o.ok)
        self.assertTrue(any("flags changed" in p for p in o.problems))
        self.assertIsNone(o.file)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_uidvalidity_change_fails_without_fetching(self):
        conn = FakeFetchIMAP({"INBOX": (8, {1: ([], raw_message(1))})})
        [o] = self._run(conn, [meta(1, dt(2016), uidvalidity=7)])
        self.assertFalse(o.ok)
        self.assertIn("UIDVALIDITY", o.problems[0])
        self.assertFalse(any(c[0] == "UID" for c in conn.commands))

    def test_wrong_message_identity_fails(self):
        conn = FakeFetchIMAP({"INBOX": (7, {1: ([], raw_message(1, mid="<other@x>"))})})
        [o] = self._run(conn, [meta(1, dt(2016))])
        self.assertFalse(o.ok)
        self.assertTrue(any("Message-ID" in p for p in o.problems))

    def test_missing_message_and_missing_folder(self):
        conn = FakeFetchIMAP({"INBOX": (7, {})})
        [o] = self._run(conn, [meta(1, dt(2016))])
        self.assertFalse(o.ok)
        [o2] = self._run(conn, [meta(2, dt(2016), folder="Gone")], {"Gone": "Gone"})
        self.assertEqual(o2.problems, ["could not open folder read-only"])

    def test_size_mismatch_is_a_warning_not_failure(self):
        raw = raw_message(1)
        conn = FakeFetchIMAP({"INBOX": (7, {1: ([], raw)})})
        [o] = self._run(conn, [meta(1, dt(2016), size=len(raw) + 10)])
        self.assertTrue(o.ok)
        self.assertEqual(len(o.warnings), 1)

    def test_uses_raw_folder_name_for_examine(self):
        raw = raw_message(1)
        conn = FakeFetchIMAP({"Archive/&AOk-t&AOk-": (7, {1: ([], raw)})})
        [o] = self._run(conn, [meta(1, dt(2016), folder="Archive/été", size=len(raw))],
                        {"Archive/été": "Archive/&AOk-t&AOk-"})
        self.assertTrue(o.ok, o.problems)

    def test_run_metadata_has_no_subjects_or_message_ids(self):
        raw = raw_message(1)
        conn = FakeFetchIMAP({"INBOX": (7, {1: ([], raw)})})
        outs = self._run(conn, [meta(1, dt(2016), size=len(raw))])
        path = write_run_metadata(self.dir, version="0.1.0", host="h", senders=["pat@example.com"],
                                  folders=["Inbox"], selection="automatic (1)",
                                  started=datetime.now(timezone.utc), outcomes=outs)
        text = path.read_text()
        doc = json.loads(text)
        self.assertEqual(doc["summary"], {"requested": 1, "saved": 1, "failed": 0, "flags_unchanged": 1})
        self.assertNotIn("s1", text.replace("sha256", ""))  # subject
        self.assertNotIn("<m1@x>", text)
        self.assertNotIn("hello", text)


class ParseFlagsTests(unittest.TestCase):
    def test_parses_and_sorts(self):
        data = [b"3 (UID 42 FLAGS (\\Seen \\Answered $Forwarded))"]
        self.assertEqual(parse_flags(data, 42), ["$Forwarded", "\\Answered", "\\Seen"])
        self.assertEqual(parse_flags([b"3 (UID 42 FLAGS ())"], 42), [])
        self.assertIsNone(parse_flags([b"3 (UID 43 FLAGS ())"], 42))


if __name__ == "__main__":
    unittest.main()
