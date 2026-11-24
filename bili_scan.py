"""Bilibili transports for the local QR-only follower checker."""

import base64
import ssl
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from io import BytesIO

from bili_fans_grpc_probe import ProbeError, fetch_contacts, parse_page, qr_login, web_get
from follower_tracker import ApiResult, BILIBILI_VISIBLE_LIMIT, TrackerError, find_ca_bundle


CONTACTS_SOURCE = "bilibili-app-contacts"


def new_tls_context():
    bundle = find_ca_bundle()
    context = ssl.create_default_context()
    if bundle:
        context.load_verify_locations(cafile=str(bundle))
    return context


def check_scan_dependencies():
    try:
        import h2.connection
        from qrcode.image.svg import SvgPathImage
    except ImportError:
        if getattr(sys, "frozen", False):
            raise ProbeError("便携包缺少扫码组件，请完整解压新版压缩包后再运行") from None
        raise ProbeError("当前 Python 缺少扫码依赖，请用同一个 Python 运行 -m pip install -r requirements.txt，或用 start_web_ui.bat 启动") from None


def encode_qr(url):
    import qrcode
    from qrcode.image.svg import SvgPathImage

    qr = qrcode.QRCode(box_size=5, border=4)
    qr.add_data(url)
    buffer = BytesIO()
    qr.make_image(image_factory=SvgPathImage, back_color="white", fill_color="black").save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def cancellable_wait(seconds, check_cancelled):
    end = time.monotonic() + seconds
    while True:
        check_cancelled()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.1))


def scan_login(context, on_qr, on_scan, check_cancelled):
    check_scan_dependencies()
    token, uid, sessdata = qr_login(
        "app", context, include_sessdata=True, on_qr=lambda url: on_qr(encode_qr(url)),
        on_scan=on_scan, wait=lambda seconds: cancellable_wait(seconds, check_cancelled),
    )
    check_cancelled()
    if not uid or len(uid) > 20 or int(uid) <= 0:
        raise ProbeError("扫码成功，但 B 站未提供有效 UID，无法检查")
    account = {"uid": uid, "name": "", "face": ""}
    if sessdata:
        try:
            code, data = web_get(
                context, "/x/web-interface/nav", {}, sessdata,
                "https://www.bilibili.com/", "核对网页登录",
            )
            if code != 0 or data.get("isLogin") is not True or str(data.get("mid")) != uid:
                sessdata = ""
            else:
                name, face = data.get("uname"), data.get("face")
                account["name"] = name if isinstance(name, str) else ""
                if isinstance(face, str):
                    parsed = urllib.parse.urlsplit(face)
                    if parsed.scheme == "https" and (parsed.hostname or "").endswith(".hdslb.com"):
                        account["face"] = face
        except (ProbeError, OSError, ValueError):
            sessdata = ""
    return account, token, sessdata


def follower_count(uid, context):
    code, data = web_get(
        context, "/x/relation/stat", {"vmid": uid}, None,
        f"https://space.bilibili.com/{uid}/", "核对粉丝总数",
    )
    total = data.get("follower")
    if code != 0 or str(data.get("mid")) != uid or type(total) is not int or total < 0:
        raise ProbeError(f"无法读取 B 站粉丝总数（code={code}）")
    return total


