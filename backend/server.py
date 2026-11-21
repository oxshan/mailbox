#!/usr/bin/env python3
"""Lightweight catch-all mailbox: SMTP receiver + HTTP API. Standard library only (Python 3.8+)."""
import asyncio
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import sqlite3
import string
import struct
import threading
import time
import urllib.error
import urllib.request
from email import message_from_bytes, policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SCENARIO = "default"


class Config:
    def __init__(self, **overrides):
        env = os.environ.get
        self.domains = [d.strip().lower() for d in env("MAIL_DOMAINS", "").split(",") if d.strip()]
        self.api_key = env("API_KEY", "")
        self.smtp_host = env("SMTP_HOST", "0.0.0.0")
        self.smtp_port = int(env("SMTP_PORT", "25"))
        self.http_host = env("HTTP_HOST", "0.0.0.0")
        self.http_port = int(env("HTTP_PORT", "8080"))
        self.data_dir = env("DATA_DIR", os.path.join(HERE, "data"))
        self.static_dir = env("STATIC_DIR", os.path.join(HERE, "..", "frontend", "dist"))
        self.retention_hours = float(env("RETENTION_HOURS", "24"))
        self.max_mail_bytes = int(env("MAX_MAIL_BYTES", str(2 * 1024 * 1024)))
        self.mx_check_port = 25
        self.public_ip = env("PUBLIC_IP", "").strip()
        self.resend_api_key = env("RESEND_API_KEY", "")
        self.resend_api_url = env("RESEND_API_URL", "https://api.resend.com/emails")
        for key, value in overrides.items():
            setattr(self, key, value)


_KEYWORD = (
    r"(?:验证码|校验码|动态码|确认码|激活码|驗證碼|verification code|security code|confirmation code"
    r"|one[- ]time (?:pass)?code|passcode|otp|code|pin)"
)
_CODE = r"(?<![0-9A-Za-z])(\d{4,8}|\d{3}[ -]\d{3})(?![0-9A-Za-z])"
_AFTER_KEYWORD = re.compile(_KEYWORD + r"[^0-9]{0,40}?" + _CODE, re.I)
_BEFORE_KEYWORD = re.compile(
    _CODE + r"[^0-9\n]{0,30}?(?:is your|is the|是您的|是你的|为您的|为你的|" + _KEYWORD + ")", re.I
)
_ANY_CODE = re.compile(_CODE)
_YEAR = re.compile(r"(?:19|20)\d\d")


def extract_code(subject, body):
    """Return the most likely verification code in a mail, or None."""
    texts = [subject or "", body or ""]
    for pattern in (_AFTER_KEYWORD, _BEFORE_KEYWORD):
        for text in texts:
            match = pattern.search(text)
            if match:
                return re.sub(r"[ -]", "", match.group(1))
    candidates = [re.sub(r"[ -]", "", m.group(1)) for text in texts for m in _ANY_CODE.finditer(text)]
    candidates = [c for c in candidates if not _YEAR.fullmatch(c)]
    six = [c for c in candidates if len(c) == 6]
    return (six or candidates or [None])[0]


def compile_template(mode, pattern):
    """Compile a mail template into a regex. Placeholder mode: literal text with {code} and {*}."""
    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("模板内容不能为空")
    if mode == "regex":
        try:
            return re.compile(pattern, re.I | re.M)
        except re.error as exc:
            raise ValueError("正则表达式无效：%s" % exc)
    if mode != "placeholder":
        raise ValueError("未知的模板类型：%s" % mode)
    if "{code}" not in pattern:
        raise ValueError("模板中需要用 {code} 标出验证码的位置")
    parts = []
    for piece in re.split(r"(\{code\}|\{\*\})", pattern):
        if piece == "{code}":
            parts.append(r"([A-Za-z0-9-]+)")
        elif piece == "{*}":
            parts.append(r"[\s\S]*?")
        else:
            parts.extend(r"\s*" if token.isspace() else re.escape(token) for token in re.split(r"(\s+)", piece) if token)
    return re.compile("".join(parts), re.I)


def extract_with_template(regex, subject, plain, rich):
    for text in ("%s\n%s" % (subject or "", plain or ""), rich or ""):
        match = regex.search(text)
        if match:
            code = match.group(1) if regex.groups else match.group(0)
            if code and code.strip():
                return code.strip()
    return None


def extract_for(template, subject, plain, rich):
    """Extract a code using the scenario's template, or the built-in rules when there is none."""
    if template:
        return extract_with_template(compile_template(template["mode"], template["pattern"]), subject, plain, rich)
    return extract_code(subject, plain) or extract_code(subject, rich)


