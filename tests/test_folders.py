import unittest

from email_archiver.folders import decode_mutf7, parse_list_response, select_folders

LIST_DATA = [
    b'(\\HasNoChildren) "/" "Inbox"',
    b'(\\HasNoChildren \\Junk) "/" "Bulk"',
    b'(\\HasNoChildren \\Trash) "/" "Trash"',
    b'(\\HasNoChildren \\Sent) "/" "Sent"',
    b'(\\HasChildren \\Noselect) "/" "Projects"',
    b'(\\HasNoChildren) "/" "Projects/Family \\"Old\\""',
    (b'(\\HasNoChildren) "/" {11}', b'Receipts 23'),
    b'(\\HasNoChildren) "/" "Caf&AOk-"',
    b'(\\HasNoChildren) "/" "Deleted Items"',
]


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.folders = parse_list_response(LIST_DATA)

    def test_parse(self):
        names = [f.name for f in self.folders]
        self.assertIn('Projects/Family "Old"', names)
        self.assertIn("Receipts 23", names)
        self.assertIn("Café", names)
        bulk = next(f for f in self.folders if f.name == "Bulk")
        self.assertIn("\\junk", bulk.flags)
        self.assertEqual(bulk.delimiter, "/")

    def test_mutf7(self):
        self.assertEqual(decode_mutf7("Caf&AOk-"), "Café")
        self.assertEqual(decode_mutf7("A&-B"), "A&B")
        self.assertEqual(decode_mutf7("&ZeVnLIqe-"), "日本語")

    def test_default_selection_excludes_spam_trash(self):
        selected, excluded = select_folders(self.folders)
        self.assertEqual(selected[0].name, "Inbox")
        sel = {f.name for f in selected}
        self.assertNotIn("Bulk", sel)
        self.assertNotIn("Trash", sel)
        self.assertNotIn("Deleted Items", sel)
        self.assertNotIn("Projects", sel)
        self.assertIn("Sent", sel)
        reasons = {f.name: r for f, r in excluded}
        self.assertEqual(reasons["Bulk"], "Spam (\\Junk attribute)")
        self.assertEqual(reasons["Deleted Items"], "Trash (name match)")

    def test_explicit_include(self):
        selected, _ = select_folders(self.folders, include=["Bulk", "Inbox"])
        self.assertEqual([f.name for f in selected], ["Inbox", "Bulk"])
        with self.assertRaises(ValueError):
            select_folders(self.folders, include=["Nope"])

    def test_extra_excluded(self):
        selected, _ = select_folders(self.folders, extra_excluded=["sent"])
        self.assertNotIn("Sent", {f.name for f in selected})


if __name__ == "__main__":
    unittest.main()
