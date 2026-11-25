"""HTTP API and static dashboard."""
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

import mxcheck
import sources
from extract import _EMAIL, compile_template, extract_with_template, html_to_text
from outbound import SendError, send_via_resend
from sources import SourceError
from store import DEFAULT_SCENARIO

MAX_FORWARDS = 20
# An external service (Resend, addy.io, Duck) refused or could not be reached. Not 502 or 504:
# a proxy such as Cloudflare replaces those responses with its own error page.
UPSTREAM_FAILED = 424
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")
# Resources with a third path segment: /api/mailboxes/<address>/code, /api/sources/<id>/check, ...
_NESTED = ("mailboxes", "mails", "sources")
_MAILBOXES = (
    "SELECT b.address, b.domain, b.created_at, b.active, b.source_id AS source,"
    " COALESCE(src.kind, 'domain') AS source_kind, src.name AS source_name,"
    " s.key AS scenario, s.name AS scenario_name,"
    " COUNT(m.id) AS mail_count, MAX(m.received_at) AS last_mail_at"
    " FROM mailboxes b LEFT JOIN scenarios s ON s.id = b.scenario_id"
    " LEFT JOIN sources src ON src.id = b.source_id"
    " LEFT JOIN mails m ON m.address = b.address AND m.deleted_at IS NULL"
    " WHERE b.deleted_at IS NULL"
)