def html_to_text(markup):
    markup = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h\d|li)>", "\n", markup)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", markup))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def parse_mail(raw):
    """Return (sender, subject, plain text, html as text) of a raw RFC822 message."""
    msg = message_from_bytes(raw, policy=policy.default)
    sender = str(msg.get("From", "") or "")
    subject = str(msg.get("Subject", "") or "")
    plain = rich = ""
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            plain = part.get_content().strip()
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            rich = html_to_text(part.get_content())
    except Exception:
        plain = raw.decode("utf-8", "replace")
    return sender, subject, plain, rich


def _read_dns_name(data, offset):
    labels, end, hops = [], None, 0
    while True:
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            hops += 1
            if hops > 32:
                raise ValueError("bad DNS name compression")
        elif length == 0:
            return ".".join(labels), (end if end is not None else offset + 1)
        else:
            labels.append(data[offset + 1 : offset + 1 + length].decode("ascii", "replace"))
            offset += 1 + length


def parse_mx_response(data):
    """Return [(priority, host)] sorted by priority from a raw DNS response."""
    _, flags, questions, answers = struct.unpack("!HHHH", data[:8])
    if flags & 0x000F:
        return []
    offset = 12
    for _ in range(questions):
        _, offset = _read_dns_name(data, offset)
        offset += 4
    records = []
    for _ in range(answers):
        _, offset = _read_dns_name(data, offset)
        rtype, _, _, length = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        if rtype == 15:
            priority = struct.unpack("!H", data[offset : offset + 2])[0]
            records.append((priority, _read_dns_name(data, offset + 2)[0].lower()))
        offset += length
    return sorted(records)


def dns_servers():
    servers = []
    try:
        with open("/etc/resolv.conf") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    servers.append(parts[1])
    except OSError:
        pass
    return servers + ["1.1.1.1", "8.8.8.8", "223.5.5.5"]


def resolve_mx(name, timeout=3):
    labels = name.encode("idna").split(b".")
    question = b"".join(bytes([len(label)]) + label for label in labels) + b"\x00" + struct.pack("!HH", 15, 1)
    query_id = secrets.randbelow(65536)
    packet = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + question
    error = OSError("no DNS server available")
    for server in dns_servers():
        family = socket.AF_INET6 if ":" in server else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(packet, (server, 53))
                data = sock.recv(4096)
            if struct.unpack("!H", data[:2])[0] == query_id:
                return parse_mx_response(data)
        except (OSError, ValueError, struct.error, IndexError) as exc:
            error = exc
    raise OSError("DNS 查询失败：%s" % error)


_PUBLIC_IP = {}


def public_ip(cfg):
    """This server's public IP: PUBLIC_IP if set, otherwise asked once from an external service."""
    if cfg.public_ip:
        return cfg.public_ip
    if "value" not in _PUBLIC_IP:
        _PUBLIC_IP["value"] = None
        for url in ("https://checkip.amazonaws.com", "https://api.ipify.org"):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "mailbox/1.0"})
                with urllib.request.urlopen(request, timeout=5) as response:
                    _PUBLIC_IP["value"] = response.read().decode().strip()
                break
            except (OSError, ValueError):
                continue
    return _PUBLIC_IP["value"]


def check_domain(name, instance_id, port=25, timeout=5, own_ip=None):
    """Check that mail for a domain reaches this server.

    The MX host resolving to our own public IP is enough; otherwise connect and compare the SMTP banner.
    """
    try:
        records = resolve_mx(name)
    except (OSError, UnicodeError) as exc:
        return {"status": "error", "message": str(exc), "mx": []}
    mx = ["%d %s" % record for record in records]
    if not records:
        return {"status": "missing", "message": "没有 MX 记录", "mx": mx}
    host = records[0][1]
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except OSError:
        return {"status": "error", "message": "MX 指向 %s，但这个主机名解析不出 IP" % host, "mx": mx}
    if own_ip and own_ip in addresses:
        return {"status": "ok", "message": "MX 指向 %s（%s），就是本服务器的公网 IP" % (host, own_ip), "mx": mx}
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            banner = sock.recv(512).decode("utf-8", "replace").strip()
    except OSError as exc:
        where = "%s（%s）" % (host, "、".join(addresses))
        if own_ip:
            message = "MX 指向 %s，与本服务器的公网 IP %s 不一致，也连不上它的 %d 端口" % (where, own_ip, port)
            return {"status": "mismatch", "message": message, "mx": mx}
        message = "MX 指向 %s，但从本机连不上它的 %d 端口，无法确认（%s）" % (where, port, exc)
        return {"status": "unverified", "message": message, "mx": mx}
    if instance_id in banner:
        return {"status": "ok", "message": "MX 指向 %s，已确认是本服务器" % host, "mx": mx}
    return {"status": "mismatch", "message": "MX 指向 %s，但那是另一台邮件服务器" % host, "mx": mx}


class SendError(Exception):
    pass


