import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bili_scan import CONTACTS_SOURCE, fetch_recent_details
from follower_tracker import ApiResult, TrackerError, snapshot_document
from web_app import MAX_HISTORY, create_app, default_web_data_root


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary.name)
        self.app = create_app(self.data_root)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.token = self.app.config["APP_TOKEN"]
        self.account = {
            "uid": "12345",
            "name": "测试UP主",
            "face": "https://i0.hdslb.com/bfs/face/avatar.jpg",
        }
        # Most tests cover storage and comparisons, without live Bilibili requests.
        details = patch("web_app.fetch_recent_details", return_value=())
        self.details = details.start()
        self.addCleanup(details.stop)

    def tearDown(self):
        self.app.extensions["task_manager"].executor.shutdown(wait=True)
        self.temporary.cleanup()

    def headers(self):
        return {"X-App-Token": self.token}

    def wait_for_task(self, task_id):
        for _ in range(100):
            response = self.client.get(f"/api/tasks/{task_id}", headers=self.headers())
            payload = response.get_json()
            if payload["status"] in {"done", "error", "cancelled"}:
                return payload
            time.sleep(0.01)
        self.fail("task did not finish")

    def submit(self):
        with patch("web_app.scan_login", return_value=(self.account, "private-app-token", "private-secret")):
            return self.submit_check()

    def submit_check(self):
        response = self.client.post(
            "/api/check", json={"delay": 0.5}, headers=self.headers(),
        )
        self.assertEqual(response.status_code, 202)
        return self.wait_for_task(response.get_json()["task_id"])

    def session(self):
        response = self.client.get("/api/session", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_repeat_checks_reuse_both_credentials_only_in_memory(self):
        complete = ApiResult([{"uid": "1", "name": "粉丝"}], True, 1, 1, [])
        with (
            patch("web_app.scan_login", return_value=(self.account, "private-app-token", "private-secret")) as login,
            patch("web_app.capture_contacts", return_value=complete) as capture,
        ):
            self.assertFalse(self.session()["authenticated"])
            first = self.submit_check()
            self.assertEqual(self.session(), {"authenticated": True, "account": self.account})
            second = self.submit_check()
        login.assert_called_once()
        self.assertEqual([call.args[:2] for call in capture.call_args_list], [
            ("12345", "private-app-token"), ("12345", "private-app-token"),
        ])
        self.assertEqual(second["session"]["account"], self.account)
        self.assertFalse(second["result"]["baseline"])
        stored = "".join(path.read_text(encoding="utf-8") for path in self.data_root.rglob("*.json"))
        public = json.dumps([first, second, self.session()])
        for secret in ("private-app-token", "private-secret"):
            self.assertNotIn(secret, stored + public)
        restarted = create_app(self.data_root)
        try:
            fresh_client = restarted.test_client()
            response = fresh_client.get("/api/session", headers={"X-App-Token": restarted.config["APP_TOKEN"]})
            self.assertEqual(response.get_json(), {"authenticated": False, "account": None})
        finally:
            restarted.extensions["task_manager"].executor.shutdown(wait=True)

    def test_switch_clears_old_credentials_and_preserves_per_account_history(self):
        second_account = {"uid": "67890", "name": "另一个账号", "face": ""}
        result = ApiResult([{"uid": "1", "name": "粉丝"}], True, 1, 1, [])
        with (
            patch("web_app.scan_login", side_effect=[
                (self.account, "first-key", "first-cookie"),
                (second_account, "second-key", "second-cookie"),
            ]) as login,
            patch("web_app.capture_contacts", return_value=result) as capture,
        ):
            self.submit_check()
            first_files = {path: path.read_bytes() for path in self.data_root.rglob("*.json")}
            response = self.client.delete("/api/session", headers=self.headers())
            self.assertEqual(response.status_code, 200)
            self.assertFalse(self.session()["authenticated"])
            self.assertEqual(self.client.delete("/api/session").status_code, 403)
            second = self.submit_check()
        self.assertEqual(login.call_count, 2)
        self.assertEqual(capture.call_args.args[:2], ("67890", "second-key"))
        self.assertEqual(second["uid"], "67890")
        self.assertTrue(second["result"]["baseline"])
        self.assertEqual(self.session()["account"], second_account)
        for path, contents in first_files.items():
            self.assertEqual(path.read_bytes(), contents)
        manager = self.app.extensions["task_manager"]
        with self.assertRaisesRegex(TrackerError, "没有此账号"):
            manager.submit_lookup("12345", ["1"])

    def test_network_error_does_not_discard_the_cached_login(self):
        complete = ApiResult([], True, 1, 0, [])
        with patch("web_app.capture_contacts", return_value=complete):
            self.submit()
        with patch("web_app.capture_contacts", side_effect=OSError), patch("web_app.scan_login") as login:
            failed = self.submit_check()
            self.assertEqual(failed["status"], "error")
            self.assertTrue(self.session()["authenticated"])
            login.assert_not_called()
        with patch("web_app.capture_contacts", return_value=complete), patch("web_app.scan_login") as login:
            self.assertEqual(self.submit_check()["status"], "done")
            login.assert_not_called()

    def test_cancelled_check_keeps_session_and_cannot_switch_while_busy(self):
        records = ApiResult([{"uid": "1", "name": "粉丝"}], True, 1, 1, [])
        with patch("web_app.capture_contacts", return_value=records):
            self.submit()
        started = threading.Event()

        def slow_fetch(*args, progress, **kwargs):
            started.set()
            while True:
                progress({"phase": "fetching", "message": "正在读取", "collected": 1})
                time.sleep(0.005)

        with patch("web_app.capture_contacts", side_effect=slow_fetch), patch("web_app.scan_login") as login:
            response = self.client.post("/api/check", json={}, headers=self.headers())
            task_id = response.get_json()["task_id"]
            try:
                self.assertTrue(started.wait(timeout=2))
                switched = self.client.delete("/api/session", headers=self.headers())
                self.assertEqual(switched.status_code, 409)
            finally:
                self.client.post(f"/api/tasks/{task_id}/cancel", headers=self.headers())
                cancelled = self.wait_for_task(task_id)
        login.assert_not_called()
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(self.session()["authenticated"])
        with patch("web_app.capture_contacts", return_value=records), patch("web_app.scan_login") as login:
            self.assertEqual(self.submit_check()["status"], "done")
            login.assert_not_called()

    def test_access_key_is_reused_even_when_login_does_not_return_sessdata(self):
        complete = ApiResult([], True, 1, 0, [])
        with (
            patch("web_app.scan_login", return_value=(self.account, "app-key-only", "")) as login,
            patch("web_app.capture_contacts", return_value=complete),
        ):
            self.submit_check()
            self.submit_check()
        login.assert_called_once()
        self.assertTrue(self.session()["authenticated"])

    def test_session_summary_requires_token(self):
        self.assertEqual(self.client.get("/api/session").status_code, 403)

    def test_index_and_security_headers(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("粉丝变动助手", response.get_data(as_text=True))
        self.assertNotIn('id="sessdata"', response.get_data(as_text=True))
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_frozen_app_keeps_data_beside_executable(self):
        executable = self.data_root / "portable" / "BiliFollowerTracker.exe"
        with (
            patch("web_app.sys.frozen", True, create=True),
            patch("web_app.sys.executable", str(executable)),
        ):
            self.assertEqual(
                default_web_data_root(),
                executable.parent / ".local-data" / "bilibili",
            )

    def test_mutation_requires_local_token(self):
        response = self.client.post(
            "/api/check", json={"sessdata": "secret"}
        )
        self.assertEqual(response.status_code, 403)

    def test_check_rejects_manual_sessdata(self):
        response = self.client.post("/api/check", json={"sessdata": "secret"}, headers=self.headers())
        self.assertEqual(response.status_code, 400)

    def test_empty_history_reports_that_baseline_is_not_established(self):
        history = self.client.get("/api/history/12345").get_json()

        self.assertFalse(history["baseline_established"])
        self.assertEqual(history["reports"], [])

    def test_two_checks_create_baseline_report_and_never_store_secret(self):
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
        with (
            patch("web_app.capture_contacts", side_effect=[before, after]),
            patch("web_app.fetch_recent_details", side_effect=[
                [("1", {"mutual": False})], [("3", {"mutual": True})],
            ]) as enrich,
        ):
            first = self.submit()
            second = self.submit()
        self.assertEqual(enrich.call_count, 2)
        self.assertEqual(enrich.call_args_list[0].args[3], {"1", "2"})
        self.assertEqual(enrich.call_args_list[1].args[3], {"1", "3"})

        self.assertEqual(first["status"], "done")
        self.assertEqual(first["uid"], "12345")
        self.assertEqual(first["account"], self.account)
        self.assertTrue(first["result"]["baseline"])
        first_history = self.client.get("/api/history/12345").get_json()
        self.assertTrue(first_history["baseline_established"])
        self.assertEqual(second["status"], "done")
        self.assertFalse(second["result"]["baseline"])
        self.assertEqual(second["result"]["diff"]["suspected_unfollow_count"], 1)
        self.assertEqual(second["result"]["diff"]["new_follow_count"], 1)
        self.assertTrue(second["result"]["diff"]["new_follows"][0]["mutual"])

        history = self.client.get("/api/history/12345").get_json()
        self.assertEqual(len(history["reports"]), 1)
        all_text = "".join(
            path.read_text(encoding="utf-8") for path in self.data_root.rglob("*.json")
        )
        self.assertNotIn("private-secret", all_text)
        self.assertNotIn("private-app-token", all_text)

    def test_incomplete_capture_does_not_create_report(self):
        incomplete = ApiResult(
            records=[{"uid": "1", "name": "甲"}],
            complete=False,
            pages=1,
            expected_total=2000,
            warnings=["名单不完整"],
        )
        with patch("web_app.capture_contacts", return_value=incomplete):
            result = self.submit()
        self.assertEqual(result["status"], "error")
        self.assertFalse(list(self.data_root.rglob("*.json")))

    def snapshot_records(self, result):
        path = self.data_root / "12345" / "snapshots" / result["snapshot_file"]
        document = json.loads(path.read_text(encoding="utf-8"))
        return {row["uid"]: row for row in document["records"]}, path

    def test_first_scan_of_20000_fans_persists_recent_1000_details(self):
        records = [{"uid": str(i), "name": f"通讯录粉丝 {i}"} for i in range(1, 20001)]

        def web(context, path, params, sessdata, referer, stage):
            self.assertEqual(path, "/x/relation/followers")
            start = (params["pn"] - 1) * 50 + 1
            return 0, {"list": [
                {"mid": i, "uname": "不应覆盖通讯录昵称", "attribute": 6, "mtime": 1700000000}
                for i in range(start, start + 50)
            ]}

        with (
            patch("web_app.capture_contacts", return_value=ApiResult(records, True, 667, 20000, [])),
            patch("web_app.fetch_recent_details", side_effect=fetch_recent_details),
            patch("bili_scan.web_get", side_effect=web) as api,
            patch("bili_scan.cancellable_wait"),
        ):
            task = self.submit()
        self.assertEqual(task["status"], "done")
        result = task["result"]
        self.assertTrue(result["baseline"])
        self.assertIsNone(result["report_file"])
        saved, _ = self.snapshot_records(result)
        self.assertEqual(len(saved), 20000)
        self.assertEqual(sum("followed_at" in row for row in saved.values()), 1000)
        self.assertTrue(saved["1000"]["mutual"])
        self.assertEqual(saved["1"]["name"], "通讯录粉丝 1")
        self.assertNotIn("relationship", saved["1001"])
        self.assertEqual(api.call_count, 20)

    def test_unchanged_list_refreshes_recent_details_and_preserves_older_values(self):
        fans = [{"uid": "1", "name": "甲"}, {"uid": "2", "name": "乙"}]
        old_time = "2024-01-01T00:00:00+00:00"
        new_time = "2025-01-01T00:00:00+00:00"
        with (
            patch("web_app.capture_contacts", side_effect=lambda *args, **kwargs: ApiResult([dict(row) for row in fans], True, 1, 2, [])),
            patch("web_app.fetch_recent_details", side_effect=[
                [(uid, {"followed_at": old_time, "mutual": True, "relationship": "mutual"}) for uid in ("1", "2")],
                [("1", {"followed_at": new_time, "mutual": False, "relationship": "follower"})],
            ]) as enrich,
        ):
            first = self.submit()["result"]
            _, old_path = self.snapshot_records(first)
            old_contents = old_path.read_bytes()
            second = self.submit()["result"]
        self.assertEqual(enrich.call_count, 2)
        self.assertEqual(enrich.call_args.args[3], {"1", "2"})
        self.assertEqual(old_path.read_bytes(), old_contents)
        saved, _ = self.snapshot_records(second)
        self.assertEqual(saved["1"]["followed_at"], new_time)
        self.assertFalse(saved["1"]["mutual"])
        self.assertEqual(saved["1"]["relationship"], "follower")
        self.assertEqual(saved["2"]["followed_at"], old_time)
        self.assertTrue(saved["2"]["mutual"])
        self.assertEqual([second["diff"][key] for key in ("new_follow_count", "suspected_unfollow_count", "renamed_count")], [0, 0, 0])

    def test_only_removals_and_rename_refresh_metadata_without_altering_membership(self):
        old = [{"uid": str(i), "name": f"旧昵称{i}", "mutual": True, "followed_at": "2024-01-01T00:00:00+00:00"} for i in range(1, 4)]
        previous = self.write_legacy("snapshot-000.json", old, True, 3)
        contents = previous.read_bytes()
        fans = [{"uid": "1", "name": "新昵称"}, {"uid": "3", "name": "旧昵称3"}]
        updates = [
            ("1", {"mutual": False, "relationship": "follower", "followed_at": None, "uid": "999", "name": "错误昵称"}),
            ("2", {"mutual": False}),  # Already absent from Contacts: do not resurrect it.
            ("4", {"mutual": True}),   # Web-only UID: do not add it to the snapshot.
        ]
        with (
            patch("web_app.capture_contacts", return_value=ApiResult(fans, True, 1, 2, [])),
            patch("web_app.fetch_recent_details", return_value=updates) as enrich,
        ):
            result = self.submit()["result"]
        self.assertEqual(enrich.call_args.args[3], {"1", "3"})
        saved, _ = self.snapshot_records(result)
        self.assertEqual(set(saved), {"1", "3"})
        self.assertEqual(saved["1"]["name"], "新昵称")
        self.assertEqual(saved["1"]["uid"], "1")
        self.assertEqual(saved["1"]["followed_at"], old[0]["followed_at"])
        self.assertFalse(saved["1"]["mutual"])
        self.assertEqual(result["diff"]["new_follow_count"], 0)
        self.assertEqual(result["diff"]["renamed"][0]["relationship"], "follower")
        self.assertEqual(result["diff"]["suspected_unfollows"][0]["uid"], "2")
        self.assertTrue(result["diff"]["suspected_unfollows"][0]["mutual"])
        self.assertEqual(previous.read_bytes(), contents)

    def test_partial_refresh_failure_keeps_successful_updates_and_history(self):
        previous = self.write_legacy("snapshot-000.json", [
            {"uid": uid, "name": uid, "mutual": True} for uid in ("1", "2")
        ], True, 2)

        def partial(*args):
            yield "1", {"mutual": False, "relationship": "follower"}
            raise OSError("second page failed")

        fans = [{"uid": uid, "name": uid} for uid in ("1", "2")]
        original = previous.read_bytes()
        with (
            patch("web_app.capture_contacts", return_value=ApiResult(fans, True, 1, 2, [])),
            patch("web_app.fetch_recent_details", side_effect=partial),
        ):
            task = self.submit()
        self.assertEqual(task["status"], "done")
        result = task["result"]
        saved, _ = self.snapshot_records(result)
        self.assertFalse(saved["1"]["mutual"])
        self.assertTrue(saved["2"]["mutual"])
        self.assertIn("已有历史资料保留", result["warnings"][0])
        self.assertEqual(previous.read_bytes(), original)
        self.assertIsNotNone(result["report_file"])

    def test_without_sessdata_scan_retains_history_and_saves_baseline(self):
        self.write_legacy("snapshot-000.json", [{"uid": "1", "name": "甲", "mutual": True}], True, 1)
        with (
            patch("web_app.scan_login", return_value=(self.account, "only-key", "")),
            patch("web_app.capture_contacts", return_value=ApiResult([{"uid": "1", "name": "甲"}], True, 1, 1, [])),
        ):
            task = self.submit_check()
        self.assertEqual(task["status"], "done")
        saved, _ = self.snapshot_records(task["result"])
        self.assertTrue(saved["1"]["mutual"])
        self.assertIn("网页凭证", task["result"]["warnings"][0])
        self.details.assert_not_called()

    def test_cancel_during_automatic_refresh_does_not_save_scan(self):
        started = threading.Event()
        old = self.write_legacy("snapshot-000.json", [{"uid": "1", "name": "甲", "mutual": True}], True, 1)
        before = {path: path.read_bytes() for path in self.data_root.rglob("*.json")}

        def slow_details(uid, sessdata, context, wanted, delay, check_cancelled):
            yield "1", {"mutual": False}
            started.set()
            while True:
                check_cancelled()
                time.sleep(0.005)

        with (
            patch("web_app.scan_login", return_value=(self.account, "key", "cookie")),
            patch("web_app.capture_contacts", return_value=ApiResult([{"uid": "1", "name": "甲"}], True, 1, 1, [])),
            patch("web_app.fetch_recent_details", side_effect=slow_details),
        ):
            response = self.client.post("/api/check", json={}, headers=self.headers())
            task_id = response.get_json()["task_id"]
            try:
                self.assertTrue(started.wait(timeout=2))
            finally:
                self.client.post(f"/api/tasks/{task_id}/cancel", headers=self.headers())
                task = self.wait_for_task(task_id)
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual({path: path.read_bytes() for path in self.data_root.rglob("*.json")}, before)
        self.assertTrue(old.exists())

    def test_count_mismatch_preserves_uid_diff_and_updates_baseline(self):
        def fan(uid, name=None):
            return {"uid": str(uid), "uname": name or f"fan-{uid}"}

        first_list = [fan(index) for index in range(1, 547)]
        second_list = [fan(2, "新昵称"), *(fan(index) for index in range(3, 547)), fan(547)]
        with (
            patch("bili_scan.fetch_contacts"),
            patch("bili_scan.parse_page", side_effect=[(first_list, b""), (second_list, b""), (second_list, b"")]),
            patch("bili_scan.follower_count", side_effect=[547, 547, 548, 548, 548, 548]),
            patch("web_app.fetch_recent_details", return_value=[]),
        ):
            first = self.submit()["result"]
            second = self.submit()["result"]
            third = self.submit()["result"]

        self.assertTrue(first["baseline"])
        self.assertIn("B 站显示 547 位粉丝", first["warnings"][0])
        report = second["diff"]
        self.assertEqual(report["count_basis"], "listed_uids")
        self.assertEqual(report["platform_total"], 548)
        self.assertEqual((report["previous_count"], report["current_count"], report["net_change"]), (546, 546, 0))
        self.assertEqual([row["uid"] for row in report["new_follows"]], ["547"])
        self.assertEqual([row["uid"] for row in report["suspected_unfollows"]], ["1"])
        self.assertEqual([row["uid"] for row in report["renamed"]], ["2"])
        self.assertEqual(third["diff"]["new_follow_count"], 0)
        self.assertEqual(third["diff"]["suspected_unfollow_count"], 0)
        name = second["report_file"]
        history_report = self.client.get(f"/api/reports/12345/{name}").get_json()
        self.assertIn("通讯录读到 546 个唯一 UID", history_report["warnings"][0])
        history = self.client.get("/api/history/12345").get_json()["reports"]
        corresponding = next(item for item in history if item["file"] == name)
        self.assertEqual((corresponding["platform_total"], corresponding["current_visible_count"]), (548, 546))

    def test_report_display_recovers_old_platform_total_without_rewriting_files(self):
        root = self.data_root / "12345"
        paths = root / "reports"
        paths.mkdir(parents=True)
        self.write_legacy("snapshot-002.json", [{"uid": "1"}], True, 547)
        existing = {
            "report-001.json": {"current_count": 747, "current_visible_count": 747},
            "report-002.json": {"count_basis": "listed_uids", "current_count": 546, "current_visible_count": 546},
            "report-003.json": {"count_basis": "listed_uids", "platform_total": None, "current_visible_count": 546},
        }
        for name, record in existing.items():
            (paths / name).write_text(json.dumps(record), encoding="utf-8")
        history = self.client.get("/api/history/12345").get_json()["reports"]
        for item, total in zip(history, (None, 547, 747), strict=True):
            self.assertEqual(item["platform_total"], total)
            name = item["file"]
            detail = self.client.get(f"/api/reports/12345/{name}").get_json()
            self.assertEqual(detail["platform_total"], total)
            self.assertEqual(detail["current_visible_count"], existing[name]["current_visible_count"])
            self.assertEqual(json.loads((paths / name).read_text(encoding="utf-8")), existing[name])

    def write_legacy(self, name, records, complete, total):
        path = self.data_root / self.account["uid"] / "snapshots" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        document = snapshot_document(records, "bilibili-web-follower-list", complete, [], expected_total=total)
        document["subject_uid"] = self.account["uid"]
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_legacy_window_migration_keeps_old_files_and_establishes_full_baseline(self):
        legacy = [{"uid": str(i), "name": "旧名"} for i in range(1, 1001)]
        original = self.write_legacy("snapshot-000.json", legacy, False, 1500)
        old_contents = original.read_bytes()
        after = [{"uid": str(i), "name": "新名" if i == 2 else "旧名"} for i in range(2, 1502)]
        with (
            patch("web_app.capture_contacts", return_value=ApiResult(after, True, 50, 1500, [])),
            patch("web_app.fetch_recent_details", return_value=()) as enrich,
        ):
            first = self.submit()
            self.write_legacy("snapshot-999.json", [{"uid": "1", "name": "旧名"}], True, 1)
            second = self.submit()
        self.assertEqual(enrich.call_count, 2)
        self.assertEqual(enrich.call_args_list[0].args[3], {row["uid"] for row in after})
        self.assertEqual(original.read_bytes(), old_contents)
        report = first["result"]["diff"]
        self.assertEqual(report["comparison_mode"], "legacy_window")
        self.assertEqual(report["new_follow_count"], 0)
        self.assertEqual(report["ignored_added_count"], 501)
        self.assertEqual(report["suspected_unfollow_count"], 1)
        self.assertEqual(report["renamed_count"], 1)
        self.assertEqual(second["result"]["diff"]["comparison_mode"], "full")
        self.assertEqual(second["result"]["diff"]["new_follow_count"], 0)
        current = original.parent / first["result"]["snapshot_file"]
        self.assertEqual(json.loads(current.read_text(encoding="utf-8"))["source"], CONTACTS_SOURCE)

    def test_complete_legacy_compares_on_first_scan_and_detail_failure_does_not_stop_it(self):
        self.write_legacy("snapshot-000.json", [{"uid": "1", "name": "旧名"}], True, 1)
        records = [{"uid": "2", "name": "新人"}]
        with (
            patch("web_app.capture_contacts", return_value=ApiResult(records, True, 1, 1, [])),
            patch("web_app.fetch_recent_details", side_effect=OSError),
        ):
            response = self.submit()
        self.assertEqual(response["status"], "done")
        self.assertEqual(response["result"]["diff"]["new_follows"], records)
        self.assertIn("缺失字段", response["result"]["warnings"][0])

    def test_running_check_can_be_cancelled_without_saving_files(self):
        started = threading.Event()

        def slow_fetch(*args, progress, **kwargs):
            started.set()
            while True:
                progress(
                    {
                        "phase": "fetching",
                        "message": "正在读取",
                        "page": 1,
                        "collected": 1,
                        "total": 10,
                    }
                )
                time.sleep(0.005)

        with (
            patch("web_app.scan_login", return_value=(self.account, "private-app-token", "private-secret")),
            patch("web_app.capture_contacts", side_effect=slow_fetch),
        ):
            response = self.client.post(
                "/api/check",
                json={"delay": 0.5},
                headers=self.headers(),
            )
            task_id = response.get_json()["task_id"]
            self.assertTrue(started.wait(timeout=1))
            cancelled = self.client.post(
                f"/api/tasks/{task_id}/cancel", headers=self.headers()
            )
            result = self.wait_for_task(task_id)

        self.assertEqual(cancelled.status_code, 202)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(list(self.data_root.rglob("*.json")))

    def test_qr_is_only_available_while_waiting_and_cancel_does_not_save(self):
        started = threading.Event()

        def pending_login(context, on_qr, on_scan, check_cancelled):
            on_qr("data:image/svg+xml;base64,example")
            started.set()
            while True:
                check_cancelled()
                time.sleep(0.005)

        with patch("web_app.scan_login", side_effect=pending_login):
            request = self.client.post("/api/check", json={}, headers=self.headers())
            task_url = f"/api/tasks/{request.get_json()['task_id']}"
            self.assertTrue(started.wait(timeout=2))
            self.assertEqual(self.client.get(task_url).status_code, 403)
            task = self.client.get(task_url, headers=self.headers()).get_json()
            self.assertEqual(task["progress"]["phase"], "waiting_scan")
            self.assertTrue(task["qr_image"].startswith("data:image/svg+xml;"))
            self.client.post(task_url + "/cancel", headers=self.headers())
            finished = self.wait_for_task(task["id"])
        self.assertEqual(finished["status"], "cancelled")
        self.assertIsNone(finished["qr_image"])
        self.assertFalse(list(self.data_root.rglob("*.json")))

    def test_lookup_uses_report_uids_and_does_not_overwrite_history(self):
        self.write_legacy("snapshot-000.json", [{"uid": "7", "name": "旧粉丝", "mutual": True}], True, 1)
        with patch("web_app.capture_contacts", return_value=ApiResult([], True, 1, 0, [])):
            finished = self.submit()
        name = finished["result"]["report_file"]
        url = f"/api/reports/12345/{name}/lookup/removed"
        detail = {"relationship": "following", "follows_me": False}
        with patch("web_app.fetch_current_relationship", return_value=detail) as lookup:
            started = self.client.post(url, headers=self.headers())
            self.assertEqual(started.status_code, 202)
            updated = self.wait_for_task(started.get_json()["task_id"])
        self.assertEqual(updated["result"]["updates"], {"7": detail})
        self.assertEqual(lookup.call_args.args[:3], ("12345", "7", "private-secret"))
        report = self.client.get(f"/api/reports/12345/{name}").get_json()
        self.assertTrue(report["suspected_unfollows"][0]["mutual"])

    def test_lookup_for_added_fans_in_older_report_only_updates_task_result(self):
        self.write_legacy("snapshot-000.json", [{"uid": "7", "name": "旧粉丝"}], True, 1)
        previously_added = {"uid": "8", "name": "当时新增的粉丝"}
        scans = [ApiResult([previously_added], True, 1, 1, []), ApiResult([], True, 1, 0, [])]
        with (
            patch("web_app.capture_contacts", side_effect=scans),
            patch("web_app.fetch_recent_details", return_value=iter([
                ("8", {"followed_at": "2025-05-10T00:00:00+00:00", "mutual": True}),
            ])),
        ):
            historical = self.submit()
            latest = self.submit()
        name = historical["result"]["report_file"]
        self.assertNotEqual(name, latest["result"]["report_file"])
        report_url = f"/api/reports/12345/{name}"
        local_files = {path: path.read_bytes() for path in self.data_root.rglob("*.json")}

        current = {"relationship": "stranger", "follows_me": False}
        with patch("web_app.fetch_current_relationship", return_value=current) as lookup:
            response = self.client.post(report_url + "/lookup/added", headers=self.headers())
            self.assertEqual(response.status_code, 202)
            updated = self.wait_for_task(response.get_json()["task_id"])

        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["result"]["updates"], {"8": current})
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args.args[:2], ("12345", "8"))
        self.assertEqual({path: path.read_bytes() for path in self.data_root.rglob("*.json")}, local_files)
        self.assertTrue(self.client.get(report_url).get_json()["new_follows"][0]["mutual"])

    def test_history_shows_latest_99_without_deleting_local_files(self):
        uid_root = self.data_root / "12345"
        snapshots = uid_root / "snapshots"
        reports = uid_root / "reports"
        snapshots.mkdir(parents=True)
        reports.mkdir(parents=True)
        for index in range(MAX_HISTORY + 6):
            (snapshots / f"snapshot-{index:03d}.json").write_text("{}", encoding="utf-8")
            (reports / f"report-{index:03d}.json").write_text("{}", encoding="utf-8")

        history = self.client.get("/api/history/12345").get_json()

        remaining_snapshots = sorted(snapshots.glob("snapshot-*.json"))
        remaining_reports = sorted(reports.glob("report-*.json"))
        self.assertEqual(len(history["reports"]), MAX_HISTORY)
        self.assertEqual(history["reports"][0]["file"], "report-104.json")
        self.assertEqual(history["reports"][-1]["file"], "report-006.json")
        self.assertEqual(len(remaining_snapshots), MAX_HISTORY + 6)
        self.assertEqual(len(remaining_reports), MAX_HISTORY + 6)

    def test_delete_removes_only_selected_uid(self):
        first = self.data_root / "12345"
        second = self.data_root / "67890"
        first.mkdir()
        second.mkdir()
        (first / "marker.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
        response = self.client.delete("/api/data/12345", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())


if __name__ == "__main__":
    unittest.main()
