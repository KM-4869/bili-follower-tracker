#!/usr/bin/env python3
"""Capture follower snapshots and compare changes between them.

The Bilibili web mode mirrors the request made by the signed-in follower page.
It keeps the session value in memory and stores only the minimal relationship
snapshot fields needed for comparisons.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEFAULT_STORE = Path(".local-data") / "snapshots"
DEFAULT_BILIBILI_ROOT = Path(".local-data") / "bilibili"
BILIBILI_FOLLOWERS_URL = "https://api.bilibili.com/x/relation/followers"
BILIBILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILIBILI_VISIBLE_LIMIT = 1000


class TrackerError(RuntimeError):
    """A user-facing validation or data-source error."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise TrackerError(f"找不到文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise TrackerError(f"JSON 格式错误：{path}（{exc}）") from exc


def get_path(value: Any, path: str) -> Any:
    """Resolve a simple dot-separated path in a JSON object."""
    if not path:
        return value
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise TrackerError(f"JSON 中不存在字段路径：{path}")
        current = current[part]
    return current


def nested_get(record: dict[str, Any], field: str) -> Any:
    return get_path(record, field)


def normalize_records(
    rows: Iterable[Any], id_field: str, name_field: str | None
) -> tuple[list[dict[str, str]], list[str]]:
    by_id: dict[str, dict[str, str]] = {}
    duplicate_ids: set[str] = set()

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise TrackerError(f"第 {index} 条记录不是 JSON 对象")
        try:
            raw_id = nested_get(row, id_field)
        except TrackerError as exc:
            raise TrackerError(f"第 {index} 条记录缺少 UID 字段 {id_field}") from exc
        if raw_id is None or str(raw_id).strip() == "":
            raise TrackerError(f"第 {index} 条记录的 UID 为空")

        uid = str(raw_id).strip()
        name = ""
        if name_field:
            try:
                raw_name = nested_get(row, name_field)
                name = "" if raw_name is None else str(raw_name).strip()
            except TrackerError:
                name = ""

        if uid in by_id:
            duplicate_ids.add(uid)
            if not by_id[uid]["name"] and name:
                by_id[uid]["name"] = name
        else:
            by_id[uid] = {"uid": uid, "name": name}

    records = sorted(by_id.values(), key=lambda item: item["uid"])
    warnings: list[str] = []
    if duplicate_ids:
        warnings.append(f"发现 {len(duplicate_ids)} 个重复 UID，已去重")
    return records, warnings