def send_via_resend(cfg, payload):
    """Send one mail through the Resend HTTPS API and return its id."""
    request = urllib.request.Request(
        cfg.resend_api_url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer " + cfg.resend_api_key,
            "Content-Type": "application/json",
            "User-Agent": "mailbox/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return str(json.loads(response.read()).get("id", ""))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("message")
        except (ValueError, AttributeError):
            detail = None
        raise SendError("Resend 返回 %d：%s" % (exc.code, detail or exc.reason))
    except (OSError, ValueError) as exc:
        raise SendError("无法连接 Resend：%s" % exc)


class Store:
    def __init__(self, path, seed_domains=()):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS mailboxes (
                address TEXT PRIMARY KEY, domain TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS mails (
                id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL, sender TEXT, subject TEXT,
                body TEXT, code TEXT, received_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_mails_address ON mails(address, received_at);
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, mode TEXT NOT NULL,
                pattern TEXT NOT NULL, sample TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS scenarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                domain TEXT NOT NULL DEFAULT '', template_id INTEGER, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS domains (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS scenario_domains (
                scenario_id INTEGER NOT NULL, domain_id INTEGER NOT NULL, PRIMARY KEY (scenario_id, domain_id));
            CREATE TABLE IF NOT EXISTS sent_mails (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, recipients TEXT NOT NULL,
                subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                provider_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
            """
        )
        self.add_column("mailboxes", "scenario_id", "INTEGER")
        self.add_column("mails", "alt", "TEXT NOT NULL DEFAULT ''")
        for table in ("mailboxes", "mails", "sent_mails"):
            self.add_column(table, "deleted_at", "REAL")
        self.execute(
            "INSERT OR IGNORE INTO scenarios (key, name, created_at) VALUES (?, '默认场景', ?)",
            (DEFAULT_SCENARIO, time.time()),
        )
        self.default_scenario_id = self.query("SELECT id FROM scenarios WHERE key = ?", (DEFAULT_SCENARIO,))[0]["id"]
        self.execute("UPDATE mailboxes SET scenario_id = ? WHERE scenario_id IS NULL", (self.default_scenario_id,))
        if not self.query("SELECT id FROM domains LIMIT 1"):
            for name in seed_domains:
                self.execute("INSERT OR IGNORE INTO domains (name, created_at) VALUES (?, ?)", (name, time.time()))
        self.execute(
            "INSERT OR IGNORE INTO scenario_domains SELECT s.id, d.id FROM scenarios s"
            " JOIN domains d ON d.name = s.domain WHERE s.domain != ''"
        )
        self.execute("UPDATE scenarios SET domain = '' WHERE domain != ''")

    def domain_names(self):
        return {row["name"] for row in self.query("SELECT name FROM domains")}

    def usable_domains(self, scenario_id):
        """Enabled domains a scenario may use: its linked ones, or every enabled domain when none are linked."""
        linked = self.query(
            "SELECT d.name, d.enabled FROM scenario_domains sd JOIN domains d ON d.id = sd.domain_id"
            " WHERE sd.scenario_id = ?",
            (scenario_id,),
        )
        rows = linked or self.query("SELECT name, enabled FROM domains")
        return [row["name"] for row in rows if row["enabled"]]

    def add_column(self, table, column, definition):
        if column not in [row["name"] for row in self.query("PRAGMA table_info(%s)" % table)]:
            self.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))

    def query(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args).rowcount

    def insert(self, sql, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args).lastrowid

    def create_mailbox(self, domain, scenario):
        alphabet = string.ascii_lowercase + string.digits
        while True:
            local = secrets.choice(string.ascii_lowercase) + "".join(secrets.choice(alphabet) for _ in range(9))
            address = "%s@%s" % (local, domain)
            now = time.time()
            if self.execute(
                "INSERT OR IGNORE INTO mailboxes (address, domain, created_at, scenario_id) VALUES (?, ?, ?, ?)",
                (address, domain, now, scenario["id"]),
            ):
                return {"address": address, "domain": domain, "scenario": scenario["key"], "created_at": now}

    def template_for(self, address):
        rows = self.query(
            "SELECT t.mode, t.pattern FROM mailboxes b JOIN scenarios s ON s.id = b.scenario_id"
            " JOIN templates t ON t.id = s.template_id WHERE b.address = ?",
            (address,),
        )
        return rows[0] if rows else None

    def add_mail(self, address, sender, subject, body, alt, code):
        now = time.time()
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO mailboxes (address, domain, created_at, scenario_id) VALUES (?, ?, ?, ?)",
                (address, address.rsplit("@", 1)[1], now, self.default_scenario_id),
            )
            self.db.execute("UPDATE mailboxes SET deleted_at = NULL WHERE address = ?", (address,))
            self.db.execute(
                "INSERT INTO mails (address, sender, subject, body, alt, code, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (address, sender, subject, body, alt, code, now),
            )

    def reparse(self, scenario_ids):
        """Re-extract codes for every mail in the given scenarios after their template changed."""
        for scenario_id in scenario_ids:
            rows = self.query(
                "SELECT t.mode, t.pattern FROM scenarios s JOIN templates t ON t.id = s.template_id WHERE s.id = ?",
                (scenario_id,),
            )
            template = rows[0] if rows else None
            mails = self.query(
                "SELECT m.id, m.subject, m.body, m.alt, m.code FROM mails m JOIN mailboxes b ON b.address = m.address"
                " WHERE b.scenario_id = ? AND m.deleted_at IS NULL",
                (scenario_id,),
            )
            for mail in mails:
                code = extract_for(template, mail["subject"], mail["body"], mail["alt"])
                if code != mail["code"]:
                    self.execute("UPDATE mails SET code = ? WHERE id = ?", (code, mail["id"]))

    def latest_code(self, address, since=0.0):
        rows = self.query(
            "SELECT code, sender, subject, received_at FROM mails"
            " WHERE address = ? AND code IS NOT NULL AND received_at >= ? AND deleted_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (address, since),
        )
        return rows[0] if rows else None

    def purge(self, max_age_seconds):
        """Soft-delete expired rows: they are flagged with deleted_at and hidden, never removed."""
        now = time.time()
        cutoff = now - max_age_seconds
        self.execute("UPDATE mails SET deleted_at = ? WHERE received_at < ? AND deleted_at IS NULL", (now, cutoff))
        self.execute("UPDATE sent_mails SET deleted_at = ? WHERE created_at < ? AND deleted_at IS NULL", (now, cutoff))
        self.execute(
            "UPDATE mailboxes SET deleted_at = ? WHERE created_at < ? AND deleted_at IS NULL"
            " AND address NOT IN (SELECT DISTINCT address FROM mails WHERE deleted_at IS NULL)",
            (now, cutoff),
        )


_ADDRESS = re.compile(r"<([^<>]*)>")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")


class SmtpSession:
    TIMEOUT = 60
    MAX_RECIPIENTS = 20

    def __init__(self, app, reader, writer):
        self.app, self.reader, self.writer = app, reader, writer

    async def reply(self, line):
        self.writer.write(line.encode() + b"\r\n")
        await self.writer.drain()

    async def readline(self):
        return await asyncio.wait_for(self.reader.readline(), self.TIMEOUT)

    async def run(self):
        cfg = self.app.cfg
        host = socket.gethostname() or "localhost"
        sender, recipients = "", []
        await self.reply("220 %s ESMTP mailbox-%s" % (host, self.app.instance_id))
        while True:
            line = await self.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            verb, _, arg = text.partition(" ")
            verb = verb.upper()
            if verb == "EHLO":
                await self.reply("250-%s\r\n250-SIZE %d\r\n250 8BITMIME" % (host, cfg.max_mail_bytes))
            elif verb == "HELO":
                await self.reply("250 %s" % host)
            elif verb == "MAIL":
                match = _ADDRESS.search(arg)
                sender, recipients = (match.group(1) if match else ""), []
                await self.reply("250 OK")
            elif verb == "RCPT":
                match = _ADDRESS.search(arg)
                address = (match.group(1) if match else arg.partition(":")[2]).strip().lower()
                local, _, domain = address.rpartition("@")
                if not local or domain not in self.app.store.domain_names():
                    await self.reply("550 No such user here")
                elif len(recipients) >= self.MAX_RECIPIENTS:
                    await self.reply("452 Too many recipients")
                else:
                    recipients.append(address)
                    await self.reply("250 OK")
            elif verb == "DATA":
                if not recipients:
                    await self.reply("503 Need RCPT first")
                    continue
                await self.reply("354 End data with <CR><LF>.<CR><LF>")
                raw = await self.read_data()
                if raw is None:
                    await self.reply("552 Message too large")
                else:
                    loop = asyncio.get_event_loop()
                    try:
                        await loop.run_in_executor(None, self.app.deliver, sender, list(recipients), raw)
                    except Exception as exc:
                        print("deliver failed: %r" % exc, flush=True)
                        await self.reply("451 Local error, try again later")
                    else:
                        await self.reply("250 OK")
                sender, recipients = "", []
            elif verb == "RSET":
                sender, recipients = "", []
                await self.reply("250 OK")
            elif verb == "NOOP":
                await self.reply("250 OK")
            elif verb == "QUIT":
                await self.reply("221 Bye")
                return
            else:
                await self.reply("502 Command not implemented")

    async def read_data(self):
        chunks, size = [], 0
        while True:
            line = await self.readline()
            if not line:
                raise ConnectionError("client closed during DATA")
            if line in (b".\r\n", b".\n"):
                break
            if line.startswith(b"."):
                line = line[1:]
            size += len(line)
            if size <= self.app.cfg.max_mail_bytes:
                chunks.append(line)
        return None if size > self.app.cfg.max_mail_bytes else b"".join(chunks)


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app = None

    def log_message(self, *args):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        key = self.headers.get("X-API-Key", "")
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
        return hmac.compare_digest(key.encode(), self.app.cfg.api_key.encode())

    def route(self):
        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not url.path.startswith("/api/"):
            return self.serve_static(url.path)
        if not self.authorized():
            return self.send_json(401, {"error": "invalid api key"})
        parts = url.path[5:].strip("/").split("/")
        handler = getattr(self, "%s_%s" % (self.command.lower(), parts[0]), None) or getattr(
            self, "api_" + parts[0], None
        )
        if handler is None or len(parts) > 2:
            return self.send_json(404, {"error": "not found"})
        try:
            if raw.strip():
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("request body must be a JSON object")
                params.update(body)
            handler(params, *parts[1:])
        except LookupError:
            self.send_json(404, {"error": "not found"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})

    do_GET = do_POST = do_PUT = do_DELETE = route

    def get_domains(self, params, item=None):
        rows = self.app.store.query(
            "SELECT d.*, (SELECT COUNT(*) FROM mailboxes b WHERE b.domain = d.name AND b.deleted_at IS NULL)"
            " AS mailbox_count,"
            " (SELECT COUNT(*) FROM scenario_domains sd WHERE sd.domain_id = d.id) AS scenario_count"
            " FROM domains d ORDER BY d.id"
        )
        for row in rows:
            row["enabled"] = bool(row["enabled"])
        self.send_json(200, {"domains": rows})

    def post_domains(self, params, item=None):
        store = self.app.store
        name = str(params.get("name", "")).strip().lower().lstrip("@")
        if item == "check":
            if name not in store.domain_names():
                raise LookupError(name)
            cfg = self.app.cfg
            return self.send_json(
                200, check_domain(name, self.app.instance_id, cfg.mx_check_port, own_ip=public_ip(cfg))
            )
        if item:
            raise LookupError(item)
        if len(name) > 253 or not _DOMAIN.fullmatch(name):
            raise ValueError("域名格式不正确：%s" % name)
        try:
            new_id = store.insert(
                "INSERT INTO domains (name, enabled, note, created_at) VALUES (?, ?, ?, ?)",
                (name, int(bool(params.get("enabled", True))), str(params.get("note", "")).strip(), time.time()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("域名已存在：%s" % name)
        self.send_json(200, {"id": new_id, "name": name})

    def put_domains(self, params, item=None):
        store, domain_id = self.app.store, int(item or 0)
        rows = store.query("SELECT enabled, note FROM domains WHERE id = ?", (domain_id,))
        if not rows:
            raise LookupError(item)
        enabled = int(bool(params.get("enabled", rows[0]["enabled"])))
        note = str(params.get("note", rows[0]["note"])).strip()
        store.execute("UPDATE domains SET enabled = ?, note = ? WHERE id = ?", (enabled, note, domain_id))
        self.send_json(200, {"id": domain_id})

    def delete_domains(self, params, item=None):
        store, domain_id = self.app.store, int(item or 0)
        if not store.execute("DELETE FROM domains WHERE id = ?", (domain_id,)):
            raise LookupError(item)
        store.execute("DELETE FROM scenario_domains WHERE domain_id = ?", (domain_id,))
        self.send_json(200, {"id": domain_id})

    def api_mailbox(self, params, item=None):
        store = self.app.store
        key = str(params.get("scenario", "")).strip().lower() or DEFAULT_SCENARIO
        rows = store.query("SELECT id, key FROM scenarios WHERE key = ?", (key,))
        if not rows:
            raise ValueError("业务场景不存在：%s" % key)
        usable = store.usable_domains(rows[0]["id"])
        if not usable:
            raise ValueError("该场景没有可用的域名，请先在域名管理中添加或启用域名")
        domain = str(params.get("domain", "")).strip().lower() or secrets.choice(usable)
        if domain not in usable:
            raise ValueError("该场景不能使用域名：%s" % domain)
        self.send_json(200, store.create_mailbox(domain, rows[0]))

    def get_templates(self, params, item=None):
        rows = self.app.store.query(
            "SELECT t.*, (SELECT COUNT(*) FROM scenarios s WHERE s.template_id = t.id) AS scenario_count"
            " FROM templates t ORDER BY t.id DESC"
        )
        self.send_json(200, {"templates": rows})

    def template_fields(self, params):
        name = str(params.get("name", "")).strip()
        mode, pattern = str(params.get("mode", "placeholder")), str(params.get("pattern", "")).strip()
        if not name:
            raise ValueError("模板名称不能为空")
        compile_template(mode, pattern)
        return name, mode, pattern, str(params.get("sample", ""))

    def scenarios_using(self, template_id):
        rows = self.app.store.query("SELECT id FROM scenarios WHERE template_id = ?", (template_id,))
        return [row["id"] for row in rows]

    def post_templates(self, params, item=None):
        if item == "test":
            regex = compile_template(str(params.get("mode", "placeholder")), str(params.get("pattern", "")))
            return self.send_json(200, {"code": extract_with_template(regex, "", str(params.get("sample", "")), "")})
        if item:
            raise LookupError(item)
        fields = self.template_fields(params)
        new_id = self.app.store.insert(
            "INSERT INTO templates (name, mode, pattern, sample, created_at) VALUES (?, ?, ?, ?, ?)",
            fields + (time.time(),),
        )
        self.send_json(200, {"id": new_id})

    def put_templates(self, params, item=None):
        template_id = int(item or 0)
        fields = self.template_fields(params)
        if not self.app.store.execute(
            "UPDATE templates SET name = ?, mode = ?, pattern = ?, sample = ? WHERE id = ?", fields + (template_id,)
        ):
            raise LookupError(item)
        self.app.store.reparse(self.scenarios_using(template_id))
        self.send_json(200, {"id": template_id})

    def delete_templates(self, params, item=None):
        template_id = int(item or 0)
        affected = self.scenarios_using(template_id)
        if not self.app.store.execute("DELETE FROM templates WHERE id = ?", (template_id,)):
            raise LookupError(item)
        self.app.store.execute("UPDATE scenarios SET template_id = NULL WHERE template_id = ?", (template_id,))
        self.app.store.reparse(affected)
        self.send_json(200, {"id": template_id})

    def get_scenarios(self, params, item=None):
        rows = self.app.store.query(
            "SELECT s.*, t.name AS template_name,"
            " (SELECT COUNT(*) FROM mailboxes b WHERE b.scenario_id = s.id AND b.deleted_at IS NULL) AS mailbox_count"
            " FROM scenarios s LEFT JOIN templates t ON t.id = s.template_id ORDER BY s.id"
        )
        links = self.app.store.query(
            "SELECT sd.scenario_id, d.id, d.name FROM scenario_domains sd JOIN domains d ON d.id = sd.domain_id"
            " ORDER BY d.id"
        )
        for row in rows:
            del row["domain"]
            mine = [link for link in links if link["scenario_id"] == row["id"]]
            row["domain_ids"] = [link["id"] for link in mine]
            row["domains"] = [link["name"] for link in mine]
        self.send_json(200, {"scenarios": rows})

    def scenario_fields(self, params):
        name = str(params.get("name", "")).strip()
        key = str(params.get("key", "")).strip().lower() or "s" + secrets.token_hex(4)
        template_id = params.get("template_id") or None
        domain_ids = params.get("domain_ids") or []
        if not name:
            raise ValueError("场景名称不能为空")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", key):
            raise ValueError("场景标识只能包含小写字母、数字、下划线和连字符，最长 32 位")
        if not isinstance(domain_ids, list):
            raise ValueError("domain_ids 必须是数组")
        domain_ids = sorted({int(value) for value in domain_ids})
        known = {row["id"] for row in self.app.store.query("SELECT id FROM domains")}
        if not known.issuperset(domain_ids):
            raise ValueError("关联的域名不存在")
        if template_id is not None:
            template_id = int(template_id)
            if not self.app.store.query("SELECT id FROM templates WHERE id = ?", (template_id,)):
                raise ValueError("邮件模板不存在")
        return name, key, template_id, domain_ids

    def link_domains(self, scenario_id, domain_ids):
        store = self.app.store
        store.execute("DELETE FROM scenario_domains WHERE scenario_id = ?", (scenario_id,))
        for domain_id in domain_ids:
            store.execute("INSERT INTO scenario_domains VALUES (?, ?)", (scenario_id, domain_id))

    def post_scenarios(self, params, item=None):
        if item:
            raise LookupError(item)
        name, key, template_id, domain_ids = self.scenario_fields(params)
        try:
            new_id = self.app.store.insert(
                "INSERT INTO scenarios (name, key, template_id, created_at) VALUES (?, ?, ?, ?)",
                (name, key, template_id, time.time()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("场景标识已存在：%s" % key)
        self.link_domains(new_id, domain_ids)
        self.send_json(200, {"id": new_id, "key": key})

    def put_scenarios(self, params, item=None):
        store, scenario_id = self.app.store, int(item or 0)
        name, key, template_id, domain_ids = self.scenario_fields(params)
        if scenario_id == store.default_scenario_id:
            key = DEFAULT_SCENARIO
        try:
            updated = store.execute(
                "UPDATE scenarios SET name = ?, key = ?, template_id = ? WHERE id = ?",
                (name, key, template_id, scenario_id),
            )
        except sqlite3.IntegrityError:
            raise ValueError("场景标识已存在：%s" % key)
        if not updated:
            raise LookupError(item)
        self.link_domains(scenario_id, domain_ids)
        store.reparse([scenario_id])
        self.send_json(200, {"id": scenario_id, "key": key})

    def delete_scenarios(self, params, item=None):
        store, scenario_id = self.app.store, int(item or 0)
        if scenario_id == store.default_scenario_id:
            raise ValueError("默认场景不能删除")
        if not store.execute("DELETE FROM scenarios WHERE id = ?", (scenario_id,)):
            raise LookupError(item)
        store.execute("DELETE FROM scenario_domains WHERE scenario_id = ?", (scenario_id,))
        store.execute(
            "UPDATE mailboxes SET scenario_id = ? WHERE scenario_id = ?", (store.default_scenario_id, scenario_id)
        )
        store.reparse([store.default_scenario_id])
        self.send_json(200, {"id": scenario_id})

    def api_code(self, params, item=None):
        address = str(params.get("address", "")).strip().lower()
        if "@" not in address:
            raise ValueError("address is required")
        since = float(params.get("since", 0))
        deadline = time.time() + min(max(float(params.get("wait", 0)), 0), 60)
        while True:
            row = self.app.store.latest_code(address, since)
            if row or time.time() >= deadline:
                break
            time.sleep(0.5)
        row = row or {}
        self.send_json(
            200,
            {
                "address": address,
                "code": row.get("code"),
                "from": row.get("sender"),
                "subject": row.get("subject"),
                "received_at": row.get("received_at"),
            },
        )

    def api_mailboxes(self, params, item=None):
        limit = min(max(int(params.get("limit", 100)), 1), 500)
        scenario = str(params.get("scenario", "")).strip().lower()
        rows = self.app.store.query(
            "SELECT b.address, b.domain, b.created_at, s.key AS scenario, s.name AS scenario_name,"
            " COUNT(m.id) AS mail_count, MAX(m.received_at) AS last_mail_at"
            " FROM mailboxes b LEFT JOIN scenarios s ON s.id = b.scenario_id"
            " LEFT JOIN mails m ON m.address = b.address AND m.deleted_at IS NULL"
            " WHERE b.deleted_at IS NULL AND (? = '' OR s.key = ?)"
            " GROUP BY b.address ORDER BY COALESCE(MAX(m.received_at), b.created_at) DESC LIMIT ?",
            (scenario, scenario, limit),
        )
        self.send_json(200, {"mailboxes": rows})

    def api_mails(self, params, item=None):
        address = str(params.get("address", "")).strip().lower()
        limit = min(max(int(params.get("limit", 50)), 1), 200)
        sql = (
            "SELECT id, address, sender AS 'from', subject, body, code, received_at FROM mails"
            " WHERE deleted_at IS NULL"
        )
        if address:
            rows = self.app.store.query(sql + " AND address = ? ORDER BY id DESC LIMIT ?", (address, limit))
        else:
            rows = self.app.store.query(sql + " ORDER BY id DESC LIMIT ?", (limit,))
        self.send_json(200, {"mails": rows})

    def get_sent(self, params, item=None):
        limit = min(max(int(params.get("limit", 50)), 1), 200)
        rows = self.app.store.query(
            "SELECT id, sender AS 'from', recipients AS 'to', subject, body, status, error, created_at"
            " FROM sent_mails WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        self.send_json(200, {"configured": bool(self.app.cfg.resend_api_key), "mails": rows})

    def post_send(self, params, item=None):
        cfg, store = self.app.cfg, self.app.store
        if not cfg.resend_api_key:
            raise ValueError("发信未配置：请设置环境变量 RESEND_API_KEY")
        sender = str(params.get("from", "")).strip().lower()
        name = re.sub(r'[\r\n"<>]', "", str(params.get("from_name", ""))).strip()
        to = params.get("to", "")
        recipients = to if isinstance(to, list) else re.split(r"[,;\s]+", str(to))
        recipients = [str(value).strip() for value in recipients if str(value).strip()]
        subject = re.sub(r"[\r\n]+", " ", str(params.get("subject", ""))).strip()
        text, rich = str(params.get("text", "")), str(params.get("html", ""))
        if not _EMAIL.fullmatch(sender) or sender.rpartition("@")[2] not in store.domain_names():
            raise ValueError("发件地址必须是已添加域名下的邮箱：%s" % sender)
        if not recipients or len(recipients) > 20:
            raise ValueError("收件人数量必须在 1 到 20 之间")
        for address in recipients:
            if not _EMAIL.fullmatch(address):
                raise ValueError("收件地址格式不正确：%s" % address)
        if not subject:
            raise ValueError("主题不能为空")
        if not text.strip() and not rich.strip():
            raise ValueError("正文不能为空")
        payload = {"from": '"%s" <%s>' % (name, sender) if name else sender, "to": recipients, "subject": subject}
        if text.strip():
            payload["text"] = text
        if rich.strip():
            payload["html"] = rich
        row = (sender, ", ".join(recipients), subject, text or html_to_text(rich))
        sql = (
            "INSERT INTO sent_mails (sender, recipients, subject, body, status, error, provider_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        try:
            provider_id = send_via_resend(cfg, payload)
        except SendError as exc:
            store.insert(sql, row + ("failed", str(exc), "", time.time()))
            return self.send_json(502, {"error": str(exc)})
        new_id = store.insert(sql, row + ("sent", "", provider_id, time.time()))
        self.send_json(200, {"id": new_id, "provider_id": provider_id})

    def api_stats(self, params, item=None):
        day_ago = time.time() - 86400
        row = self.app.store.query(
            "SELECT (SELECT COUNT(*) FROM mailboxes WHERE deleted_at IS NULL) AS mailboxes,"
            " (SELECT COUNT(*) FROM mails WHERE deleted_at IS NULL) AS mails,"
            " (SELECT COUNT(*) FROM mails WHERE received_at >= ? AND deleted_at IS NULL) AS mails_24h,"
            " (SELECT COUNT(*) FROM mails WHERE code IS NOT NULL AND deleted_at IS NULL) AS codes",
            (day_ago,),
        )[0]
        row["domains"] = len(self.app.store.domain_names())
        self.send_json(200, row)

    def serve_static(self, path):
        root = os.path.realpath(self.app.cfg.static_dir)
        target = os.path.realpath(os.path.join(root, path.lstrip("/")))
        if not target.startswith(root + os.sep) or not os.path.isfile(target):
            target = os.path.join(root, "index.html")
        if not os.path.isfile(target):
            return self.send_json(404, {"error": "frontend not built"})
        with open(target, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MailApp:
    def __init__(self, cfg):
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        if not cfg.api_key:
            cfg.api_key = self.load_or_create_key()
        self.store = Store(os.path.join(cfg.data_dir, "mail.db"), cfg.domains)
        self.instance_id = secrets.token_hex(8)
        self.loop = None
        self.http = None
        self.smtp_port = self.http_port = None
        self.smtp_error = None
        self.stopped = threading.Event()

    @property
    def key_file(self):
        return os.path.join(self.cfg.data_dir, "api_key")

    def load_or_create_key(self):
        path = self.key_file
        if os.path.isfile(path):
            with open(path) as handle:
                return handle.read().strip()
        key = secrets.token_urlsafe(24)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            handle.write(key)
        return key

    def deliver(self, sender, recipients, raw):
        header_from, subject, plain, rich = parse_mail(raw)
        for address in recipients:
            code = extract_for(self.store.template_for(address), subject, plain, rich)
            self.store.add_mail(address, header_from or sender, subject, plain or rich, rich if plain else "", code)

    async def handle_smtp(self, reader, writer):
        try:
            await SmtpSession(self, reader, writer).run()
        except (asyncio.TimeoutError, ConnectionError, ValueError, OSError):
            pass
        finally:
            writer.close()

    def run_smtp(self, ready):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            server = self.loop.run_until_complete(
                asyncio.start_server(self.handle_smtp, self.cfg.smtp_host, self.cfg.smtp_port, limit=1024 * 1024)
            )
        except OSError as exc:
            self.smtp_error = exc
            ready.set()
            return
        self.smtp_port = server.sockets[0].getsockname()[1]
        ready.set()
        self.loop.run_forever()
        server.close()
        self.loop.run_until_complete(server.wait_closed())
        self.loop.close()

    def run_purge(self):
        while not self.stopped.wait(600):
            self.store.purge(self.cfg.retention_hours * 3600)

    def start(self):
        handler = type("Handler", (ApiHandler,), {"app": self})
        self.http = ThreadingHTTPServer((self.cfg.http_host, self.cfg.http_port), handler)
        self.http.daemon_threads = True
        self.http_port = self.http.server_address[1]
        ready = threading.Event()
        for target, args in ((self.run_smtp, (ready,)), (self.http.serve_forever, ()), (self.run_purge, ())):
            threading.Thread(target=target, args=args, daemon=True).start()
        ready.wait()
        if self.smtp_error:
            self.http.shutdown()
            self.http.server_close()
            raise self.smtp_error

    def stop(self):
        self.stopped.set()
        self.http.shutdown()
        self.http.server_close()
        self.loop.call_soon_threadsafe(self.loop.stop)


def main():
    cfg = Config()
    from_env = bool(cfg.api_key)
    app = MailApp(cfg)
    try:
        app.start()
    except OSError as exc:
        raise SystemExit("cannot listen on SMTP port %d: %s" % (cfg.smtp_port, exc))
    print("domains : %s" % (", ".join(sorted(app.store.domain_names())) or "none yet, add them in the dashboard"), flush=True)
    print("smtp    : %s:%d" % (cfg.smtp_host, app.smtp_port), flush=True)
    print("http    : %s:%d" % (cfg.http_host, app.http_port), flush=True)
    print("api key : %s" % ("from API_KEY env" if from_env else "stored in %s" % app.key_file), flush=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    app.stop()


if __name__ == "__main__":
    main()