def capture_contacts(uid, token, context, delay, progress, check_cancelled):
    """Follow the Contacts cursor to the end; the platform count is advisory."""
    def read_total():
        try:
            return follower_count(uid, context)
        except (ProbeError, OSError):
            return None

    check_cancelled()
    total = read_total()
    cursor = b""
    seen_cursors = set()
    by_uid = {}
    page = 0
    while True:
        check_cancelled()
        page += 1
        try:
            fans, next_cursor = parse_page(fetch_contacts(token, context, cursor))
        except ProbeError as exc:
            raise ProbeError(f"通讯录第 {page} 页：{exc}") from None
        uids = {fan["uid"] for fan in fans}
        if len(uids) != len(fans) or not by_uid.keys().isdisjoint(uids):
            raise ProbeError(f"通讯录第 {page} 页出现重复 UID，本次不保存快照")
        for fan in fans:
            by_uid[fan["uid"]] = {"uid": fan["uid"], "name": fan["uname"]}
        progress({"phase": "fetching", "message": f"已读取 {len(by_uid)} 位粉丝",
                  "page": page, "collected": len(by_uid), "total": total})
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise ProbeError("通讯录返回重复游标，本次不保存快照")
        cursor = next_cursor
        seen_cursors.add(cursor)
        cancellable_wait(delay, check_cancelled)
    progress({"phase": "validating", "message": "通讯录已读到末页，正在读取粉丝总数作参考",
              "page": page, "collected": len(by_uid), "total": total})
    check_cancelled()
    final_total = read_total()
    check_cancelled()
    reference_total = final_total if final_total is not None else total
    warnings = []
    if total is not None and final_total is not None and total != final_total:
        warnings.append(f"B 站显示的粉丝总数在检查期间从 {total} 变为 {final_total}，结果仅反映名单变化")
    elif final_total is None:
        warnings.append("未能获取检查结束时的 B 站粉丝总数，仍按通讯录名单比较")
    if reference_total is not None and len(by_uid) != reference_total:
        timing = "（检查开始时）" if final_total is None else ""
        warnings.append(f"B 站显示 {reference_total} 位粉丝{timing}，通讯录读到 {len(by_uid)} 个唯一 UID；按实际名单比较")
    return ApiResult(list(by_uid.values()), True, page, reference_total, warnings)


def parse_follow_time(value):
    try:
        if isinstance(value, (int, str)) and not isinstance(value, bool) and int(value) > 0:
            return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def fetch_recent_details(uid, sessdata, context, wanted, delay, check_cancelled):
    """Refresh metadata for listed UIDs from at most 1000 web rows, never membership."""
    remaining = set(wanted)
    if not remaining:
        return
    for page in range(1, BILIBILI_VISIBLE_LIMIT // 50 + 1):
        check_cancelled()
        code, data = web_get(
            context, "/x/relation/followers", {"vmid": uid, "order": "desc", "pn": page, "ps": 50},
            sessdata, f"https://space.bilibili.com/{uid}/fans/fans", "刷新最近粉丝信息",
        )
        rows = data.get("list")
        if code != 0 or not isinstance(rows, list):
            raise TrackerError(f"网页粉丝接口补充失败（code={code}）")
        for row in rows[:50]:
            if not isinstance(row, dict) or str(row.get("mid")) not in remaining:
                continue
            mid = str(row["mid"])
            remaining.remove(mid)
            detail = {}
            followed = parse_follow_time(row.get("mtime"))
            if followed:
                detail["followed_at"] = followed
            attribute = str(row.get("attribute"))
            if attribute in {"0", "2", "6"}:
                detail["mutual"] = attribute == "6"
                detail["relationship"] = "mutual" if detail["mutual"] else "follower"
            yield mid, detail
        if not remaining or len(rows) < 50:
            break
        if page < BILIBILI_VISIBLE_LIMIT // 50:
            cancellable_wait(delay, check_cancelled)


def follow_direction(value, target):
    if not isinstance(value, dict) or type(value.get("attribute")) is not int:
        return None
    attribute = value.get("attribute")
    mid = str(value.get("mid"))
    if mid == target:
        if attribute in (2, 6):
            return True
        if attribute in (0, 128):
            return False
    if mid in ("0", "None") and attribute == 0:
        return False
    return None


def fetch_current_relationship(uid, target, sessdata, context):
    code, data = web_get(
        context, "/x/web-interface/relation", {"mid": target}, sessdata,
        f"https://space.bilibili.com/{target}/", "补查关系",
    )
    if code != 0:
        raise ProbeError(f"关系接口返回 code={code}")
    follows_me = follow_direction(data.get("be_relation"), uid)
    i_follow = follow_direction(data.get("relation"), target)
    result = {}
    if i_follow is not None and follows_me is not None:
        result["relationship"] = {
            (True, True): "mutual", (True, False): "following",
            (False, True): "follower", (False, False): "stranger",
        }[i_follow, follows_me]
    if follows_me is not None:
        result["follows_me"] = follows_me
    if follows_me:
        followed = parse_follow_time(data["be_relation"].get("mtime"))
        if followed:
            result["followed_at"] = followed
    return result
