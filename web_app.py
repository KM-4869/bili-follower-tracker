#!/usr/bin/env python3
"""Local web interface for the Bilibili follower snapshot tool."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import threading
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template, request

from bili_fans_grpc_probe import ProbeError
from bili_scan import (
    CONTACTS_SOURCE, cancellable_wait, capture_contacts, fetch_current_relationship,
    fetch_recent_details, new_tls_context, scan_login,
)
from follower_tracker import (
    BILIBILI_VISIBLE_LIMIT,
    DEFAULT_BILIBILI_ROOT,
    TrackerError,
    compare_documents,
    load_snapshot,
    save_snapshot,
    snapshot_document,
)


MAX_HISTORY = 99
ACTIVE_TASKS = {"queued", "running", "cancelling"}
RELATION_TABS = {"removed": "suspected_unfollows", "added": "new_follows", "renamed": "renamed"}


class TaskCancelled(RuntimeError):
    """Raised when a running check is cooperatively cancelled."""


@dataclass(frozen=True, repr=False)
class LoginSession:
    """Credentials stay on the manager, never in serializable task results."""

    account: dict[str, str]
    access_key: str
    sessdata: str


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


def report_platform_total(report: dict[str, Any], report_path: Path) -> int | None:
    if "platform_total" in report:
        total = report["platform_total"]
    elif report.get("count_basis") != "listed_uids":
        total = report.get("current_count")
    else:
        # Earlier UID-based reports kept the platform count only in the matching snapshot.
        root = (report_path.parent.parent / "snapshots").resolve()
        snapshot_path = (root / report_path.name.replace("report-", "snapshot-", 1)).resolve()
        if snapshot_path.parent != root:
            return None
        try:
            total = load_snapshot(snapshot_path).get("expected_total")
        except (OSError, TrackerError, ValueError):
            return None
    return total if type(total) is int and total >= 0 else None


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


def choose_web_baseline(paths, uid):
    legacy = (None, None)
    for path in reversed(sorted(paths)):
        try:
            document = load_snapshot(path)
        except (OSError, ValueError, TrackerError):
            continue
        if str(document.get("subject_uid")) != uid:
            continue
        records = document["records"]
        if not all(isinstance(row, dict) and valid_uid(str(row.get("uid", ""))) for row in records):
            continue
        if document.get("source") == CONTACTS_SOURCE:
            if document.get("complete") is True:
                return path, document
            continue
        if legacy[0] is None and (
            document.get("complete") is True or (
                document.get("source") == "bilibili-web-follower-list"
                and len(records) == BILIBILI_VISIBLE_LIMIT
                and len({str(row["uid"]) for row in records}) == BILIBILI_VISIBLE_LIMIT
                and type(document.get("expected_total")) is int
                and document["expected_total"] > BILIBILI_VISIBLE_LIMIT
            )
        ):
            legacy = (path, document)
    return legacy


class TaskManager:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.tasks: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.login_session: LoginSession | None = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="follower-check")

    def has_active_task(self) -> bool:
        with self.lock:
            return any(
                task["status"] in ACTIVE_TASKS
                for task in self.tasks.values()
            )

    def _submit(self, worker, args, *, uid=None) -> str:
        task_id = uuid.uuid4().hex
        task = {
            "id": task_id,
            "uid": uid,
            "account": None,
            "qr_image": None,
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
            if any(value["status"] in ACTIVE_TASKS for value in self.tasks.values()):
                raise TrackerError("已有任务正在运行，请等待完成")
            self.tasks[task_id] = task
            if len(self.tasks) > MAX_HISTORY:
                finished = [
                    key
                    for key, value in self.tasks.items()
                    if value["status"] in {"done", "error", "cancelled"}
                ]
                for key in finished[: len(self.tasks) - MAX_HISTORY]:
                    self.tasks.pop(key, None)
        self.executor.submit(worker, task_id, *args)
        return task_id

    def submit(self, delay: float) -> str:
        return self._submit(self._run, (delay,))

    def submit_lookup(self, uid, ids):
        with self.lock:
            session = self.login_session
        if not session or session.account["uid"] != uid or not session.sessdata:
            raise TrackerError("没有此账号的扫码会话，请先用此账号扫码检查")
        return self._submit(self._run_lookup, (uid, ids, session), uid=uid)

    def _session_status(self):
        session = self.login_session
        return {"authenticated": session is not None, "account": dict(session.account) if session else None}

    def session_status(self):
        with self.lock:
            return self._session_status()

    def clear_session(self):
        with self.lock:
            if any(task["status"] in ACTIVE_TASKS for task in self.tasks.values()):
                raise TrackerError("请先结束正在运行的任务，再切换账号")
            self.login_session = None

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            task = self.tasks.get(task_id)
            return json.loads(json.dumps({**task, "session": self._session_status()}, ensure_ascii=False)) if task else None

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
            task["qr_image"] = None
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
            task["qr_image"] = None
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

    def _run(self, task_id: str, delay: float) -> None:
        with self.lock:
            if self.tasks[task_id]["cancel_requested"]:
                self.tasks[task_id]["status"] = "cancelled"
                return
            self.tasks[task_id]["status"] = "running"
            session = self.login_session
            self.tasks[task_id]["progress"] = {
                "phase": "connecting",
                "message": "正在使用本次运行中已登录的账号" if session else "正在向 B 站申请登录二维码",
                "page": 0,
                "collected": 0,
                "total": None,
            }

        uid = ""
        result: dict[str, Any] | None = None
        def qr_ready(image):
            self._raise_if_cancelled(task_id)
            self.update_progress(task_id, {
                "phase": "waiting_scan", "message": "请用 B 站 App 扫码并确认登录",
            })
            with self.lock:
                if not self.tasks[task_id]["cancel_requested"]:
                    self.tasks[task_id]["qr_image"] = image

        def scanned():
            self.update_progress(task_id, {
                "phase": "scanned", "message": "已扫码，请在手机上确认登录",
            })

        try:
            context = new_tls_context()
            if session is None:
                account, access_key, sessdata = scan_login(
                    context, qr_ready, scanned, lambda: self._raise_if_cancelled(task_id),
                )
                session = LoginSession(dict(account), access_key, sessdata)
            account, access_key, sessdata = session.account, session.access_key, session.sessdata
            uid = account["uid"]
            safe_root = (self.data_root / uid).resolve()
            if not valid_uid(uid) or safe_root.parent != self.data_root.resolve():
                raise TrackerError("扫码账号 UID 无效")
            self._raise_if_cancelled(task_id)
            with self.lock:
                if self.tasks[task_id]["cancel_requested"]:
                    raise TaskCancelled
                self.login_session = session
                self.tasks[task_id]["qr_image"] = None
                self.tasks[task_id]["uid"] = uid
                self.tasks[task_id]["account"] = account
                self.tasks[task_id]["progress"] = {
                    "phase": "identified",
                    "message": f"已登录为 {account['name'] or '未命名用户'}（UID {uid}）",
                    "page": 0,
                    "collected": 0,
                    "total": None,
                }
            result = self._capture_and_compare(
                uid,
                access_key,
                sessdata,
                context,
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
            if cancelled and result is not None:
                self._rollback_result(uid, result)
                self._mark_cancelled(task_id)
        except TaskCancelled:
            self._mark_cancelled(task_id)
        except (TrackerError, ProbeError) as exc:
            if self._cancel_requested(task_id):
                self._mark_cancelled(task_id)
            else:
                with self.lock:
                    self.tasks[task_id]["status"] = "error"
                    self.tasks[task_id]["error"] = str(exc)
        except OSError:
            if self._cancel_requested(task_id):
                self._mark_cancelled(task_id)
            else:
                with self.lock:
                    self.tasks[task_id]["status"] = "error"
                    self.tasks[task_id]["error"] = "网络连接或本地文件读写失败，本次未更新基线"
        except Exception as exc:
            if self._cancel_requested(task_id):
                self._mark_cancelled(task_id)
            else:
                with self.lock:
                    task = self.tasks[task_id]
                    phase = task["progress"]["phase"]
                    task["status"] = "error"
                    task["error"] = "程序出现未预期错误，请查看本地终端日志"
                frames = traceback.extract_tb(exc.__traceback__)
                locations = " → ".join(f"{Path(frame.filename).name}:{frame.lineno}" for frame in frames)
                print(f"检查任务异常（{phase}）：{type(exc).__name__}，{locations}", file=sys.stderr, flush=True)
        finally:
            with self.lock:
                self.tasks[task_id]["qr_image"] = None

    def _run_lookup(self, task_id, uid, ids, session):
        with self.lock:
            if not self.tasks[task_id]["cancel_requested"]:
                self.tasks[task_id]["status"] = "running"
        check_cancelled = lambda: self._raise_if_cancelled(task_id)
        try:
            check_cancelled()
            with self.lock:
                if self.login_session is not session:
                    raise TrackerError("扫码会话已切换，请重新发起补查")
            context = new_tls_context()
            updates = {}
            warning = ""
            for index, target in enumerate(ids):
                if index:
                    cancellable_wait(1, check_cancelled)
                self.update_progress(task_id, {
                    "phase": "lookup", "message": f"正在补查第 {index + 1} / {len(ids)} 人",
                    "collected": index, "total": len(ids),
                })
                try:
                    detail = fetch_current_relationship(uid, target, session.sessdata, context)
                except (TrackerError, ProbeError, OSError):
                    warning = "补查遇到接口错误，未查到的记录保持原样，可稍后重试"
                    break
                if detail:
                    updates[target] = detail
            with self.lock:
                task = self.tasks[task_id]
                if task["cancel_requested"]:
                    raise TaskCancelled
                task["result"] = {"updates": updates, "warning": warning}
                task["status"] = "done"
        except TaskCancelled:
            self._mark_cancelled(task_id)
        except Exception:
            with self.lock:
                self.tasks[task_id].update(status="error", error="本次补查失败，请检查网络后重试")
            raise

    def _capture_and_compare(
        self,
        uid: str,
        access_key: str,
        sessdata: str,
        context: Any,
        delay: float,
        progress: Any,
        check_cancelled: Any,
    ) -> dict[str, Any]:
        uid_root = self.data_root / uid
        snapshot_store = uid_root / "snapshots"
        previous_path, previous_document = choose_web_baseline(snapshot_store.glob("snapshot-*.json"), uid)

        destination: Path | None = None
        report_path: Path | None = None
        try:
            check_cancelled()
            api_result = capture_contacts(
                uid, access_key, context, delay, progress=progress, check_cancelled=check_cancelled,
            )
            check_cancelled()
            if not api_result.complete:
                raise TrackerError("通讯录名单未确认完整，本次不保存快照或报告")
            by_uid = {row["uid"]: row for row in api_result.records}
            document = snapshot_document(
                records=api_result.records,
                source=CONTACTS_SOURCE,
                complete=True,
                warnings=list(api_result.warnings),
                pages=api_result.pages,
                expected_total=api_result.expected_total,
            )
            document["subject_uid"] = uid
            if previous_document is not None:
                for old in previous_document["records"]:
                    current = by_uid.get(str(old["uid"]))
                    if current is not None:
                        for key in ("followed_at", "mutual", "relationship"):
                            if key in old:
                                current[key] = old[key]
            if by_uid:
                if sessdata:
                    progress({"phase": "enriching", "message": "正在刷新最近最多 1000 位粉丝的关注时间和关系",
                              "page": api_result.pages, "collected": len(by_uid), "total": len(by_uid)})
                    try:
                        for mid, detail in fetch_recent_details(uid, sessdata, context, set(by_uid), delay, check_cancelled):
                            current = by_uid.get(mid)
                            if current is not None:
                                # Contacts owns membership and nicknames; web rows only refresh metadata.
                                current.update({
                                    key: detail[key] for key in ("followed_at", "mutual", "relationship")
                                    if key in detail and detail[key] is not None
                                })
                    except (ProbeError, TrackerError, OSError):
                        document["warnings"].append("网页接口刷新未完成，已有历史资料保留，缺失字段留空；通讯录比对不受影响")
                else:
                    document["warnings"].append("扫码未提供可用的网页凭证，已有历史资料保留，缺失字段留空")
            check_cancelled()
            destination = save_snapshot(document, snapshot_store)
            check_cancelled()

            response: dict[str, Any] = {
                "uid": uid,
                "complete": api_result.complete,
                "baseline": False,
                "current_count": len(api_result.records),
                "visible_count": len(api_result.records),
                "expected_total": api_result.expected_total,
                "pages": api_result.pages,
                "warnings": document["warnings"],
                "snapshot_file": destination.name,
                "report_file": None,
                "diff": empty_diff(),
            }
            response["diff"]["current_count"] = response["current_count"]
            response["diff"]["count_basis"] = "listed_uids"
            response["comparison_ready"] = True

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
            report = compare_documents(previous_document, document, legacy_window=not previous_document.get("complete"))
            report["count_basis"] = "listed_uids"
            report["platform_total"] = api_result.expected_total
            report["previous_count"] = report["previous_visible_count"]
            report["current_count"] = report["current_visible_count"]
            report["net_change"] = (
                None if report["comparison_mode"] == "legacy_window"
                else report["current_count"] - report["previous_count"]
            )
            report["warnings"] = [*document["warnings"], *report["warnings"]]
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
        except Exception:
            if report_path:
                report_path.unlink(missing_ok=True)
            if destination:
                destination.unlink(missing_ok=True)
            raise


def valid_uid(uid: str) -> bool:
    return uid.isascii() and uid.isdecimal() and len(uid) <= 20 and int(uid) > 0


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
        TRUSTED_HOSTS=["127.0.0.1", "localhost"],
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
            "default-src 'self'; connect-src 'self'; img-src 'self' data: https://*.hdslb.com; "
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
        if not isinstance(payload, dict) or payload.keys() - {"delay"}:
            abort(400, description="新版仅支持二维码登录，不接受手动登录凭证")
        try:
            delay = float(payload.get("delay", 1.0))
        except (TypeError, ValueError):
            abort(400, description="请求间隔必须是数字")
        if not 0.5 <= delay <= 5.0:
            abort(400, description="请求间隔必须在 0.5 到 5 秒之间")
        try:
            task_id = manager.submit(delay)
        except TrackerError as exc:
            abort(409, description=str(exc))
        return jsonify({"task_id": task_id}), 202

    @app.get("/api/session")
    def session_status() -> Any:
        if request.headers.get("X-App-Token") != app.config["APP_TOKEN"]:
            abort(403, description="本地会话校验失败，请刷新页面")
        return jsonify(manager.session_status())

    @app.delete("/api/session")
    def clear_session() -> Any:
        try:
            manager.clear_session()
        except TrackerError as exc:
            abort(409, description=str(exc))
        return jsonify(manager.session_status())

    @app.get("/api/tasks/<task_id>")
    def task_status(task_id: str) -> Any:
        if request.headers.get("X-App-Token") != app.config["APP_TOKEN"]:
            abort(403, description="本地会话校验失败，请刷新页面")
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
        baseline_path, _ = choose_web_baseline((uid_root / "snapshots").glob("snapshot-*.json"), uid)

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
                    "current_visible_count": payload.get("current_visible_count"),
                    "platform_total": report_platform_total(payload, path),
                    "net_change": payload.get("net_change", 0),
                    "suspected_unfollow_count": payload.get(
                        "suspected_unfollow_count", 0
                    ),
                    "new_follow_count": payload.get("new_follow_count", 0),
                    "comparison_mode": payload.get("comparison_mode"),
                    "renamed_count": payload.get("renamed_count", 0),
                    "confidence": payload.get("confidence", "normal"),
                }
            )
            if len(reports) >= MAX_HISTORY:
                break
        return jsonify(
            {
                "uid": uid,
                "baseline_established": baseline_path is not None,
                "reports": reports,
            }
        )

    @app.get("/api/reports/<uid>/<filename>")
    def report_detail(uid: str, filename: str) -> Any:
        report = read_report(uid, filename)
        report["platform_total"] = report_platform_total(report, app.config["DATA_ROOT"] / uid / "reports" / filename)
        return jsonify(report)

    def read_report(uid: str, filename: str) -> dict:
        uid_root = safe_uid_root(app.config["DATA_ROOT"], uid)
        if not filename.startswith("report-") or not filename.endswith(".json"):
            abort(404)
        reports_root = (uid_root / "reports").resolve()
        path = (reports_root / filename).resolve()
        if path.parent != reports_root or not path.is_file():
            abort(404)
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                abort(500, description="报告文件无法读取")
            return report
        except (OSError, json.JSONDecodeError):
            abort(500, description="报告文件无法读取")

    @app.post("/api/reports/<uid>/<filename>/lookup/<category>")
    def start_lookup(uid: str, filename: str, category: str) -> Any:
        if category not in RELATION_TABS:
            abort(404)
        report = read_report(uid, filename)
        ids = list(dict.fromkeys(
            str(row["uid"]) for row in report.get(RELATION_TABS[category], [])
            if isinstance(row, dict) and valid_uid(str(row.get("uid", "")))
        ))
        if not ids:
            abort(400, description="这类变化没有需要补查的记录")
        try:
            task_id = manager.submit_lookup(uid, ids)
        except TrackerError as exc:
            abort(409, description=str(exc))
        return jsonify({"task_id": task_id}), 202

    @app.delete("/api/data/<uid>")
    def delete_account_data(uid: str) -> Any:
        if manager.has_active_task():
            abort(409, description="请先结束正在运行的任务")
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
