#!/usr/bin/env python3
"""Test Bilibili app QR login and cursor pagination for the signed-in account's fans."""

from __future__ import annotations

import argparse
import getpass
import gzip
import hashlib
import json
import math
import secrets
import socket
import ssl
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path


# Publicly documented client parameters, not user login credentials.
# Login protocol reference: BBDownNext v2.3.0, BBDown.Core/Auth/Login.App.cs.
LOGIN_CLIENTS = {
    "app": ("783bbb7264451d82", "2653583c8873dea268ab9386918b1d65", "android"),
    "tv": ("4409e2ce8ffd12b8", "59b43e04ad6965f34319062b478f83dd", "android_tv_yst"),
}
QR_GENERATE = "https://passport.snm0516.aisee.tv/x/passport-tv-login/qrcode/auth_code"
QR_POLL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll"
CONTACTS_HOST = "grpc.biliapi.net"
CONTACTS_PATH = "/bilibili.app.im.v1.im/Contacts"
QR_TIMEOUT = 175
NETWORK_TIMEOUT = 12
MAX_RESPONSE = 1024 * 1024
WEB_API = "https://api.bilibili.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
)


class ProbeError(Exception):
    """A failure safe to print without exposing a credential or response body."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def tls_context(ca_file: Path | None = None) -> ssl.SSLContext:
    if ca_file is not None:
        return ssl.create_default_context(cafile=str(ca_file))

    context = ssl.create_default_context()
    if context.cert_store_stats()["x509_ca"] == 0:
        # The MSYS2 UCRT Python on this machine also has an MSYS CA bundle.
        msys_root = Path(sys.base_prefix).parent
        bundle = msys_root / "usr" / "ssl" / "certs" / "ca-bundle.crt"
        if bundle.is_file():
            context.load_verify_locations(cafile=str(bundle))
    return context


def random_id(length: int) -> str:
    alphabet = string.ascii_letters + string.digits + "_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def login_params(client_key: str, platform: str) -> dict[str, str]:
    device_id = random_id(20)
    buvid = random_id(37)
    fingerprint = datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3] + random_id(45)
    return {
        "appkey": client_key,
        "auth_code": "",
        "bili_local_id": device_id,
        "build": "102801",
        "buvid": buvid,
        "channel": "master",
        "device": "OnePlus",
        "device_id": device_id,
        "device_name": "OnePlus7TPro",
        "device_platform": "Android10OnePlusHD1910",
        "fingerprint": fingerprint,
        "guid": buvid,
        "local_fingerprint": fingerprint,
        "local_id": buvid,
        "mobi_app": platform,
        "networkstate": "wifi",
        "platform": "android",
        "sys_ver": "29",
        "ts": str(int(time.time())),
    }


def signed_form(params: dict[str, str], client_secret: str) -> bytes:
    query = urllib.parse.urlencode(sorted(params.items()), quote_via=urllib.parse.quote)
    signature = hashlib.md5((query + client_secret).encode("ascii")).hexdigest()
    return (query + "&sign=" + signature).encode("ascii")


def passport_post(opener, url, params, client_secret, stage) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=signed_form(params, client_secret),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    return read_api_json(opener, request, stage)


def read_api_json(opener, request, stage) -> tuple[int, dict]:
    try:
        with opener.open(request, timeout=NETWORK_TIMEOUT) as response:
            body = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        raise ProbeError(f"{stage}失败（HTTP {exc.code}）") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise ProbeError(f"{stage}失败，TLS 证书校验未通过，可通过 --ca-file 指定证书包") from None
        raise ProbeError(f"{stage}失败（{type(exc.reason).__name__}）") from None

    try:
        if len(body) > MAX_RESPONSE:
            raise ValueError
        payload = json.loads(body)
        code = int(payload["code"])
    except (ValueError, KeyError, TypeError):
        raise ProbeError(f"{stage}返回了无法识别的 JSON 响应") from None
    data = payload.get("data")
    return code, data if isinstance(data, dict) else {}


def show_qr(url: str) -> None:
    import qrcode

    validate_qr_url(url)
    print("请用手机 B 站 APP 扫描二维码并确认登录，可按 Ctrl+C 取消。", flush=True)
    qr = qrcode.QRCode(border=4)
    qr.add_data(url)
    qr.print_ascii(tty=sys.stdout.isatty())


def validate_qr_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "passport.bilibili.com", "passport.snm0516.aisee.tv"
    } or parsed.username is not None or parsed.password is not None:
        raise ProbeError("B 站返回了无法识别的扫码地址")


def qr_login(
    mode: str, context: ssl.SSLContext, *, include_sessdata: bool = False,
    on_qr=show_qr, on_scan=None, wait=time.sleep,
) -> tuple[str, str, str]:
    app_key, app_secret, platform = LOGIN_CLIENTS[mode]
    opener = urllib.request.build_opener(
        NoRedirect(), urllib.request.HTTPSHandler(context=context)
    )
    params = login_params(app_key, platform)
    code, data = passport_post(opener, QR_GENERATE, params, app_secret, "申请二维码")
    if code != 0:
        raise ProbeError(f"申请二维码失败（B 站 code={code}）")
    url, auth_code = data.get("url"), data.get("auth_code")
    if not isinstance(url, str) or not isinstance(auth_code, str) or not auth_code:
        raise ProbeError("二维码响应缺少扫码地址或会话标识")
    deadline = time.monotonic() + QR_TIMEOUT
    validate_qr_url(url)
    on_qr(url)
    params.update(auth_code=auth_code)
    needs_confirmation = False
    while time.monotonic() < deadline:
        wait(2)
        params["ts"] = str(int(time.time()))
        code, data = passport_post(opener, QR_POLL, params, app_secret, "扫码登录")
        if code == 0:
            token_info = data.get("token_info")
            if not isinstance(token_info, dict):
                token_info = {}
            token = data.get("access_token") or token_info.get("access_token")
            uid = str(data.get("mid") or token_info.get("mid") or "")
            if not isinstance(token, str) or not token:
                raise ProbeError("扫码成功，但响应缺少 access_token")
            cookie = scan_sessdata(data) if include_sessdata else ""
            return token, uid if uid.isascii() and uid.isdecimal() else "", cookie
        if code == 86038:
            break
        if code == 86090 and not needs_confirmation:
            if on_scan is None:
                print("已扫码，请在手机上确认。", flush=True)
            else:
                on_scan()
            needs_confirmation = True
        if code not in (86039, 86090, 86101):
            raise ProbeError(f"扫码登录失败（B 站 code={code}）")
    raise ProbeError("二维码已过期，请重新扫码")


def scan_sessdata(data: dict) -> str:
    cookie_info = data.get("cookie_info")
    if not isinstance(cookie_info, dict):
        return ""
    cookies = cookie_info.get("cookies")
    if not isinstance(cookies, list):
        return ""
    for cookie in cookies:
        if isinstance(cookie, dict) and cookie.get("name") == "SESSDATA":
            value = cookie.get("value")
            return value if isinstance(value, str) else ""
    return ""


def fetch_contacts(access_key: str, context: ssl.SSLContext, cursor: bytes = b"") -> bytes:
    import h2.config
    import h2.connection
    import h2.events

    if not access_key or any(not 33 <= ord(char) <= 126 for char in access_key):
        raise ProbeError("令牌格式无效，请使用单行 APP access_key")

    context.set_alpn_protocols(["h2"])
    connection = h2.connection.H2Connection(
        config=h2.config.H2Configuration(header_encoding="utf-8")
    )
    headers = [
        (":method", "POST"),
        (":scheme", "https"),
        (":authority", CONTACTS_HOST),
        (":path", CONTACTS_PATH),
        ("content-type", "application/grpc"),
        ("te", "trailers"),
        ("grpc-accept-encoding", "identity, gzip"),
        ("authorization", "identify_v1 " + access_key, True),
    ]
    metadata = {}
    body = bytearray()
    # Protocol fields observed in the contributor's Contacts test scripts.
    request = b"\x08\x03"
    if cursor:
        request += b"\x12" + encode_varint(len(cursor)) + cursor
    frame = b"\x00" + len(request).to_bytes(4, "big") + request
    with socket.create_connection((CONTACTS_HOST, 443), timeout=NETWORK_TIMEOUT) as tcp:
        with context.wrap_socket(tcp, server_hostname=CONTACTS_HOST) as tls:
            if tls.selected_alpn_protocol() != "h2":
                raise ProbeError("通讯录服务器未协商 HTTP/2")
            connection.initiate_connection()
            stream = connection.get_next_available_stream_id()
            connection.send_headers(stream, headers)
            connection.send_data(stream, frame, end_stream=True)
            tls.sendall(connection.data_to_send())
            ended = False
            while not ended:
                chunk = tls.recv(65536)
                if not chunk:
                    raise ProbeError("通讯录服务器在响应完成前关闭了连接")
                for event in connection.receive_data(chunk):
                    if isinstance(event, (h2.events.ResponseReceived, h2.events.TrailersReceived)):
                        metadata.update(event.headers)
                    elif isinstance(event, h2.events.DataReceived):
                        body.extend(event.data)
                        if len(body) > MAX_RESPONSE:
                            raise ProbeError("通讯录响应超过测试脚本的大小上限")
                        connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, h2.events.StreamReset):
                        raise ProbeError("通讯录服务器重置了请求流")
                    elif isinstance(event, h2.events.StreamEnded):
                        ended = True
                pending = connection.data_to_send()
                if pending and not ended:
                    tls.sendall(pending)

    http_status = metadata.get(":status", "")
    grpc_status = metadata.get("grpc-status", "")
    if http_status != "200" or grpc_status != "0":
        http_status = http_status if http_status.isascii() and http_status.isdecimal() else "missing"
        grpc_status = grpc_status if grpc_status.isascii() and grpc_status.isdecimal() else "missing"
        raise ProbeError(f"通讯录查询失败（http={http_status}，grpc-status={grpc_status}）")
    if len(body) < 5 or int.from_bytes(body[1:5], "big") != len(body) - 5:
        raise ProbeError("通讯录响应缺少完整的 gRPC 消息帧")
    if body[0] == 0:
        return bytes(body[5:])
    if body[0] == 1 and metadata.get("grpc-encoding") == "gzip":
        try:
            with gzip.GzipFile(fileobj=BytesIO(body[5:])) as compressed:
                payload = compressed.read(MAX_RESPONSE + 1)
        except (OSError, EOFError):
            raise ProbeError("通讯录响应的 gzip 数据损坏") from None
        if len(payload) > MAX_RESPONSE:
            raise ProbeError("通讯录响应解压后超过大小上限")
        return payload
    raise ProbeError("通讯录响应使用了不支持的压缩格式")


def encode_varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset == len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            if value < 2**64:
                return value, offset
            break
    raise ProbeError("通讯录响应含无效的 Protobuf 整数")


def protobuf_fields(data: bytes):
    offset = 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        field, wire = divmod(tag, 8)
        if not field:
            raise ProbeError("通讯录响应含无效的 Protobuf 字段编号")
        if wire == 0:
            value, offset = read_varint(data, offset)
        else:
            if wire == 2:
                size, offset = read_varint(data, offset)
            elif wire == 1:
                size = 8
            elif wire == 5:
                size = 4
            else:
                raise ProbeError("通讯录响应含未知的 Protobuf 字段类型")
            end = offset + size
            if end > len(data):
                raise ProbeError("通讯录响应的 Protobuf 字段不完整")
            value = data[offset:end]
            offset = end
        yield field, wire, value


def describe_fields(fields, depth: int = 0, *, contact: bool = False) -> list[dict]:
    """Describe wire values without assigning business meanings to unknown tags."""
    result = []
    for number, wire, value in fields:
        field = {"field": number, "wire": wire}
        if wire == 0:
            field["value"] = value
            if value >= 2**63:
                field["int64"] = value - 2**64
        elif wire in (1, 5):
            field["hex"] = value.hex()
            field["unsigned_le"] = int.from_bytes(value, "little")
        else:
            field["bytes"] = len(value)
            try:
                decoded = value.decode("utf-8")
            except UnicodeDecodeError:
                decoded = None
            is_text = decoded is not None and all(
                c.isprintable() or c in "\r\n\t" for c in decoded
            )
            if is_text:
                field["text"] = decoded
                if decoded.lstrip().startswith(("{", "[")):
                    try:
                        field["json"] = json.loads(decoded)
                    except (ValueError, RecursionError):
                        pass
            inspect_nested = not is_text or (contact and number == 3)
            if inspect_nested:
                field["hex"] = value.hex()
            if inspect_nested and depth < 5:
                try:
                    nested = list(protobuf_fields(value))
                except ProbeError:
                    nested = []
                if nested:
                    field["nested_candidate"] = describe_fields(nested, depth + 1)
        result.append(field)
    return result


def parse_page(payload: bytes, *, raw_fields: bool = False) -> tuple[list[dict], bytes]:
    fans = []
    cursor = b""
    for field, wire, value in protobuf_fields(payload):
        if (field, wire) == (4, 2):
            cursor = value
        if (field, wire) != (1, 2):
            continue
        raw_contact = list(protobuf_fields(value))
        fields = {(number, kind): item for number, kind, item in raw_contact}
        uid = fields.get((1, 0), 0)
        if not uid:
            raise ProbeError("一条通讯录记录缺少有效 UID")
        fan = {
            "uid": str(uid),
            "uname": fields.get((2, 2), b"").decode("utf-8", errors="replace"),
            "face": fields.get((8, 2), b"").decode("utf-8", errors="replace"),
        }
        if raw_fields:
            nested_4 = None
            try:
                for number, wire, item in protobuf_fields(fields.get((3, 2), b"")):
                    if (number, wire) == (4, 0):
                        nested_4 = item
            except ProbeError:
                # Field 3 is still undocumented; only report a cleanly decoded value.
                nested_4 = None
            field_9 = fields.get((9, 0))
            if field_9 is not None and field_9 >= 2**63:
                field_9 -= 2**64
            fan.update({
                "3.4": nested_4, "9": field_9, "fields": describe_fields(raw_contact, contact=True)
            })
        fans.append(fan)
    if cursor and not fans:
        raise ProbeError("通讯录返回了游标，但没有解出粉丝记录")
    return fans, cursor


def web_get(context, path, params, sessdata, referer, stage) -> tuple[int, dict]:
    if sessdata is not None and (
        not sessdata or any(not 33 <= ord(char) <= 126 or char == ";" for char in sessdata)
    ):
        raise ProbeError("请输入单独的 SESSDATA 值")
    url = WEB_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Referer": referer,
        "User-Agent": USER_AGENT,
    }
    if sessdata is not None:
        headers["Cookie"] = "SESSDATA=" + sessdata
    request = urllib.request.Request(url, headers=headers)
    # urllib sends HTTP/1.1. fetch_contacts previously set this context to h2.
    context.set_alpn_protocols(["http/1.1"])
    opener = urllib.request.build_opener(
        NoRedirect(), urllib.request.HTTPSHandler(context=context)
    )
    return read_api_json(opener, request, stage)


def checked_sessdata(uid: str, sessdata: str, context, *, allow_prompt: bool = True) -> str:
    if not uid or uid == "0":
        raise ProbeError("扫码响应没有有效的账号 UID，无法确认网页凭证属于同一账号")
    if not sessdata:
        if not allow_prompt:
            raise ProbeError("本次扫码没有返回 SESSDATA，无法验证旧版粉丝接口（未手动补填）")
        print("本次扫码响应没有附带 SESSDATA。可在本机隐藏输入同账号的 SESSDATA。")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                sessdata = getpass.getpass("SESSDATA（留空则只显示名单）：").strip()
            except (EOFError, getpass.GetPassWarning):
                raise ProbeError("请在交互式 PowerShell 中运行，以便隐藏输入 SESSDATA") from None
        if not sessdata:
            return ""
    code, account = web_get(
        context, "/x/web-interface/nav", {}, sessdata, "https://www.bilibili.com/", "核对网页登录"
    )
    if code != 0 or account.get("isLogin") is not True or str(account.get("mid")) != uid:
        raise ProbeError(f"网页凭证无效或与扫码账号不一致（B 站 code={code}）")
    return sessdata


def format_follow_time(timestamp: object) -> str:
    try:
        if isinstance(timestamp, str) and timestamp.isascii() and timestamp.isdecimal():
            timestamp = int(timestamp)
        if type(timestamp) is int and timestamp > 0:
            return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().isoformat(
                sep=" ", timespec="seconds"
            )
    except (ValueError, OverflowError, OSError):
        pass
    return "未知"


def probe_web_first_page(uid: str, sessdata: str, context) -> None:
    sessdata = checked_sessdata(uid, sessdata, context, allow_prompt=False)
    print(f"已用扫码返回的 SESSDATA 核对网页登录（UID={uid}）。", flush=True)
    code, data = web_get(
        context, "/x/relation/followers", {"vmid": uid, "order": "desc", "pn": 1, "ps": 50},
        sessdata, f"https://space.bilibili.com/{uid}/fans/fans", "读取旧版粉丝接口第一页"
    )
    if code != 0:
        raise ProbeError(f"旧版粉丝接口第一页读取失败（B 站 code={code}）")
    rows = data.get("list")
    if not isinstance(rows, list):
        raise ProbeError("旧版粉丝接口未返回 data.list，无法验证第一页名单")
    total = data.get("total")
    total_label = str(total) if type(total) is int and total >= 0 else "未知"
    if not rows and total_label != "0":
        raise ProbeError(f"旧版粉丝接口返回空名单（粉丝总数：{total_label}），未能验证可读取名单")
    print(f"旧版粉丝接口第一页返回 {len(rows)} 条（粉丝总数：{total_label}）。", flush=True)
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ProbeError(f"旧版粉丝接口第 {index} 条粉丝记录格式异常")
        mid = str(row.get("mid", ""))
        if not mid.isascii() or not mid.isdecimal() or mid == "0":
            raise ProbeError(f"旧版粉丝接口第 {index} 条粉丝记录缺少有效 UID")
        name = row.get("uname")
        nickname = json.dumps(name if isinstance(name, str) else "", ensure_ascii=False)
        followed_at = format_follow_time(row.get("mtime"))
        attribute = str(row.get("attribute", ""))
        mutual = ("是" if attribute == "6" else "否") if attribute.isascii() and attribute.isdecimal() else "未知"
        print(f"#{index}  UID={mid}  昵称={nickname}  关注时间={followed_at}  互关={mutual}", flush=True)


def follower_relationship(data: dict, own_uid: str, fan_uid: str) -> tuple[str, str]:
    incoming = data.get("be_relation")
    if not isinstance(incoming, dict) or str(incoming.get("mid")) != own_uid:
        return "未知", "未知"
    if incoming.get("attribute") not in (2, 6):
        return "未知", "未知"

    followed_at = format_follow_time(incoming.get("mtime"))

    mutual = "未知"
    outgoing = data.get("relation")
    if isinstance(outgoing, dict):
        target = str(outgoing.get("mid"))
        attribute = outgoing.get("attribute")
        if target == fan_uid and attribute in (2, 6):
            mutual = "是"
        elif (target == fan_uid and attribute in (0, 128)) or (
            target in ("0", "None") and attribute == 0
        ):
            mutual = "否"
    return followed_at, mutual


def check_follower_fields(access_key: str, uid: str, sessdata: str, context, delay: float) -> None:
    fans, _ = parse_page(fetch_contacts(access_key, context))
    print(f"通讯录第一页共 {len(fans)} 条。", flush=True)
    if not fans:
        return

    stop_reason = ""
    try:
        sessdata = checked_sessdata(uid, sessdata, context)
    except ProbeError as exc:
        stop_reason = str(exc)
    except OSError as exc:
        stop_reason = f"核对网页登录失败（{type(exc).__name__}）"
    if not sessdata and not stop_reason:
        stop_reason = "没有网页凭证"
    if stop_reason:
        print(f"关系补查不可用。{stop_reason}。", file=sys.stderr)

    print("关注时间按本机时区显示，格式末尾带时区偏移。", flush=True)
    for index, fan in enumerate(fans, 1):
        mid = fan["uid"]
        followed_at = mutual = "未知"
        note = ""
        if not stop_reason:
            time.sleep(delay)
            try:
                code, data = web_get(
                    context, "/x/web-interface/relation", {"mid": mid}, sessdata,
                    f"https://space.bilibili.com/{mid}", f"第 {index} 条双向关系查询"
                )
            except ProbeError as exc:
                stop_reason = str(exc)
            except OSError as exc:
                stop_reason = f"请求失败（{type(exc).__name__}）"
            else:
                if code == 0:
                    followed_at, mutual = follower_relationship(data, uid, mid)
                else:
                    note = f"  补查返回 code={code}"
                    if code in (-101, -352, -412, -509):
                        stop_reason = f"B 站返回 code={code}"
            if stop_reason:
                print(f"已停止关系补查。{stop_reason}。", file=sys.stderr)
        nickname = json.dumps(fan["uname"], ensure_ascii=False)
        print(
            f"#{index}  UID={mid}  昵称={nickname}  关注时间={followed_at}  互关={mutual}{note}",
            flush=True,
        )


def probe_pages(
    access_key: str,
    context: ssl.SSLContext,
    pages: int,
    delay: float,
    summary_only: bool,
) -> None:
    seen_uids: set[str] = set()
    seen_cursors: set[bytes] = set()
    cursor = b""
    total_rows = 0
    completed_pages = 0
    try:
        for page in range(1, pages + 1):
            if page > 1:
                time.sleep(delay)
            try:
                fans, next_cursor = parse_page(fetch_contacts(access_key, context, cursor))
            except ProbeError as exc:
                raise ProbeError(f"第 {page} 页：{exc}") from None

            new_fans = []
            for fan in fans:
                uid = fan["uid"]
                if uid not in seen_uids:
                    seen_uids.add(uid)
                    new_fans.append(fan)
            completed_pages += 1
            total_rows += len(fans)
            print(
                f"第 {page} 页  http=200  grpc-status=0  本页 {len(fans)} 条，"
                f"新增 UID {len(new_fans)} 个，累计 {len(seen_uids)} 个不同 UID",
                flush=True,
            )
            if not summary_only:
                for fan in new_fans:
                    print(json.dumps(fan, ensure_ascii=False))

            if next_cursor and next_cursor in seen_cursors:
                raise ProbeError(f"第 {page} 页返回了重复游标，已停止翻页")
            if page > 1 and fans and not new_fans:
                raise ProbeError(f"第 {page} 页的 UID 全部已出现，已停止翻页")
            if not next_cursor:
                print("接口未返回下一页游标。")
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            print("已达到指定页数，接口仍返回了下一页游标。")
    finally:
        if completed_pages:
            print(
                f"汇总：成功读取 {completed_pages} 页，"
                f"累计 {total_rows} 条，去重后 {len(seen_uids)} 个 UID",
                flush=True,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="扫码登录并测试连续读取自己的 B 站粉丝")
    parser.add_argument("--mode", choices=LOGIN_CLIENTS, default="app", help="扫码模式，默认 app")
    parser.add_argument("--key-file", type=Path, help="使用已有的单行 APP 令牌文件，跳过扫码")
    parser.add_argument("--ca-file", type=Path, help="自定义 TLS 根证书包")
    parser.add_argument("--pages", type=int, default=1, help="最多读取的页数，默认 1")
    parser.add_argument("--delay", type=float, default=1.0, help="翻页或关系补查的请求间隔秒数，默认 1，最少 1")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--summary-only", action="store_true", help="只输出每页计数和汇总")
    output.add_argument("--check-fields", action="store_true", help="扫码后列出第一页粉丝的 UID、昵称、关注时间和互关")
    output.add_argument("--raw-fields", action="store_true", help="只读取通讯录第一页并展开全部原始字段")
    output.add_argument("--web-first-page", action="store_true", help="仅用扫码返回的 SESSDATA 读取旧版网页粉丝接口第一页")
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("--pages 必须是正整数")
    if not math.isfinite(args.delay) or args.delay < 1:
        parser.error("--delay 必须是大于等于 1 的有限秒数")
    if args.check_fields and (args.key_file or args.pages != 1):
        parser.error("--check-fields 需要扫码，并且只查询第一页")
    if args.raw_fields and args.pages != 1:
        parser.error("--raw-fields 只查询第一页")
    if args.web_first_page and (args.key_file or args.pages != 1):
        parser.error("--web-first-page 需要扫码，并且只查询第一页")
    try:
        import h2.exceptions

        context = tls_context(args.ca_file)
        if args.key_file is not None:
            access_key = args.key_file.read_text(encoding="utf-8-sig").strip()
            if access_key.startswith("identify_v1 "):
                access_key = access_key.removeprefix("identify_v1 ").strip()
        else:
            import qrcode  # Check before creating a login session.

            access_key, uid, sessdata = qr_login(
                args.mode, context, include_sessdata=args.check_fields or args.web_first_page
            )
            if args.web_first_page:
                probe_web_first_page(uid, sessdata, context)
                return 0
            if args.check_fields:
                check_follower_fields(access_key, uid, sessdata, context, args.delay)
                return 0
            print(f"扫码成功{f'（UID {uid}）' if uid else ''}，正在读取粉丝。", flush=True)
        if args.raw_fields:
            payload = fetch_contacts(access_key, context)
            fans, cursor = parse_page(payload, raw_fields=True)
            print(f"通讯录原始字段，第一页共 {len(fans)} 条（null 表示未读到该字段）。")
            page_fields = describe_fields(
                (number, wire, value) for number, wire, value in protobuf_fields(payload)
                if (number, wire) not in ((1, 2), (4, 2))
            )
            print(json.dumps({"page_fields": page_fields, "cursor_bytes": len(cursor)}, ensure_ascii=False))
            for fan in fans:
                print(json.dumps({key: fan[key] for key in ("uid", "3.4", "9", "fields")}, ensure_ascii=False))
            return 0
        probe_pages(access_key, context, args.pages, args.delay, args.summary_only)
    except ModuleNotFoundError as exc:
        print(f"缺少 Python 依赖（{exc.name}）。请在测试虚拟环境中安装 h2 qrcode。", file=sys.stderr)
        return 1
    except ssl.SSLCertVerificationError:
        print("TLS 证书校验失败，可通过 --ca-file 指定证书包。", file=sys.stderr)
        return 1
    except ProbeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, h2.exceptions.H2Error) as exc:
        print(f"测试失败（{type(exc).__name__}），请检查凭证文件、终端编码或网络连接。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
