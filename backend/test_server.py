import base64
import hashlib
import json
import os
import smtplib
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, quote, urlencode

import accounts
import mxcheck
from config import Config
from extract import compile_template, extract_code, extract_with_template, html_to_text, parse_mail
from mxcheck import parse_mx_response
from server import MailApp
from store import SCHEMA_VERSION, Store


class ExtractCodeTest(unittest.TestCase):
    def test_cases(self):
        cases = [
            ("Your verification code", "Your verification code is 483920. It expires in 10 minutes.", "483920"),
            ("【某站】验证码", "您的验证码是：7391，5分钟内有效。", "7391"),
            ("482913 is your Example code", "Enter it to continue. © 2026 Example Inc.", "482913"),
            ("Sign in", "Use code 123-456 to sign in.", "123456"),
            ("Confirm your email", "Hi,\n\n  847 291\n\nis your one-time code.", "847291"),
            ("Welcome", "Order placed in 2026. Reference 55512.", "55512"),
            ("Welcome", "Thanks for joining us in 2026!", None),
            ("Hello", "No numbers here.", None),
            ("Your OTP", "OTP: 90817263 valid for 300 seconds", "90817263"),
        ]
        for subject, body, expected in cases:
            self.assertEqual(extract_code(subject, body), expected, (subject, body))

    def test_html_to_text(self):
        markup = "<html><head><style>.a{color:#123456}</style></head><body><p>Code:&nbsp;<b>246810</b></p></body></html>"
        text = html_to_text(markup)
        self.assertNotIn("123456", text)
        self.assertEqual(extract_code("", text), "246810")


class TemplateTest(unittest.TestCase):
    def test_placeholder(self):
        regex = compile_template("placeholder", "Your code is {code}.")
        self.assertEqual(extract_with_template(regex, "", "Hi\nyour   code is\nA7K9Q2. Thanks", ""), "A7K9Q2")
        self.assertEqual(extract_with_template(regex, "Your code is 5512.", "", ""), "5512")
        self.assertEqual(extract_with_template(regex, "", "plain", "Your code is 7788."), "7788")
        self.assertIsNone(extract_with_template(regex, "", "nothing", ""))
        self.assertEqual(extract_with_template(compile_template("placeholder", "(ref) {code}"), "", "(ref) G-482913", ""), "G-482913")

    def test_regex(self):
        self.assertEqual(extract_with_template(compile_template("regex", r"token=(\w+)"), "", "url?token=abc123&x", ""), "abc123")
        self.assertEqual(extract_with_template(compile_template("regex", r"\d{3}-\d{3}"), "", "pin 123-456", ""), "123-456")

    def test_invalid(self):
        for mode, pattern in (("regex", "("), ("placeholder", "no marker"), ("placeholder", " "), ("other", "{code}")):
            with self.assertRaises(ValueError):
                compile_template(mode, pattern)


class ParseMailTest(unittest.TestCase):
    def test_recipient_headers_and_mentions(self):
        raw = (
            b"From: Shop <shop_at_example.com_alias@duck.com>\r\n"
            b"To: Calm-Tree-Owl@duck.com, Other <o@example.org>\r\n"
            b"Delivered-To: collect@a.test\r\n"
            b"X-Original-Recipient: hidden@duck.com\r\n"
            b"Message-ID: <abc@mail.example.com>\r\n"
            b"Subject: Hi\r\n\r\ncode 1234\r\n"
        )
        mail = parse_mail(raw)
        self.assertEqual(mail["recipients"], ["calm-tree-owl@duck.com", "o@example.org", "collect@a.test"])
        self.assertEqual(mail["mentions"], ["hidden@duck.com"])
        self.assertEqual((mail["subject"], mail["plain"]), ("Hi", "code 1234"))
        self.assertIn("Delivered-To: collect@a.test", mail["headers"])


class OldDatabaseTest(unittest.TestCase):
    def test_old_database_is_kept_aside_not_converted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mail.db")
            old = sqlite3.connect(path)
            old.executescript(
                """
                CREATE TABLE mailboxes (address TEXT PRIMARY KEY, domain TEXT NOT NULL, created_at REAL NOT NULL);
                INSERT INTO mailboxes VALUES ('old@a.test', 'a.test', 1);
                """
            )
            old.commit()
            old.close()
            store = Store(path, ["a.test"])
            self.assertTrue(os.path.basename(store.retired).startswith("mail.db.v0-"))
            self.assertEqual(store.query("SELECT * FROM mailboxes"), [])
            self.assertEqual(store.domain_names(), {"a.test"})
            self.assertEqual(store.query("PRAGMA user_version")[0]["user_version"], SCHEMA_VERSION)
            kept = sqlite3.connect(store.retired)
            self.assertEqual(kept.execute("SELECT address FROM mailboxes").fetchall(), [("old@a.test",)])
            kept.close()
            store.execute("INSERT INTO domains (name, created_at) VALUES ('b.test', 1)")
            store.db.close()
            again = Store(path, ["ignored.test"])
            self.assertIsNone(again.retired)
            self.assertEqual(again.domain_names(), {"a.test", "b.test"})
            self.assertEqual(again.default_scenario_id, store.default_scenario_id)
            again.db.close()

    def test_newer_database_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mail.db")
            newer = sqlite3.connect(path)
            newer.executescript("CREATE TABLE x (y); PRAGMA user_version = %d;" % (SCHEMA_VERSION + 1))
            newer.close()
            with self.assertRaises(RuntimeError):
                Store(path)
            self.assertTrue(os.path.isfile(path))


