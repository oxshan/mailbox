"""External mailbox sources: services that hand out forwarding addresses (addy.io, DuckDuckGo Email Protection).

A source forwards everything to its collector address, which is an address on one of our own domains,
so the mail still arrives over our SMTP server.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

ADDY_FORMATS = ("random_characters", "uuid", "random_words", "random_male_name", "random_female_name", "random_noun")
DUCK_DOMAIN = "duck.com"
# Duck's endpoint asks requests without a browser User-Agent for a captcha.
_BROWSER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/146.0.0.0 Safari/537.36"
)


class SourceError(Exception):
    pass


def call(label, method, url, token, body=None, agent="mailbox/1.0"):
    """Call a source's JSON API. Returns the parsed reply, or None when it is empty."""
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": agent}
    if agent == "mailbox/1.0":
        headers["X-Requested-With"] = "XMLHttpRequest"
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=None if body is None else json.dumps(body).encode(), method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(raw)
            detail = detail.get("message") or detail.get("error")
        except (ValueError, AttributeError):
            # addy.io answers some refusals, such as a plan limit, in plain text.
            detail = raw[:200] if raw and not raw.startswith("<") else None
        if exc.code in (401, 403) and not detail:
            detail = "令牌无效或已过期"
        elif exc.code == 429 and not detail:
            detail = "请求过于频繁或额度已用完"
        raise SourceError("%s 返回 %d：%s" % (label, exc.code, detail or exc.reason))
    except OSError as exc:
        raise SourceError("无法连接 %s：%s" % (label, exc))
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise SourceError("%s 返回了无法解析的内容" % label)


class Addy:
    """addy.io, through its documented API (https://app.addy.io/docs/)."""

    label = "addy.io"
    capabilities = ("create", "toggle", "delete")

    def __init__(self, cfg, source):
        self.base = cfg.addy_api_url.rstrip("/")
        self.token, self.collector = source["token"], source["collector"]
        self.domain, self.format = source["alias_domain"], source["alias_format"]
        # A collector that is a connected mail account is read by logging in, so addy may keep forwarding
        # to whatever recipient it already has, as long as that mail ends up in the account.
        self.via_account = bool(source.get("via_account"))

    def call(self, method, path, body=None):
        return call(self.label, method, self.base + path, self.token, body)

    def recipients(self):
        return (self.call("GET", "/recipients") or {}).get("data", [])

    def recipient(self, rows=None):
        """The addy recipient that is our collector address, or None."""
        rows = self.recipients() if rows is None else rows
        return next((row for row in rows if str(row.get("email", "")).lower() == self.collector), None)

    def check(self):
        self.call("GET", "/api-token-details")
        rows = self.recipients()
        recipient = self.recipient(rows)
        if recipient is None and self.via_account:
            others = "、".join(str(row.get("email", "")) for row in rows) or "（没有）"
            message = (
                "令牌有效。收集地址 %s 是接入的邮箱账号，不需要是 addy 收件人：新别名转发到 addy 的默认收件人 %s，"
                "请确认这些邮件最终会进到 %s"
            )
            return {"status": "ok", "message": message % (self.collector, others, self.collector)}
        if recipient is None:
            try:
                recipient = (self.call("POST", "/recipients", {"email": self.collector}) or {}).get("data")
            except SourceError as exc:
                if "already exists" not in str(exc):
                    raise
                message = "令牌有效，但 %s 已是另一个 addy 账户的收件人，addy 不允许它再加到这个账户，请换一个收集地址"
                return {"status": "error", "message": message % self.collector}
        if not recipient:
            return {"status": "error", "message": "addy 没有返回收件人信息"}
        if not recipient.get("email_verified_at"):
            message = "令牌有效。收集地址 %s 已加为 addy 收件人但还没验证：验证邮件会出现在这个地址的收件箱里，点开其中的链接即可"
            return {"status": "pending", "message": message % self.collector}
        return {"status": "ok", "message": "令牌有效，收集地址 %s 已是验证过的 addy 收件人" % self.collector}

    def create(self, description):
        recipient = self.recipient()
        if not recipient and not self.via_account:
            raise SourceError("收集地址 %s 还不是 addy 的收件人，请先在来源页面检测该来源" % self.collector)
        domain = self.domain or (self.call("GET", "/domain-options") or {}).get("defaultAliasDomain")
        if not domain:
            raise SourceError("addy 没有返回默认别名域名，请在来源里填写别名域名")
        body = {"domain": domain, "description": description}
        if recipient:
            body["recipient_ids"] = [recipient["id"]]
        if self.format:
            body["format"] = self.format
        alias = (self.call("POST", "/aliases", body) or {}).get("data") or {}
        if not alias.get("email") or not alias.get("id"):
            raise SourceError("addy 返回的别名信息不完整")
        return {"address": str(alias["email"]).lower(), "remote_id": str(alias["id"])}

    def find(self, address):
        """The addy id of an alias that was not created through us, or None."""
        query = urllib.parse.urlencode({"filter[search]": address, "filter[deleted]": "with"})
        for row in (self.call("GET", "/aliases?" + query) or {}).get("data", []):
            if str(row.get("email", "")).lower() == address:
                return str(row["id"])
        return None

    def set_active(self, remote_id, active):
        if active:
            self.call("POST", "/active-aliases", {"id": remote_id})
        else:
            self.call("DELETE", "/active-aliases/" + urllib.parse.quote(remote_id, safe=""))

    def delete(self, remote_id):
        self.call("DELETE", "/aliases/" + urllib.parse.quote(remote_id, safe=""))


class Duck:
    """DuckDuckGo Email Protection, through the endpoint its own apps use. It has no documented API:
    addresses can only be generated, not listed, deactivated or deleted."""

    label = "Duck"
    capabilities = ("create",)

    def __init__(self, cfg, source):
        self.base = cfg.duck_api_url.rstrip("/")
        self.token = source["token"]

    def check(self):
        message = "Duck 没有不生成地址就能校验令牌的接口，令牌是否有效要到生成邮箱时才知道"
        return {"status": "unknown", "message": message}

    def create(self, description):
        reply = call(self.label, "POST", self.base + "/email/addresses", self.token, agent=_BROWSER_AGENT) or {}
        local = str(reply.get("address", "")).strip().lower()
        if not local:
            raise SourceError("Duck 没有返回地址")
        return {"address": "%s@%s" % (local, DUCK_DOMAIN), "remote_id": ""}


KINDS = {"addy": Addy, "duck": Duck}


def client(cfg, source):
    return KINDS[source["kind"]](cfg, source)