def as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def mask(token):
    return "…" if len(token) <= 8 else "%s…%s" % (token[:4], token[-4:])


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app = None

    def log_message(self, *args):
        pass

    def send_json(self, status, payload):
        if status == 200:
            payload = {"code": 0, "msg": "ok", "data": payload}
        else:
            payload = {"code": status, "msg": payload["error"], "data": None}
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
        parts = [unquote(part) for part in url.path[5:].strip("/").split("/")]
        handler = getattr(self, "%s_%s" % (self.command.lower(), parts[0]), None) or getattr(
            self, "api_" + parts[0], None
        )
        if handler is None or len(parts) > (3 if parts[0] in _NESTED else 2):
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
        except SourceError as exc:
            self.send_json(UPSTREAM_FAILED, {"error": str(exc)})

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
                200,
                mxcheck.check_domain(name, self.app.instance_id, cfg.mx_check_port, own_ip=mxcheck.public_ip(cfg)),
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
        rows = store.query("SELECT name FROM domains WHERE id = ?", (domain_id,))
        if not rows:
            raise LookupError(item)
        for source in store.collectors().values():
            if source["collector"].endswith("@" + rows[0]["name"]):
                raise ValueError("来源「%s」的收集地址在这个域名下，请先修改或删除该来源" % source["name"])
        store.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
        store.execute("DELETE FROM scenario_domains WHERE domain_id = ?", (domain_id,))
        self.send_json(200, {"id": domain_id})

    def source_view(self, row):
        view = {key: value for key, value in row.items() if key != "token"}
        view["token_mask"] = mask(row["token"])
        view["enabled"] = bool(row["enabled"])
        view["capabilities"] = list(sources.KINDS[row["kind"]].capabilities)
        return view

    def get_sources(self, params, item=None, action=None):
        if item is not None:
            raise LookupError(item)
        rows = self.app.store.query(
            "SELECT s.*, (SELECT COUNT(*) FROM mailboxes b WHERE b.source_id = s.id AND b.deleted_at IS NULL)"
            " AS mailbox_count,"
            " (SELECT COUNT(*) FROM scenario_sources ss WHERE ss.source_id = s.id) AS scenario_count"
            " FROM sources s ORDER BY s.id"
        )
        self.send_json(200, {"sources": [self.source_view(row) for row in rows]})

    def source_fields(self, params, current):
        """Validated column values for a source, starting from its current row when it is being edited."""
        store = self.app.store
        fields = dict(current)
        for key in ("name", "collector", "alias_domain", "alias_format", "note"):
            if key in params:
                fields[key] = str(params[key]).strip()
        fields["collector"] = fields["collector"].lower()
        fields["alias_domain"] = fields["alias_domain"].lower().lstrip("@")
        if str(params.get("token", "")).strip():
            fields["token"] = str(params["token"]).strip()
        if "enabled" in params:
            fields["enabled"] = int(as_bool(params["enabled"]))
        if not fields["name"]:
            raise ValueError("来源名称不能为空")
        if not fields["token"]:
            raise ValueError("令牌不能为空")
        collector = fields["collector"]
        if len(collector) > 254 or not _EMAIL.fullmatch(collector):
            raise ValueError("收集地址格式不正确：%s" % collector)
        if collector.rpartition("@")[2] not in store.domain_names():
            raise ValueError("收集地址必须是已添加域名下的邮箱：%s" % collector)
        box = store.mailbox(collector)
        if box and box["source_id"] is not None:
            raise ValueError("收集地址不能是外部来源的邮箱：%s" % collector)
        if fields["kind"] != "addy":
            fields["alias_domain"] = fields["alias_format"] = ""
        if fields["alias_domain"] and not _DOMAIN.fullmatch(fields["alias_domain"]):
            raise ValueError("别名域名格式不正确：%s" % fields["alias_domain"])
        if fields["alias_format"] and fields["alias_format"] not in sources.ADDY_FORMATS:
            raise ValueError("别名格式只能是：%s" % "、".join(sources.ADDY_FORMATS))
        return fields

    def post_sources(self, params, item=None, action=None):
        store = self.app.store
        if item is not None:
            source = store.source(int(item)) if item.isdigit() else None
            if not source or action != "check":
                raise LookupError(item)
            return self.send_json(200, sources.client(self.app.cfg, source).check())
        kind = str(params.get("kind", "")).strip().lower()
        if kind not in sources.KINDS:
            raise ValueError("来源类型只能是：%s" % "、".join(sorted(sources.KINDS)))
        blank = {
            "kind": kind, "name": "", "token": "", "collector": "", "alias_domain": "", "alias_format": "",
            "enabled": 1, "note": "",
        }
        fields = self.source_fields(params, blank)
        try:
            new_id = store.insert(
                "INSERT INTO sources (kind, name, token, collector, alias_domain, alias_format, enabled, note,"
                " created_at) VALUES (:kind, :name, :token, :collector, :alias_domain, :alias_format, :enabled,"
                " :note, :created_at)",
                dict(fields, created_at=time.time()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("收集地址已被另一个来源使用：%s" % fields["collector"])
        self.send_json(200, {"id": new_id})

    def put_sources(self, params, item=None, action=None):
        store = self.app.store
        source = store.source(int(item)) if item and item.isdigit() and action is None else None
        if not source:
            raise LookupError(item)
        fields = self.source_fields(params, source)
        try:
            store.execute(
                "UPDATE sources SET name = :name, token = :token, collector = :collector,"
                " alias_domain = :alias_domain, alias_format = :alias_format, enabled = :enabled, note = :note"
                " WHERE id = :id",
                fields,
            )
        except sqlite3.IntegrityError:
            raise ValueError("收集地址已被另一个来源使用：%s" % fields["collector"])
        self.send_json(200, {"id": source["id"]})

    def delete_sources(self, params, item=None, action=None):
        store = self.app.store
        source = store.source(int(item)) if item and item.isdigit() and action is None else None
        if not source:
            raise LookupError(item)
        used = store.query("SELECT COUNT(*) AS n FROM mailboxes WHERE source_id = ?", (source["id"],))[0]["n"]
        if used:
            raise ValueError("该来源下还有 %d 个邮箱，请先删除这些邮箱" % used)
        store.execute("DELETE FROM sources WHERE id = ?", (source["id"],))
        store.execute("DELETE FROM scenario_sources WHERE source_id = ?", (source["id"],))
        self.send_json(200, {"id": source["id"]})

    def get_forwards(self, params, item=None):
        rows = self.app.store.query("SELECT * FROM forwards ORDER BY id")
        for row in rows:
            row["enabled"] = bool(row["enabled"])
        cfg = self.app.cfg
        self.send_json(
            200,
            {"configured": bool(cfg.resend_api_key), "hourly_limit": cfg.forward_hourly_limit, "forwards": rows},
        )

    def post_forwards(self, params, item=None):
        store = self.app.store
        if item:
            raise LookupError(item)
        address = str(params.get("address", "")).strip().lower()
        if len(address) > 254 or not _EMAIL.fullmatch(address):
            raise ValueError("邮箱地址格式不正确：%s" % address)
        if address.rpartition("@")[2] in store.domain_names():
            raise ValueError("不能转发到本系统自己的域名：%s" % address)
        if len(store.query("SELECT id FROM forwards")) >= MAX_FORWARDS:
            raise ValueError("转发邮箱最多 %d 个" % MAX_FORWARDS)
        try:
            new_id = store.insert(
                "INSERT INTO forwards (address, enabled, note, created_at) VALUES (?, ?, ?, ?)",
                (address, int(bool(params.get("enabled", True))), str(params.get("note", "")).strip(), time.time()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("转发邮箱已存在：%s" % address)
        self.send_json(200, {"id": new_id, "address": address})

    def put_forwards(self, params, item=None):
        store, forward_id = self.app.store, int(item or 0)
        rows = store.query("SELECT enabled, note FROM forwards WHERE id = ?", (forward_id,))
        if not rows:
            raise LookupError(item)
        enabled = int(bool(params.get("enabled", rows[0]["enabled"])))
        note = str(params.get("note", rows[0]["note"])).strip()
        store.execute("UPDATE forwards SET enabled = ?, note = ? WHERE id = ?", (enabled, note, forward_id))
        self.send_json(200, {"id": forward_id})

    def delete_forwards(self, params, item=None):
        store, forward_id = self.app.store, int(item or 0)
        if not store.execute("DELETE FROM forwards WHERE id = ?", (forward_id,)):
            raise LookupError(item)
        self.send_json(200, {"id": forward_id})

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
            return self.send_json(200, {"verify_code": extract_with_template(regex, "", str(params.get("sample", "")), "")})
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
        store = self.app.store
        rows = store.query(
            "SELECT s.*, t.name AS template_name,"
            " (SELECT COUNT(*) FROM mailboxes b WHERE b.scenario_id = s.id AND b.deleted_at IS NULL) AS mailbox_count"
            " FROM scenarios s LEFT JOIN templates t ON t.id = s.template_id ORDER BY s.id"
        )
        domains = store.query(
            "SELECT sd.scenario_id, d.id, d.name FROM scenario_domains sd JOIN domains d ON d.id = sd.domain_id"
            " ORDER BY d.id"
        )
        linked = store.query(
            "SELECT ss.scenario_id, s.id, s.name FROM scenario_sources ss JOIN sources s ON s.id = ss.source_id"
            " ORDER BY s.id"
        )
        for row in rows:
            for field, ids, links in (("domains", "domain_ids", domains), ("sources", "source_ids", linked)):
                mine = [link for link in links if link["scenario_id"] == row["id"]]
                row[ids] = [link["id"] for link in mine]
                row[field] = [link["name"] for link in mine]
        self.send_json(200, {"scenarios": rows})

    def linked_ids(self, params, field, table, missing):
        ids = params.get(field) or []
        if not isinstance(ids, list):
            raise ValueError("%s 必须是数组" % field)
        ids = sorted({int(value) for value in ids})
        known = {row["id"] for row in self.app.store.query("SELECT id FROM %s" % table)}
        if not known.issuperset(ids):
            raise ValueError(missing)
        return ids

    def scenario_fields(self, params):
        name = str(params.get("name", "")).strip()
        key = str(params.get("key", "")).strip().lower() or "s" + secrets.token_hex(4)
        template_id = params.get("template_id") or None
        if not name:
            raise ValueError("场景名称不能为空")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", key):
            raise ValueError("场景标识只能包含小写字母、数字、下划线和连字符，最长 32 位")
        domain_ids = self.linked_ids(params, "domain_ids", "domains", "关联的域名不存在")
        source_ids = self.linked_ids(params, "source_ids", "sources", "关联的来源不存在")
        if template_id is not None:
            template_id = int(template_id)
            if not self.app.store.query("SELECT id FROM templates WHERE id = ?", (template_id,)):
                raise ValueError("邮件模板不存在")
        return name, key, template_id, domain_ids, source_ids

    def link(self, scenario_id, domain_ids, source_ids):
        store = self.app.store
        for table, ids in (("scenario_domains", domain_ids), ("scenario_sources", source_ids)):
            store.execute("DELETE FROM %s WHERE scenario_id = ?" % table, (scenario_id,))
            for linked_id in ids:
                store.execute("INSERT INTO %s VALUES (?, ?)" % table, (scenario_id, linked_id))

    def post_scenarios(self, params, item=None):
        if item:
            raise LookupError(item)
        name, key, template_id, domain_ids, source_ids = self.scenario_fields(params)
        try:
            new_id = self.app.store.insert(
                "INSERT INTO scenarios (name, key, template_id, created_at) VALUES (?, ?, ?, ?)",
                (name, key, template_id, time.time()),
            )
        except sqlite3.IntegrityError:
            raise ValueError("场景标识已存在：%s" % key)
        self.link(new_id, domain_ids, source_ids)
        self.send_json(200, {"id": new_id, "key": key})

    def put_scenarios(self, params, item=None):
        store, scenario_id = self.app.store, int(item or 0)
        name, key, template_id, domain_ids, source_ids = self.scenario_fields(params)
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
        self.link(scenario_id, domain_ids, source_ids)
        store.reparse([scenario_id])
        self.send_json(200, {"id": scenario_id, "key": key})

    def delete_scenarios(self, params, item=None):
        store, scenario_id = self.app.store, int(item or 0)
        if scenario_id == store.default_scenario_id:
            raise ValueError("默认场景不能删除")
        if not store.execute("DELETE FROM scenarios WHERE id = ?", (scenario_id,)):
            raise LookupError(item)
        self.link(scenario_id, [], [])
        store.execute(
            "UPDATE mailboxes SET scenario_id = ? WHERE scenario_id = ?", (store.default_scenario_id, scenario_id)
        )
        store.reparse([store.default_scenario_id])
        self.send_json(200, {"id": scenario_id})

    def mailbox_rows(self, where="", args=(), limit=1):
        rows = self.app.store.query(
            _MAILBOXES + where + " GROUP BY b.address"
            " ORDER BY COALESCE(MAX(m.received_at), b.created_at) DESC LIMIT ?",
            tuple(args) + (limit,),
        )
        for row in rows:
            row["active"] = bool(row["active"])
        return rows

    def get_mailboxes(self, params, item=None, action=None):
        if item is None:
            return self.list_mailboxes(params)
        address = item.strip().lower()
        if action == "code":
            return self.send_code(address, params)
        if action == "mails":
            return self.send_mails(params, address)
        rows = self.mailbox_rows(" AND b.address = ?", (address,)) if action is None else []
        if not rows:
            raise LookupError(item)
        self.send_json(200, rows[0])

    def list_mailboxes(self, params):
        limit = min(max(int(params.get("limit", 100)), 1), 500)
        where, args = "", []
        scenario = str(params.get("scenario", "")).strip().lower()
        source = str(params.get("source", "")).strip().lower()
        active = str(params.get("active", "")).strip()
        if scenario:
            where, args = where + " AND s.key = ?", args + [scenario]
        if source == "domain":
            where += " AND b.source_id IS NULL"
        elif source:
            where, args = where + " AND b.source_id = ?", args + [int(source)]
        if active:
            where, args = where + " AND b.active = ?", args + [int(as_bool(active))]
        self.send_json(200, {"mailboxes": self.mailbox_rows(where, args, limit)})

    def pick_source(self, scenario_id, params):
        """The external source a new mailbox comes from, or None for one of our own domains.

        A scenario with linked sources is limited to them and picks among them (and its linked domains)
        when the caller names nothing. A scenario without any uses our own domains unless a source is named.
        """
        store = self.app.store
        linked = store.linked_sources(scenario_id)
        wanted, domain = str(params.get("source", "")).strip(), str(params.get("domain", "")).strip()
        if wanted and domain:
            raise ValueError("source 和 domain 不能同时指定")
        if wanted:
            source = store.source(int(wanted)) if wanted.isdigit() else None
            if not source:
                raise ValueError("来源不存在：%s" % wanted)
            if linked and source["id"] not in [row["id"] for row in linked]:
                raise ValueError("该场景不能使用来源：%s" % source["name"])
            if not source["enabled"]:
                raise ValueError("来源已停用：%s" % source["name"])
            return source
        if domain or not linked:
            return None
        options = [row for row in linked if row["enabled"]]
        own_domains = store.query("SELECT 1 FROM scenario_domains WHERE scenario_id = ? LIMIT 1", (scenario_id,))
        if own_domains and store.usable_domains(scenario_id):
            options.append(None)
        if not options:
            raise ValueError("该场景关联的来源都已停用")
        return secrets.choice(options)

    def post_mailboxes(self, params, item=None, action=None):
        if item is not None:
            raise LookupError(item)
        store = self.app.store
        key = str(params.get("scenario", "")).strip().lower() or DEFAULT_SCENARIO
        rows = store.query("SELECT id, key, name FROM scenarios WHERE key = ?", (key,))
        if not rows:
            raise ValueError("业务场景不存在：%s" % key)
        scenario = rows[0]
        source = self.pick_source(scenario["id"], params)
        if source:
            alias = sources.client(self.app.cfg, source).create("mailbox：%s" % scenario["name"])
            address = alias["address"]
            if not store.add_mailbox(address, scenario["id"], source["id"], alias["remote_id"]):
                store.execute(
                    "UPDATE mailboxes SET scenario_id = ?, source_id = ?, remote_id = ?, deleted_at = NULL"
                    " WHERE address = ?",
                    (scenario["id"], source["id"], alias["remote_id"], address),
                )
        else:
            usable = store.usable_domains(scenario["id"])
            if not usable:
                raise ValueError("该场景没有可用的域名，请先在域名管理中添加或启用域名")
            domain = str(params.get("domain", "")).strip().lower() or secrets.choice(usable)
            if domain not in usable:
                raise ValueError("该场景不能使用域名：%s" % domain)
            address = store.create_local_mailbox(domain, scenario["id"])
        self.send_json(200, self.mailbox_rows(" AND b.address = ?", (address,))[0])

    def owned_mailbox(self, item, action):
        """The mailbox a request names, with the client of its source when that source is external."""
        store = self.app.store
        box = store.mailbox((item or "").strip().lower()) if action is None else None
        if not box:
            raise LookupError(item)
        source = store.source(box["source_id"]) if box["source_id"] is not None else None
        return box, (sources.client(self.app.cfg, source) if source else None)

    def remote_id(self, box, client):
        """The id of an alias at its source, looked up there when the alias was not made through us."""
        if box["remote_id"]:
            return box["remote_id"]
        found = client.find(box["address"])
        if found:
            self.app.store.execute("UPDATE mailboxes SET remote_id = ? WHERE address = ?", (found, box["address"]))
        return found

    def put_mailboxes(self, params, item=None, action=None):
        box, client = self.owned_mailbox(item, action)
        if "active" not in params:
            raise ValueError("active is required")
        active = as_bool(params["active"])
        synced = False
        if client and "toggle" in client.capabilities:
            remote_id = self.remote_id(box, client)
            if not remote_id:
                raise SourceError("%s 上找不到别名 %s" % (client.label, box["address"]))
            client.set_active(remote_id, active)
            synced = True
        self.app.store.execute("UPDATE mailboxes SET active = ? WHERE address = ?", (int(active), box["address"]))
        self.send_json(200, {"address": box["address"], "active": active, "synced": synced})

    def delete_mailboxes(self, params, item=None, action=None):
        box, client = self.owned_mailbox(item, action)
        synced = False
        if client and "delete" in client.capabilities:
            remote_id = self.remote_id(box, client)
            if remote_id:
                client.delete(remote_id)
                synced = True
        self.app.store.remove_mailbox(box["address"])
        self.send_json(200, {"address": box["address"], "synced": synced})

    def send_code(self, address, params):
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
                "verify_code": row.get("code"),
                "from": row.get("sender"),
                "subject": row.get("subject"),
                "received_at": row.get("received_at"),
            },
        )

    def send_mails(self, params, address=""):
        limit = min(max(int(params.get("limit", 50)), 1), 200)
        sql = (
            "SELECT id, address, sender AS 'from', subject, body, headers, code AS verify_code, received_at,"
            " read_at IS NOT NULL AS read"
            " FROM mails WHERE deleted_at IS NULL"
        )
        if address:
            rows = self.app.store.query(sql + " AND address = ? ORDER BY id DESC LIMIT ?", (address, limit))
        else:
            rows = self.app.store.query(sql + " ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["read"] = bool(row["read"])
        self.send_json(200, {"mails": rows})

    def get_mails(self, params, item=None, action=None):
        if item is not None:
            raise LookupError(item)
        self.send_mails(params, str(params.get("address", "")).strip().lower())

    def post_mails(self, params, item=None, action=None):
        store, mail_id = self.app.store, int(item) if item and item.isdigit() and action == "read" else 0
        if not store.query("SELECT id FROM mails WHERE id = ? AND deleted_at IS NULL", (mail_id,)):
            raise LookupError(item)
        store.execute("UPDATE mails SET read_at = ? WHERE id = ? AND read_at IS NULL", (time.time(), mail_id))
        self.send_json(200, {"id": mail_id, "read": True})

    def get_sent(self, params, item=None):
        limit = min(max(int(params.get("limit", 50)), 1), 200)
        rows = self.app.store.query(
            "SELECT id, sender AS 'from', recipients AS 'to', subject, body, status, error, kind, created_at"
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
            return self.send_json(UPSTREAM_FAILED, {"error": str(exc)})
        new_id = store.insert(sql, row + ("sent", "", provider_id, time.time()))
        self.send_json(200, {"id": new_id, "provider_id": provider_id})

    def api_stats(self, params, item=None):
        store, day_ago = self.app.store, time.time() - 86400
        row = store.query(
            "SELECT (SELECT COUNT(*) FROM mailboxes WHERE deleted_at IS NULL) AS mailboxes,"
            " (SELECT COUNT(*) FROM mails WHERE deleted_at IS NULL) AS mails,"
            " (SELECT COUNT(*) FROM mails WHERE received_at >= ? AND deleted_at IS NULL) AS mails_24h,"
            " (SELECT COUNT(*) FROM mails WHERE code IS NOT NULL AND deleted_at IS NULL) AS codes,"
            " (SELECT COUNT(*) FROM sources) AS sources",
            (day_ago,),
        )[0]
        row["domains"] = len(store.domain_names())
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
