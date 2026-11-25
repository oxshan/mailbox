"""External mail accounts we log in to and read, instead of receiving their mail over SMTP.

Only Outlook / Hotmail for now: a refresh token is exchanged for an access token, then the inbox and the
junk folder are read through Microsoft Graph, or over IMAP when the token was not granted for Graph.
"""
import base64
import calendar
import imaplib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

from extract import describe_headers, parse_mail

KINDS = ("outlook",)
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
AUTHORIZE_SCOPE = "offline_access openid email profile https://graph.microsoft.com/Mail.Read"
GRAPH_FOLDERS = ("inbox", "junkemail")
IMAP_FOLDERS = ("INBOX", "Junk")
FETCH_PER_FOLDER = 20


class AccountError(Exception):
    pass


def _request(label, request):
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw)
            error = detail.get("error")
            detail = detail.get("error_description") or (error.get("message") if isinstance(error, dict) else error)
        except (ValueError, AttributeError):
            detail = None
        raise AccountError("%s 返回 %d：%s" % (label, exc.code, str(detail or exc.reason).splitlines()[0][:200]))
    except (OSError, ValueError) as exc:
        raise AccountError("无法连接 %s：%s" % (label, exc))


def access_token(url, account, scope):
    """Exchange the refresh token. Returns (access token, refresh token to keep from now on)."""
    form = {
        "client_id": account["client_id"],
        "grant_type": "refresh_token",
        "refresh_token": account["refresh_token"],
        "scope": scope,
    }
    request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
    reply = _request("微软登录", request)
    if not reply.get("access_token"):
        raise AccountError("微软登录没有返回访问令牌")
    return reply["access_token"], reply.get("refresh_token") or account["refresh_token"]


def _form(url, form):
    """POST a form to Microsoft's sign-in service. Returns its JSON reply, also when it reports an error."""
    request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            reply = json.loads(exc.read())
        except ValueError:
            reply = None
        if not isinstance(reply, dict) or not reply.get("error"):
            raise AccountError("微软登录返回 %d：%s" % (exc.code, exc.reason))
        return reply
    except (OSError, ValueError) as exc:
        raise AccountError("无法连接微软登录：%s" % exc)


def _refused(reply):
    return AccountError("微软登录：%s" % str(reply.get("error_description") or reply["error"]).splitlines()[0][:200])


def start_authorization(cfg):
    """Ask Microsoft for a device code: the user enters its user_code on verification_uri to sign in."""
    reply = _form(cfg.ms_device_url, {"client_id": cfg.ms_client_id, "scope": AUTHORIZE_SCOPE})
    if not reply.get("device_code"):
        raise _refused(reply) if reply.get("error") else AccountError("微软登录没有返回设备码")
    return reply


def finish_authorization(cfg, device_code):
    """Check whether the user has signed in. Returns None while waiting, else (address, refresh token)."""
    form = {
        "client_id": cfg.ms_client_id,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
    }
    reply = _form(cfg.ms_token_url, form)
    if reply.get("error") in ("authorization_pending", "slow_down"):
        return None
    if reply.get("error"):
        raise _refused(reply)
    if not reply.get("refresh_token"):
        raise AccountError("微软登录没有返回 refresh_token")
    claims = {}
    try:
        payload = reply["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (KeyError, IndexError, ValueError):
        pass
    address = str(claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if "@" not in address:
        raise AccountError("授权成功，但微软没有返回邮箱地址")
    return address, reply["refresh_token"]


def _graph_time(value):
    try:
        return float(calendar.timegm(time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return time.time()


def _graph_address(entry):
    box = (entry or {}).get("emailAddress") or {}
    name, address = box.get("name") or "", box.get("address") or ""
    return "%s <%s>" % (name, address) if name and name != address else address


def fetch_graph(cfg, account):
    token, refresh_token = access_token(cfg.ms_token_url, account, GRAPH_SCOPE)
    query = urllib.parse.urlencode(
        {
            "$top": FETCH_PER_FOLDER,
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,internetMessageHeaders",
        }
    )
    messages = []
    for folder in GRAPH_FOLDERS:
        request = urllib.request.Request(
            "%s/me/mailFolders/%s/messages?%s" % (cfg.ms_graph_url.rstrip("/"), folder, query),
            headers={"Authorization": "Bearer " + token, "Prefer": 'outlook.body-content-type="text"'},
        )
        for item in _request("Microsoft Graph", request).get("value", []):
            headers = [(h.get("name", ""), h.get("value", "")) for h in item.get("internetMessageHeaders") or []]
            if not headers:
                headers = [("From", _graph_address(item.get("from"))), ("Subject", item.get("subject") or "")]
                headers += [("To", _graph_address(entry)) for entry in item.get("toRecipients") or []]
                headers += [("Cc", _graph_address(entry)) for entry in item.get("ccRecipients") or []]
            mail = describe_headers(headers)
            mail.update(plain=((item.get("body") or {}).get("content") or "").strip(), rich="", markup="", attachments=0)
            messages.append(
                {"remote_id": str(item.get("id", "")), "received_at": _graph_time(item.get("receivedDateTime")), "mail": mail}
            )
    return messages, refresh_token


def fetch_imap(cfg, account):
    token, refresh_token = access_token(cfg.ms_imap_token_url, account, IMAP_SCOPE)
    login = "user=%s\x01auth=Bearer %s\x01\x01" % (account["address"], token)
    messages = []
    try:
        connection = imaplib.IMAP4_SSL(cfg.outlook_imap_host, timeout=20)
        try:
            connection.authenticate("XOAUTH2", lambda _: login.encode())
            for folder in IMAP_FOLDERS:
                if connection.select('"%s"' % folder, readonly=True)[0] != "OK":
                    continue
                numbers = connection.search(None, "ALL")[1][0].split()[-FETCH_PER_FOLDER:]
                for number in numbers:
                    parts = connection.fetch(number, "(RFC822)")[1]
                    raw = next((part[1] for part in parts if isinstance(part, tuple)), None)
                    if not raw:
                        continue
                    mail = parse_mail(raw)
                    try:
                        received_at = parsedate_to_datetime(mail["date"]).timestamp()
                    except (TypeError, ValueError):
                        received_at = time.time()
                    remote_id = mail["message_id"] or "%s/%s" % (folder, number.decode())
                    messages.append({"remote_id": remote_id, "received_at": received_at, "mail": mail})
        finally:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    except (imaplib.IMAP4.error, OSError) as exc:
        raise AccountError("Outlook IMAP 失败：%s" % exc)
    return messages, refresh_token


def fetch(cfg, account):
    """Read an account's recent mail. Returns (messages, refresh token to keep, channel used)."""
    try:
        return fetch_graph(cfg, account) + ("graph",)
    except AccountError as graph_error:
        try:
            return fetch_imap(cfg, account) + ("imap",)
        except AccountError as imap_error:
            raise AccountError("%s；改用 IMAP 也失败：%s" % (graph_error, imap_error))
