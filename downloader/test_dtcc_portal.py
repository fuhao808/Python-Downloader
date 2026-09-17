from pathlib import Path
from datetime import date
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

import dtcc_portal as d


class DtccPortalTests(unittest.TestCase):
    def test_login_detection(self):
        self.assertTrue(
            d._looks_like_login(
                "https://portal.dtcc.com/pkmslogin.form",
                "text/html",
                b"<html></html>",
            )
        )
        self.assertTrue(
            d._looks_like_login(
                "https://portal.dtcc.com/",
                "text/html",
                b'<form action="/pkmslogin.form">',
            )
        )
        self.assertFalse(
            d._looks_like_login(
                "https://portal.dtcc.com/report.csv",
                "text/csv",
                b"a,b\n1,2\n",
            )
        )

    def test_filename_parsing_and_safety(self):
        self.assertEqual(
            d._response_filename(
                {"content-disposition": "attachment; filename*=UTF-8''daily%20file.csv"},
                "https://portal.dtcc.com/x",
                1,
            ),
            "daily file.csv",
        )
        self.assertEqual(
            d._response_filename({}, "https://portal.dtcc.com/a/report.zip", 1),
            "report.zip",
        )
        self.assertEqual(d._safe_filename("../../secret.txt"), "secret.txt")

    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            (tmp_path / "report.csv").write_text("old")
            self.assertEqual(d._unique_path(tmp_path, "report.csv").name, "report-2.csv")

    def test_rejects_unsafe_download_hosts(self):
        for url in (
            "http://portal.dtcc.com/file",
            "https://example.com/file",
            "file:///tmp/x",
        ):
            with self.subTest(url=url), self.assertRaises(Exception):
                d._dtcc_url(url)

    def test_accepts_dtcc_subdomain(self):
        self.assertEqual(
            d._dtcc_url("https://portal.dtcc.com/file"),
            "https://portal.dtcc.com/file",
        )

    def test_public_session_factory(self):
        client = d.open_dtcc_session(
            session_dir=".test-session",
            username="example-user",
            transport="requests",
        )
        self.assertIsInstance(client, d.DtccPortalSession)
        self.assertEqual(client.username, "example-user")
        self.assertEqual(client.transport, "requests")
        self.assertIsNone(client.session)

    def test_request_requires_open_context(self):
        client = d.open_dtcc_session(session_dir=".test-session")
        with self.assertRaises(d.DtccError):
            client.request("GET", "https://portal.dtcc.com/")

    def test_extracts_cash_and_mmi_exports_for_one_day(self):
        html = """
        <div id="overviewDDV_FD">
          <a href="getAllocationListFromOverview.do?requestFor=H&amp;participantId=&amp;stlmtDate=2026-09-15&amp;category=DV&amp;srchTypeCd=CPY-DIST-CASH&amp;eventGroupCode=D&amp;statusIdentifier=UA&amp;expectedCount=3">3</a>
          <a href="getAllocationListFromOverview.do?requestFor=H&amp;participantId=&amp;stlmtDate=2026-09-15&amp;category=DV&amp;srchTypeCd=CPY-DIST-CASH&amp;eventGroupCode=D&amp;statusIdentifier=1A&amp;expectedCount=2">2</a>
          <a href="getAllocationListFromOverview.do?requestFor=H&amp;participantId=&amp;stlmtDate=2026-09-15&amp;category=DV&amp;srchTypeCd=CPY-DIST-CASH&amp;eventGroupCode=D&amp;total=true&amp;statusIdentifier=UA&amp;expectedCount=99">99</a>
        </div>
        <div id="overviewDMD">
          <a href="getAllocationListFromOverview.do?requestFor=H&amp;participantId=&amp;stlmtDate=2026-09-15&amp;category=MD&amp;srchTypeCd=CPY-DIST-MMI&amp;eventGroupCode=D&amp;statusIdentifier=1A&amp;expectedCount=1">1</a>
          <a href="getAllocationListFromOverview.do?requestFor=H&amp;participantId=&amp;stlmtDate=2026-09-14&amp;category=MD&amp;srchTypeCd=CPY-DIST-MMI&amp;eventGroupCode=D&amp;statusIdentifier=UA&amp;expectedCount=4">4</a>
        </div>
        """
        exports = d._extract_distribution_exports(
            html,
            date(2026, 9, 15),
            "TEST_ENTITY",
            ("cash", "mmi"),
        )
        self.assertEqual(
            [(item["category"], item["status"], item["expected_count"]) for item in exports],
            [("cash", "UA", 3), ("cash", "1A", 2), ("mmi", "1A", 1)],
        )
        for item in exports:
            query = parse_qs(urlparse(item["url"]).query)
            self.assertEqual(query["participantId"], ["TEST_ENTITY"])
            self.assertEqual(query["export"], ["all"])
            self.assertEqual(query["callingApp"], ["allocations"])

    def test_business_date_parser(self):
        self.assertEqual(d._business_date("2026-09-15"), date(2026, 9, 15))
        self.assertEqual(d._business_date(date(2026, 9, 15)), date(2026, 9, 15))
        with self.assertRaises(ValueError):
            d._business_date("09/15/2026")


if __name__ == "__main__":
    unittest.main()
