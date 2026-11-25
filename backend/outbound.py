"""Outgoing mail through the Resend HTTPS API."""
import json
import urllib.error
import urllib.request


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
