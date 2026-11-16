#!/usr/bin/env python3
"""QQ 邮箱管理 CLI（IMAP 收件/搜索/归档 + SMTP 发信）。

只用 Python 标准库：imaplib / smtplib / email / ctypes（Windows DPAPI）。
凭据保存在 %USERPROFILE%\\.dsh\\qq-mail\\accounts.json，密码字段用 DPAPI 加密：
文件被拷到别的 Windows 用户或别的机器上都无法解密。

退出码：0 成功，2 用法错误，3 认证失败，4 网络/协议错误，5 目标不存在。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as dt
import email
import email.policy
import email.utils
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------------- constants

CONFIG_DIR = Path(os.environ.get("DSH_QQ_MAIL_HOME") or (Path.home() / ".dsh" / "qq-mail"))
CONFIG_PATH = CONFIG_DIR / "accounts.json"

DEFAULT_IMAP_HOST = "imap.qq.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_HOST = "smtp.qq.com"
DEFAULT_SMTP_PORT = 465

TIMEOUT = 30
BODY_LIMIT = 4000
LIST_LIMIT = 20

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# QQ 邮箱常见文件夹（IMAP 名 → 中文说明）
FOLDER_ALIASES = {
    "INBOX": "收件箱",
    "Sent Messages": "已发送",
    "Drafts": "草稿箱",
    "Deleted Messages": "已删除",
    "Junk": "垃圾邮件",
}


class MailError(Exception):
    """带退出码与修复建议的业务错误。"""

    def __init__(self, message: str, code: int = 4, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint


def out(text: str = "") -> None:
    print(text)


def emit_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- config

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"version": 1, "accounts": {}}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MailError(f"配置文件损坏：{CONFIG_PATH}（{exc}）", 2,
                        "修复或删除该文件后重新 accounts add。")


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def resolve_account(cfg: dict, name: str | None) -> tuple[str, dict]:
    accounts = cfg.get("accounts") or {}
    if not accounts:
        raise MailError("还没有配置任何 QQ 账号", 2,
                        "先运行：mail.py accounts add --email you@qq.com")
    if name:
        if name not in accounts:
            raise MailError(f"未知账号 {name!r}；现有：{', '.join(sorted(accounts))}", 2)
        return name, accounts[name]
    if len(accounts) == 1:
        return next(iter(accounts.items()))
    for key, value in accounts.items():
        if value.get("default"):
            return key, value
    raise MailError("配置了多个账号，请用 --account 指定；或给某个账号加 default: true", 2)


# ---------------------------------------------------------------- DPAPI

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


CRYPTPROTECT_UI_FORBIDDEN = 0x01


def _to_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def dpapi_protect(plain: str) -> str:
    """用当前 Windows 用户的 DPAPI 加密，返回 'dpapi:<base64>'。"""
    if os.name != "nt":
        raise MailError("DPAPI 仅支持 Windows", 2, "改用 --plaintext 保存明文凭据。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    in_blob, keep = _to_blob(plain.encode("utf-8"))
    out_blob = _Blob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), "qq-mail", None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    del keep
    if not ok:
        raise MailError(f"DPAPI 加密失败：{ctypes.WinError(ctypes.get_last_error())}", 4)
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
    return "dpapi:" + base64.b64encode(raw).decode("ascii")


def dpapi_unprotect(token: str) -> str:
    if not token.startswith("dpapi:"):
        raise MailError("凭据不是 DPAPI 格式", 3, "重新运行 accounts add 保存授权码。")
    if os.name != "nt":
        raise MailError("DPAPI 仅支持 Windows", 3)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    in_blob, keep = _to_blob(base64.b64decode(token[len("dpapi:"):]))
    out_blob = _Blob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    del keep
    if not ok:
        raise MailError(
            "DPAPI 解密失败：凭据属于其他 Windows 用户或已损坏", 3,
            "用当前用户重新运行：mail.py accounts add --email you@qq.com",
        )
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
    return raw.decode("utf-8")


def account_secret(account: dict) -> str:
    token = account.get("secret", "")
    if token.startswith("dpapi:"):
        return dpapi_unprotect(token)
    if token.startswith("plain:"):
        return base64.b64decode(token[len("plain:"):]).decode("utf-8")
    raise MailError("账号缺少凭据，请重新 accounts add", 3)


# ---------------------------------------------------------------- modified UTF-7

def mutf7_decode(text: str) -> str:
    """IMAP 文件夹名的 modified UTF-7 → str。"""
    result, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch != "&":
            result.append(ch)
            i += 1
            continue
        end = text.find("-", i)
        if end == -1:
            result.append("&")
            break
        chunk = text[i + 1:end]
        if not chunk:
            result.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                result.append(base64.b64decode(b64).decode("utf-16-be"))
            except Exception:
                result.append(text[i:end + 1])
        i = end + 1
    return "".join(result)


def mutf7_encode(text: str) -> str:
    """str → IMAP modified UTF-7。"""
    parts: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        raw = "".join(pending).encode("utf-16-be")
        b64 = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        parts.append("&" + b64 + "-")
        pending.clear()

    for ch in text:
        code = ord(ch)
        if 0x20 <= code <= 0x7E:
            flush()
            # RFC 3501：字面 & 必须写成 "&-"，否则解码端会把后面整段当成 base64
            parts.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(parts)


def quote_raw(encoded: str) -> str:
    """给已是 IMAP 编码的文件夹名加引号。"""
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_quote(name: str) -> str:
    """把人类可读的文件夹名编码成 IMAP 参数。"""
    return quote_raw(mutf7_encode(name))


# ---------------------------------------------------------------- IMAP

def imap_connect(account: dict) -> imaplib.IMAP4_SSL:
    host = account.get("imap_host", DEFAULT_IMAP_HOST)
    port = int(account.get("imap_port", DEFAULT_IMAP_PORT))
    secret = account_secret(account)
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=TIMEOUT,
                                 ssl_context=ssl.create_default_context())
    except (OSError, ssl.SSLError) as exc:
        raise MailError(f"连接 {host}:{port} 失败：{exc}", 4,
                        "检查网络或代理，确认 993 端口未被拦截。")
    try:
        conn.login(account["email"], secret)
    except imaplib.IMAP4.error as exc:
        raise MailError(f"IMAP 登录被拒绝：{exc}", 3,
                        "QQ 邮箱：设置 → 账号与安全 → 开启 IMAP/SMTP 服务并生成 16 位授权码；"
                        "确认填的是授权码而不是 QQ 密码，且没有多余空格。")
    return conn


def imap_close(conn) -> None:
    if conn is None:
        return
    try:
        conn.logout()
    except Exception:
        pass


LIST_PATTERN = re.compile(
    rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>NIL|"[^"]*")\s+(?P<rest>.*)$', re.S)


def list_folders(conn) -> list[dict]:
    typ, data = conn.list()
    if typ != "OK":
        raise MailError(f"LIST 失败：{data}", 4)
    folders = []
    for raw in data or []:
        if raw is None:
            continue
        if isinstance(raw, tuple):
            raw = b"".join(raw)
        line = raw.strip()
        match = LIST_PATTERN.match(line)
        if not match:
            continue
        flags = match.group("flags").decode("ascii", "replace").split()
        rest = match.group("rest")
        literal = re.match(rb'^\{(\d+)\}\r?\n(?P<body>.*)$', rest, re.S)
        if literal:
            name_bytes = literal.group("body")
        elif rest.startswith(b'"') and rest.endswith(b'"') and len(rest) >= 2:
            name_bytes = rest[1:-1].replace(b'\\\\', b'\\').replace(b'\\"', b'"')
        else:
            name_bytes = rest
        name = name_bytes.decode("utf-8", "replace").strip()
        folders.append({
            "raw": name,
            "name": mutf7_decode(name),
            "selectable": "\\Noselect" not in flags,
            "flags": flags,
        })
    return folders


def folder_status(conn, raw_name: str) -> dict:
    typ, data = conn.status(quote_raw(raw_name), "(MESSAGES UNSEEN)")
    result = {"messages": None, "unseen": None}
    if typ != "OK" or not data:
        return result
    first = data[0]
    text = first.decode("utf-8", "replace") if isinstance(first, bytes) else str(first)
    for key in ("MESSAGES", "UNSEEN"):
        found = re.search(rf"{key}\s+(\d+)", text)
        if found:
            result[key.lower()] = int(found.group(1))
    return result


def parse_fetch_flags(head: bytes) -> list[str]:
    found = re.search(rb"FLAGS \((?P<flags>[^)]*)\)", head)
    if not found:
        return []
    return sorted(flag.decode("ascii", "replace") for flag in found.group("flags").split())


def fetch_headers(conn, uid: str) -> tuple[bytes, list[str]]:
    fields = "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])"
    typ, data = conn.uid("FETCH", uid, fields)
    if typ != "OK":
        raise MailError(f"读取邮件 {uid} 失败：{data}", 4)
    flags: list[str] = []
    payload = b""
    for item in data or []:
        if isinstance(item, tuple):
            flags = parse_fetch_flags(item[0])
            payload = item[1]
        elif isinstance(item, bytes):
            found = re.search(rb"FLAGS \(([^)]*)\)", item)
            if found:
                flags = sorted(flag.decode("ascii", "replace") for flag in found.group(1).split())
    return payload, flags


def fetch_full(conn, uid: str) -> bytes:
    typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK":
        raise MailError(f"读取邮件 {uid} 失败：{data}", 4)
    for item in data or []:
        if isinstance(item, tuple):
            return item[1]
    raise MailError(f"邮件 {uid} 没有返回内容", 5)


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def header_summary(raw_headers: bytes) -> dict:
    msg = email.message_from_bytes(raw_headers, policy=email.policy.default)
    return {
        "from": decode_header_value(msg.get("From")),
        "to": decode_header_value(msg.get("To")),
        "cc": decode_header_value(msg.get("Cc")),
        "subject": decode_header_value(msg.get("Subject")),
        "date": decode_header_value(msg.get("Date")),
        "message_id": (msg.get("Message-ID") or "").strip(),
    }


def parse_since(value: str) -> str:
    """支持 7d / 4w / 2026-01-01 → IMAP 日期串（英文月份，避免受系统区域设置影响）。"""
    value = value.strip()
    match = re.fullmatch(r"(\d+)\s*([dwmy])", value, re.I)
    days = {"d": 1, "w": 7, "m": 30, "y": 365}
    if match:
        target = dt.datetime.now() - dt.timedelta(days=int(match.group(1)) * days[match.group(2).lower()])
    else:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                target = dt.datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            raise MailError(f"无法识别的日期 {value!r}", 2,
                            "用 7d、4w 或 2026-01-01 这样的写法。")
    return f"{target.day:02d}-{MONTHS[target.month - 1]}-{target.year}"


def looks_like_address(value: str) -> bool:
    return "@" in value


def build_criteria(args) -> list[str]:
    """只下发 QQ 服务端真能正确执行的条件。

    实测：QQ 的 FROM 需要完整地址（子串匹配 0 封），TEXT 被直接忽略（等价于 ALL），
    非 ASCII 条件（中文主题等）会让 imaplib 以 ascii 编码失败。这些一律改由本地有界扫描。
    """
    criteria: list[str] = []
    if getattr(args, "unread", False):
        criteria.append("UNSEEN")
    if getattr(args, "flagged", False):
        criteria.append("FLAGGED")
    from_term = getattr(args, "from_", None)
    if from_term and from_term.isascii() and looks_like_address(from_term):
        criteria += ["FROM", f'"{from_term}"']
    subject = getattr(args, "subject", None)
    if subject and subject.isascii():
        criteria += ["SUBJECT", f'"{subject}"']
    if getattr(args, "since", None):
        criteria += ["SINCE", parse_since(args.since)]
    return criteria or ["ALL"]


def apply_local_filters(conn, uids: list[str], args) -> tuple[list[str], list[str], int]:
    """在服务端结果上按需做本地过滤（发件人子串、中文主题、正文关键字）。"""
    from_term = getattr(args, "from_", None)
    subject_term = getattr(args, "subject", None)
    text_term = getattr(args, "text", None)
    need_from = bool(from_term) and not (from_term.isascii() and looks_like_address(from_term))
    need_subject = bool(subject_term) and not subject_term.isascii()
    need_text = bool(text_term)
    if not (need_from or need_subject or need_text):
        return uids, [], 0

    window = uids[: max(1, getattr(args, "scan", 300))]
    kept: list[str] = []
    for uid in window:
        raw, _ = fetch_headers(conn, uid)
        info = header_summary(raw) if raw else {}
        haystack = " ".join([info.get("from", ""), info.get("to", ""),
                             info.get("cc", ""), info.get("subject", "")]).lower()
        if need_from and from_term.lower() not in haystack:
            continue
        if need_subject and subject_term.lower() not in info.get("subject", "").lower():
            continue
        if need_text:
            msg = email.message_from_bytes(fetch_full(conn, uid),
                                           policy=email.policy.default)
            if text_term.lower() not in (extract_body(msg) + " " + info.get("subject", "")).lower():
                continue
        kept.append(uid)

    notes = []
    if need_from:
        notes.append(f'发件人「{from_term}」不是完整地址，已在最近 {len(window)} 封内本地匹配')
    if need_subject:
        notes.append(f'主题「{subject_term}」含非 ASCII 字符，已在最近 {len(window)} 封内本地匹配')
    if need_text:
        notes.append(f'正文关键字「{text_term}」在最近 {len(window)} 封内本地匹配'
                     '（QQ 服务端会忽略 TEXT 条件）')
    return kept, notes, len(window)


def select_folder(conn, folder: str, readonly: bool) -> None:
    typ, data = conn.select(imap_quote(folder), readonly=readonly)
    if typ != "OK":
        raise MailError(f"无法打开文件夹 {folder}：{data}", 5,
                        "用 folders 子命令查看可用文件夹名。")


def search_uids(conn, folder: str, criteria: list[str]) -> list[str]:
    select_folder(conn, folder, readonly=True)
    if any(not item.isascii() for item in criteria):
        raise MailError("内部错误：非 ASCII 条件不能下发给 QQ 服务端", 4,
                        "这属于用法层不该出现的情况，请把 filter 参数原样报告给维护者。")
    typ, data = conn.uid("SEARCH", None, *criteria)
    if typ != "OK":
        raise MailError(f"搜索失败：{data}", 4,
                        "QQ IMAP 对中文关键字支持不稳定，可改用英文片段或 --since 缩小范围。")
    uids = (data[0] or b"").split()
    return [uid.decode() for uid in reversed(uids)]


# ---------------------------------------------------------------- body / parts

def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    import html as html_mod
    return html_mod.unescape(text)


def normalize_text(text: str) -> str:
    """压掉 HTML 邮件常见的 &nbsp; 空行与行尾空白，保留段落结构。"""
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\r\n", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    collapsed = "\n".join(lines)
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    return collapsed.strip()


def extract_body(msg) -> str:
    plain, html = "", ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if not isinstance(content, str):
            continue
        if part.get_content_type() == "text/plain" and not plain:
            plain = content
        elif part.get_content_type() == "text/html" and not html:
            html = content
    body = plain or strip_html(html)
    return normalize_text(body)


def list_attachments(msg) -> list[dict]:
    items = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = decode_header_value(part.get_filename())
        if not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        items.append({
            "filename": filename,
            "content_type": part.get_content_type(),
            "size": len(payload),
            "_payload": payload,
        })
    return items


# ---------------------------------------------------------------- sending

def build_message(args, account: dict, *, in_reply_to: str = "",
                  references: str = "", quote: str = "") -> EmailMessage:
    msg = EmailMessage()
    sender = account["email"]
    display = account.get("display_name") or ""
    msg["From"] = email.utils.formataddr((display, sender)) if display else sender
    msg["To"] = ", ".join(args.to)
    if getattr(args, "cc", None):
        msg["Cc"] = ", ".join(args.cc)
    msg["Subject"] = args.subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=sender.split("@")[-1])
    if account.get("reply_to"):
        msg["Reply-To"] = account["reply_to"]
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to

    body = args.body or ""
    if getattr(args, "body_file", None):
        body = Path(args.body_file).read_text(encoding="utf-8")
    if quote:
        body = (body.rstrip() + "\n\n" if body.strip() else "") + quote
    msg.set_content(body)

    for path_str in getattr(args, "attach", None) or []:
        path = Path(path_str).expanduser()
        if not path.exists():
            raise MailError(f"附件不存在：{path}", 2)
        data = path.read_bytes()
        guess = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, _, subtype = guess.partition("/")
        if maintype == "text":
            try:
                msg.add_attachment(data.decode("utf-8"), subtype=subtype, filename=path.name)
                continue
            except UnicodeDecodeError:
                maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
    return msg


def collect_recipients(msg: EmailMessage) -> list[str]:
    recipients: list[str] = []
    for field in ("To", "Cc", "Bcc"):
        raw = msg.get(field)
        if raw:
            recipients += [addr for _, addr in email.utils.getaddresses([str(raw)]) if addr]
    return recipients


def smtp_send(account: dict, msg: EmailMessage) -> dict:
    host = account.get("smtp_host", DEFAULT_SMTP_HOST)
    port = int(account.get("smtp_port", DEFAULT_SMTP_PORT))
    secret = account_secret(account)
    recipients = collect_recipients(msg)
    if not recipients:
        raise MailError("没有收件人", 2)

    payload = msg.as_bytes()
    try:
        if port == 587:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
            server.starttls(context=ssl.create_default_context())
        else:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT,
                                     context=ssl.create_default_context())
        server.login(account["email"], secret)
        refused = server.send_message(msg)
        server.quit()
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(f"SMTP 认证失败：{exc}", 3,
                        "确认用的是 16 位授权码；刚重置过授权码就用 accounts add 更新。")
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"SMTP 发送失败：{exc}", 4,
                        "QQ 对第三方客户端有频率限制；报错含 too many/频率 时稍后再试。")
    return {"sent": True, "recipients": recipients, "bytes": len(payload),
            "refused": dict(refused) if refused else {}}


# ---------------------------------------------------------------- commands

def cmd_accounts(args) -> int:
    cfg = load_config()
    accounts = cfg.get("accounts") or {}

    if args.action == "list":
        rows = []
        for name, value in sorted(accounts.items()):
            token = str(value.get("secret", ""))
            rows.append({
                "name": name,
                "email": value.get("email", ""),
                "secret": "DPAPI 已加密" if token.startswith("dpapi:") else "明文",
                "default": bool(value.get("default")),
                "imap": f'{value.get("imap_host", DEFAULT_IMAP_HOST)}:{value.get("imap_port", DEFAULT_IMAP_PORT)}',
                "smtp": f'{value.get("smtp_host", DEFAULT_SMTP_HOST)}:{value.get("smtp_port", DEFAULT_SMTP_PORT)}',
            })
        if args.json:
            emit_json({"ok": True, "config": str(CONFIG_PATH), "accounts": rows})
            return 0
        if not rows:
            out(f"还没有账号。配置文件：{CONFIG_PATH}")
            out("添加：mail.py accounts add --email you@qq.com")
            return 0
        out(f"配置文件：{CONFIG_PATH}")
        for row in rows:
            flag = " [默认]" if row["default"] else ""
            out(f'- {row["name"]}{flag}  {row["email"]}  凭据：{row["secret"]}')
            out(f'    IMAP {row["imap"]}   SMTP {row["smtp"]}')
        return 0

    if args.action == "add":
        target_email = (args.email or "").strip()
        if not target_email or "@" not in target_email:
            raise MailError("缺少或非法的 --email", 2,
                            "mail.py accounts add --email you@qq.com")
        name = (args.name or target_email.split("@")[0]).strip()
        if args.password_stdin:
            secret = sys.stdin.readline().strip()
        elif args.password_file:
            secret = Path(args.password_file).read_text(encoding="utf-8").strip()
        else:
            import getpass
            secret = getpass.getpass("粘贴 QQ 邮箱 16 位授权码（输入不回显）：").strip()
        if not secret:
            raise MailError("授权码为空", 2,
                            "QQ 邮箱：设置 → 账号与安全 → 开启 IMAP/SMTP 服务后生成授权码。")
        token = ("plain:" + base64.b64encode(secret.encode()).decode()) if args.plaintext \
            else dpapi_protect(secret)
        account = {
            "email": target_email,
            "secret": token,
            "display_name": args.display_name or "",
            "imap_host": args.imap_host or DEFAULT_IMAP_HOST,
            "imap_port": args.imap_port or DEFAULT_IMAP_PORT,
            "smtp_host": args.smtp_host or DEFAULT_SMTP_HOST,
            "smtp_port": args.smtp_port or DEFAULT_SMTP_PORT,
        }
        if args.set_default or not accounts:
            for value in accounts.values():
                value["default"] = False
            account["default"] = True
        accounts[name] = account
        cfg["accounts"] = accounts
        save_config(cfg)
        out(f'已保存账号 {name} <{target_email}>（凭据：{"明文" if args.plaintext else "DPAPI 加密"}）')
        if args.no_verify:
            return 0
        return _verify(name, account, args.json)

    if args.action == "remove":
        if not args.name or args.name not in accounts:
            raise MailError(f"未知账号 {args.name!r}", 2)
        del accounts[args.name]
        cfg["accounts"] = accounts
        save_config(cfg)
        out(f"已删除账号 {args.name}")
        return 0

    name, account = resolve_account(cfg, args.account or args.name)
    return _verify(name, account, args.json)


def _verify(name: str, account: dict, as_json: bool) -> int:
    result = {"account": name, "email": account["email"], "imap": False, "smtp": False}
    conn = None
    try:
        conn = imap_connect(account)
        typ, _ = conn.noop()
        result["imap"] = typ == "OK"
        result["folders"] = len([f for f in list_folders(conn) if f["selectable"]])
    finally:
        imap_close(conn)

    secret = account_secret(account)
    host = account.get("smtp_host", DEFAULT_SMTP_HOST)
    port = int(account.get("smtp_port", DEFAULT_SMTP_PORT))
    try:
        if port == 587:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
            server.starttls(context=ssl.create_default_context())
        else:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT,
                                     context=ssl.create_default_context())
        server.login(account["email"], secret)
        server.quit()
        result["smtp"] = True
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(f"SMTP 认证失败：{exc}", 3,
                        "IMAP 能登录但 SMTP 不行，通常是授权码被重置；重新 accounts add。")
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"SMTP 连接失败：{exc}", 4)

    if as_json:
        emit_json({"ok": True, **result})
    else:
        out(f'账号 {name} <{account["email"]}>')
        out(f'  IMAP  {"可用" if result["imap"] else "不可用"}  文件夹 {result.get("folders", 0)} 个')
        out(f'  SMTP  {"可用" if result["smtp"] else "不可用"}')
    return 0


def cmd_folders(args) -> int:
    cfg = load_config()
    name, account = resolve_account(cfg, args.account)
    conn = imap_connect(account)
    try:
        rows = []
        for item in list_folders(conn):
            if not item["selectable"]:
                continue
            stats = folder_status(conn, item["raw"])
            if stats.get("messages") is None and stats.get("unseen") is None:
                continue  # 纯容器文件夹（如"其他文件夹"）不做 SELECT，STATUS 无数据
            rows.append({
                "folder": item["name"],
                "alias": FOLDER_ALIASES.get(item["name"], ""),
                "messages": stats.get("messages"),
                "unseen": stats.get("unseen"),
            })
    finally:
        imap_close(conn)
    if args.json:
        emit_json({"ok": True, "account": name, "folders": rows})
        return 0
    out(f'账号 {name} <{account["email"]}>')
    for row in rows:
        alias = f'（{row["alias"]}）' if row["alias"] else ""
        out(f'- {row["folder"]}{alias}  邮件 {row["messages"]}  未读 {row["unseen"]}')
    return 0


def cmd_list(args) -> int:
    cfg = load_config()
    name, account = resolve_account(cfg, args.account)
    conn = imap_connect(account)
    try:
        server_uids = search_uids(conn, args.folder, build_criteria(args))
        uids, notes, scanned = apply_local_filters(conn, server_uids, args)
        selected = uids[: max(1, args.limit)]
        rows = []
        for uid in selected:
            raw, flags = fetch_headers(conn, uid)
            rows.append({"uid": uid, "flags": flags,
                         **(header_summary(raw) if raw else {})})
    finally:
        imap_close(conn)
    if args.json:
        emit_json({"ok": True, "account": name, "folder": args.folder,
                   "total_matched": len(uids), "server_matched": len(server_uids),
                   "scanned": scanned, "notes": notes,
                   "returned": len(rows), "messages": rows})
        return 0
    out(f'账号 {name} / {args.folder}  匹配 {len(uids)} 封，显示 {len(rows)} 封')
    for note in notes:
        out(f"（{note}）")
    for row in rows:
        mark = " " if "\\Seen" in row.get("flags", []) else "●"
        out(f'{mark} [{row["uid"]}] {row.get("date", "")[:31]}')
        out(f'    {row.get("subject") or "(无主题)"}   ← {row.get("from", "")}')
    if len(uids) > len(rows):
        out(f"（还有 {len(uids) - len(rows)} 封未显示，用 --limit 调整）")
    return 0


def cmd_search(args) -> int:
    if not args.folder:
        args.folder = "INBOX"
    return cmd_list(args)


def cmd_read(args) -> int:
    cfg = load_config()
    name, account = resolve_account(cfg, args.account)
    conn = imap_connect(account)
    try:
        select_folder(conn, args.folder, readonly=False)
        raw_headers, flags = fetch_headers(conn, args.uid)
        msg = email.message_from_bytes(fetch_full(conn, args.uid),
                                       policy=email.policy.default)
        body = extract_body(msg)
        attachments = list_attachments(msg)
        if not args.mark_unread:
            try:
                conn.uid("STORE", args.uid, "+FLAGS", "(\\Seen)")
            except Exception:
                pass
    finally:
        imap_close(conn)

    info = header_summary(raw_headers)
    limit = args.body_limit
    truncated = limit > 0 and len(body) > limit
    shown = body[:limit] if truncated else body

    if args.json:
        emit_json({"ok": True, "account": name, "folder": args.folder, "uid": args.uid,
                   "flags": flags, **info, "body": shown, "body_total_chars": len(body),
                   "truncated": truncated,
                   "attachments": [{k: v for k, v in a.items() if k != "_payload"}
                                   for a in attachments]})
        return 0

    out(f'主题：{info["subject"] or "(无主题)"}')
    out(f'发件人：{info["from"]}')
    if info["to"]:
        out(f'收件人：{info["to"]}')
    if info["cc"]:
        out(f'抄送：{info["cc"]}')
    out(f'日期：{info["date"]}')
    out(f'UID：{args.uid}   文件夹：{args.folder}')
    if attachments:
        out("附件：" + ", ".join(f'{a["filename"]}（{a["size"]} 字节）' for a in attachments))
    out("-" * 60)
    out(shown)
    if truncated:
        out("-" * 60)
        out(f"（正文共 {len(body)} 字符，已显示前 {limit}；需要全文用 --full）")
    return 0


def cmd_attachment(args) -> int:
    cfg = load_config()
    name, account = resolve_account(cfg, args.account)
    conn = imap_connect(account)
    try:
        select_folder(conn, args.folder, readonly=True)
        msg = email.message_from_bytes(fetch_full(conn, args.uid),
                                       policy=email.policy.default)
        items = list_attachments(msg)
    finally:
        imap_close(conn)
    if not items:
        raise MailError(f"邮件 {args.uid} 没有附件", 5)
    outdir = Path(args.save_dir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    saved = []
    for item in items:
        if args.name and args.name not in item["filename"]:
            continue
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", item["filename"]) or "attachment.bin"
        target = outdir / safe
        target.write_bytes(item["_payload"])
        saved.append({"filename": safe, "bytes": len(item["_payload"]), "path": str(target)})
    if not saved:
        raise MailError(f"没有匹配 {args.name!r} 的附件", 5)
    if args.json:
        emit_json({"ok": True, "account": name, "uid": args.uid, "saved": saved})
        return 0
    for row in saved:
        out(f'已保存 {row["path"]}（{row["bytes"]} 字节）')
    return 0


def _preview(msg: EmailMessage, as_json: bool, save_eml: str | None) -> None:
    raw = msg.as_bytes()
    if save_eml:
        path = Path(save_eml).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    body = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            body = part.get_content()
            break
    attachments = [part.get_filename() for part in msg.walk() if part.get_filename()]
    if as_json:
        emit_json({"ok": True, "dry_run": True, "from": str(msg.get("From", "")),
                   "to": str(msg.get("To", "")), "cc": str(msg.get("Cc", "")),
                   "subject": str(msg.get("Subject", "")), "bytes": len(raw),
                   "attachments": attachments,
                   "eml": str(Path(save_eml).resolve()) if save_eml else None,
                   "body_preview": body[:BODY_LIMIT]})
        return
    out("=== 未发送（dry-run）===")
    for field in ("From", "To", "Cc", "Subject"):
        if msg.get(field):
            out(f"{field}: {msg.get(field)}")
    if attachments:
        out("附件：" + ", ".join(str(a) for a in attachments))
    out("-" * 60)
    out(body[:BODY_LIMIT] + ("…" if len(body) > BODY_LIMIT else ""))
    out("-" * 60)
    out(f"报文 {len(raw)} 字节；确认无误后加 --confirm 真正发送。")
    if save_eml:
        out(f"已保存 .eml：{Path(save_eml).resolve()}")


def _dispatch_send(args, msg: EmailMessage, account: dict, name: str) -> int:
    if args.confirm:
        result = smtp_send(account, msg)
        if args.json:
            emit_json({"ok": True, "account": name, "dry_run": False, **result})
        else:
            out(f'已发送给 {", ".join(result["recipients"])}（{result["bytes"]} 字节）')
        return 0
    _preview(msg, args.json, args.save_eml)
    return 0


def cmd_send(args) -> int:
    cfg = load_config()
    name, account = resolve_account(cfg, args.account)
    return _dispatch_send(args, build_message(args, account), account, name)


def _fetch_original(account: dict, folder: str, uid: str) -> tuple[dict, str]:
    conn = imap_connect(account)
    try:
        select_folder(conn, folder, readonly=True)
        raw_headers, _ = fetch_headers(conn, uid)
        msg = email.message_from_bytes(fetch_full(conn, uid),
                                       policy=email.policy.default)
    finally:
        imap_close(conn)
    return header_summary(raw_headers), extract_body(msg)


def cmd_reply(args) -> int:
    cfg = load_config()
    name, account = resolve_account(cfg, args.account)
    info, body = _fetch_original(account, args.folder, args.uid)
    subject = info["subject"] or ""
    if not re.match(r"(?i)^re\s*:", subject):
        subject = "Re: " + subject
    sender = email.utils.parseaddr(info["from"])[1]
    recipients = [sender] if sender else []
    if args.all:
        extra = []
        for field in ("to", "cc"):
            raw = info.get(field) or ""
            extra += [addr for _, addr in email.utils.getaddresses([raw]) if addr]
        recipients = sorted({a for a in [*recipients, *extra] if a and a != account["email"]})
    if not recipients:
        raise MailError("无法确定回复对象", 5, "改用 send 显式指定 --to。")
    args.to = recipients
    args.subject = subject
    quote_lines = [f'在 {info["date"]}，{info["from"]} 写道：']
    quote_lines += ["> " + line for line in body.splitlines()]
    msg = build_message(args, account, in_reply_to=info["message_id"],
                        references=info["message_id"], quote="\n".join(quote_lines))
    return _dispatch_send(args, msg, account, name)


def cmd_forward(args) -> int:
    cfg = load_config()
    name, account = resolve_account(cfg, args.account)
    info, body = _fetch_original(account, args.folder, args.uid)
    subject = info["subject"] or ""
    if not re.match(r"(?i)^fwd?\s*:", subject):
        subject = "Fwd: " + subject
    args.subject = subject
    header_block = (
        "\n\n---------- 转发邮件 ----------\n"
        f'发件人：{info["from"]}\n'
        f'日期：{info["date"]}\n'
        f'主题：{info["subject"]}\n'
        f'收件人：{info["to"]}\n\n'
    )
    msg = build_message(args, account, quote=header_block + body)
    return _dispatch_send(args, msg, account, name)


def cmd_mark(args) -> int:
    cfg = load_config()
    name, account = resolve_account(cfg, args.account)
    mapping = {
        "seen": ("+FLAGS", "(\\Seen)"),
        "unseen": ("-FLAGS", "(\\Seen)"),
        "flagged": ("+FLAGS", "(\\Flagged)"),
        "unflagged": ("-FLAGS", "(\\Flagged)"),
    }
    op, flags = mapping[args.flag]
    conn = imap_connect(account)
    try:
        select_folder(conn, args.folder, readonly=False)
        typ, data = conn.uid("STORE", args.uid, op, flags)
        if typ != "OK":
            raise MailError(f"设置标记失败：{data}", 4)
    finally:
        imap_close(conn)
    if args.json:
        emit_json({"ok": True, "account": name, "uid": args.uid, "flag": args.flag})
    else:
        out(f"邮件 {args.uid} 已标记为 {args.flag}")
    return 0


def cmd_move(args) -> int:
    cfg = load_config()
    name, account = resolve_account(cfg, args.account)
    target = args.to or "Deleted Messages"
    conn = imap_connect(account)
    purged = False
    try:
        select_folder(conn, args.folder, readonly=False)
        typ, data = conn.uid("COPY", args.uid, imap_quote(target))
        if typ != "OK":
            raise MailError(f"复制到 {target} 失败：{data}", 4,
                            "用 folders 子命令确认目标文件夹名。")
        conn.uid("STORE", args.uid, "+FLAGS", "(\\Deleted)")
        if not args.keep_source:
            # UIDPLUS 的 UID EXPUNGE 只清这一封，不动同文件夹里其他带删除标记的邮件
            try:
                typ, _ = conn.uid("EXPUNGE", args.uid)
                purged = typ == "OK"
            except Exception:
                purged = False
        if args.purge:
            conn.expunge()
            purged = True
    finally:
        imap_close(conn)
    if args.json:
        emit_json({"ok": True, "account": name, "uid": args.uid,
                   "from_folder": args.folder, "to_folder": target,
                   "purged": bool(args.purge), "source_cleaned": purged})
    else:
        out(f'邮件 {args.uid} 已移动：{args.folder} → {target}')
        if purged:
            out("（原文件夹中已移除）")
        elif args.keep_source:
            out("（按 --keep-source 保留了原邮件，只是加了删除标记）")
        else:
            out("（服务端不支持 UID EXPUNGE：原邮件仍带删除标记，可用 --purge 清理整个文件夹）")
    return 0


def cmd_purge(args) -> int:
    """彻底移除邮件：单封走 UID EXPUNGE，整夹走 EXPUNGE。"""
    cfg = load_config()
    name, account = resolve_account(cfg, args.account)
    conn = imap_connect(account)
    removed = False
    try:
        select_folder(conn, args.folder, readonly=False)
        if args.all_flagged:
            typ, data = conn.expunge()
            removed = typ == "OK"
        else:
            if not args.uid:
                raise MailError("需要 --uid，或用 --all-flagged 清空本文件夹的删除标记", 2)
            conn.uid("STORE", args.uid, "+FLAGS", "(\\Deleted)")
            typ, _ = conn.uid("EXPUNGE", args.uid)
            removed = typ == "OK"
    finally:
        imap_close(conn)
    if args.json:
        emit_json({"ok": True, "account": name, "folder": args.folder,
                   "uid": args.uid, "all_flagged": bool(args.all_flagged),
                   "removed": removed})
    else:
        scope = "本文件夹全部带删除标记的邮件" if args.all_flagged else f"邮件 {args.uid}"
        out(f"已彻底移除：{scope}（{args.folder}）" if removed else
            f"服务端未确认移除：{scope}（{args.folder}）——可能不支持 UID EXPUNGE")
    return 0


def cmd_selftest(args) -> int:
    """离线自测：不联网、不需要授权码。"""
    checks: list[dict] = []

    def check(name: str, fn) -> None:
        try:
            checks.append({"check": name, "ok": True, "detail": fn()})
        except Exception as exc:  # noqa: BLE001
            checks.append({"check": name, "ok": False,
                           "detail": f"{type(exc).__name__}: {exc}"})

    def mutf7_roundtrip() -> str:
        for sample in ["INBOX", "已发送", "工作/项目 A", "a&b", "邮件 & 归档 2026"]:
            if mutf7_decode(mutf7_encode(sample)) != sample:
                raise AssertionError(f"往返不一致：{sample}")
        return "5 组样例往返一致"

    def dpapi_roundtrip() -> str:
        if os.name != "nt":
            return "跳过（非 Windows）"
        secret = "abcdefgh12345678"
        token = dpapi_protect(secret)
        if not token.startswith("dpapi:"):
            raise AssertionError("返回值缺少 dpapi: 前缀")
        if dpapi_unprotect(token) != secret:
            raise AssertionError("解密结果不一致")
        return "加密→解密一致"

    def mime_build() -> str:
        ns = argparse.Namespace(to=["a@qq.com"], cc=None,
                                subject="自测：中文 & emoji 📧",
                                body="正文第一行\n第二行", body_file=None,
                                attach=[], reply_to=None)
        msg = build_message(ns, {"email": "me@qq.com", "display_name": "测试"})
        raw = msg.as_bytes()
        if b"Subject:" not in raw or b"\n\n" not in raw:
            raise AssertionError("报文结构异常")
        text = raw.decode("utf-8", "replace")
        if "a@qq.com" not in text:
            raise AssertionError("收件人未进入报文")
        return f"{len(raw)} 字节，主题/收件人正确"

    def since_parse() -> str:
        if not re.fullmatch(r"\d{2}-[A-Z][a-z]{2}-\d{4}", parse_since("7d")):
            raise AssertionError("相对日期解析异常")
        if parse_since("2026-01-05") != "05-Jan-2026":
            raise AssertionError("绝对日期解析异常")
        return "相对/绝对日期均正确"

    check("modified UTF-7 往返", mutf7_roundtrip)
    check("DPAPI 往返", dpapi_roundtrip)
    check("MIME 报文构建", mime_build)
    check("搜索日期解析", since_parse)
    check("配置文件路径", lambda: str(CONFIG_PATH))

    failed = [item for item in checks if not item["ok"]]
    if args.json:
        emit_json({"ok": not failed, "checks": checks})
    else:
        for item in checks:
            out(f'[{"PASS" if item["ok"] else "FAIL"}] {item["check"]}：{item["detail"]}')
    return 0 if not failed else 4


# ---------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="以 JSON 输出，便于机器解析")

    parser = argparse.ArgumentParser(
        prog="mail.py",
        description="QQ 邮箱管理：收件、搜索、阅读、归档、发信（IMAP + SMTP，凭据 DPAPI 加密）。",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于机器解析")
    sub = parser.add_subparsers(dest="command", required=True)

    accounts = sub.add_parser("accounts", parents=[common],
                              help="账号管理：list / add / remove / test")
    accounts.add_argument("action", choices=["list", "add", "remove", "test"])
    accounts.add_argument("--name", help="账号别名（默认取邮箱 @ 前部分）")
    accounts.add_argument("--email", help="完整 QQ 邮箱地址")
    accounts.add_argument("--display-name", help="发件人显示名")
    accounts.add_argument("--password-stdin", action="store_true",
                          help="从 stdin 读授权码（脚本用，避免交互）")
    accounts.add_argument("--password-file", help="从文件读授权码")
    accounts.add_argument("--plaintext", action="store_true", help="明文保存凭据（不推荐）")
    accounts.add_argument("--no-verify", action="store_true", help="保存后不立即验证连通性")
    accounts.add_argument("--set-default", action="store_true", help="设为默认账号")
    accounts.add_argument("--account", help="test 时指定已保存的账号")
    accounts.add_argument("--imap-host")
    accounts.add_argument("--imap-port", type=int)
    accounts.add_argument("--smtp-host")
    accounts.add_argument("--smtp-port", type=int)
    accounts.set_defaults(func=cmd_accounts)

    folders = sub.add_parser("folders", parents=[common],
                             help="列出文件夹与未读数")
    folders.add_argument("--account")
    folders.set_defaults(func=cmd_folders)

    def add_list_like(p):
        p.add_argument("--account")
        p.add_argument("--folder", default="INBOX")
        p.add_argument("--limit", type=int, default=LIST_LIMIT)
        p.add_argument("--unread", action="store_true", help="只看未读")
        p.add_argument("--flagged", action="store_true", help="只看星标")
        p.add_argument("--from", dest="from_", help="按发件人过滤")
        p.add_argument("--subject", help="按主题过滤")
        p.add_argument("--text", help="正文关键字（本地扫描，默认最近 300 封）")
        p.add_argument("--since", help="起始时间：7d / 4w / 2026-01-01")
        p.add_argument("--scan", type=int, default=300,
                       help="本地匹配时最多扫描多少封（默认 300）")

    listing = sub.add_parser("list", parents=[common], help="列出邮件（最新在前）")
    add_list_like(listing)
    listing.set_defaults(func=cmd_list)

    search = sub.add_parser("search", parents=[common], help="搜索邮件（同 list 的过滤参数）")
    add_list_like(search)
    search.set_defaults(func=cmd_search)

    read = sub.add_parser("read", parents=[common], help="读一封邮件")
    read.add_argument("--account")
    read.add_argument("--folder", default="INBOX")
    read.add_argument("--uid", required=True)
    read.add_argument("--full", dest="body_limit", action="store_const", const=0,
                      default=BODY_LIMIT, help="输出全文（默认截断 4000 字符）")
    read.add_argument("--body-limit", type=int)
    read.add_argument("--mark-unread", action="store_true", help="读后不标记为已读")
    read.set_defaults(func=cmd_read)

    send = sub.add_parser("send", parents=[common], help="发送新邮件（默认 dry-run）")
    send.add_argument("--account")
    send.add_argument("--to", action="append", required=True, help="收件人，可重复")
    send.add_argument("--cc", action="append")
    send.add_argument("--subject", required=True)
    send.add_argument("--body", default="")
    send.add_argument("--body-file")
    send.add_argument("--attach", action="append", help="附件路径，可重复")
    send.add_argument("--confirm", action="store_true", help="真正发送（不加只预览）")
    send.add_argument("--save-eml", help="把报文另存为 .eml")
    send.set_defaults(func=cmd_send)

    reply = sub.add_parser("reply", parents=[common], help="回复邮件（默认 dry-run）")
    reply.add_argument("--account")
    reply.add_argument("--folder", default="INBOX")
    reply.add_argument("--uid", required=True)
    reply.add_argument("--body", default="")
    reply.add_argument("--body-file")
    reply.add_argument("--attach", action="append")
    reply.add_argument("--all", action="store_true", help="回复全部（含抄送）")
    reply.add_argument("--confirm", action="store_true")
    reply.add_argument("--save-eml")
    reply.set_defaults(func=cmd_reply)

    forward = sub.add_parser("forward", parents=[common], help="转发邮件（默认 dry-run）")
    forward.add_argument("--account")
    forward.add_argument("--folder", default="INBOX")
    forward.add_argument("--uid", required=True)
    forward.add_argument("--to", action="append", required=True)
    forward.add_argument("--cc", action="append")
    forward.add_argument("--body", default="")
    forward.add_argument("--body-file")
    forward.add_argument("--attach", action="append")
    forward.add_argument("--confirm", action="store_true")
    forward.add_argument("--save-eml")
    forward.set_defaults(func=cmd_forward)

    mark = sub.add_parser("mark", parents=[common], help="设置已读/未读/星标")
    mark.add_argument("--account")
    mark.add_argument("--folder", default="INBOX")
    mark.add_argument("--uid", required=True)
    mark.add_argument("--flag", required=True,
                      choices=["seen", "unseen", "flagged", "unflagged"])
    mark.set_defaults(func=cmd_mark)

    move = sub.add_parser("move", parents=[common], help="移动邮件（默认进已删除）")
    move.add_argument("--account")
    move.add_argument("--folder", default="INBOX")
    move.add_argument("--uid", required=True)
    move.add_argument("--to", help="目标文件夹，默认 'Deleted Messages'")
    move.add_argument("--keep-source", action="store_true",
                      help="只在原文件夹加删除标记，不真正移除")
    move.add_argument("--purge", action="store_true",
                      help="移动后 expunge 整个原文件夹（清理所有带删除标记的邮件）")
    move.set_defaults(func=cmd_move)

    purge = sub.add_parser("purge", parents=[common],
                           help="彻底移除邮件（不可撤销，需用户明确要求）")
    purge.add_argument("--account")
    purge.add_argument("--folder", default="INBOX")
    purge.add_argument("--uid")
    purge.add_argument("--all-flagged", action="store_true",
                       help="清空本文件夹所有带删除标记的邮件")
    purge.set_defaults(func=cmd_purge)

    attachment = sub.add_parser("attachment", parents=[common], help="下载附件到工作区")
    attachment.add_argument("--account")
    attachment.add_argument("--folder", default="INBOX")
    attachment.add_argument("--uid", required=True)
    attachment.add_argument("--save-dir", required=True)
    attachment.add_argument("--name", help="只取文件名包含该子串的附件")
    attachment.set_defaults(func=cmd_attachment)

    selftest = sub.add_parser("selftest", parents=[common],
                              help="离线自测（不联网、不需要授权码）")
    selftest.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except MailError as exc:
        if getattr(args, "json", False):
            emit_json({"ok": False, "error": {"code": exc.code, "message": exc.message,
                                              "hint": exc.hint}})
        else:
            print(f"错误：{exc.message}", file=sys.stderr)
            if exc.hint:
                print(f"建议：{exc.hint}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
