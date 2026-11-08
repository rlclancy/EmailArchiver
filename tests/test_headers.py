import unittest
from datetime import date, timezone

from email_archiver.headers import (
    FetchRecord, build_meta, parse_date_header, parse_fetch_response, parse_internaldate,
)


def header(date_value="Sun, 31 Dec 2023 23:30:00 -0800", sender="Pat <Pat@Example.com>",
           subject="Hello", mid="<abc@example.com>") -> bytes:
    lines = []
    if date_value is not None:
        lines.append(f"Date: {date_value}")
    lines.append(f"From: {sender}")
    lines.append(f"Subject: {subject}")
    if mid:
        lines.append(f"Message-ID: {mid}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class FetchParseTests(unittest.TestCase):
    def test_tuple_with_trailing_items(self):
        data = [
            (b'1 (RFC822.SIZE 1234 INTERNALDATE " 1-Jan-2024 07:30:00 +0000" '
             b'BODY[HEADER.FIELDS (DATE FROM SUBJECT MESSAGE-ID)] {20}', b"From: a@b.com\r\n\r\n"),
            b" UID 42)",
            (b'2 (UID 43 RFC822.SIZE 99 INTERNALDATE "02-Jan-2024 00:00:00 -0500" BODY[...] {5}', b"x\r\n\r\n"),
            b")",
        ]
        recs = parse_fetch_response(data)
        self.assertEqual([(r.uid, r.size) for r in recs], [(42, 1234), (43, 99)])
        self.assertEqual(recs[0].internaldate, " 1-Jan-2024 07:30:00 +0000")

    def test_response_without_literal(self):
        data = [b'3 (UID 7 RFC822.SIZE 10 INTERNALDATE "01-Feb-2020 10:00:00 +0000" BODY[HEADER.FIELDS (DATE)] "")']
        recs = parse_fetch_response(data)
        self.assertEqual(recs[0].uid, 7)
        self.assertEqual(recs[0].header, b"")


class DateTests(unittest.TestCase):
    def test_year_uses_header_timezone(self):
        dt = parse_date_header("Sun, 31 Dec 2023 23:30:00 -0800")
        self.assertEqual(dt.year, 2023)
        self.assertEqual(dt.astimezone(timezone.utc).year, 2024)

    def test_invalid_dates(self):
        self.assertIsNone(parse_date_header("not a date"))
        self.assertIsNone(parse_date_header(""))
        self.assertIsNone(parse_date_header("Thu, 01 Jan 1970 00:00:00 +0000"))
        self.assertIsNone(parse_date_header("Fri, 01 Jan 2100 00:00:00 +0000"))

    def test_internaldate(self):
        dt = parse_internaldate(" 7-Jul-2023 14:02:11 -0700")
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour), (2023, 7, 7, 14))
        self.assertEqual(dt.utcoffset().total_seconds(), -7 * 3600)
        self.assertIsNone(parse_internaldate("garbage"))


class MetaTests(unittest.TestCase):
    def meta(self, **kw):
        idate = kw.pop("idate", "01-Jan-2024 07:30:00 +0000")
        return build_meta(FetchRecord(uid=5, size=100, internaldate=idate, header=header(**kw)),
                          "Inbox", 999)

    def test_exact_match_case_insensitive(self):
        m = self.meta()
        self.assertTrue(m.matches({"pat@example.com"}))
        self.assertFalse(m.matches({"at@example.com"}))
        self.assertFalse(self.meta(sender="pat@example.com.evil.org").matches({"pat@example.com"}))

    def test_year_and_boundary(self):
        m = self.meta()
        self.assertEqual(m.year, 2023)
        self.assertFalse(m.used_fallback)
        self.assertTrue(m.near_year_boundary())
        self.assertFalse(self.meta(date_value="Mon, 10 Jul 2023 10:00:00 +0000").near_year_boundary())

    def test_fallback_to_internaldate(self):
        m = self.meta(date_value=None)
        self.assertTrue(m.used_fallback)
        self.assertEqual(m.year, 2024)

    def test_date_range_uses_header_calendar_date(self):
        m = self.meta()  # 2023-12-31 in its own zone
        self.assertTrue(m.in_range(date(2023, 1, 1), date(2023, 12, 31)))
        self.assertFalse(m.in_range(date(2024, 1, 1), None))

    def test_dedup_keys(self):
        self.assertEqual(self.meta().dedup_key, ("message-id", "<abc@example.com>"))
        self.assertEqual(self.meta(mid=None).dedup_key[0], "provisional")

    def test_encoded_subject(self):
        self.assertEqual(self.meta(subject="=?utf-8?q?Caf=C3=A9?=").subject, "Café")


if __name__ == "__main__":
    unittest.main()