def normalize_bilibili_records(
    rows: Iterable[Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep only the Bilibili fields needed for relationship snapshots."""
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise TrackerError(f"第 {index} 条记录不是 JSON 对象")
        raw_id = row.get("mid")
        if raw_id is None or str(raw_id).strip() == "":
            raise TrackerError(f"第 {index} 条记录缺少有效的 mid")

        uid = str(raw_id).strip()
        raw_name = row.get("uname")
        record: dict[str, Any] = {
            "uid": uid,
            "name": "" if raw_name is None else str(raw_name).strip(),
            # The snapshot itself is sorted by UID for stable files.  Keep the
            # original API position separately so the server-side time order
            # can still be audited.
            "source_rank": index,
        }

        try:
            mtime = int(row.get("mtime"))
            if mtime > 0:
                record["followed_at"] = datetime.fromtimestamp(
                    mtime, timezone.utc
                ).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            pass

        try:
            attribute = int(row.get("attribute"))
            record["mutual"] = attribute == 6
        except (TypeError, ValueError):
            pass

        if uid in by_id:
            duplicate_ids.add(uid)
            existing = by_id[uid]
            if not existing.get("name") and record["name"]:
                existing["name"] = record["name"]
            for key in ("followed_at", "mutual"):
                if key not in existing and key in record:
                    existing[key] = record[key]
        else:
            by_id[uid] = record

    records = sorted(by_id.values(), key=lambda item: item["uid"])
    warnings: list[str] = []
    if duplicate_ids:
        warnings.append(f"发现 {len(duplicate_ids)} 个重复 UID，已去重")
    return records, warnings


def load_file_records(
    path: Path, items_path: str, id_field: str, name_field: str | None
) -> tuple[list[dict[str, str]], list[str]]:
    if path.suffix.lower() == ".csv":
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except FileNotFoundError as exc:
            raise TrackerError(f"找不到文件：{path}") from exc
    else:
        payload = read_json(path)
        rows = get_path(payload, items_path)

    if not isinstance(rows, list):
        raise TrackerError("粉丝记录必须是 JSON 数组或 CSV 数据行")
    return normalize_records(rows, id_field, name_field)


def validate_official_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise TrackerError("联网模式只允许 HTTPS 地址")
    if host != "bilibili.com" and not host.endswith(".bilibili.com"):
        raise TrackerError("联网模式只允许连接 bilibili.com 的官方域名")
    sensitive_keys = {"access_token", "access_key", "token", "cookie", "sessdata"}
    query_keys = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query)}
    if sensitive_keys & query_keys:
        raise TrackerError("不要把 token 或 Cookie 放入 URL；请通过 --token-env 提供 token")


def paged_url(
    base_url: str,
    page: int,
    page_size: int,
    page_param: str,
    size_param: str,
) -> str:
    parsed = urllib.parse.urlparse(base_url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query[page_param] = str(page)
    query[size_param] = str(page_size)
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


@dataclass
class ApiResult:
    records: list[dict[str, Any]]
    complete: bool
    pages: int
    expected_total: int | None
    warnings: list[str]
    time_order_verified: bool = False
    visible_boundary: str | None = None
    partial_comparable: bool = False


JsonRequester = Callable[[str, dict[str, str], float], Any]
ProgressCallback = Callable[[dict[str, Any]], None]


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward an authorization header through an HTTP redirect."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def find_ca_bundle() -> Path | None:
    if getattr(sys, "frozen", False):
        try:
            import certifi

            bundled_ca = Path(certifi.where())
            if bundled_ca.is_file():
                return bundled_ca
        except (ImportError, OSError):
            pass

    verify_paths = ssl.get_default_verify_paths()
    if verify_paths.cafile and Path(verify_paths.cafile).is_file():
        return Path(verify_paths.cafile)

    executable = Path(sys.executable).resolve()
    candidates = [
        Path(sys.prefix).parent / "usr" / "ssl" / "cert.pem",
        Path(sys.prefix).parent / "usr" / "ssl" / "certs" / "ca-bundle.crt",
        # In an MSYS2 venv, sys.prefix points to the project, not the runtime.
        Path(sys.base_prefix).parent / "usr" / "ssl" / "cert.pem",
        Path(sys.base_prefix).parent / "usr" / "ssl" / "certs" / "ca-bundle.crt",
    ]
    if len(executable.parents) >= 3:
        msys_root = executable.parents[2]
        candidates.extend(
            [
                msys_root / "usr" / "ssl" / "cert.pem",
                msys_root / "usr" / "ssl" / "certs" / "ca-bundle.crt",
            ]
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def build_https_opener() -> urllib.request.OpenerDirector:
    ca_bundle = find_ca_bundle()
    context = (
        ssl.create_default_context(cafile=str(ca_bundle))
        if ca_bundle
        else ssl.create_default_context()
    )
    return urllib.request.build_opener(
        NoRedirectHandler(), urllib.request.HTTPSHandler(context=context)
    )


def request_json(url: str, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = build_https_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(content_type))
    except urllib.error.HTTPError as exc:
        raise TrackerError(f"接口返回 HTTP {exc.code}：{exc.reason}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise TrackerError(
                "SSL 证书校验失败：当前 Python 缺少可用的根证书包；"
                "请安装对应环境的 ca-certificates，不能用关闭校验绕过"
            ) from exc
        raise TrackerError(f"接口连接失败：{exc.reason}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrackerError("接口响应不是有效的 JSON") from exc


def fetch_api_records(args: argparse.Namespace) -> ApiResult:
    validate_official_url(args.url)
    headers = {
        "Accept": "application/json",
        "User-Agent": "bili-follower-tracker/0.3",
    }
    if args.token_env:
        token = os.environ.get(args.token_env)
        if not token:
            raise TrackerError(f"环境变量 {args.token_env} 未设置")
        if args.auth_header.lower() == "cookie":
            raise TrackerError("本工具不接受 Cookie；请使用开放平台 token")
        headers[args.auth_header] = f"{args.auth_scheme}{token}"

    all_rows: list[Any] = []
    seen_page_hashes: set[str] = set()
    warnings: list[str] = []
    expected_total: int | None = None
    complete = False
    pages = 0

    for offset in range(args.max_pages):
        page = args.page_start + offset
        url = paged_url(args.url, page, args.page_size, args.page_param, args.size_param)
        payload = request_json(url, headers, args.timeout)

        if isinstance(payload, dict) and "code" in payload:
            code = payload["code"]
            if code not in (0, "0", None):
                message = payload.get("message") or payload.get("msg") or "未知错误"
                raise TrackerError(f"接口业务错误 code={code}：{message}")

        rows = get_path(payload, args.items_path)
        if not isinstance(rows, list):
            raise TrackerError(f"字段 {args.items_path or '<root>'} 不是数组")

        fingerprint = hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if rows and fingerprint in seen_page_hashes:
            warnings.append(f"第 {page} 页与之前页面完全重复，停止翻页")
            break
        seen_page_hashes.add(fingerprint)
        all_rows.extend(rows)
        pages += 1

        if args.total_path:
            raw_total = get_path(payload, args.total_path)
            try:
                page_total = int(raw_total)
            except (TypeError, ValueError) as exc:
                raise TrackerError(f"总数字段 {args.total_path} 不是整数") from exc
            if expected_total is None:
                expected_total = page_total
            elif expected_total != page_total:
                warnings.append(
                    f"翻页过程中总数从 {expected_total} 变为 {page_total}，名单可能正在变化"
                )
                expected_total = page_total

        if expected_total is not None and len(all_rows) >= expected_total:
            complete = True
            break
        if len(rows) < args.page_size:
            complete = True
            break
        if args.delay and offset + 1 < args.max_pages:
            time.sleep(args.delay)

    records, normalize_warnings = normalize_records(
        all_rows, args.id_field, args.name_field
    )
    warnings.extend(normalize_warnings)

    if expected_total is not None and len(records) != expected_total:
        complete = False
        warnings.append(
            f"接口声明总数 {expected_total}，但仅得到 {len(records)} 个唯一 UID"
        )
    if expected_total is None:
        warnings.append("接口未提供总数，只能根据末页长度推断分页结束")
    if not complete:
        warnings.append("未能证明分页完整；该快照不能用于可靠的取关判断")

    return ApiResult(records, complete, pages, expected_total, warnings)


def normalize_sessdata(value: str) -> str:
    sessdata = value.strip()
    if sessdata.lower().startswith("sessdata="):
        sessdata = sessdata.split("=", 1)[1].strip()
    if not sessdata:
        raise TrackerError("SESSDATA 为空")
    if ";" in sessdata or "\r" in sessdata or "\n" in sessdata:
        raise TrackerError("请只提供 SESSDATA 的值，不要提供整段 Cookie")
    return sessdata


def bilibili_api_error(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise TrackerError("粉丝接口响应不是 JSON 对象")
    code = payload.get("code")
    if code in (0, "0", None):
        return
    message = payload.get("message") or payload.get("msg") or "未知错误"
    hints = {
        -101: "登录状态无效或 SESSDATA 已过期",
        -352: "请求被风控拦截，请停止重试并稍后在网页确认",
        22118: "该账号的粉丝列表当前不可见",
    }
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        numeric_code = None
    hint = hints.get(numeric_code)
    suffix = f"；{hint}" if hint else ""
    raise TrackerError(f"粉丝接口错误 code={code}：{message}{suffix}")


def fetch_bilibili_account(
    sessdata: str,
    timeout: float = 15.0,
    requester: JsonRequester = request_json,
) -> dict[str, str]:
    """Validate SESSDATA and return the minimal signed-in account profile."""
    sessdata = normalize_sessdata(sessdata)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": f"SESSDATA={sessdata}",
        "Referer": "https://www.bilibili.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
    }
    payload = requester(BILIBILI_NAV_URL, headers, timeout)
    if not isinstance(payload, dict):
        raise TrackerError("登录信息接口响应不是 JSON 对象")

    code = payload.get("code")
    try:
        numeric_code = int(code)
    except (TypeError, ValueError):
        numeric_code = None
    if numeric_code != 0:
        message = payload.get("message") or payload.get("msg") or "未知错误"
        hint = "；登录状态无效或 SESSDATA 已过期" if numeric_code == -101 else ""
        raise TrackerError(f"登录验证失败 code={code}：{message}{hint}")

    data = payload.get("data")
    if not isinstance(data, dict) or data.get("isLogin") is not True:
        raise TrackerError("登录验证失败：SESSDATA 无效或已过期")

    raw_uid = data.get("mid")
    uid = "" if raw_uid is None else str(raw_uid).strip()
    if not uid.isdigit() or int(uid) <= 0:
        raise TrackerError("登录信息中缺少有效的账号 UID")

    raw_name = data.get("uname")
    name = "" if raw_name is None else str(raw_name).strip()
    raw_face = data.get("face")
    face = "" if raw_face is None else str(raw_face).strip()
    if face:
        parsed_face = urllib.parse.urlparse(face)
        face_host = (parsed_face.hostname or "").lower()
        if parsed_face.scheme != "https" or not face_host.endswith(".hdslb.com"):
            face = ""

    return {"uid": uid, "name": name, "face": face}


def fetch_bilibili_records(
    uid: str,
    sessdata: str,
    page_size: int = 50,
    delay: float = 1.0,
    timeout: float = 15.0,
    requester: JsonRequester = request_json,
    visible_limit: int = BILIBILI_VISIBLE_LIMIT,
    progress: ProgressCallback | None = None,
) -> ApiResult:
    if not uid.isdigit() or int(uid) <= 0:
        raise TrackerError("UID 必须是正整数")
    if not 1 <= page_size <= 50:
        raise TrackerError("网页接口的 --page-size 必须在 1 到 50 之间")
    if visible_limit <= 0:
        raise TrackerError("visible_limit 必须大于 0")

    sessdata = normalize_sessdata(sessdata)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": f"SESSDATA={sessdata}",
        "Referer": f"https://space.bilibili.com/{uid}/fans/fans",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
    }
    all_rows: list[Any] = []
    seen_page_hashes: set[str] = set()
    warnings: list[str] = []
    expected_total: int | None = None
    total_stable = True
    reached_end = False
    pages = 0
    raw_mtimes: list[int] = []
    all_mtimes_valid = True
    max_pages = (visible_limit + page_size - 1) // page_size

    for page in range(1, max_pages + 1):
        if progress:
            progress(
                {
                    "phase": "fetching",
                    "message": f"正在读取第 {page} 页",
                    "page": page,
                    "collected": len(all_rows),
                    "total": expected_total,
                }
            )
        url = paged_url(
            f"{BILIBILI_FOLLOWERS_URL}?vmid={uid}&order=desc",
            page,
            page_size,
            "pn",
            "ps",
        )
        payload = requester(url, headers, timeout)
        bilibili_api_error(payload)
        data = get_path(payload, "data")
        if not isinstance(data, dict):
            raise TrackerError("粉丝接口缺少 data 对象")
        rows = data.get("list")
        if not isinstance(rows, list):
            raise TrackerError("粉丝接口的 data.list 不是数组")

        for row in rows:
            try:
                mtime = int(row.get("mtime")) if isinstance(row, dict) else 0
            except (TypeError, ValueError):
                mtime = 0
            if mtime <= 0:
                all_mtimes_valid = False
            else:
                raw_mtimes.append(mtime)
        try:
            page_total = int(data["total"])
        except KeyError as exc:
            raise TrackerError("粉丝接口缺少 data.total，无法验证名单完整性") from exc
        except (TypeError, ValueError) as exc:
            raise TrackerError("粉丝接口的 data.total 不是整数") from exc

        if expected_total is None:
            expected_total = page_total
        elif expected_total != page_total:
            warnings.append(
                f"翻页过程中粉丝总数从 {expected_total} 变为 {page_total}，本次不用于取关判断"
            )
            expected_total = page_total
            total_stable = False

        fingerprint = hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if rows and fingerprint in seen_page_hashes:
            warnings.append(f"第 {page} 页与之前页面重复，停止翻页")
            break
        seen_page_hashes.add(fingerprint)
        all_rows.extend(rows)
        pages += 1

        records_so_far, _ = normalize_bilibili_records(all_rows)
        if progress:
            progress(
                {
                    "phase": "fetching",
                    "message": f"已读取 {len(records_so_far)} 位粉丝",
                    "page": page,
                    "collected": len(records_so_far),
                    "total": expected_total,
                }
            )
        target = min(expected_total, visible_limit)
        if len(records_so_far) >= target:
            reached_end = True
            break
        if len(rows) < page_size:
            reached_end = True
            break
        if delay and page < max_pages:
            time.sleep(delay)

    records, normalize_warnings = normalize_bilibili_records(all_rows)
    if progress:
        progress(
            {
                "phase": "validating",
                "message": "正在校验名单完整性",
                "page": pages,
                "collected": len(records),
                "total": expected_total,
            }
        )
    warnings.extend(normalize_warnings)
    time_order_verified = bool(
        all_mtimes_valid
        and len(raw_mtimes) == len(all_rows)
        and all(
            raw_mtimes[index] >= raw_mtimes[index + 1]
            for index in range(len(raw_mtimes) - 1)
        )
    )
    visible_boundary = (
        datetime.fromtimestamp(min(raw_mtimes), timezone.utc).isoformat()
        if raw_mtimes and len(raw_mtimes) == len(all_rows)
        else None
    )
    complete = bool(
        reached_end
        and total_stable
        and expected_total is not None
        and expected_total <= visible_limit
        and len(records) == expected_total
    )
    partial_comparable = bool(
        not complete
        and reached_end
        and total_stable
        and expected_total is not None
        and expected_total > visible_limit
        and len(records) == visible_limit
        and len(all_rows) == visible_limit
        and time_order_verified
        and visible_boundary is not None
    )

    if expected_total is not None and expected_total > visible_limit:
        if partial_comparable:
            warnings.append(
                f"粉丝总数 {expected_total} 超过完整名单范围 {visible_limit}；"
                "已验证可见窗口按关注时间倒序，仅比较时间边界内可确认的变化"
            )
        else:
            reasons: list[str] = []
            if len(records) != visible_limit or len(all_rows) != visible_limit:
                reasons.append(f"未取得 {visible_limit} 个不重复记录")
            if not all_mtimes_valid:
                reasons.append("存在缺失或无效的关注时间")
            elif not time_order_verified:
                reasons.append("接口返回未按关注时间倒序排列")
            reason = "、".join(reasons) or "可见窗口未通过一致性校验"
            warnings.append(
                f"粉丝总数 {expected_total} 超过网页接口可见范围 {visible_limit}；"
                f"{reason}，不能进行时间边界比较"
            )
    elif expected_total is not None and len(records) != expected_total:
        warnings.append(
            f"接口声明总数 {expected_total}，但仅得到 {len(records)} 个唯一 UID"
        )
    if not complete and not partial_comparable:
        warnings.append("本次名单既不完整也未通过时间边界校验，已禁止生成取关结论")

    return ApiResult(
        records,
        complete,
        pages,
        expected_total,
        warnings,
        time_order_verified=time_order_verified,
        visible_boundary=visible_boundary,
        partial_comparable=partial_comparable,
    )


def snapshot_document(
    records: list[dict[str, Any]],
    source: str,
    complete: bool,
    warnings: list[str],
    pages: int | None = None,
    expected_total: int | None = None,
    time_order_verified: bool = False,
    visible_boundary: str | None = None,
    partial_comparable: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": utc_now().isoformat(),
        "source": source,
        "complete": complete,
        "record_count": len(records),
        "expected_total": expected_total,
        "pages": pages,
        "time_order_verified": time_order_verified,
        "visible_boundary": visible_boundary,
        "partial_comparable": partial_comparable,
        "warnings": warnings,
        "records": records,
    }


def save_snapshot(document: dict[str, Any], store: Path) -> Path:
    store.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
    destination = store / f"snapshot-{stamp}-{uuid.uuid4().hex[:8]}.json"
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return destination


def load_snapshot(path: Path) -> dict[str, Any]:
    document = read_json(path)
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise TrackerError(f"不是受支持的快照文件：{path}")
    if not isinstance(document.get("records"), list):
        raise TrackerError(f"快照缺少 records 数组：{path}")
    return document


def choose_snapshots(
    store: Path, previous: Path | None, current: Path | None
) -> tuple[Path, Path]:
    if bool(previous) != bool(current):
        raise TrackerError("--previous 和 --current 必须同时提供")
    if previous and current:
        return previous, current
    files = sorted(store.glob("snapshot-*.json"))
    if len(files) < 2:
        raise TrackerError("至少需要两个快照才能比较")
    return files[-2], files[-1]


def parse_record_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def has_verified_time_window(document: dict[str, Any]) -> bool:
    """Return whether an incomplete snapshot safely describes a newest-N window."""
    if not document.get("partial_comparable") or not document.get("time_order_verified"):
        return False
    records = document.get("records")
    if not isinstance(records, list) or not records:
        return False
    try:
        expected_total = int(document.get("expected_total"))
    except (TypeError, ValueError):
        return False
    if expected_total <= len(records):
        return False

    boundary = parse_record_time(document.get("visible_boundary"))
    if any(not isinstance(item, dict) for item in records):
        return False
    times = [parse_record_time(item.get("followed_at")) for item in records]
    if boundary is None or any(value is None for value in times):
        return False

    ranks: list[int] = []
    for item in records:
        try:
            ranks.append(int(item.get("source_rank")))
        except (TypeError, ValueError):
            return False
    if len(set(ranks)) != len(records) or min(ranks) != 1 or max(ranks) != len(records):
        return False

    concrete_times = [value for value in times if value is not None]
    return min(concrete_times) == boundary


def is_comparison_basis(document: dict[str, Any]) -> bool:
    return bool(document.get("complete")) or has_verified_time_window(document)


def record_is_inside_snapshot_boundary(
    record: dict[str, Any], covering_snapshot: dict[str, Any]
) -> bool:
    if covering_snapshot.get("complete"):
        return True
    if not has_verified_time_window(covering_snapshot):
        return False
    record_time = parse_record_time(record.get("followed_at"))
    boundary = parse_record_time(covering_snapshot.get("visible_boundary"))
    # Equal timestamps may be split arbitrarily at the 1000th position, so the
    # comparison deliberately uses a strict boundary.
    return bool(record_time and boundary and record_time > boundary)


def compare_documents(
    previous: dict[str, Any], current: dict[str, Any], allow_incomplete: bool = False,
    *, legacy_window: bool = False,
) -> dict[str, Any]:
    full_comparison = bool(previous.get("complete") and current.get("complete"))
    legacy_window = bool(legacy_window and not previous.get("complete") and current.get("complete"))
    bounded_comparison = bool(
        not full_comparison
        and is_comparison_basis(previous)
        and is_comparison_basis(current)
    )
    apply_time_boundary = bounded_comparison and not allow_incomplete and not legacy_window
    if not allow_incomplete and not full_comparison and not bounded_comparison and not legacy_window:
        raise TrackerError("快照不完整且没有经过验证的时间边界，已拒绝比较")

    old = {str(item["uid"]): item for item in previous["records"]}
    new = {str(item["uid"]): item for item in current["records"]}
    raw_removed_ids = old.keys() - new.keys()
    raw_added_ids = new.keys() - old.keys()
    if legacy_window:
        removed_ids = sorted(raw_removed_ids)
        added_ids = []
    elif apply_time_boundary:
        removed_ids = sorted(
            uid
            for uid in raw_removed_ids
            if record_is_inside_snapshot_boundary(old[uid], current)
        )
        added_ids = sorted(
            uid
            for uid in raw_added_ids
            if record_is_inside_snapshot_boundary(new[uid], previous)
        )
    else:
        removed_ids = sorted(raw_removed_ids)
        added_ids = sorted(raw_added_ids)
    shared_ids = old.keys() & new.keys()
    renamed = []
    for uid in sorted(shared_ids):
        if not old[uid].get("name") or not new[uid].get("name") or old[uid]["name"] == new[uid]["name"]:
            continue
        item = {
            "uid": uid,
            "before": old[uid].get("name", ""),
            "after": new[uid].get("name", ""),
        }
        for key in ("followed_at", "mutual", "relationship"):
            if key in new[uid]:
                item[key] = new[uid][key]
        renamed.append(item)

    warnings: list[str] = []
    removal_ratio = len(removed_ids) / max(len(old), 1)
    confidence = "bounded" if apply_time_boundary or legacy_window else "normal"
    if legacy_window:
        warnings.append("旧基线覆盖有限，本次不统计新增；已保存完整名单作为后续基线")
    elif allow_incomplete and (not previous.get("complete") or not current.get("complete")):
        confidence = "low"
        warnings.append("至少一个快照不完整，结果仅供调试")
    elif apply_time_boundary:
        ignored_removed = len(raw_removed_ids) - len(removed_ids)
        ignored_added = len(raw_added_ids) - len(added_ids)
        warnings.append(
            "名单超过完整可见范围，本报告只统计严格位于关注时间边界内的变化；"
            f"边界外已忽略 {ignored_removed} 个消失项和 {ignored_added} 个新增项"
        )
    if removal_ratio > 0.25:
        confidence = "low"
        warnings.append("单次消失超过上次名单的 25%，优先排查接口、权限或分页异常")

    try:
        previous_total = int(previous.get("expected_total"))
    except (TypeError, ValueError):
        previous_total = len(old)
    try:
        current_total = int(current.get("expected_total"))
    except (TypeError, ValueError):
        current_total = len(new)

    return {
        "kind": "snapshot_diff",
        "comparison_mode": (
            "legacy_window" if legacy_window else "time_boundary" if apply_time_boundary
            else "debug_incomplete" if allow_incomplete and not full_comparison else "full"
        ),
        "previous_captured_at": previous.get("captured_at"),
        "current_captured_at": current.get("captured_at"),
        "previous_count": previous_total,
        "current_count": current_total,
        "previous_visible_count": len(old),
        "current_visible_count": len(new),
        "previous_visible_boundary": previous.get("visible_boundary"),
        "current_visible_boundary": current.get("visible_boundary"),
        "net_change": current_total - previous_total,
        "suspected_unfollow_count": len(removed_ids),
        "suspected_unfollows": [old[uid] for uid in removed_ids],
        "new_follow_count": len(added_ids),
        "new_follows": [new[uid] for uid in added_ids],
        "ignored_removed_count": len(raw_removed_ids) - len(removed_ids),
        "ignored_added_count": len(raw_added_ids) - len(added_ids),
        "renamed_count": len(renamed),
        "renamed": renamed,
        "confidence": confidence,
        "warnings": warnings,
    }


def write_report(report: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as handle:
            handle.write(rendered)
        print(f"报告已保存：{output}")
    else:
        print(rendered, end="")


def add_mapping_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--items-path", default="", help="记录数组的点分字段路径；根数组留空")
    parser.add_argument("--id-field", default="uid", help="每条记录中的 UID 字段路径")
    parser.add_argument("--name-field", default="name", help="每条记录中的昵称字段路径")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用完整粉丝名单快照验证『疑似取关』识别的可行性"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    file_parser = subparsers.add_parser("capture-file", help="从本地 JSON/CSV 创建快照")
    file_parser.add_argument("input", type=Path)
    file_parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    file_parser.add_argument(
        "--incomplete", action="store_true", help="标记输入数据不完整（用于测试保护逻辑）"
    )
    add_mapping_arguments(file_parser)

    api_parser = subparsers.add_parser(
        "capture-api", help="从明确配置的 B 站官方开放平台 HTTPS 接口创建快照"
    )
    api_parser.add_argument("--url", required=True, help="官方接口地址；不要包含 token")
    api_parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    api_parser.add_argument("--token-env", help="保存开放平台 token 的环境变量名")
    api_parser.add_argument("--auth-header", default="Authorization")
    api_parser.add_argument("--auth-scheme", default="Bearer ")
    api_parser.add_argument("--page-param", default="page")
    api_parser.add_argument("--size-param", default="page_size")
    api_parser.add_argument("--page-start", type=int, default=1)
    api_parser.add_argument("--page-size", type=int, default=50)
    api_parser.add_argument("--max-pages", type=int, default=200)
    api_parser.add_argument("--total-path", default="", help="响应中总数的点分字段路径")
    api_parser.add_argument("--timeout", type=float, default=15.0)
    api_parser.add_argument("--delay", type=float, default=1.0, help="分页请求间隔秒数")
    add_mapping_arguments(api_parser)

    bili_parser = subparsers.add_parser(
        "check-bilibili",
        help="按 UID 从已登录网页使用的粉丝接口抓取快照并自动比较",
    )
    bili_parser.add_argument("--uid", required=True, help="要检查的 UP 主 UID")
    bili_parser.add_argument(
        "--sessdata-env",
        default="BILIBILI_SESSDATA",
        help="仅保存 SESSDATA 值的环境变量名",
    )
    bili_parser.add_argument(
        "--prompt-sessdata",
        action="store_true",
        help="运行时隐藏输入 SESSDATA，避免进入终端历史",
    )
    bili_parser.add_argument(
        "--store-root", type=Path, default=DEFAULT_BILIBILI_ROOT
    )
    bili_parser.add_argument("--page-size", type=int, default=50)
    bili_parser.add_argument("--timeout", type=float, default=15.0)
    bili_parser.add_argument("--delay", type=float, default=1.0, help="分页请求间隔秒数")

    diff_parser = subparsers.add_parser("diff", help="比较最近两个或指定的快照")
    diff_parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    diff_parser.add_argument("--previous", type=Path)
    diff_parser.add_argument("--current", type=Path)
    diff_parser.add_argument("--output", type=Path)
    diff_parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="仅调试时使用：允许比较不完整快照，结果置信度为低",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.command == "capture-api":
        if args.page_size <= 0 or args.max_pages <= 0:
            raise TrackerError("--page-size 和 --max-pages 必须大于 0")
        if args.delay < 0 or args.timeout <= 0:
            raise TrackerError("--delay 不能小于 0，--timeout 必须大于 0")
    if args.command == "check-bilibili":
        if not args.uid.isdigit() or int(args.uid) <= 0:
            raise TrackerError("--uid 必须是正整数")
        if not 1 <= args.page_size <= 50:
            raise TrackerError("--page-size 必须在 1 到 50 之间")
        if args.delay < 0 or args.timeout <= 0:
            raise TrackerError("--delay 不能小于 0，--timeout 必须大于 0")


def run(args: argparse.Namespace) -> int:
    validate_args(args)
    if args.command == "capture-file":
        records, warnings = load_file_records(
            args.input, args.items_path, args.id_field, args.name_field
        )
        document = snapshot_document(
            records=records,
            source=f"file:{args.input.name}",
            complete=not args.incomplete,
            warnings=warnings,
            expected_total=len(records) if not args.incomplete else None,
        )
        destination = save_snapshot(document, args.store)
        print(f"快照已保存：{destination}（{len(records)} 个唯一 UID）")
        for warning in warnings:
            print(f"警告：{warning}", file=sys.stderr)
        return 0

    if args.command == "capture-api":
        result = fetch_api_records(args)
        document = snapshot_document(
            records=result.records,
            source=f"official-api:{urllib.parse.urlparse(args.url).hostname}",
            complete=result.complete,
            warnings=result.warnings,
            pages=result.pages,
            expected_total=result.expected_total,
        )
        destination = save_snapshot(document, args.store)
        state = "完整" if result.complete else "不完整"
        print(
            f"快照已保存：{destination}（{len(result.records)} 个唯一 UID，{state}）"
        )
        for warning in result.warnings:
            print(f"警告：{warning}", file=sys.stderr)
        return 0 if result.complete else 2

    if args.command == "check-bilibili":
        raw_sessdata = (
            getpass.getpass("请输入 SESSDATA（输入不会显示）：")
            if args.prompt_sessdata
            else os.environ.get(args.sessdata_env)
        )
        if not raw_sessdata:
            raise TrackerError(
                f"环境变量 {args.sessdata_env} 未设置；"
                "可改用 --prompt-sessdata 隐藏输入"
            )
        uid_root = args.store_root / args.uid
        snapshot_store = uid_root / "snapshots"
        previous_files = sorted(snapshot_store.glob("snapshot-*.json"))
        result = fetch_bilibili_records(
            uid=args.uid,
            sessdata=raw_sessdata,
            page_size=args.page_size,
            delay=args.delay,
            timeout=args.timeout,
        )
        document = snapshot_document(
            records=result.records,
            source="bilibili-web-follower-list",
            complete=result.complete,
            warnings=result.warnings,
            pages=result.pages,
            expected_total=result.expected_total,
            time_order_verified=result.time_order_verified,
            visible_boundary=result.visible_boundary,
            partial_comparable=result.partial_comparable,
        )
        document["subject_uid"] = args.uid
        document["coverage_limit"] = BILIBILI_VISIBLE_LIMIT
        destination = save_snapshot(document, snapshot_store)
        state = (
            "完整"
            if result.complete
            else "可做时间边界比较" if result.partial_comparable else "不可比较"
        )
        print(
            f"UID {args.uid} 快照已保存：{destination}"
            f"（{len(result.records)} 个唯一 UID，{state}）"
        )
        for warning in result.warnings:
            print(f"警告：{warning}", file=sys.stderr)
        if not result.complete and not result.partial_comparable:
            return 2

        previous_path: Path | None = None
        previous_document: dict[str, Any] | None = None
        for candidate in reversed(previous_files):
            candidate_document = load_snapshot(candidate)
            if (
                is_comparison_basis(candidate_document)
                and str(candidate_document.get("subject_uid")) == args.uid
            ):
                previous_path = candidate
                previous_document = candidate_document
                break
        if previous_path is None or previous_document is None:
            print("这是首个可比较快照，已建立基线；下次运行将自动比较变化。")
            return 0

        report = compare_documents(previous_document, document)
        report["subject_uid"] = args.uid
        report["previous_file"] = str(previous_path)
        report["current_file"] = str(destination)
        reports_store = uid_root / "reports"
        report_name = destination.name.replace("snapshot-", "report-", 1)
        report_path = reports_store / report_name
        write_report(report, report_path)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    previous_path, current_path = choose_snapshots(
        args.store, args.previous, args.current
    )
    previous = load_snapshot(previous_path)
    current = load_snapshot(current_path)
    report = compare_documents(previous, current, args.allow_incomplete)
    report["previous_file"] = str(previous_path)
    report["current_file"] = str(current_path)
    write_report(report, args.output)
    return 0


def main() -> int:
    parser = build_parser()
    try:
        return run(parser.parse_args())
    except TrackerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
