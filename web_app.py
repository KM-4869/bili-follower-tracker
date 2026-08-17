#!/usr/bin/env python3
"""Local web interface for the Bilibili follower snapshot tool."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template, request

from follower_tracker import (
    ApiResult,
    BILIBILI_VISIBLE_LIMIT,
    DEFAULT_BILIBILI_ROOT,
    TrackerError,
    compare_documents,
    fetch_bilibili_records,
    is_comparison_basis,
    load_snapshot,
    save_snapshot,
    snapshot_document,
)


MAX_HISTORY = 99


class TaskCancelled(RuntimeError):
    """Raised when a running check is cooperatively cancelled."""


def resource_root() -> Path:
    """Return bundled resources when frozen, otherwise the source directory."""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parent


def default_web_data_root() -> Path:
    """Keep portable data beside the executable without changing source-mode behavior."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".local-data" / "bilibili"
    return DEFAULT_BILIBILI_ROOT


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def empty_diff() -> dict[str, Any]:
    return {
        "previous_count": 0,
        "current_count": 0,
        "net_change": 0,
        "suspected_unfollow_count": 0,
        "suspected_unfollows": [],
        "new_follow_count": 0,
        "new_follows": [],
        "renamed_count": 0,
        "renamed": [],
        "confidence": "normal",
        "comparison_mode": "none",
        "ignored_removed_count": 0,
        "ignored_added_count": 0,
        "warnings": [],
    }


