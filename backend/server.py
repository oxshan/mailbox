#!/usr/bin/env python3
"""Lightweight mailbox manager: SMTP receiver + HTTP API. Standard library only (Python 3.8+)."""
import asyncio
import os
import re
import secrets
import signal
import threading
import time
from email.utils import parseaddr
from http.server import ThreadingHTTPServer

from api import ApiHandler
from config import Config
from extract import _EMAIL, extract_for, parse_mail
from outbound import SendError, send_via_resend
from smtp import SmtpSession
from store import Store


class MailApp:
    def __init__(self, cfg):
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        if not cfg.api_key:
            cfg.api_key = self.load_or_create_key()
        self.store = Store(os.path.join(cfg.data_dir, "mail.db"), cfg.domains)
        if self.store.retired:
            print("old database kept as %s; started with an empty one" % self.store.retired, flush=True)
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
        """Store a received mail under every recipient, or under the alias it was forwarded from."""
        store = self.store
        mail = parse_mail(raw)
        mail["sender"] = mail["sender"] or sender
        collectors = store.collectors()
        accepted = []
        for recipient in recipients:
            source = collectors.get(recipient)
            alias = store.resolve_alias(source, mail["recipients"], mail["mentions"]) if source else None
            address = alias or recipient
            if store.is_disabled(address):
                continue
            code = extract_for(store.template_for(address), mail["subject"], mail["plain"], mail["rich"])
            store.add_mail(address, mail, code, source["id"] if alias else None)
            accepted.append(recipient)
        try:
            self.forward(
                mail["sender"], accepted, mail["subject"], mail["plain"] or mail["rich"], mail["markup"],
                mail["attachments"],
            )
        except Exception as exc:
            print("forward failed: %r" % exc, flush=True)

    def forward(self, origin, recipients, subject, text, markup, attachments):
        """Send a copy of a received mail to every enabled forward address through Resend."""
        cfg, store = self.cfg, self.store
        if not cfg.resend_api_key:
            return
        name, origin = parseaddr(origin)
        origin = origin.lower()
        own = store.domain_names()
        targets = [
            row["address"]
            for row in store.query("SELECT address FROM forwards WHERE enabled = 1 ORDER BY id")
            if row["address"] != origin and row["address"].rpartition("@")[2] not in own
        ]
        if not targets:
            return
        name = re.sub(r'[\r\n"<>]', "", name or origin).strip()
        subject = re.sub(r"[\r\n]+", " ", subject).strip() or "(无主题)"
        if attachments:
            note = "原邮件含 %d 个附件，未随转发发送" % attachments
            text = "%s\n\n[%s]" % (text, note)
            markup = markup and "%s<hr><p>%s</p>" % (markup, note)
        sql = (
            "INSERT INTO sent_mails (sender, recipients, subject, body, status, error, provider_id, kind, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'forward', ?)"
        )
        limit = cfg.forward_hourly_limit
        for address in recipients:
            row = (address, ", ".join(targets), subject, text)
            used = store.query(
                "SELECT COUNT(*) AS n FROM sent_mails WHERE kind = 'forward' AND created_at >= ?", (time.time() - 3600,)
            )[0]["n"]
            if limit > 0 and used >= limit:
                if used == limit:
                    error = "已达到每小时转发上限（%d 封），一小时内的后续邮件不再转发" % limit
                    store.insert(sql, row + ("failed", error, "", time.time()))
                return
            payload = {
                "from": '"%s via Mailbox" <%s>' % (name, address) if name else address,
                "to": targets,
                "subject": subject,
                "text": text or "(正文为空)",
            }
            if _EMAIL.fullmatch(origin):
                payload["reply_to"] = origin
            if markup.strip():
                payload["html"] = markup
            try:
                provider_id = send_via_resend(cfg, payload)
            except SendError as exc:
                store.insert(sql, row + ("failed", str(exc), "", time.time()))
            else:
                store.insert(sql, row + ("sent", "", provider_id, time.time()))

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
