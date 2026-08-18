import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from follower_tracker import ApiResult
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

    def tearDown(self):
        self.app.extensions["task_manager"].executor.shutdown(wait=True)
        self.temporary.cleanup()

    def headers(self):
        return {"X-App-Token": self.token}

    def wait_for_task(self, task_id):
        for _ in range(100):
            response = self.client.get(f"/api/tasks/{task_id}")
            payload = response.get_json()
            if payload["status"] in {"done", "error", "cancelled"}:
                return payload
            time.sleep(0.01)
        self.fail("task did not finish")

    def submit(self):
        with patch("web_app.fetch_bilibili_account", return_value=self.account):
            response = self.client.post(
                "/api/check",
                json={"sessdata": "private-secret", "delay": 0.5},
                headers=self.headers(),
            )
            self.assertEqual(response.status_code, 202)
            return self.wait_for_task(response.get_json()["task_id"])

    def test_index_and_security_headers(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("粉丝变动助手", response.get_data(as_text=True))
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
        with patch("web_app.fetch_bilibili_records", side_effect=[before, after]):
            first = self.submit()
            second = self.submit()

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

        history = self.client.get("/api/history/12345").get_json()
        self.assertEqual(len(history["reports"]), 1)
        all_text = "".join(
            path.read_text(encoding="utf-8") for path in self.data_root.rglob("*.json")
        )
        self.assertNotIn("private-secret", all_text)

    def test_incomplete_capture_does_not_create_report(self):
        incomplete = ApiResult(
            records=[{"uid": "1", "name": "甲"}],
            complete=False,
            pages=1,
            expected_total=2000,
            warnings=["名单不完整"],
        )
        with patch("web_app.fetch_bilibili_records", return_value=incomplete):
            result = self.submit()
        self.assertEqual(result["status"], "done")
        self.assertFalse(result["result"]["complete"])
        self.assertIsNone(result["result"]["report_file"])

    def test_verified_partial_windows_create_bounded_report(self):
        before = ApiResult(
            records=[
                {"uid": "1", "name": "较新后消失", "source_rank": 1, "followed_at": "2024-01-06T00:00:00+00:00"},
                {"uid": "2", "name": "仍在", "source_rank": 2, "followed_at": "2024-01-05T00:00:00+00:00"},
                {"uid": "3", "name": "边界", "source_rank": 3, "followed_at": "2024-01-04T00:00:00+00:00"},
            ],
            complete=False,
            pages=20,
            expected_total=1500,
            warnings=[],
            time_order_verified=True,
            visible_boundary="2024-01-04T00:00:00+00:00",
            partial_comparable=True,
        )
        after = ApiResult(
            records=[
                {"uid": "4", "name": "新增", "source_rank": 1, "followed_at": "2024-01-07T00:00:00+00:00"},
                {"uid": "2", "name": "仍在", "source_rank": 2, "followed_at": "2024-01-05T00:00:00+00:00"},
                {"uid": "5", "name": "边界", "source_rank": 3, "followed_at": "2024-01-04T00:00:00+00:00"},
            ],
            complete=False,
            pages=20,
            expected_total=1500,
            warnings=[],
            time_order_verified=True,
            visible_boundary="2024-01-04T00:00:00+00:00",
            partial_comparable=True,
        )
        with patch("web_app.fetch_bilibili_records", side_effect=[before, after]):
            first = self.submit()
            second = self.submit()

        self.assertTrue(first["result"]["baseline"])
        self.assertTrue(first["result"]["partial_comparable"])
        self.assertEqual(second["result"]["diff"]["comparison_mode"], "time_boundary")
        self.assertEqual(second["result"]["diff"]["suspected_unfollow_count"], 1)
        self.assertEqual(second["result"]["diff"]["ignored_removed_count"], 1)

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
            patch("web_app.fetch_bilibili_account", return_value=self.account),
            patch("web_app.fetch_bilibili_records", side_effect=slow_fetch),
        ):
            response = self.client.post(
                "/api/check",
                json={"sessdata": "private-secret", "delay": 0.5},
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