class TaskManager:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.tasks: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="follower-check")

    def has_active_task(self) -> bool:
        with self.lock:
            return any(
                task["status"] in {"queued", "running", "cancelling"}
                for task in self.tasks.values()
            )

    def submit(self, uid: str, sessdata: str, delay: float) -> str:
        task_id = uuid.uuid4().hex
        task = {
            "id": task_id,
            "uid": uid,
            "status": "queued",
            "created_at": utc_iso(),
            "progress": {
                "phase": "queued",
                "message": "检查任务已创建",
                "page": 0,
                "collected": 0,
                "total": None,
            },
            "result": None,
            "error": None,
            "cancel_requested": False,
        }
        with self.lock:
            self.tasks[task_id] = task
            if len(self.tasks) > MAX_HISTORY:
                finished = [
                    key
                    for key, value in self.tasks.items()
                    if value["status"] in {"done", "error", "cancelled"}
                ]
                for key in finished[: len(self.tasks) - MAX_HISTORY]:
                    self.tasks.pop(key, None)
        self.executor.submit(self._run, task_id, uid, sessdata, delay)
        return task_id

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            task = self.tasks.get(task_id)
            return json.loads(json.dumps(task, ensure_ascii=False)) if task else None

    def update_progress(self, task_id: str, progress: dict[str, Any]) -> None:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            if task["cancel_requested"]:
                raise TaskCancelled
            task["progress"] = progress

    def cancel(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task["status"] not in {"queued", "running", "cancelling"}:
                return False
            task["cancel_requested"] = True
            task["status"] = "cancelling"
            task["progress"] = {
                **task["progress"],
                "phase": "cancelling",
                "message": "正在中止，本次结果不会保存",
            }
            return True

    def _cancel_requested(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            return bool(task and task["cancel_requested"])

    def _raise_if_cancelled(self, task_id: str) -> None:
        if self._cancel_requested(task_id):
            raise TaskCancelled

    def _mark_cancelled(self, task_id: str) -> None:
        with self.lock:
            task = self.tasks[task_id]
            task["status"] = "cancelled"
            task["result"] = None
            task["error"] = None
            task["progress"] = {
                **task["progress"],
                "phase": "cancelled",
                "message": "检查已中止，本次未保存任何记录",
            }

    def _rollback_result(self, uid: str, result: dict[str, Any]) -> None:
        uid_root = self.data_root / uid
        snapshot_name = result.get("snapshot_file")
        report_name = result.get("report_file")
        if snapshot_name:
            (uid_root / "snapshots" / Path(snapshot_name).name).unlink(missing_ok=True)
        if report_name:
            (uid_root / "reports" / Path(report_name).name).unlink(missing_ok=True)

    def _run(self, task_id: str, uid: str, sessdata: str, delay: float) -> None:
        with self.lock:
            if self.tasks[task_id]["cancel_requested"]:
                self.tasks[task_id]["status"] = "cancelled"
                return
            self.tasks[task_id]["status"] = "running"
            self.tasks[task_id]["progress"] = {
                "phase": "connecting",
                "message": "正在验证登录状态",
                "page": 0,
                "collected": 0,
                "total": None,
            }

        try:
            result = self._capture_and_compare(
                uid,
                sessdata,
                delay,
                lambda value: self.update_progress(task_id, value),
                lambda: self._raise_if_cancelled(task_id),
            )
            with self.lock:
                cancelled = self.tasks[task_id]["cancel_requested"]
                if not cancelled:
                    self.tasks[task_id]["status"] = "done"
                    self.tasks[task_id]["result"] = result
                    self.tasks[task_id]["progress"] = {
                        "phase": "done",
                        "message": "检查完成",
                        "page": result["pages"],
                        "collected": result["current_count"],
                        "total": result["expected_total"],
                    }
            if cancelled:
                self._rollback_result(uid, result)
                self._mark_cancelled(task_id)
        except TaskCancelled:
            self._mark_cancelled(task_id)
        except TrackerError as exc:
            if self._cancel_requested(task_id):
                self._mark_cancelled(task_id)
            else:
                with self.lock:
                    self.tasks[task_id]["status"] = "error"
                    self.tasks[task_id]["error"] = str(exc)
        except Exception:
            with self.lock:
                self.tasks[task_id]["status"] = "error"
                self.tasks[task_id]["error"] = "程序出现未预期错误，请查看本地终端日志"
            raise
        finally:
            sessdata = ""

    def _capture_and_compare(
        self,
        uid: str,
        sessdata: str,
        delay: float,
        progress: Any,
        check_cancelled: Any,
    ) -> dict[str, Any]:
        uid_root = self.data_root / uid
        snapshot_store = uid_root / "snapshots"
        previous_files = sorted(snapshot_store.glob("snapshot-*.json"))

        destination: Path | None = None
        report_path: Path | None = None
        try:
            check_cancelled()
            api_result: ApiResult = fetch_bilibili_records(
                uid=uid,
                sessdata=sessdata,
                page_size=50,
                delay=delay,
                timeout=20.0,
                progress=progress,
            )
            check_cancelled()
            document = snapshot_document(
                records=api_result.records,
                source="bilibili-web-follower-list",
                complete=api_result.complete,
                warnings=api_result.warnings,
                pages=api_result.pages,
                expected_total=api_result.expected_total,
                time_order_verified=api_result.time_order_verified,
                visible_boundary=api_result.visible_boundary,
                partial_comparable=api_result.partial_comparable,
            )
            document["subject_uid"] = uid
            document["coverage_limit"] = BILIBILI_VISIBLE_LIMIT
            destination = save_snapshot(document, snapshot_store)
            check_cancelled()

            response: dict[str, Any] = {
                "uid": uid,
                "complete": api_result.complete,
                "baseline": False,
                "current_count": api_result.expected_total or len(api_result.records),
                "visible_count": len(api_result.records),
                "expected_total": api_result.expected_total,
                "pages": api_result.pages,
                "warnings": api_result.warnings,
                "snapshot_file": destination.name,
                "report_file": None,
                "diff": empty_diff(),
            }
            response["diff"]["current_count"] = response["current_count"]
            response["comparison_ready"] = bool(
                api_result.complete or api_result.partial_comparable
            )
            response["partial_comparable"] = api_result.partial_comparable
            response["visible_boundary"] = api_result.visible_boundary
            if not response["comparison_ready"]:
                return response

            previous_path: Path | None = None
            previous_document: dict[str, Any] | None = None
            for candidate in reversed(previous_files):
                candidate_document = load_snapshot(candidate)
                if (
                    is_comparison_basis(candidate_document)
                    and str(candidate_document.get("subject_uid")) == uid
                ):
                    previous_path = candidate
                    previous_document = candidate_document
                    break

            if previous_path is None or previous_document is None:
                response["baseline"] = True
                return response

            progress(
                {
                    "phase": "comparing",
                    "message": "正在与上次快照比较",
                    "page": api_result.pages,
                    "collected": len(api_result.records),
                    "total": api_result.expected_total,
                }
            )
            report = compare_documents(previous_document, document)
            report["subject_uid"] = uid
            report["previous_file"] = str(previous_path)
            report["current_file"] = str(destination)
            report_name = destination.name.replace("snapshot-", "report-", 1)
            report_path = uid_root / "reports" / report_name
            write_json(report_path, report)
            check_cancelled()
            response["report_file"] = report_name
            response["diff"] = report
            return response
        except TaskCancelled:
            if report_path:
                report_path.unlink(missing_ok=True)
            if destination:
                destination.unlink(missing_ok=True)
            raise
        except TrackerError:
            try:
                check_cancelled()
            except TaskCancelled:
                if report_path:
                    report_path.unlink(missing_ok=True)
                if destination:
                    destination.unlink(missing_ok=True)
                raise
            raise


def valid_uid(uid: str) -> bool:
    return uid.isdigit() and int(uid) > 0


def safe_uid_root(data_root: Path, uid: str) -> Path:
    if not valid_uid(uid):
        abort(400, description="UID 必须是正整数")
    root = data_root.resolve()
    target = (root / uid).resolve()
    if target.parent != root:
        abort(400, description="无效的数据目录")
    return target


def create_app(data_root: Path | None = None) -> Flask:
    resources = resource_root()
    app = Flask(
        __name__,
        template_folder=str(resources / "templates"),
        static_folder=str(resources / "static"),
    )
    app.config.update(
        DATA_ROOT=Path(data_root or default_web_data_root()),
        APP_TOKEN=secrets.token_urlsafe(32),
        MAX_CONTENT_LENGTH=16 * 1024,
        JSON_AS_ASCII=False,
    )
    manager = TaskManager(app.config["DATA_ROOT"])
    app.extensions["task_manager"] = manager

    @app.after_request
    def secure_response(response: Any) -> Any:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.before_request
    def verify_local_token() -> None:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.path.startswith(
            "/api/"
        ):
            if request.headers.get("X-App-Token") != app.config["APP_TOKEN"]:
                abort(403, description="本地会话校验失败，请刷新页面")

    @app.get("/")
    def index() -> str:
        return render_template("index.html", app_token=app.config["APP_TOKEN"])

    @app.post("/api/check")
    def start_check() -> Any:
        payload = request.get_json(silent=True) or {}
        uid = str(payload.get("uid", "")).strip()
        sessdata = str(payload.get("sessdata", "")).strip()
        try:
            delay = float(payload.get("delay", 1.0))
        except (TypeError, ValueError):
            abort(400, description="请求间隔必须是数字")
        if not valid_uid(uid):
            abort(400, description="UID 必须是正整数")
        if not sessdata:
            abort(400, description="请输入 SESSDATA")
        if not 0.5 <= delay <= 5.0:
            abort(400, description="请求间隔必须在 0.5 到 5 秒之间")
        if manager.has_active_task():
            abort(409, description="已有检查正在运行，请等待完成")
        task_id = manager.submit(uid, sessdata, delay)
        return jsonify({"task_id": task_id}), 202

    @app.get("/api/tasks/<task_id>")
    def task_status(task_id: str) -> Any:
        if not task_id.isalnum() or len(task_id) != 32:
            abort(404)
        task = manager.get(task_id)
        if not task:
            abort(404, description="找不到检查任务")
        return jsonify(task)

    @app.post("/api/tasks/<task_id>/cancel")
    def cancel_task(task_id: str) -> Any:
        if not task_id.isalnum() or len(task_id) != 32:
            abort(404)
        if not manager.cancel(task_id):
            abort(409, description="检查已经结束，无法中止")
        return jsonify({"task_id": task_id, "status": "cancelling"}), 202

    @app.get("/api/history/<uid>")
    def history(uid: str) -> Any:
        uid_root = safe_uid_root(app.config["DATA_ROOT"], uid)
        baseline_established = False
        for path in reversed(sorted((uid_root / "snapshots").glob("snapshot-*.json"))):
            try:
                snapshot = load_snapshot(path)
            except (OSError, TrackerError):
                continue
            if (
                str(snapshot.get("subject_uid")) == uid
                and is_comparison_basis(snapshot)
            ):
                baseline_established = True
                break

        reports: list[dict[str, Any]] = []
        for path in reversed(sorted((uid_root / "reports").glob("report-*.json"))):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            reports.append(
                {
                    "file": path.name,
                    "captured_at": payload.get("current_captured_at"),
                    "previous_count": payload.get("previous_count", 0),
                    "current_count": payload.get("current_count", 0),
                    "net_change": payload.get("net_change", 0),
                    "suspected_unfollow_count": payload.get(
                        "suspected_unfollow_count", 0
                    ),
                    "new_follow_count": payload.get("new_follow_count", 0),
                    "renamed_count": payload.get("renamed_count", 0),
                    "confidence": payload.get("confidence", "normal"),
                }
            )
            if len(reports) >= MAX_HISTORY:
                break
        return jsonify(
            {
                "uid": uid,
                "baseline_established": baseline_established,
                "reports": reports,
            }
        )

    @app.get("/api/reports/<uid>/<filename>")
    def report_detail(uid: str, filename: str) -> Any:
        uid_root = safe_uid_root(app.config["DATA_ROOT"], uid)
        if not filename.startswith("report-") or not filename.endswith(".json"):
            abort(404)
        reports_root = (uid_root / "reports").resolve()
        path = (reports_root / filename).resolve()
        if path.parent != reports_root or not path.is_file():
            abort(404)
        try:
            return jsonify(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            abort(500, description="报告文件无法读取")

    @app.delete("/api/data/<uid>")
    def delete_account_data(uid: str) -> Any:
        uid_root = safe_uid_root(app.config["DATA_ROOT"], uid)
        if uid_root.exists():
            shutil.rmtree(uid_root)
        return jsonify({"deleted": True, "uid": uid})

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    @app.errorhandler(500)
    def api_error(error: Any) -> Any:
        if request.path.startswith("/api/"):
            return jsonify({"error": error.description}), error.code
        return error

    return app


app = create_app()


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 Bili Follower Tracker 本地界面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port 必须在 1024 到 65535 之间")

    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        timer = threading.Timer(0.8, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    print(f"Bili Follower Tracker 已启动：{url}")
    print("关闭此窗口即可停止服务。")
    try:
        from waitress import serve
    except ImportError as exc:
        raise SystemExit("缺少 waitress，请运行：python -m pip install -r requirements.txt") from exc
    serve(app, host="127.0.0.1", port=args.port, threads=4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
