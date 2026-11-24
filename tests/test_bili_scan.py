import sys
import unittest
from unittest.mock import patch

from bili_fans_grpc_probe import ProbeError, encode_varint
from bili_scan import capture_contacts, fetch_current_relationship, fetch_recent_details, scan_login


def field(number, value):
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def page(start, stop, cursor=b""):
    payload = b"".join(
        field(1, b"\x08" + encode_varint(uid) + field(2, f"fan-{uid}".encode()))
        for uid in range(start, stop)
    )
    return payload + (field(4, cursor) if cursor else b"")


class BiliScanTests(unittest.TestCase):
    def test_missing_qrcode_reports_dependency_before_requesting_code(self):
        with patch.dict(sys.modules, {"h2.connection": object(), "qrcode": None}), patch("bili_scan.qr_login") as qr_login:
            with self.assertRaisesRegex(ProbeError, "缺少扫码依赖"):
                scan_login(None, None, None, None)
            qr_login.assert_not_called()

    def test_20000_contacts_are_paginated_without_1000_limit(self):
        def contacts(token, context, cursor):
            start = int(cursor or b"1")
            stop = min(start + 30, 20001)
            return page(start, stop, str(stop).encode() if stop < 20001 else b"")

        with (
            patch("bili_scan.fetch_contacts", side_effect=contacts) as fetch,
            patch("bili_scan.follower_count", return_value=20000) as count,
            patch("bili_scan.cancellable_wait"),
        ):
            result = capture_contacts("123", "token", None, 1, lambda _: None, lambda: None)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.records), 20000)
        self.assertEqual(result.records[-1]["uid"], "20000")
        self.assertEqual(fetch.call_count, 667)
        self.assertEqual(count.call_count, 2)

    def test_page_failures_duplicate_ids_and_repeated_cursor_are_rejected(self):
        for payloads in (
            [page(1, 2, b"next"), ProbeError("page failed")],
            [page(1, 2, b"next"), page(1, 3)],
            [page(1, 2, b"same"), page(2, 3, b"same")],
            [field(4, b"next")],
        ):
            with (
                self.subTest(payloads=payloads),
                patch("bili_scan.fetch_contacts", side_effect=payloads),
                patch("bili_scan.follower_count", return_value=2),
                patch("bili_scan.cancellable_wait"),
            ):
                with self.assertRaises(ProbeError):
                    capture_contacts("123", "token", None, 1, lambda _: None, lambda: None)

    def test_platform_counts_never_stop_cursor_pagination(self):
        for totals, reference, warning in (
            ([3, 3], 3, "读到 2 个唯一 UID"),
            ([0, 0], 0, "读到 2 个唯一 UID"),
            ([1, 2], 2, "从 1 变为 2"),
            ([ProbeError("statistics unavailable"), 3], 3, "读到 2 个唯一 UID"),
            ([3, OSError("statistics unavailable")], 3, "未能获取"),
            ([OSError("statistics unavailable"), ProbeError("statistics unavailable")], None, "未能获取"),
        ):
            with (
                self.subTest(totals=totals),
                patch("bili_scan.fetch_contacts", side_effect=[page(1, 2, b"next"), page(2, 3)]) as fetch,
                patch("bili_scan.follower_count", side_effect=totals),
                patch("bili_scan.cancellable_wait"),
            ):
                result = capture_contacts("123", "token", None, 1, lambda _: None, lambda: None)
            self.assertTrue(result.complete)
            self.assertEqual([row["uid"] for row in result.records], ["1", "2"])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(result.expected_total, reference)
            self.assertIn(warning, " ".join(result.warnings))

    def test_web_details_only_return_wanted_ids_and_stop_after_1000(self):
        def request(context, path, params, sessdata, referer, stage):
            start = (params["pn"] - 1) * 50 + 1
            return 0, {"list": [{"mid": i, "attribute": 6, "mtime": 1700000000} for i in range(start, start + 50)]}

        with patch("bili_scan.web_get", side_effect=request) as web, patch("bili_scan.cancellable_wait"):
            result = dict(fetch_recent_details("123", "cookie", None, {"1", "1000", "1001"}, 1, lambda: None))
        self.assertEqual(set(result), {"1", "1000"})
        self.assertEqual(result["1000"]["relationship"], "mutual")
        self.assertEqual(web.call_count, 20)

    def test_empty_contacts_does_not_request_web_details(self):
        with patch("bili_scan.web_get") as web:
            result = list(fetch_recent_details("123", "cookie", None, set(), 1, lambda: None))
        self.assertEqual(result, [])
        web.assert_not_called()

    def test_web_details_skip_invalid_values_and_never_return_membership_fields(self):
        rows = [
            {"mid": 1, "uname": "忽略昵称", "mtime": 0, "attribute": "bad"},
            {"mid": 2, "uname": "忽略昵称", "mtime": 1700000000, "attribute": 2},
            {"mid": 999, "mtime": 1700000000, "attribute": 6},
        ]
        with patch("bili_scan.web_get", return_value=(0, {"list": rows})):
            result = dict(fetch_recent_details("123", "cookie", None, {"1", "2"}, 1, lambda: None))
        self.assertEqual(result["1"], {})
        self.assertFalse(result["2"]["mutual"])
        self.assertEqual(result["2"]["relationship"], "follower")
        self.assertNotIn("999", result)
        self.assertNotIn("name", result["2"])
        self.assertNotIn("uid", result["2"])

    def test_lookup_returns_four_current_results_and_retains_missing_direction(self):
        for outgoing, incoming, label in (
            (2, 2, "mutual"), (2, 0, "following"), (0, 2, "follower"), (0, 0, "stranger"), (None, 0, None),
        ):
            with self.subTest(label=label):
                result = {
                    "relation": {"mid": 456, "attribute": outgoing},
                    "be_relation": {"mid": 123, "attribute": incoming, "mtime": 1700000000},
                }
                with patch("bili_scan.web_get", return_value=(0, result)):
                    detail = fetch_current_relationship("123", "456", "cookie", None)
                self.assertEqual(detail.get("relationship"), label)
                self.assertEqual(detail["follows_me"], incoming == 2)
                self.assertEqual("followed_at" in detail, incoming == 2)