class DnsTest(unittest.TestCase):
    def test_parse_mx_response(self):
        question = b"\x07example\x03com\x00" + struct.pack("!HH", 15, 1)
        first = b"\xc0\x0c" + struct.pack("!HHIH", 15, 1, 300, 9) + struct.pack("!H", 20) + b"\x04mail\xc0\x0c"
        rdata = struct.pack("!H", 10) + b"\x03MX1\x07example\x03com\x00"
        second = b"\xc0\x0c" + struct.pack("!HHIH", 15, 1, 300, len(rdata)) + rdata
        other = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 300, 4) + b"\x01\x02\x03\x04"
        packet = struct.pack("!HHHHHH", 7, 0x8180, 1, 3, 0, 0) + question + first + other + second
        self.assertEqual(parse_mx_response(packet), [(10, "mx1.example.com"), (20, "mail.example.com")])
        nxdomain = struct.pack("!HHHHHH", 7, 0x8183, 1, 0, 0, 0) + question
        self.assertEqual(parse_mx_response(nxdomain), [])


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = Config(
            domains=["a.test", "b.test"],
            api_key="secret",
            smtp_host="127.0.0.1",
            smtp_port=0,
            http_host="127.0.0.1",
            http_port=0,
            data_dir=cls.tmp.name,
            max_mail_bytes=20000,
        )
        cls.app = MailApp(cfg)
        cls.app.start()

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.tmp.cleanup()

    def api(self, path, key="secret", method="GET", body=None):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.app.http_port, path),
            method=method,
            headers={"X-API-Key": key, "Content-Type": "application/json"},
            data=None if body is None else json.dumps(body).encode(),
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read())
        self.assertEqual((body["code"], body["msg"]), (0, "ok"))
        return body["data"]

    def new_box(self, **body):
        return self.api("/api/mailboxes", method="POST", body=body)

    def code(self, address, **params):
        return self.api("/api/mailboxes/%s/code?%s" % (quote(address), urlencode(params)))

    def api_error(self, *args, **kwargs):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.api(*args, **kwargs)
        body = json.loads(ctx.exception.read())
        self.assertEqual((body["code"], body["data"]), (ctx.exception.code, None))
        return ctx.exception.code, body["msg"]

    def test_scenario_generates_mailbox_and_parses_with_template(self):
        template = self.api(
            "/api/templates",
            method="POST",
            body={"name": "令牌", "mode": "placeholder", "pattern": "Your access token: {code}"},
        )
        scenario = self.api(
            "/api/scenarios",
            method="POST",
            body={"name": "注册", "key": "signup", "domain_ids": [self.domain_id("b.test")], "template_id": template["id"]},
        )
        box = self.new_box(scenario="signup")
        self.assertTrue(box["address"].endswith("@b.test"))
        self.assertEqual(box["scenario"], "signup")
        self.send(box["address"], "Welcome", "Order 123456 confirmed.\nYour access token:  AB12-CD34\nBye")
        self.assertEqual(self.code(box["address"])["verify_code"], "AB12-CD34")
        self.send(box["address"], "Other", "Your verification code is 998877")
        self.assertEqual(self.code(box["address"])["verify_code"], "AB12-CD34")

        listed = self.api("/api/mailboxes?scenario=signup")["mailboxes"]
        self.assertEqual([b["address"] for b in listed], [box["address"]])
        self.assertEqual(listed[0]["scenario_name"], "注册")
        scenarios = {s["key"]: s for s in self.api("/api/scenarios")["scenarios"]}
        self.assertEqual(scenarios["signup"]["template_name"], "令牌")
        self.assertEqual(scenarios["signup"]["domains"], ["b.test"])
        self.assertEqual(scenarios["default"]["domains"], [])
        self.assertEqual(scenarios["signup"]["mailbox_count"], 1)

        self.api(
            "/api/templates/%d" % template["id"],
            method="PUT",
            body={"name": "令牌", "mode": "regex", "pattern": r"code is (\d{6})"},
        )
        self.assertEqual(self.code(box["address"])["verify_code"], "998877")

        self.api("/api/scenarios/%d" % scenario["id"], method="DELETE")
        moved = {b["address"]: b for b in self.api("/api/mailboxes")["mailboxes"]}[box["address"]]
        self.assertEqual(moved["scenario"], "default")
        codes = [m["verify_code"] for m in self.api("/api/mails?address=" + box["address"])["mails"]]
        self.assertEqual(codes, ["998877", "123456"])
        self.api("/api/templates/%d" % template["id"], method="DELETE")
        self.assertEqual(self.api("/api/templates")["templates"], [])

    def test_template_and_scenario_validation(self):
        self.assertEqual(self.api_error("/api/mailboxes", method="POST", body={"scenario": "nope"})[0], 400)
        self.assertEqual(
            self.api_error("/api/templates", method="POST", body={"name": "x", "mode": "regex", "pattern": "("})[0], 400
        )
        self.assertIn(
            "{code}",
            self.api_error("/api/templates", method="POST", body={"name": "x", "pattern": "no placeholder"})[1],
        )
        self.assertEqual(self.api_error("/api/scenarios", method="POST", body={"name": "x", "key": "Bad Key!"})[0], 400)
        self.assertEqual(self.api_error("/api/scenarios", method="POST", body={"name": "x", "key": "default"})[0], 400)
        self.assertEqual(
            self.api_error("/api/scenarios", method="POST", body={"name": "x", "domain_ids": [99999]})[0], 400
        )
        default_id = self.app.store.default_scenario_id
        self.assertEqual(self.api_error("/api/scenarios/%d" % default_id, method="DELETE")[0], 400)
        self.assertEqual(self.api_error("/api/scenarios/99999", method="DELETE")[0], 404)
        self.assertEqual(self.api_error("/api/templates/99999", method="PUT", body={"name": "x", "pattern": "{code}"})[0], 404)

    def domain_id(self, name):
        return {d["name"]: d["id"] for d in self.api("/api/domains")["domains"]}[name]

    def test_domain_management(self):
        names = [d["name"] for d in self.api("/api/domains")["domains"]]
        self.assertEqual(names[:2], ["a.test", "b.test"])
        self.assertEqual(self.api_error("/api/domains", method="POST", body={"name": "not a domain"})[0], 400)
        self.assertEqual(self.api_error("/api/domains", method="POST", body={"name": "a.test"})[0], 400)
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            self.send("x@c.test", "Hi", "code 1234")

        c_id = self.api("/api/domains", method="POST", body={"name": "@C.Test", "note": "备用"})["id"]
        d_id = self.api("/api/domains", method="POST", body={"name": "d.test"})["id"]
        scenario = self.api("/api/scenarios", method="POST", body={"name": "多域名", "key": "multi", "domain_ids": [c_id, d_id]})
        seen = {self.new_box(scenario="multi")["domain"] for _ in range(30)}
        self.assertEqual(seen, {"c.test", "d.test"})
        self.assertEqual(self.api_error("/api/mailboxes", method="POST", body={"scenario": "multi", "domain": "a.test"})[0], 400)

        self.api("/api/domains/%d" % d_id, method="PUT", body={"enabled": False})
        seen = {self.new_box(scenario="multi")["domain"] for _ in range(15)}
        self.assertEqual(seen, {"c.test"})
        self.assertNotIn("d.test", {self.new_box()["domain"] for _ in range(30)})
        self.send("still@d.test", "Hi", "code 4455")
        self.assertEqual(self.code("still@d.test")["verify_code"], "4455")
        listed = {d["name"]: d for d in self.api("/api/domains")["domains"]}
        self.assertEqual((listed["d.test"]["enabled"], listed["c.test"]["note"]), (False, "备用"))
        self.assertEqual(listed["c.test"]["scenario_count"], 1)

        self.api("/api/domains/%d" % c_id, method="PUT", body={"enabled": False})
        self.assertIn("没有可用的域名", self.api_error("/api/mailboxes", method="POST", body={"scenario": "multi"})[1])

        self.api("/api/domains/%d" % c_id, method="DELETE")
        self.api("/api/domains/%d" % d_id, method="DELETE")
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            self.send("x@c.test", "Hi", "code 1234")
        scenarios = {s["key"]: s for s in self.api("/api/scenarios")["scenarios"]}
        self.assertEqual(scenarios["multi"]["domains"], [])
        self.api("/api/scenarios/%d" % scenario["id"], method="DELETE")
        self.assertEqual(self.api_error("/api/domains/%d" % c_id, method="DELETE")[0], 404)

    def test_domain_check(self):
        original, original_ip = mxcheck.resolve_mx, mxcheck.public_ip
        check = lambda: self.api("/api/domains/check", method="POST", body={"name": "a.test"})
        self.app.cfg.mx_check_port = self.app.smtp_port
        try:
            mxcheck.public_ip = lambda cfg: None
            mxcheck.resolve_mx = lambda name: [(10, "127.0.0.1")]
            self.assertEqual(check()["status"], "ok")
            mxcheck.resolve_mx = lambda name: []
            self.assertEqual(check()["status"], "missing")
            mxcheck.resolve_mx = lambda name: [(10, "127.0.0.1")]
            self.app.cfg.mx_check_port = self.app.http_port
            result = check()
            self.assertIn(result["status"], ("mismatch", "unverified"))
            self.assertEqual(result["mx"], ["10 127.0.0.1"])

            self.app.cfg.mx_check_port = 1
            self.assertEqual(check()["status"], "unverified")
            mxcheck.public_ip = lambda cfg: "127.0.0.1"
            result = check()
            self.assertEqual(result["status"], "ok")
            self.assertIn("公网 IP", result["message"])
            mxcheck.public_ip = lambda cfg: "203.0.113.9"
            self.assertEqual(check()["status"], "mismatch")
            mxcheck.resolve_mx = lambda name: [(10, "no-such-host.invalid")]
            with mock.patch.object(mxcheck.socket, "getaddrinfo", side_effect=OSError("unknown host")):
                self.assertEqual(mxcheck.check_domain("a.test", "x", own_ip="203.0.113.9")["status"], "error")
        finally:
            mxcheck.resolve_mx, mxcheck.public_ip = original, original_ip
        self.assertEqual(self.api_error("/api/domains/check", method="POST", body={"name": "nope.test"})[0], 404)

    def fake_resend(self):
        """Start a stand-in for the Resend API and point the app at it. Returns (server, recorded calls)."""
        calls = []

        class FakeResend(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.headers["Authorization"], self.headers["User-Agent"], body))
                failed = body["subject"] == "reject"
                reply = json.dumps({"message": "domain is not verified"} if failed else {"id": "re_123"}).encode()
                self.send_response(403 if failed else 200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeResend)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        cfg = self.app.cfg
        cfg.resend_api_key, cfg.resend_api_url = "re_test", "http://127.0.0.1:%d/emails" % fake.server_address[1]
        return fake, calls

    def test_forward(self):
        cfg = self.app.cfg
        self.assertEqual(self.api("/api/forwards"), {"configured": False, "hourly_limit": 60, "forwards": []})
        for bad in ("not-an-address", "", "me@a.test"):
            self.assertEqual(self.api_error("/api/forwards", method="POST", body={"address": bad})[0], 400, bad)
        first = self.api("/api/forwards", method="POST", body={"address": " Me@Example.com ", "note": "主邮箱"})["id"]
        self.assertEqual(self.api_error("/api/forwards", method="POST", body={"address": "me@example.com"})[0], 400)
        second = self.api("/api/forwards", method="POST", body={"address": "other@example.org", "enabled": False})["id"]
        listed = self.api("/api/forwards")["forwards"]
        self.assertEqual(
            [(f["address"], f["enabled"], f["note"]) for f in listed],
            [("me@example.com", True, "主邮箱"), ("other@example.org", False, "")],
        )

        def forwarded():
            return [m for m in self.api("/api/sent?limit=200")["mails"] if m["kind"] == "forward"]

        self.send("fw@a.test", "Unconfigured", "code 1111")
        self.assertEqual(forwarded(), [])

        fake, calls = self.fake_resend()
        limit = cfg.forward_hourly_limit
        try:
            self.assertTrue(self.api("/api/forwards")["configured"])
            self.send("fw@a.test", "Your code", "code 2468", html="<p>code <b>2468</b></p>")
            self.assertEqual(self.code("fw@a.test")["verify_code"], "2468")
            self.assertEqual(len(calls), 1)
            sent = calls[0][2]
            self.assertEqual(sent["from"], '"Example via Mailbox" <fw@a.test>')
            self.assertEqual((sent["to"], sent["reply_to"]), (["me@example.com"], "noreply@example.com"))
            self.assertEqual((sent["subject"], sent["text"].strip()), ("Your code", "code 2468"))
            self.assertIn("<b>2468</b>", sent["html"])
            record = forwarded()[0]
            self.assertEqual((record["from"], record["to"], record["status"]), ("fw@a.test", "me@example.com", "sent"))

            self.api("/api/forwards/%d" % second, method="PUT", body={"enabled": True, "note": "备用"})
            self.send("fw@a.test", "", "plain only")
            sent = calls[-1][2]
            self.assertEqual(sent["to"], ["me@example.com", "other@example.org"])
            self.assertEqual(sent["subject"], "(无主题)")
            self.assertNotIn("html", sent)

            self.send("fw@a.test", "Auto reply", "out of office", sender="me@example.com")
            self.assertEqual(calls[-1][2]["to"], ["other@example.org"])

            self.send("fw@a.test", "reject", "body")
            self.assertEqual((forwarded()[0]["status"], forwarded()[0]["subject"]), ("failed", "reject"))
            self.assertIn("domain is not verified", forwarded()[0]["error"])

            cfg.forward_hourly_limit = len(forwarded())
            before = len(calls)
            self.send("fw@a.test", "Over the limit", "code 3579")
            self.send("fw@a.test", "Still over", "code 3580")
            self.assertEqual(len(calls), before)
            self.assertEqual(self.code("fw@a.test")["verify_code"], "3580")
            records = forwarded()
            self.assertEqual(len(records), cfg.forward_hourly_limit + 1)
            self.assertEqual((records[0]["status"], records[0]["subject"]), ("failed", "Over the limit"))
            self.assertIn("上限", records[0]["error"])
            cfg.forward_hourly_limit = limit

            self.api("/api/forwards/%d" % first, method="PUT", body={"enabled": False})
            self.api("/api/forwards/%d" % second, method="DELETE")
            self.send("fw@a.test", "Nobody listening", "code 9999")
            self.assertEqual(len(calls), before)
            self.assertEqual(self.api_error("/api/forwards/%d" % second, method="DELETE")[0], 404)
            self.assertEqual(self.api_error("/api/forwards/%d" % second, method="PUT", body={"enabled": True})[0], 404)
        finally:
            cfg.resend_api_key, cfg.forward_hourly_limit = "", limit
            fake.shutdown()
            fake.server_close()
            self.app.store.execute("DELETE FROM forwards")
            self.app.store.execute("DELETE FROM sent_mails WHERE kind = 'forward'")

    def test_send_mail(self):
        mail = {"from": "Hello@A.test", "from_name": "客服", "to": "x@example.com, y@example.com", "subject": "Hi", "text": "body"}
        self.assertFalse(self.api("/api/sent")["configured"])
        self.assertIn("RESEND_API_KEY", self.api_error("/api/send", method="POST", body=mail)[1])

        fake, calls = self.fake_resend()
        cfg = self.app.cfg
        try:
            self.assertEqual(self.api("/api/send", method="POST", body=mail)["provider_id"], "re_123")
            auth, agent, sent = calls[-1]
            self.assertEqual((auth, agent), ("Bearer re_test", "mailbox/1.0"))
            self.assertEqual(sent["from"], '"客服" <hello@a.test>')
            self.assertEqual(sent["to"], ["x@example.com", "y@example.com"])
            self.assertEqual((sent["subject"], sent["text"]), ("Hi", "body"))

            code, message = self.api_error("/api/send", method="POST", body=dict(mail, subject="reject"))
            self.assertEqual(code, 424)
            self.assertIn("domain is not verified", message)

            for bad in ({"from": "a@evil.test"}, {"to": "not-an-address"}, {"to": ""}, {"subject": " "}, {"text": ""}):
                self.assertEqual(self.api_error("/api/send", method="POST", body=dict(mail, **bad))[0], 400, bad)
            self.assertEqual(len(calls), 2)

            listed = self.api("/api/sent")
            self.assertTrue(listed["configured"])
            self.assertEqual([(m["status"], m["subject"]) for m in listed["mails"]], [("failed", "reject"), ("sent", "Hi")])
            self.assertEqual(listed["mails"][1]["to"], "x@example.com, y@example.com")
            self.assertIn("domain is not verified", listed["mails"][0]["error"])

            cfg.resend_api_url = "http://127.0.0.1:1/emails"
            self.assertEqual(self.api_error("/api/send", method="POST", body=mail)[0], 424)
        finally:
            cfg.resend_api_key = ""
            fake.shutdown()
            fake.server_close()

    def test_template_test_endpoint(self):
        body = {"mode": "placeholder", "pattern": "验证码：{code}，{*}有效", "sample": "您的验证码：X9K2P，10 分钟内有效"}
        self.assertEqual(self.api("/api/templates/test", method="POST", body=body)["verify_code"], "X9K2P")
        body["sample"] = "没有验证码"
        self.assertIsNone(self.api("/api/templates/test", method="POST", body=body)["verify_code"])

    def send(self, to, subject, body, html=None, sender="Example <noreply@example.com>"):
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP("127.0.0.1", self.app.smtp_port, timeout=10) as client:
            client.send_message(msg)

    def fake_sources(self):
        """Start a stand-in for the addy.io and Duck APIs and point the app at it. Returns (server, state)."""
        state = {"calls": [], "recipients": [], "aliases": [], "limited": False}

        class FakeSources(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, payload=None):
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_call(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                path, _, query = self.path.partition("?")
                state["calls"].append((self.command, path, body, self.headers["User-Agent"]))
                good = self.headers["Authorization"] in ("Bearer addy_good_token_123", "Bearer duck_good_token_456")
                if path == "/duck/email/addresses":
                    if not good:
                        return self.reply(401)
                    state["aliases"].append({"id": "", "email": "quack-%d@duck.com" % (len(state["aliases"]) + 1)})
                    return self.reply(201, {"address": state["aliases"][-1]["email"].split("@")[0].upper()})
                if not good:
                    return self.reply(401, {"message": "Unauthenticated."})
                if path == "/addy/api-token-details":
                    return self.reply(200, {"name": "test"})
                if path == "/addy/recipients" and self.command == "GET":
                    return self.reply(200, {"data": state["recipients"]})
                if path == "/addy/recipients":
                    if body["email"] in state.get("taken", ()):
                        return self.reply(422, {"message": "A recipient with that email already exists."})
                    row = {"id": "rc-%d" % (len(state["recipients"]) + 1), "email": body["email"], "email_verified_at": None}
                    state["recipients"].append(row)
                    return self.reply(201, {"data": row})
                if path == "/addy/domain-options":
                    return self.reply(200, {"data": ["anonaddy.me"], "defaultAliasDomain": "anonaddy.me"})
                if path == "/addy/aliases" and self.command == "POST":
                    if state["limited"]:
                        return self.reply(429, {"message": "You have reached your hourly limit for creating new aliases"})
                    if state.get("plain"):
                        body = b"You've reached your recipient limit"
                        self.send_response(403)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        return self.wfile.write(body)
                    number = len(state["aliases"]) + 1
                    alias = {"id": "al-%d" % number, "email": "W%d@%s" % (number, body["domain"]), "active": True}
                    state["aliases"].append(alias)
                    return self.reply(201, {"data": alias})
                if path == "/addy/aliases":
                    wanted = parse_qs(query)["filter[search]"][0]
                    return self.reply(200, {"data": [a for a in state["aliases"] if wanted in a["email"].lower()]})
                return self.reply(204)

            do_GET = do_POST = do_DELETE = handle_call

        fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeSources)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        cfg = self.app.cfg
        cfg.addy_api_url = "http://127.0.0.1:%d/addy" % fake.server_address[1]
        cfg.duck_api_url = "http://127.0.0.1:%d/duck/" % fake.server_address[1]
        return fake, state

    def close_sources(self, fake):
        fake.shutdown()
        fake.server_close()
        store = self.app.store
        store.execute("DELETE FROM mails WHERE address IN (SELECT address FROM mailboxes WHERE source_id IS NOT NULL)")
        store.execute("DELETE FROM mailboxes WHERE source_id IS NOT NULL OR address LIKE 'collect-%'")
        store.execute("DELETE FROM mails WHERE address LIKE 'collect-%'")
        store.execute("DELETE FROM sources")
        store.execute("DELETE FROM scenario_sources")

    def relay(self, collector, to, subject, body, sender="Example <noreply@example.com>"):
        """Deliver a mail the way a forwarding service does: addressed to an alias, handed to the collector."""
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = sender, to, subject
        msg.set_content(body)
        with smtplib.SMTP("127.0.0.1", self.app.smtp_port, timeout=10) as client:
            client.sendmail("bounce@forwarder.example", [collector], msg.as_bytes())

    def boxes(self, query=""):
        return {b["address"]: b for b in self.api("/api/mailboxes?limit=500" + query)["mailboxes"]}

    def test_source_validation(self):
        post = lambda **body: self.api_error("/api/sources", method="POST", body=body)
        good = {"kind": "addy", "name": "主号", "token": "addy_good_token_123", "collector": "collect-v@a.test"}
        self.assertEqual(post(**dict(good, kind="gmail"))[0], 400)
        self.assertIn("名称", post(**dict(good, name=" "))[1])
        self.assertIn("令牌", post(**dict(good, token=""))[1])
        self.assertIn("已添加域名", post(**dict(good, collector="me@gmail.com"))[1])
        self.assertIn("别名格式", post(**dict(good, alias_format="custom"))[1])
        self.assertIn("别名域名", post(**dict(good, alias_domain="not a domain"))[1])
        first = self.api("/api/sources", method="POST", body=good)["id"]
        try:
            self.assertIn("另一个来源", post(**dict(good, kind="duck", name="副号"))[1])
            self.assertEqual(self.api_error("/api/sources/%d/nope" % first, method="POST")[0], 404)
            self.assertEqual(self.api_error("/api/sources/99999", method="PUT", body={"name": "x"})[0], 404)
            self.assertIn("收集地址", self.api_error("/api/domains/%d" % self.domain_id("a.test"), method="DELETE")[1])
            self.api("/api/sources/%d" % first, method="PUT", body={"name": "改名", "token": "", "enabled": False})
            listed = self.api("/api/sources")["sources"][0]
            self.assertEqual((listed["name"], listed["enabled"], listed["token_mask"]), ("改名", False, "addy…_123"))
            self.assertNotIn("token", listed)
            self.assertEqual(listed["capabilities"], ["create", "toggle", "delete"])
            self.assertEqual(self.app.store.source(first)["token"], "addy_good_token_123")
            self.assertEqual(self.api("/api/stats")["sources"], 1)
        finally:
            self.api("/api/sources/%d" % first, method="DELETE")
        self.assertEqual(self.api("/api/sources")["sources"], [])

    def test_addy_source(self):
        fake, state = self.fake_sources()
        collector = "collect-addy@a.test"
        try:
            source = self.api(
                "/api/sources",
                method="POST",
                body={"kind": "addy", "name": "addy 主号", "token": "addy_good_token_123", "collector": "Collect-Addy@A.test"},
            )["id"]
            check = lambda: self.api("/api/sources/%d/check" % source, method="POST")
            self.assertIn("不是 addy 的收件人", self.api_error("/api/mailboxes", method="POST", body={"source": source})[1])
            self.assertEqual(check()["status"], "pending")
            self.assertEqual(state["recipients"][0]["email"], collector)
            state["recipients"][0]["email_verified_at"] = "2026-11-25 10:00:00"
            self.assertEqual(check()["status"], "ok")
            self.assertEqual(len(state["recipients"]), 1)

            box = self.new_box(source=source)
            self.assertEqual((box["address"], box["domain"]), ("w1@anonaddy.me", "anonaddy.me"))
            self.assertEqual((box["source"], box["source_kind"], box["source_name"]), (source, "addy", "addy 主号"))
            self.assertEqual((box["scenario"], box["active"]), ("default", True))
            created = [call for call in state["calls"] if call[:2] == ("POST", "/addy/aliases")][0]
            self.assertEqual((created[2]["domain"], created[2]["recipient_ids"]), ("anonaddy.me", ["rc-1"]))
            self.assertNotIn("format", created[2])
            self.assertEqual(created[3], "mailbox/1.0")

            self.relay(collector, "W1@anonaddy.me", "Verify", "Your verification code is 700123")
            self.assertEqual(self.code(box["address"])["verify_code"], "700123")
            self.assertEqual(self.api("/api/mailboxes/%s/mails" % box["address"])["mails"][0]["address"], box["address"])
            self.assertIn("To: W1@anonaddy.me", self.api("/api/mails?address=" + box["address"])["mails"][0]["headers"])
            self.assertNotIn(collector, self.boxes())

            self.relay(collector, "made-elsewhere@anonaddy.me", "Verify", "Your verification code is 700456")
            elsewhere = self.boxes("&source=%d" % source)["made-elsewhere@anonaddy.me"]
            self.assertEqual((elsewhere["source_kind"], elsewhere["scenario"], elsewhere["mail_count"]), ("addy", "default", 1))
            self.assertEqual(sorted(self.boxes("&source=%d" % source)), ["made-elsewhere@anonaddy.me", "w1@anonaddy.me"])
            self.assertNotIn("w1@anonaddy.me", self.boxes("&source=domain"))

            self.relay(collector, collector, "Confirm your recipient", "Click the link, code 700789")
            self.assertEqual(self.boxes()[collector]["source_kind"], "domain")

            path = "/api/mailboxes/" + box["address"]
            self.assertEqual(self.api(path, method="PUT", body={"active": False}), {"address": box["address"], "active": False, "synced": True})
            self.assertEqual(state["calls"][-1][:2], ("DELETE", "/addy/active-aliases/al-1"))
            self.assertFalse(self.api(path)["active"])
            self.relay(collector, "w1@anonaddy.me", "Verify", "Your verification code is 700999")
            self.assertEqual(self.code(box["address"])["verify_code"], "700123")
            self.assertEqual(sorted(self.boxes("&active=false")), ["w1@anonaddy.me"])
            self.api(path, method="PUT", body={"active": True})
            self.assertEqual(state["calls"][-1][:3], ("POST", "/addy/active-aliases", {"id": "al-1"}))

            state["aliases"].append({"id": "al-77", "email": "made-elsewhere@anonaddy.me"})
            self.api("/api/mailboxes/made-elsewhere@anonaddy.me", method="PUT", body={"active": False})
            self.assertEqual(state["calls"][-1][:2], ("DELETE", "/addy/active-aliases/al-77"))
            self.assertEqual(self.app.store.mailbox("made-elsewhere@anonaddy.me")["remote_id"], "al-77")

            state["limited"] = True
            before = len(self.boxes())
            status, message = self.api_error("/api/mailboxes", method="POST", body={"source": source})
            self.assertEqual(status, 424)
            self.assertIn("hourly limit", message)
            self.assertEqual(len(self.boxes()), before)
            state["limited"] = False
            state["plain"] = True
            self.assertEqual(
                self.api_error("/api/mailboxes", method="POST", body={"source": source}),
                (424, "addy.io 返回 403：You've reached your recipient limit"),
            )
            state["plain"] = False

            self.api("/api/sources/%d" % source, method="PUT", body={"alias_domain": "me.anonaddy.com", "alias_format": "uuid"})
            self.assertEqual(self.new_box(source=source)["domain"], "me.anonaddy.com")
            self.assertEqual(state["calls"][-1][2]["format"], "uuid")

            self.assertIn("3 个邮箱", self.api_error("/api/sources/%d" % source, method="DELETE")[1])
            self.assertEqual(self.api(path, method="DELETE"), {"address": box["address"], "synced": True})
            self.assertEqual(state["calls"][-1][:2], ("DELETE", "/addy/aliases/al-1"))
            self.assertEqual(self.api_error(path)[0], 404)
            self.assertEqual(self.api_error(path, method="DELETE")[0], 404)
            self.assertIsNone(self.code(box["address"])["verify_code"])

            self.api("/api/sources/%d" % source, method="PUT", body={"token": "addy_bad_token"})
            self.assertEqual(self.api_error("/api/sources/%d/check" % source, method="POST"), (424, "addy.io 返回 401：Unauthenticated."))
        finally:
            self.close_sources(fake)

    def test_addy_collector_cases(self):
        fake, state = self.fake_sources()
        store = self.app.store
        try:
            state["recipients"].append({"id": "rc-gmail", "email": "me@gmail.com", "email_verified_at": "2026-11-01"})
            state["taken"] = ["taken@a.test"]
            add = lambda collector: self.api(
                "/api/sources",
                method="POST",
                body={"kind": "addy", "name": collector, "token": "addy_good_token_123", "collector": collector},
            )["id"]
            check = lambda source: self.api("/api/sources/%d/check" % source, method="POST")

            taken = check(add("taken@a.test"))
            self.assertEqual(taken["status"], "error")
            self.assertIn("另一个 addy 账户的收件人", taken["message"])

            store.add_account("outlook", "me@outlook.com", "client-1", "rt-good-0")
            via = add("me@outlook.com")
            calls = len(state["calls"])
            result = check(via)
            self.assertEqual(result["status"], "ok")
            self.assertIn("me@gmail.com", result["message"])
            self.assertNotIn("POST", [call[0] for call in state["calls"][calls:]])
            box = self.new_box(source=via)
            created = [call for call in state["calls"] if call[:2] == ("POST", "/addy/aliases")][-1]
            self.assertNotIn("recipient_ids", created[2])
            self.assertEqual(box["source_kind"], "addy")
        finally:
            fake.shutdown()
            fake.server_close()
            store.execute("DELETE FROM mailboxes WHERE source_id IS NOT NULL OR account_id IS NOT NULL")
            for table in ("sources", "accounts", "scenario_sources"):
                store.execute("DELETE FROM %s" % table)

    def test_duck_source(self):
        fake, state = self.fake_sources()
        collector = "collect-duck@b.test"
        try:
            source = self.api(
                "/api/sources",
                method="POST",
                body={"kind": "duck", "name": "Duck", "token": "duck_good_token_456", "collector": collector, "alias_format": "uuid"},
            )["id"]
            listed = self.api("/api/sources")["sources"][0]
            self.assertEqual((listed["capabilities"], listed["alias_format"]), (["create"], ""))
            self.assertEqual(self.api("/api/sources/%d/check" % source, method="POST")["status"], "unknown")

            box = self.new_box(source=source)
            self.assertEqual((box["address"], box["source_kind"]), ("quack-1@duck.com", "duck"))
            self.assertIn("Mozilla/5.0", state["calls"][-1][3])

            relay = lambda code: self.relay(
                collector, "Quack-1@duck.com", "Verify", "Your verification code is %s" % code,
                sender="Shop <shop_at_example.com_quack-9@duck.com>",
            )
            relay("810001")
            self.assertEqual(self.code(box["address"])["verify_code"], "810001")
            self.assertNotIn("quack-9@duck.com", self.boxes())
            self.relay(collector, "someone@gmail.com", "Not for an alias", "code 810002")
            self.assertEqual(self.boxes()[collector]["mail_count"], 1)

            path = "/api/mailboxes/" + box["address"]
            calls = len(state["calls"])
            self.assertEqual(self.api(path, method="PUT", body={"active": False})["synced"], False)
            relay("810003")
            self.assertEqual(self.code(box["address"])["verify_code"], "810001")
            self.assertEqual(self.api(path, method="DELETE"), {"address": box["address"], "synced": False})
            self.assertEqual(len(state["calls"]), calls)
            relay("810004")
            again = self.boxes()[box["address"]]
            self.assertEqual((again["source"], again["scenario"], again["active"]), (source, "default", True))
            self.assertEqual(self.code(box["address"])["verify_code"], "810004")

            self.api("/api/sources/%d" % source, method="PUT", body={"token": "duck_bad_token"})
            self.assertEqual(self.api_error("/api/mailboxes", method="POST", body={"source": source}), (424, "Duck 返回 401：令牌无效或已过期"))
        finally:
            self.close_sources(fake)

    def test_scenario_sources(self):
        fake, state = self.fake_sources()
        scenario_ids = []
        try:
            add = lambda name, collector: self.api(
                "/api/sources",
                method="POST",
                body={"kind": "duck", "name": name, "token": "duck_good_token_456", "collector": collector},
            )["id"]
            first, second = add("甲", "collect-1@a.test"), add("乙", "collect-2@a.test")
            scene = lambda **body: scenario_ids.append(self.api("/api/scenarios", method="POST", body=body)["id"])
            scene(name="只用甲", key="only-first", source_ids=[first])
            scene(name="甲和域名", key="mixed", source_ids=[first], domain_ids=[self.domain_id("b.test")])
            self.assertEqual(
                self.api_error("/api/scenarios", method="POST", body={"name": "x", "source_ids": [99999]})[0], 400
            )
            listed = {s["key"]: s for s in self.api("/api/scenarios")["scenarios"]}
            self.assertEqual((listed["mixed"]["source_ids"], listed["mixed"]["sources"]), ([first], ["甲"]))
            self.assertEqual(listed["default"]["source_ids"], [])
            self.assertEqual(self.api("/api/sources")["sources"][0]["scenario_count"], 2)

            self.assertEqual({self.new_box()["source_kind"] for _ in range(10)}, {"domain"})
            self.assertEqual(self.new_box(source=second)["source"], second)
            self.assertEqual({self.new_box(scenario="only-first")["source"] for _ in range(5)}, {first})
            self.assertIn("不能使用来源", self.api_error("/api/mailboxes", method="POST", body={"scenario": "only-first", "source": second})[1])
            self.assertEqual(self.new_box(scenario="only-first", domain="a.test")["source_kind"], "domain")
            self.assertEqual(self.api_error("/api/mailboxes", method="POST", body={"source": first, "domain": "a.test"})[0], 400)
            self.assertEqual(self.api_error("/api/mailboxes", method="POST", body={"source": 99999})[0], 400)
            mixed = [self.new_box(scenario="mixed") for _ in range(40)]
            self.assertEqual({(b["source_kind"], b["domain"]) for b in mixed}, {("duck", "duck.com"), ("domain", "b.test")})
            self.assertEqual(self.boxes("&scenario=only-first&source=%d" % first).keys() <= self.boxes("&source=%d" % first).keys(), True)

            self.api("/api/sources/%d" % first, method="PUT", body={"enabled": False})
            self.assertIn("已停用", self.api_error("/api/mailboxes", method="POST", body={"source": first})[1])
            self.assertIn("都已停用", self.api_error("/api/mailboxes", method="POST", body={"scenario": "only-first"})[1])
            self.assertEqual({self.new_box(scenario="mixed")["source_kind"] for _ in range(5)}, {"domain"})
        finally:
            for scenario_id in scenario_ids:
                self.api("/api/scenarios/%d" % scenario_id, method="DELETE")
            self.close_sources(fake)
        self.assertEqual(self.app.store.query("SELECT * FROM scenario_sources"), [])

    def test_disable_and_delete_local_mailbox(self):
        address = self.new_box(domain="a.test")["address"]
        path = "/api/mailboxes/" + address
        self.send(address, "Code", "code 310001")
        self.assertEqual(self.api_error(path, method="PUT", body={})[0], 400)
        self.assertEqual(self.api(path, method="PUT", body={"active": False}), {"address": address, "active": False, "synced": False})
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            self.send(address, "Code", "code 310002")
        self.assertIn(address, self.boxes("&active=0"))
        self.assertNotIn(address, self.boxes("&active=1"))
        self.app.store.execute("UPDATE mailboxes SET created_at = created_at - 7200 WHERE address = ?", (address,))
        self.app.store.execute("UPDATE mails SET received_at = received_at - 7200 WHERE address = ?", (address,))
        self.app.store.purge(3600)
        self.assertFalse(self.api(path)["active"])
        self.api(path, method="PUT", body={"active": True})
        self.send(address, "Code", "code 310003")
        self.assertEqual(self.code(address)["verify_code"], "310003")
        self.assertEqual(self.api(path, method="DELETE"), {"address": address, "synced": False})
        self.assertEqual(self.api_error(path)[0], 404)
        self.assertEqual(self.api("/api/mailboxes/%s/mails" % address)["mails"], [])
        self.assertEqual(self.api_error("/api/mailboxes/%s/nope" % address)[0], 404)
        self.assertEqual(self.api_error("/api/domains/1/extra")[0], 404)

    def fake_microsoft(self):
        """Start a stand-in for Microsoft's token endpoint and Graph, and point the app at it."""
        state = {"inbox": [], "junkemail": [], "tokens": 0, "graph_down": False, "device": "pending"}

        class FakeMicrosoft(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                if self.path == "/devicecode":
                    state["device_request"] = form
                    reply = {"device_code": "dc-1", "user_code": "ABCD1234", "expires_in": 900, "interval": 1}
                    return self.reply(200, dict(reply, verification_uri="https://login.microsoft.com/device"))
                if form.get("grant_type") == ["authorization_code"]:
                    state["code_request"] = form
                    if form["code"] != ["code-1"]:
                        detail = "AADSTS70008: The provided authorization code has expired.\r\nTrace ID: abc"
                        return self.reply(400, {"error": "invalid_grant", "error_description": detail})
                    claims = base64.urlsafe_b64encode(json.dumps({"preferred_username": "Link@Outlook.com"}).encode())
                    id_token = "h.%s.sig" % claims.decode().rstrip("=")
                    return self.reply(200, {"refresh_token": "rt-good-link", "id_token": id_token})
                if "device_code" in form:
                    if form["device_code"] != ["dc-1"] or state["device"] == "declined":
                        detail = "AADSTS70016: The user declined.\r\nTrace ID: abc"
                        return self.reply(400, {"error": "authorization_declined", "error_description": detail})
                    if state["device"] == "pending":
                        return self.reply(400, {"error": "authorization_pending", "error_description": "waiting"})
                    claims = base64.urlsafe_b64encode(json.dumps({"email": state["device"]}).encode()).decode().rstrip("=")
                    return self.reply(200, {"refresh_token": "rt-good-device", "id_token": "h.%s.sig" % claims})
                if not form["refresh_token"][0].startswith("rt-good") or form["client_id"] != ["client-1"]:
                    detail = "AADSTS70000: The refresh token is invalid.\r\nTrace ID: abc"
                    return self.reply(400, {"error": "invalid_grant", "error_description": detail})
                state["tokens"] += 1
                state["scope"] = form["scope"][0]
                self.reply(200, {"access_token": "at-1", "refresh_token": "rt-good-%d" % state["tokens"]})

            def do_GET(self):
                folder = self.path.split("/mailFolders/")[1].split("/")[0]
                if state["graph_down"] or self.headers["Authorization"] != "Bearer at-1":
                    return self.reply(403, {"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}})
                self.reply(200, {"value": state[folder]})

        fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeMicrosoft)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        cfg = self.app.cfg
        base = "http://127.0.0.1:%d" % fake.server_address[1]
        cfg.ms_token_url, cfg.ms_imap_token_url, cfg.ms_graph_url = base + "/token", base + "/token", base + "/graph"
        cfg.ms_device_url = base + "/devicecode"
        return fake, state

    @staticmethod
    def graph_message(remote_id, subject, body, to="me@outlook.com", received="2026-11-25T08:00:00Z", headers=True):
        message = {
            "id": remote_id,
            "subject": subject,
            "receivedDateTime": received,
            "from": {"emailAddress": {"name": "GitHub", "address": "noreply@github.com"}},
            "toRecipients": [{"emailAddress": {"name": to, "address": to}}],
            "body": {"contentType": "text", "content": body},
        }
        if headers:
            message["internetMessageHeaders"] = [
                {"name": "From", "value": "GitHub <noreply@github.com>"},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
            ]
        return message

    def test_outlook_authorization(self):
        fake, state = self.fake_microsoft()
        store = self.app.store
        authorize = lambda **body: self.api("/api/accounts/authorize", method="POST", body=body)
        try:
            self.assertEqual(self.app.cfg.ms_client_id, "9e5f94bc-e8a4-4e73-b8be-63364c29d753")
            started = authorize()
            self.assertEqual((started["user_code"], started["interval"]), ("ABCD1234", 1))
            self.assertEqual(started["verification_uri"], "https://login.microsoft.com/device")
            self.assertEqual(state["device_request"]["client_id"], [self.app.cfg.ms_client_id])
            self.assertIn("Mail.Read", state["device_request"]["scope"][0])
            self.assertEqual(authorize(id=started["id"]), {"status": "pending"})
            state["device"] = "New@Outlook.com"
            self.assertEqual(authorize(id=started["id"]), {"status": "done", "address": "new@outlook.com", "added": True})
            account = store.query("SELECT * FROM accounts WHERE address = 'new@outlook.com'")[0]
            self.assertEqual((account["client_id"], account["refresh_token"]), (self.app.cfg.ms_client_id, "rt-good-device"))
            self.assertEqual(self.boxes("&source=account")["new@outlook.com"]["source_kind"], "outlook")
            self.assertIn("已过期", self.api_error("/api/accounts/authorize", method="POST", body={"id": started["id"]})[1])

            store.execute("UPDATE accounts SET refresh_token = 'old', status = 'error', error = 'x' WHERE id = ?", (account["id"],))
            again = authorize()
            self.assertEqual(authorize(id=again["id"]), {"status": "done", "address": "new@outlook.com", "added": False})
            renewed = store.account(account["id"])
            self.assertEqual((renewed["refresh_token"], renewed["status"], renewed["error"]), ("rt-good-device", "", ""))

            state["device"] = "declined"
            declined = authorize()
            status, message = self.api_error("/api/accounts/authorize", method="POST", body={"id": declined["id"]})
            self.assertEqual((status, message), (424, "微软登录：AADSTS70016: The user declined."))
            self.assertEqual(self.app.authorizations, {})
            state["device"] = "x@a.test"
            own = authorize()
            self.assertIn("自有域名", self.api_error("/api/accounts/authorize", method="POST", body={"id": own["id"]})[1])

            callback = lambda url="": self.api("/api/accounts/callback", method="POST", body={"callback_url": url})
            callback_error = lambda url: self.api_error("/api/accounts/callback", method="POST", body={"callback_url": url})
            link = callback()
            self.assertEqual((link["redirect_uri"], link["expires_in"]), ("http://localhost:8080", 600))
            query = parse_qs(link["url"].split("?", 1)[1])
            self.assertTrue(link["url"].startswith(self.app.cfg.ms_authorize_url + "?"))
            self.assertEqual(query["client_id"], [self.app.cfg.ms_client_id])
            self.assertEqual((query["response_type"], query["code_challenge_method"]), (["code"], ["S256"]))
            self.assertEqual(query["redirect_uri"], ["http://localhost:8080"])
            back = "http://localhost:8080/?code=%s&state=%s"
            self.assertIn("没有授权码", callback_error("http://localhost:8080/?state=" + query["state"][0])[1])
            self.assertIn("进行中的授权", callback_error(back % ("code-1", "someone-else"))[1])
            denied = callback_error("http://localhost:8080/?error=access_denied&error_description=The+user+declined.")
            self.assertEqual(denied, (400, "微软登录：The user declined."))
            status, message = callback_error(back % ("code-old", query["state"][0]))
            self.assertEqual((status, message), (424, "微软登录：AADSTS70008: The provided authorization code has expired."))
            self.assertIn("进行中的授权", callback_error(back % ("code-1", query["state"][0]))[1])

            fresh = parse_qs(callback()["url"].split("?", 1)[1])
            done = callback("  " + back % ("code-1", fresh["state"][0]) + "#  ")
            self.assertEqual(done, {"status": "done", "address": "link@outlook.com", "added": True})
            sent = state["code_request"]
            self.assertEqual(sent["redirect_uri"], ["http://localhost:8080"])
            verifier = sent["code_verifier"][0]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            self.assertEqual([challenge], fresh["code_challenge"])
            account = store.query("SELECT client_id, refresh_token FROM accounts WHERE address = 'link@outlook.com'")[0]
            self.assertEqual(account, {"client_id": self.app.cfg.ms_client_id, "refresh_token": "rt-good-link"})
            self.assertEqual(self.app.authorizations, {})
        finally:
            fake.shutdown()
            fake.server_close()
            store.execute("DELETE FROM accounts")
            store.execute("DELETE FROM mailboxes WHERE account_id IS NOT NULL")

    def test_outlook_account(self):
        fake, state = self.fake_microsoft()
        store = self.app.store
        post = lambda **body: self.api_error("/api/accounts", method="POST", body=body)
        good = {"address": "Me@Outlook.com", "client_id": "client-1", "refresh_token": "rt-good-0", "note": "主号"}
        try:
            self.assertEqual(post(**dict(good, kind="gmail"))[0], 400)
            self.assertIn("格式不正确", post(**dict(good, address="nope"))[1])
            self.assertIn("自有域名", post(**dict(good, address="x@a.test"))[1])
            self.assertIn("缺少", post(**dict(good, refresh_token=""))[1])
            self.assertIn("第 2 行", post(text="a@outlook.com----pw----c----t\nbroken line")[1])
            self.assertEqual(self.api("/api/accounts", method="POST", body=good), {"added": ["me@outlook.com"], "skipped": []})
            self.assertIn("已存在", post(**good)[1])
            imported = self.api(
                "/api/accounts",
                method="POST",
                body={"text": "me@outlook.com----pw----client-1----rt-good-0\n\nBad@Hotmail.com--------client-1----rt-bad----with----dashes\n"},
            )
            self.assertEqual(imported, {"added": ["bad@hotmail.com"], "skipped": ["me@outlook.com"]})
            self.assertEqual(store.query("SELECT refresh_token FROM accounts WHERE address = 'bad@hotmail.com'")[0]["refresh_token"], "rt-bad----with----dashes")

            listed = {a["address"]: a for a in self.api("/api/accounts")["accounts"]}
            me, bad = listed["me@outlook.com"], listed["bad@hotmail.com"]
            self.assertEqual((me["kind"], me["note"], me["enabled"], me["status"], me["token_mask"]), ("outlook", "主号", True, "", "rt-g…od-0"))
            self.assertNotIn("refresh_token", me)
            box = self.boxes("&source=account")["me@outlook.com"]
            self.assertEqual((box["source_kind"], box["account"], box["source"], box["scenario"]), ("outlook", me["id"], None, "default"))
            self.assertNotIn("me@outlook.com", self.boxes("&source=domain"))
            self.assertEqual(self.api("/api/stats")["accounts"], 2)

            state["inbox"] = [
                self.graph_message("m1", "Old code", "Your verification code is 410001", received="2026-11-01T08:00:00Z"),
                self.graph_message("m2", "New code", "Your verification code is 410002", headers=False),
            ]
            state["inbox"][1]["body"] = {"contentType": "html", "content": "<p>Your verification code is <b>410002</b></p>"}
            state["junkemail"] = [self.graph_message("m3", "Junk", "no code here", received="2026-11-02T08:00:00Z")]
            sync = "/api/accounts/%d/sync" % me["id"]
            self.assertEqual(self.api(sync, method="POST"), {"fetched": 3, "new": 3, "channel": "graph"})
            self.assertEqual(state["scope"], "https://graph.microsoft.com/.default")
            self.assertEqual(self.api(sync, method="POST")["new"], 0)
            self.assertEqual(store.account(me["id"])["refresh_token"], "rt-good-2")
            mails = self.api("/api/mailboxes/me@outlook.com/mails")["mails"]
            self.assertEqual([m["subject"] for m in mails], ["New code", "Junk", "Old code"])
            self.assertEqual(mails[0]["from"], "GitHub <noreply@github.com>")
            self.assertIn("To: me@outlook.com", mails[0]["headers"])
            self.assertEqual(mails[0]["html"], "<p>Your verification code is <b>410002</b></p>")
            self.assertEqual(mails[0]["body"], "Your verification code is 410002")
            self.assertEqual(mails[1]["html"], "")
            self.assertLess(mails[2]["received_at"], mails[0]["received_at"])
            self.assertEqual(self.code("me@outlook.com")["verify_code"], "410002")
            listed = {a["address"]: a for a in self.api("/api/accounts")["accounts"]}["me@outlook.com"]
            self.assertEqual((listed["status"], listed["channel"], listed["mail_count"]), ("ok", "graph", 3))

            state["inbox"].append(self.graph_message("m4", "Fresh", "Your verification code is 410004", received="2026-11-25T09:00:00Z"))
            store.execute("UPDATE accounts SET synced_at = synced_at - 60 WHERE id = ?", (me["id"],))
            self.assertEqual(self.code("me@outlook.com")["verify_code"], "410004")
            state["inbox"].append(self.graph_message("m5", "Too soon", "Your verification code is 410005", received="2026-11-25T09:01:00Z"))
            self.assertEqual(self.code("me@outlook.com")["verify_code"], "410004")

            store.execute("UPDATE mails SET received_at = 1 WHERE address = 'me@outlook.com'")
            store.purge(3600)
            self.assertEqual(len(self.api("/api/mails?address=me@outlook.com")["mails"]), 4)
            self.assertIn("me@outlook.com", self.boxes())

            source = self.api(
                "/api/sources",
                method="POST",
                body={"kind": "duck", "name": "Duck 转 Outlook", "token": "duck_good_token_456", "collector": "me@outlook.com"},
            )["id"]
            self.assertIn("已接入的邮箱账号", self.api_error("/api/sources", method="POST", body={"kind": "duck", "name": "x", "token": "t", "collector": "other@outlook.com"})[1])
            store.add_mailbox("calm-owl@duck.com", store.default_scenario_id, source)
            state["inbox"].append(self.graph_message("m6", "Via alias", "Your verification code is 410006", to="Calm-Owl@duck.com", received="2026-11-25T09:02:00Z"))
            store.execute("UPDATE accounts SET synced_at = synced_at - 60 WHERE id = ?", (me["id"],))
            self.assertEqual(self.code("calm-owl@duck.com")["verify_code"], "410006")
            self.assertEqual(self.boxes()["calm-owl@duck.com"]["source_kind"], "duck")
            self.assertNotIn("Via alias", [m["subject"] for m in self.api("/api/mails?address=me@outlook.com")["mails"]])

            bad_sync = "/api/accounts/%d/sync" % bad["id"]
            status, message = self.api_error(bad_sync, method="POST")
            self.assertEqual(status, 424)
            self.assertIn("AADSTS70000: The refresh token is invalid.", message)
            self.assertNotIn("Trace ID", message)
            self.assertIn("改用 IMAP 也失败", message)
            listed = {a["address"]: a for a in self.api("/api/accounts")["accounts"]}["bad@hotmail.com"]
            self.assertEqual((listed["status"], listed["error"]), ("error", message))
            self.assertIsNone(self.code("bad@hotmail.com")["verify_code"])

            state["graph_down"] = True
            relayed = {"remote_id": "<i1@x>", "received_at": time.time(), "mail": parse_mail(b"From: a@b.c\r\nTo: me@outlook.com\r\nSubject: Over IMAP\r\n\r\ncode 410007\r\n")}
            with mock.patch.object(accounts, "fetch_imap", return_value=([relayed], "rt-imap")) as imap:
                self.assertEqual(self.api(sync, method="POST"), {"fetched": 1, "new": 1, "channel": "imap"})
                self.assertEqual(imap.call_count, 1)
            self.assertEqual(store.account(me["id"])["refresh_token"], "rt-imap")
            self.assertEqual(self.api("/api/mails?address=me@outlook.com")["mails"][0]["subject"], "Over IMAP")

            self.api("/api/accounts/%d" % me["id"], method="PUT", body={"enabled": False, "note": "停用"})
            edited = store.account(me["id"])
            self.assertEqual((edited["enabled"], edited["note"], edited["refresh_token"]), (0, "停用", "rt-imap"))
            self.api("/api/accounts/%d" % me["id"], method="PUT", body={"refresh_token": "rt-good-9"})
            self.assertEqual(store.account(me["id"])["refresh_token"], "rt-good-9")

            self.assertIn("邮箱账号", self.api_error("/api/mailboxes/me@outlook.com", method="DELETE")[1])
            self.assertIn("Duck 转 Outlook", self.api_error("/api/accounts/%d" % me["id"], method="DELETE")[1])
            store.execute("DELETE FROM mailboxes WHERE source_id = ?", (source,))
            self.api("/api/sources/%d" % source, method="DELETE")
            self.api("/api/accounts/%d" % me["id"], method="DELETE")
            self.assertNotIn("me@outlook.com", self.boxes())
            self.assertEqual(self.api_error("/api/accounts/%d" % me["id"], method="DELETE")[0], 404)
            self.assertEqual(self.api_error("/api/accounts/%d/nope" % bad["id"], method="POST")[0], 404)
        finally:
            fake.shutdown()
            fake.server_close()
            for table in ("accounts", "account_seen", "sources"):
                store.execute("DELETE FROM %s" % table)
            store.execute("DELETE FROM mailboxes WHERE account_id IS NOT NULL OR source_id IS NOT NULL")
            store.execute("DELETE FROM mails WHERE address LIKE '%@outlook.com' OR address LIKE '%@duck.com'")

    def test_requires_api_key(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.api("/api/domains", key="wrong")
        self.assertEqual(ctx.exception.code, 401)

    def test_create_mailbox(self):
        self.assertEqual([d["name"] for d in self.api("/api/domains")["domains"]][:2], ["a.test", "b.test"])
        box = self.new_box(domain="b.test")
        self.assertRegex(box["address"], r"^[a-z][a-z0-9]{9}@b\.test$")
        self.assertIn(self.new_box()["domain"], ["a.test", "b.test"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.new_box(domain="evil.test")
        self.assertEqual(ctx.exception.code, 400)

    def test_receive_and_query_code(self):
        address = self.new_box(domain="a.test")["address"]
        self.assertIsNone(self.code(address)["verify_code"])
        self.send(address, "验证码", "您的验证码是 135790，请勿泄露。")
        result = self.code(address)
        self.assertEqual(result["verify_code"], "135790")
        self.assertEqual(result["subject"], "验证码")
        self.assertIn("noreply@example.com", result["from"])
        self.send(address, "New code", "plain", html="<p>Your code is <b>864209</b></p>")
        self.assertEqual(self.code(address)["verify_code"], "864209")
        mails = self.api("/api/mails?address=" + address)["mails"]
        self.assertEqual([m["verify_code"] for m in mails], ["864209", "135790"])
        self.assertIn("<b>864209</b>", mails[0]["html"])
        self.assertEqual(mails[1]["html"], "")
        boxes = {b["address"]: b for b in self.api("/api/mailboxes")["mailboxes"]}
        self.assertEqual(boxes[address]["mail_count"], 2)
        self.assertGreaterEqual(self.api("/api/stats")["codes"], 2)

    def test_mark_mail_read(self):
        address = self.new_box(domain="a.test")["address"]
        self.send(address, "First", "code 100001")
        self.send(address, "Second", "code 100002")
        second, first = self.api("/api/mails?address=" + address)["mails"]
        self.assertEqual((first["read"], second["read"]), (False, False))
        self.assertEqual(self.api("/api/mails/%d/read" % first["id"], method="POST"), {"id": first["id"], "read": True})
        self.api("/api/mails/%d/read" % first["id"], method="POST")
        second, first = self.api("/api/mails?address=" + address)["mails"]
        self.assertEqual((first["read"], second["read"]), (True, False))
        self.assertEqual(self.code(address)["verify_code"], "100002")
        self.assertEqual(self.api_error("/api/mails/99999999/read", method="POST")[0], 404)

    def test_since_filters_old_codes(self):
        address = self.new_box(domain="a.test")["address"]
        self.send(address, "Code", "code 111222")
        future = time.time() + 5
        self.assertIsNone(self.code(address, since=future)["verify_code"])

    def test_long_poll_waits_for_mail(self):
        address = self.new_box(domain="a.test")["address"]
        threading.Timer(1.0, self.send, (address, "Login", "Your code: 5566")).start()
        started = time.time()
        result = self.code(address, wait=10)
        self.assertEqual(result["verify_code"], "5566")
        self.assertLess(time.time() - started, 5)

    def test_catch_all_and_unknown_domain(self):
        self.send("anything.goes@b.test", "Hi", "PIN 4821")
        self.assertEqual(self.code("Anything.Goes@b.test")["verify_code"], "4821")
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            self.send("someone@other.test", "Hi", "code 1234")

    def test_oversized_mail_rejected(self):
        with self.assertRaises(smtplib.SMTPResponseException) as ctx:
            self.send("big@a.test", "Big", "x" * 50000)
        self.assertEqual(ctx.exception.smtp_code, 552)

    def test_purge_hides_old_mail_without_deleting_it(self):
        store = self.app.store
        scenario = self.api("/api/scenarios", method="POST", body={"name": "保留", "key": "keep"})
        address = self.new_box(scenario="keep", domain="a.test")["address"]
        self.send(address, "Code", "code 998877")
        cfg = self.app.cfg
        cfg.resend_api_key, original_url = "re_test", cfg.resend_api_url
        cfg.resend_api_url = "http://127.0.0.1:1/emails"
        try:
            self.api_error("/api/send", method="POST", body={"from": "a@a.test", "to": "x@example.com", "subject": "old", "text": "t"})
        finally:
            cfg.resend_api_key, cfg.resend_api_url = "", original_url
        store.execute("UPDATE mails SET received_at = received_at - 7200 WHERE address = ?", (address,))
        store.execute("UPDATE mailboxes SET created_at = created_at - 7200 WHERE address = ?", (address,))
        store.execute("UPDATE sent_mails SET created_at = created_at - 7200 WHERE subject = 'old'")
        before = self.api("/api/stats")
        store.purge(3600)

        self.assertEqual(self.api("/api/mails?address=" + address)["mails"], [])
        self.assertIsNone(self.code(address)["verify_code"])
        self.assertNotIn(address, [b["address"] for b in self.api("/api/mailboxes")["mailboxes"]])
        self.assertNotIn("old", [m["subject"] for m in self.api("/api/sent")["mails"]])
        after = self.api("/api/stats")
        self.assertEqual((after["mails"], after["mailboxes"]), (before["mails"] - 1, before["mailboxes"] - 1))

        kept = store.query("SELECT code, deleted_at FROM mails WHERE address = ?", (address,))
        self.assertEqual(kept[0]["code"], "998877")
        self.assertIsNotNone(kept[0]["deleted_at"])
        self.assertIsNotNone(store.query("SELECT deleted_at FROM mailboxes WHERE address = ?", (address,))[0]["deleted_at"])
        self.assertIsNotNone(store.query("SELECT deleted_at FROM sent_mails WHERE subject = 'old'")[0]["deleted_at"])

        self.send(address, "Again", "code 112233")
        self.assertEqual(self.code(address)["verify_code"], "112233")
        revived = {b["address"]: b for b in self.api("/api/mailboxes")["mailboxes"]}[address]
        self.assertEqual((revived["scenario"], revived["mail_count"]), ("keep", 1))
        self.api("/api/scenarios/%d" % scenario["id"], method="DELETE")


if __name__ == "__main__":
    unittest.main()
