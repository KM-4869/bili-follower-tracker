import contextlib
import io
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from follower_tracker import (
    ApiResult,
    TrackerError,
    build_parser,
    compare_documents,
    fetch_bilibili_account,
    fetch_bilibili_records,
    find_ca_bundle,
    load_file_records,
    normalize_bilibili_records,
    normalize_sessdata,
    paged_url,
    run,
    save_snapshot,
    snapshot_document,
    validate_official_url,
)


class FollowerTrackerTests(unittest.TestCase):
    def test_diff_finds_added_removed_and_renamed(self):
        before = snapshot_document(
            [
                {"uid": "1", "name": "甲"},
                {
                    "uid": "2",
                    "name": "乙",
                    "followed_at": "2024-01-01T00:00:00+00:00",
                    "mutual": True,
                },
                {"uid": "3", "name": "旧昵称"},
            ],
            "test",
            True,
            [],
        )
        after = snapshot_document(
            [
                {"uid": "1", "name": "甲"},
                {
                    "uid": "3",
                    "name": "新昵称",
                    "followed_at": "2024-01-02T00:00:00+00:00",
                    "mutual": False,
                },
                {
                    "uid": "4",
                    "name": "丁",
                    "followed_at": "2024-01-03T00:00:00+00:00",
                    "mutual": False,
                },
            ],
            "test",
            True,
            [],
        )

        report = compare_documents(before, after)

        self.assertEqual(report["net_change"], 0)
        self.assertEqual(report["suspected_unfollows"][0]["mutual"], True)
        self.assertEqual(report["new_follows"][0]["followed_at"], "2024-01-03T00:00:00+00:00")
        self.assertEqual(report["renamed_count"], 1)
        self.assertEqual(report["renamed"][0]["mutual"], False)
        self.assertEqual(report["confidence"], "low")

    def test_incomplete_snapshot_is_rejected(self):
        before = snapshot_document([], "test", False, ["incomplete"])
        after = snapshot_document([], "test", True, [])
        with self.assertRaises(TrackerError):
            compare_documents(before, after)

    def test_nested_json_and_duplicate_uid_are_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(
                '{"data":{"items":[{"mid":2,"uname":"乙"},'
                '{"mid":1,"uname":"甲"},{"mid":2,"uname":""}]}}',
                encoding="utf-8",
            )
            records, warnings = load_file_records(path, "data.items", "mid", "uname")
        self.assertEqual(
            records,
            [{"uid": "1", "name": "甲"}, {"uid": "2", "name": "乙"}],
        )
        self.assertTrue(warnings)

    def test_snapshot_names_do_not_collide(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary)
            document = snapshot_document([], "test", True, [])
            first = save_snapshot(document, store)
            second = save_snapshot(document, store)
        self.assertNotEqual(first.name, second.name)

    def test_api_url_must_be_official_https_without_token(self):
        validate_official_url("https://open.example.bilibili.com/followers")
        with self.assertRaises(TrackerError):
            validate_official_url("http://open.example.bilibili.com/followers")
        with self.assertRaises(TrackerError):
            validate_official_url("https://example.com/followers")
        with self.assertRaises(TrackerError):
            validate_official_url("https://open.bilibili.com/followers?access_token=secret")

    def test_paged_url_preserves_existing_query(self):
        url = paged_url(
            "https://open.bilibili.com/followers?scope=mine",
            3,
            50,
            "page",
            "page_size",
        )
        self.assertIn("scope=mine", url)
        self.assertIn("page=3", url)
        self.assertIn("page_size=50", url)

    def test_bilibili_fetch_paginates_and_extracts_minimal_fields(self):
        requests = []

        def fake_requester(url, headers, timeout):
            requests.append((url, headers, timeout))
            page = int(parse_qs(urlparse(url).query)["pn"][0])
            rows = {
                1: [
                    {
                        "mid": 2,
                        "uname": "乙",
                        "mtime": 1704067200,
                        "attribute": 6,
                        "face": "not-stored",
                    },
                    {
                        "mid": 1,
                        "uname": "甲",
                        "mtime": 1704153600,
                        "attribute": 2,
                        "sign": "not-stored",
                    },
                ],
                2: [{"mid": 3, "uname": "丙", "vip": {"vipStatus": 1}}],
            }[page]
            return {"code": 0, "data": {"list": rows, "total": 3}}

        result = fetch_bilibili_records(
            "12345", "SESSDATA=test-value", page_size=2, delay=0, requester=fake_requester
        )

        self.assertTrue(result.complete)
        self.assertEqual(result.pages, 2)
        self.assertEqual(
            result.records,
            [
                {
                    "uid": "1",
                    "name": "甲",
                    "source_rank": 2,
                    "followed_at": "2024-01-02T00:00:00+00:00",
                    "mutual": False,
                },
                {
                    "uid": "2",
                    "name": "乙",
                    "source_rank": 1,
                    "followed_at": "2024-01-01T00:00:00+00:00",
                    "mutual": True,
                },
                {"uid": "3", "name": "丙", "source_rank": 3},
            ],
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][1]["Cookie"], "SESSDATA=test-value")
        self.assertNotIn("test-value", requests[0][0])

    def test_bilibili_relationship_fields_are_optional_and_deduplicated(self):
        records, warnings = normalize_bilibili_records(
            [
                {"mid": 7, "uname": "", "mtime": None, "attribute": None},
                {"mid": 7, "uname": "后来补全", "mtime": 1704067200, "attribute": 6},
                {"mid": 8, "uname": "无附加字段"},
            ]
        )

        self.assertEqual(
            records,
            [
                {
                    "uid": "7",
                    "name": "后来补全",
                    "source_rank": 1,
                    "followed_at": "2024-01-01T00:00:00+00:00",
                    "mutual": True,
                },
                {"uid": "8", "name": "无附加字段", "source_rank": 3},
            ],
        )
        self.assertTrue(warnings)

    def test_sessdata_identifies_signed_in_account(self):
        requests = []

        def fake_requester(url, headers, timeout):
            requests.append((url, headers, timeout))
            return {
                "code": 0,
                "data": {
                    "isLogin": True,
                    "mid": 12345,
                    "uname": "测试UP主",
                    "face": "https://i0.hdslb.com/bfs/face/avatar.jpg",
                },
            }

        account = fetch_bilibili_account(
            "SESSDATA=private-secret", requester=fake_requester
        )

        self.assertEqual(
            account,
            {
                "uid": "12345",
                "name": "测试UP主",
                "face": "https://i0.hdslb.com/bfs/face/avatar.jpg",
            },
        )
        self.assertEqual(requests[0][1]["Cookie"], "SESSDATA=private-secret")
        self.assertNotIn("private-secret", requests[0][0])

    def test_invalid_sessdata_cannot_identify_account(self):
        def fake_requester(url, headers, timeout):
            return {
                "code": -101,
                "message": "账号未登录",
                "data": {"isLogin": False},
            }

        with self.assertRaisesRegex(TrackerError, "SESSDATA 已过期"):
            fetch_bilibili_account("expired", requester=fake_requester)

    def test_bilibili_fetch_marks_visible_limit_as_incomplete(self):
        def fake_requester(url, headers, timeout):
            page = int(parse_qs(urlparse(url).query)["pn"][0])
            start = (page - 1) * 2
            rows = [
                {"mid": start + 1, "uname": f"用户{start + 1}"},
                {"mid": start + 2, "uname": f"用户{start + 2}"},
            ]
            return {"code": 0, "data": {"list": rows, "total": 5}}

        result = fetch_bilibili_records(
            "12345",
            "test-value",
            page_size=2,
            delay=0,
            requester=fake_requester,
            visible_limit=4,
        )

        self.assertFalse(result.complete)
        self.assertEqual(len(result.records), 4)
        self.assertFalse(result.partial_comparable)
        self.assertTrue(any("超过网页接口可见范围" in item for item in result.warnings))

    def test_bilibili_fetch_verifies_descending_time_window(self):
        def fake_requester(url, headers, timeout):
            page = int(parse_qs(urlparse(url).query)["pn"][0])
            start = (page - 1) * 2
            rows = [
                {
                    "mid": start + 1,
                    "uname": f"用户{start + 1}",
                    "mtime": 1700000000 - start,
                    "attribute": 2,
                },
                {
                    "mid": start + 2,
                    "uname": f"用户{start + 2}",
                    "mtime": 1699999999 - start,
                    "attribute": 2,
                },
            ]
            return {"code": 0, "data": {"list": rows, "total": 5}}

        result = fetch_bilibili_records(
            "12345",
            "test-value",
            page_size=2,
            delay=0,
            requester=fake_requester,
            visible_limit=4,
        )

        self.assertFalse(result.complete)
        self.assertTrue(result.time_order_verified)
        self.assertTrue(result.partial_comparable)
        self.assertEqual(result.visible_boundary, "2023-11-14T22:13:17+00:00")
        self.assertEqual(
            sorted(item["source_rank"] for item in result.records), [1, 2, 3, 4]
        )

    def test_time_boundary_comparison_excludes_ambiguous_edge(self):
        previous = snapshot_document(
            [
                {"uid": "1", "name": "较新后消失", "source_rank": 1, "followed_at": "2024-01-06T00:00:00+00:00"},
                {"uid": "2", "name": "仍在", "source_rank": 2, "followed_at": "2024-01-05T00:00:00+00:00"},
                {"uid": "3", "name": "旧边界", "source_rank": 3, "followed_at": "2024-01-04T00:00:00+00:00"},
                {"uid": "6", "name": "边界仍在", "source_rank": 4, "followed_at": "2024-01-04T00:00:00+00:00"},
            ],
            "test",
            False,
            [],
            expected_total=6,
            time_order_verified=True,
            visible_boundary="2024-01-04T00:00:00+00:00",
            partial_comparable=True,
        )
        current = snapshot_document(
            [
                {"uid": "4", "name": "确定新增", "source_rank": 1, "followed_at": "2024-01-07T00:00:00+00:00"},
                {"uid": "2", "name": "仍在", "source_rank": 2, "followed_at": "2024-01-05T00:00:00+00:00"},
                {"uid": "5", "name": "边界新增", "source_rank": 3, "followed_at": "2024-01-04T00:00:00+00:00"},
                {"uid": "6", "name": "边界仍在", "source_rank": 4, "followed_at": "2024-01-04T00:00:00+00:00"},
            ],
            "test",
            False,
            [],
            expected_total=7,
            time_order_verified=True,
            visible_boundary="2024-01-04T00:00:00+00:00",
            partial_comparable=True,
        )

        report = compare_documents(previous, current)

        self.assertEqual(report["comparison_mode"], "time_boundary")
        self.assertEqual([item["uid"] for item in report["suspected_unfollows"]], ["1"])
        self.assertEqual([item["uid"] for item in report["new_follows"]], ["4"])
        self.assertEqual(report["ignored_removed_count"], 1)
        self.assertEqual(report["ignored_added_count"], 1)
        self.assertEqual(report["net_change"], 1)
        self.assertEqual(report["confidence"], "bounded")

    def test_sessdata_rejects_full_cookie(self):
        self.assertEqual(normalize_sessdata("SESSDATA=value"), "value")
        with self.assertRaises(TrackerError):
            normalize_sessdata("SESSDATA=value; bili_jct=secret")

    def test_msys_ca_bundle_fallback_can_be_resolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "ucrt64"
            executable = prefix / "bin" / "python.exe"
            ca_bundle = root / "usr" / "ssl" / "cert.pem"
            executable.parent.mkdir(parents=True)
            executable.touch()
            ca_bundle.parent.mkdir(parents=True)
            ca_bundle.write_text("test-ca", encoding="ascii")
            verify_paths = types.SimpleNamespace(cafile=None)
            with (
                patch("follower_tracker.sys.prefix", str(prefix)),
                patch("follower_tracker.sys.executable", str(executable)),
                patch("follower_tracker.ssl.get_default_verify_paths", return_value=verify_paths),
            ):
                self.assertEqual(find_ca_bundle(), ca_bundle)

    def test_msys_ca_bundle_uses_base_prefix_from_virtual_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "project" / ".venv"
            base_prefix = root / "msys64" / "ucrt64"
            ca_bundle = base_prefix.parent / "usr" / "ssl" / "certs" / "ca-bundle.crt"
            ca_bundle.parent.mkdir(parents=True)
            ca_bundle.touch()
            with (
                patch("follower_tracker.sys.prefix", str(prefix)),
                patch("follower_tracker.sys.base_prefix", str(base_prefix)),
                patch("follower_tracker.sys.executable", str(prefix / "bin" / "python.exe")),
                patch("follower_tracker.ssl.get_default_verify_paths", return_value=types.SimpleNamespace(cafile=None)),
            ):
                self.assertEqual(find_ca_bundle(), ca_bundle)

    def test_check_bilibili_builds_baseline_then_report_without_storing_secret(self):
        before = ApiResult(
            records=[{"uid": "1", "name": "甲"}, {"uid": "2", "name": "乙"}],
            complete=True,
            pages=1,
            expected_total=2,
            warnings=[],
        )
        after = ApiResult(
            records=[{"uid": "1", "name": "甲"}, {"uid": "3", "name": "丙"}],
            complete=True,
            pages=1,
            expected_total=2,
            warnings=[],
        )
        parser = build_parser()
        with tempfile.TemporaryDirectory() as temporary:
            args = parser.parse_args(
                [
                    "check-bilibili",
                    "--uid",
                    "12345",
                    "--sessdata-env",
                    "TEST_BILI_SESSDATA",
                    "--store-root",
                    temporary,
                    "--delay",
                    "0",
                ]
            )
            with (
                patch.dict(os.environ, {"TEST_BILI_SESSDATA": "private-secret"}),
                patch("follower_tracker.fetch_bilibili_records", side_effect=[before, after]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(run(args), 0)
                self.assertEqual(run(args), 0)

            root = Path(temporary) / "12345"
            self.assertEqual(len(list((root / "snapshots").glob("snapshot-*.json"))), 2)
            reports = list((root / "reports").glob("report-*.json"))
            self.assertEqual(len(reports), 1)
            report_text = reports[0].read_text(encoding="utf-8")
            self.assertIn('"uid": "2"', report_text)
            all_text = "".join(
                path.read_text(encoding="utf-8") for path in root.rglob("*.json")
            )
            self.assertNotIn("private-secret", all_text)


if __name__ == "__main__":
    unittest.main()
