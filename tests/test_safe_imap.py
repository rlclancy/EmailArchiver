import unittest

from email_archiver.safe_imap import MailboxSafetyError, ReadOnlyIMAP, check_command, imap_quote


class CheckCommandTests(unittest.TestCase):
    def test_allows_read_only_commands(self):
        for name in ("CAPABILITY", "LOGIN", "LIST", "EXAMINE", "NOOP", "LOGOUT"):
            check_command(name, ())
        check_command("UID", ("SEARCH", "FROM", '"a@b.com"'))
        check_command("UID", ("FETCH", "1:5",
                              "(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS (DATE FROM)])"))

    def test_blocks_mutating_commands(self):
        for name in ("SELECT", "STORE", "COPY", "MOVE", "APPEND", "EXPUNGE",
                     "DELETE", "RENAME", "CREATE", "CLOSE", "SUBSCRIBE"):
            with self.subTest(name=name), self.assertRaises(MailboxSafetyError):
                check_command(name, ())

    def test_blocks_mutating_uid_subcommands(self):
        for sub in ("STORE", "COPY", "MOVE", "EXPUNGE"):
            with self.subTest(sub=sub), self.assertRaises(MailboxSafetyError):
                check_command("UID", (sub, "1", "+FLAGS", "(\\Seen)"))

    def test_blocks_fetch_items_that_set_seen(self):
        for items in ("(BODY[])", "(BODY[TEXT])", "(RFC822)", "(RFC822.TEXT)",
                      "(RFC822.HEADER)", "(BINARY[1])", "(uid body[])"):
            with self.subTest(items=items), self.assertRaises(MailboxSafetyError):
                check_command("UID", ("FETCH", "1", items))

    def test_guard_is_wired_into_imaplib(self):
        conn = object.__new__(ReadOnlyIMAP)  # no network: guard fires before any I/O
        conn.state = "SELECTED"
        with self.assertRaises(MailboxSafetyError):
            conn._command("STORE", "1", "+FLAGS", "(\\Seen)")
        with self.assertRaises(MailboxSafetyError):
            conn.uid("FETCH", "1", "(BODY[])")
        with self.assertRaises(MailboxSafetyError):
            conn.select("INBOX", readonly=False)

    def test_quote(self):
        self.assertEqual(imap_quote('My "Mail"\\x'), '"My \\"Mail\\"\\\\x"')


if __name__ == "__main__":
    unittest.main()
