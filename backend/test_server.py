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

import server
from server import (
    Config,
    MailApp,
    Store,
    compile_template,
    extract_code,
    extract_with_template,
    html_to_text,
    parse_mx_response,
)


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


class MigrationTest(unittest.TestCase):
    def test_old_database_is_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mail.db")
            old = sqlite3.connect(path)
            old.executescript(
                """
                CREATE TABLE mailboxes (address TEXT PRIMARY KEY, domain TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE mails (id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL, sender TEXT,
                    subject TEXT, body TEXT, code TEXT, received_at REAL NOT NULL);
                INSERT INTO mailboxes VALUES ('old@a.test', 'a.test', 1);
                INSERT INTO mails (address, sender, subject, body, code, received_at)
                    VALUES ('old@a.test', 'x', 's', 'code 1234', '1234', 2);
                """
            )
            old.commit()
            old.close()
            store = Store(path)
            box = store.query("SELECT * FROM mailboxes")[0]
            self.assertEqual(box["scenario_id"], store.default_scenario_id)
            self.assertEqual(store.query("SELECT alt, code FROM mails")[0], {"alt": "", "code": "1234"})
            self.assertEqual(Store(path).default_scenario_id, store.default_scenario_id)

    def test_single_domain_scenarios_become_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mail.db")
            store = Store(path, ["a.test", "b.test"])
            store.execute("INSERT INTO scenarios (key, name, domain, created_at) VALUES ('old', 'Old', 'b.test', 1)")
            store.db.close()
            store = Store(path, ["ignored.test"])
            self.assertEqual(store.domain_names(), {"a.test", "b.test"})
            old_id = store.query("SELECT id, domain FROM scenarios WHERE key = 'old'")[0]
            self.assertEqual(old_id["domain"], "")
            self.assertEqual(store.usable_domains(old_id["id"]), ["b.test"])
            self.assertEqual(sorted(store.usable_domains(store.default_scenario_id)), ["a.test", "b.test"])


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
            return json.loads(response.read())

    def api_error(self, *args, **kwargs):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.api(*args, **kwargs)
        return ctx.exception.code, json.loads(ctx.exception.read())["error"]

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
        box = self.api("/api/mailbox?scenario=signup", method="POST")
        self.assertTrue(box["address"].endswith("@b.test"))
        self.assertEqual(box["scenario"], "signup")
        self.send(box["address"], "Welcome", "Order 123456 confirmed.\nYour access token:  AB12-CD34\nBye")
        self.assertEqual(self.api("/api/code?address=" + box["address"])["code"], "AB12-CD34")
        self.send(box["address"], "Other", "Your verification code is 998877")
        self.assertEqual(self.api("/api/code?address=" + box["address"])["code"], "AB12-CD34")

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
        self.assertEqual(self.api("/api/code?address=" + box["address"])["code"], "998877")

        self.api("/api/scenarios/%d" % scenario["id"], method="DELETE")
        moved = {b["address"]: b for b in self.api("/api/mailboxes")["mailboxes"]}[box["address"]]
        self.assertEqual(moved["scenario"], "default")
        codes = [m["code"] for m in self.api("/api/mails?address=" + box["address"])["mails"]]
        self.assertEqual(codes, ["998877", "123456"])
        self.api("/api/templates/%d" % template["id"], method="DELETE")
        self.assertEqual(self.api("/api/templates")["templates"], [])

    def test_template_and_scenario_validation(self):
        self.assertEqual(self.api_error("/api/mailbox?scenario=nope")[0], 400)
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
        seen = {self.api("/api/mailbox?scenario=multi")["domain"] for _ in range(30)}
        self.assertEqual(seen, {"c.test", "d.test"})
        self.assertEqual(self.api_error("/api/mailbox?scenario=multi&domain=a.test")[0], 400)

        self.api("/api/domains/%d" % d_id, method="PUT", body={"enabled": False})
        seen = {self.api("/api/mailbox?scenario=multi")["domain"] for _ in range(15)}
        self.assertEqual(seen, {"c.test"})
        self.assertNotIn("d.test", {self.api("/api/mailbox")["domain"] for _ in range(30)})
        self.send("still@d.test", "Hi", "code 4455")
        self.assertEqual(self.api("/api/code?address=still@d.test")["code"], "4455")
        listed = {d["name"]: d for d in self.api("/api/domains")["domains"]}
        self.assertEqual((listed["d.test"]["enabled"], listed["c.test"]["note"]), (False, "备用"))
        self.assertEqual(listed["c.test"]["scenario_count"], 1)

        self.api("/api/domains/%d" % c_id, method="PUT", body={"enabled": False})
        self.assertIn("没有可用的域名", self.api_error("/api/mailbox?scenario=multi")[1])

        self.api("/api/domains/%d" % c_id, method="DELETE")
        self.api("/api/domains/%d" % d_id, method="DELETE")
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            self.send("x@c.test", "Hi", "code 1234")
        scenarios = {s["key"]: s for s in self.api("/api/scenarios")["scenarios"]}
        self.assertEqual(scenarios["multi"]["domains"], [])
        self.api("/api/scenarios/%d" % scenario["id"], method="DELETE")
        self.assertEqual(self.api_error("/api/domains/%d" % c_id, method="DELETE")[0], 404)

    def test_domain_check(self):
        original, original_ip = server.resolve_mx, server.public_ip
        check = lambda: self.api("/api/domains/check", method="POST", body={"name": "a.test"})
        self.app.cfg.mx_check_port = self.app.smtp_port
        try:
            server.public_ip = lambda cfg: None
            server.resolve_mx = lambda name: [(10, "127.0.0.1")]
            self.assertEqual(check()["status"], "ok")
            server.resolve_mx = lambda name: []
            self.assertEqual(check()["status"], "missing")
            server.resolve_mx = lambda name: [(10, "127.0.0.1")]
            self.app.cfg.mx_check_port = self.app.http_port
            result = check()
            self.assertIn(result["status"], ("mismatch", "unverified"))
            self.assertEqual(result["mx"], ["10 127.0.0.1"])

            self.app.cfg.mx_check_port = 1
            self.assertEqual(check()["status"], "unverified")
            server.public_ip = lambda cfg: "127.0.0.1"
            result = check()
            self.assertEqual(result["status"], "ok")
            self.assertIn("公网 IP", result["message"])
            server.public_ip = lambda cfg: "203.0.113.9"
            self.assertEqual(check()["status"], "mismatch")
            server.resolve_mx = lambda name: [(10, "no-such-host.invalid")]
            with mock.patch.object(server.socket, "getaddrinfo", side_effect=OSError("unknown host")):
                self.assertEqual(server.check_domain("a.test", "x", own_ip="203.0.113.9")["status"], "error")
        finally:
            server.resolve_mx, server.public_ip = original, original_ip
        self.assertEqual(self.api_error("/api/domains/check", method="POST", body={"name": "nope.test"})[0], 404)

    def test_send_mail(self):
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

        mail = {"from": "Hello@A.test", "from_name": "客服", "to": "x@example.com, y@example.com", "subject": "Hi", "text": "body"}
        self.assertFalse(self.api("/api/sent")["configured"])
        self.assertIn("RESEND_API_KEY", self.api_error("/api/send", method="POST", body=mail)[1])

        fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeResend)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        cfg = self.app.cfg
        cfg.resend_api_key, cfg.resend_api_url = "re_test", "http://127.0.0.1:%d/emails" % fake.server_address[1]
        try:
            self.assertEqual(self.api("/api/send", method="POST", body=mail)["provider_id"], "re_123")
            auth, agent, sent = calls[-1]
            self.assertEqual((auth, agent), ("Bearer re_test", "mailbox/1.0"))
            self.assertEqual(sent["from"], '"客服" <hello@a.test>')
            self.assertEqual(sent["to"], ["x@example.com", "y@example.com"])
            self.assertEqual((sent["subject"], sent["text"]), ("Hi", "body"))

            code, message = self.api_error("/api/send", method="POST", body=dict(mail, subject="reject"))
            self.assertEqual(code, 502)
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
            self.assertEqual(self.api_error("/api/send", method="POST", body=mail)[0], 502)
        finally:
            cfg.resend_api_key = ""
            fake.shutdown()
            fake.server_close()

    def test_template_test_endpoint(self):
        body = {"mode": "placeholder", "pattern": "验证码：{code}，{*}有效", "sample": "您的验证码：X9K2P，10 分钟内有效"}
        self.assertEqual(self.api("/api/templates/test", method="POST", body=body)["code"], "X9K2P")
        body["sample"] = "没有验证码"
        self.assertIsNone(self.api("/api/templates/test", method="POST", body=body)["code"])

    def send(self, to, subject, body, html=None):
        msg = EmailMessage()
        msg["From"] = "Example <noreply@example.com>"
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP("127.0.0.1", self.app.smtp_port, timeout=10) as client:
            client.send_message(msg)

    def test_requires_api_key(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.api("/api/domains", key="wrong")
        self.assertEqual(ctx.exception.code, 401)

    def test_create_mailbox(self):
        self.assertEqual([d["name"] for d in self.api("/api/domains")["domains"]][:2], ["a.test", "b.test"])
        box = self.api("/api/mailbox?domain=b.test", method="POST")
        self.assertRegex(box["address"], r"^[a-z][a-z0-9]{9}@b\.test$")
        self.assertIn(self.api("/api/mailbox")["domain"], ["a.test", "b.test"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.api("/api/mailbox?domain=evil.test")
        self.assertEqual(ctx.exception.code, 400)

    def test_receive_and_query_code(self):
        address = self.api("/api/mailbox?domain=a.test")["address"]
        self.assertIsNone(self.api("/api/code?address=" + address)["code"])
        self.send(address, "验证码", "您的验证码是 135790，请勿泄露。")
        result = self.api("/api/code?address=" + address)
        self.assertEqual(result["code"], "135790")
        self.assertEqual(result["subject"], "验证码")
        self.assertIn("noreply@example.com", result["from"])
        self.send(address, "New code", "plain", html="<p>Your code is <b>864209</b></p>")
        self.assertEqual(self.api("/api/code?address=" + address)["code"], "864209")
        mails = self.api("/api/mails?address=" + address)["mails"]
        self.assertEqual([m["code"] for m in mails], ["864209", "135790"])
        boxes = {b["address"]: b for b in self.api("/api/mailboxes")["mailboxes"]}
        self.assertEqual(boxes[address]["mail_count"], 2)
        self.assertGreaterEqual(self.api("/api/stats")["codes"], 2)

    def test_since_filters_old_codes(self):
        address = self.api("/api/mailbox?domain=a.test")["address"]
        self.send(address, "Code", "code 111222")
        future = time.time() + 5
        self.assertIsNone(self.api("/api/code?address=%s&since=%s" % (address, future))["code"])

    def test_long_poll_waits_for_mail(self):
        address = self.api("/api/mailbox?domain=a.test")["address"]
        threading.Timer(1.0, self.send, (address, "Login", "Your code: 5566")).start()
        started = time.time()
        result = self.api("/api/code?address=%s&wait=10" % address)
        self.assertEqual(result["code"], "5566")
        self.assertLess(time.time() - started, 5)

    def test_catch_all_and_unknown_domain(self):
        self.send("anything.goes@b.test", "Hi", "PIN 4821")
        self.assertEqual(self.api("/api/code?address=Anything.Goes@b.test")["code"], "4821")
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            self.send("someone@other.test", "Hi", "code 1234")

    def test_oversized_mail_rejected(self):
        with self.assertRaises(smtplib.SMTPResponseException) as ctx:
            self.send("big@a.test", "Big", "x" * 50000)
        self.assertEqual(ctx.exception.smtp_code, 552)

    def test_purge_hides_old_mail_without_deleting_it(self):
        store = self.app.store
        scenario = self.api("/api/scenarios", method="POST", body={"name": "保留", "key": "keep"})
        address = self.api("/api/mailbox?scenario=keep&domain=a.test")["address"]
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
        self.assertIsNone(self.api("/api/code?address=" + address)["code"])
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
        self.assertEqual(self.api("/api/code?address=" + address)["code"], "112233")
        revived = {b["address"]: b for b in self.api("/api/mailboxes")["mailboxes"]}[address]
        self.assertEqual((revived["scenario"], revived["mail_count"]), ("keep", 1))
        self.api("/api/scenarios/%d" % scenario["id"], method="DELETE")


if __name__ == "__main__":
    unittest.main()
