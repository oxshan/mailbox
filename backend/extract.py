"""Mail parsing and verification code extraction."""
import html
import re
from email import message_from_bytes, policy

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


_BLOCK_OPEN = re.compile(r"<(script|style|head)\b", re.I)


def _drop_blocks(markup):
    """Remove <script>, <style> and <head> elements with their content.

    Done by scanning rather than with one backtracking regex: mail is untrusted, and many unclosed opening
    tags would make such a regex rescan the rest of the message for each one.
    """
    lower, parts, pos, unclosed = markup.lower(), [], 0, set()
    while True:
        match = _BLOCK_OPEN.search(markup, pos)
        if not match:
            break
        name = match.group(1).lower()
        end = -1 if name in unclosed else lower.find("</" + name, match.end())
        if end < 0:
            # Nothing closes it from here on, and nothing will for a later tag of the same name either.
            unclosed.add(name)
            parts.append(markup[pos : match.end()])
            pos = match.end()
            continue
        parts.append(markup[pos : match.start()] + " ")
        close = markup.find(">", end)
        pos = len(markup) if close < 0 else close + 1
    parts.append(markup[pos:])
    return "".join(parts)


def html_to_text(markup):
    """The readable text of an HTML body. Every step is linear in the length of the mail."""
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h\d|li)>", "\n", _drop_blocks(markup))
    # [^<>] instead of [^>]: a "<" never ends a tag, so a run of stray "<" cannot make each one rescan to the end.
    text = html.unescape(re.sub(r"<[^<>]*>", " ", markup))
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_RECIPIENT_HEADERS = (
    "to", "cc", "delivered-to", "x-original-to", "original-recipient", "envelope-to", "x-envelope-to",
    "x-forwarded-to", "x-forwarded-for", "resent-to",
)
MAX_HEADER_CHARS = 8000


def _addresses(values):
    found = []
    for value in values:
        for address in _EMAIL.findall(value):
            address = address.lower()
            if address not in found:
                found.append(address)
    return found


def describe_headers(headers):
    """The header-derived fields of a mail, from its (name, value) header list.

    "recipients" lists the addresses in the recipient headers and "mentions" those in the X- headers; a
    forwarding service keeps the alias a mail was sent to in one of them.
    """
    first = lambda wanted: next((value for name, value in headers if name.lower() == wanted), "")
    return {
        "sender": first("from"),
        "subject": first("subject"),
        "date": first("date"),
        "message_id": first("message-id").strip(),
        "headers": "\n".join("%s: %s" % header for header in headers)[:MAX_HEADER_CHARS],
        "recipients": _addresses(
            value for wanted in _RECIPIENT_HEADERS for name, value in headers if name.lower() == wanted
        ),
        "mentions": _addresses(value for name, value in headers if name.lower().startswith("x-")),
    }


def parse_mail(raw):
    """Parse a raw RFC822 message into the fields of describe_headers plus its content."""
    msg = message_from_bytes(raw, policy=policy.default)
    try:
        headers = [(name, str(value)) for name, value in msg.items()]
    except Exception:
        headers = []
    mail = describe_headers(headers)
    mail.update(plain="", rich="", markup="", attachments=0)
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            mail["plain"] = part.get_content().strip()
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            mail["markup"] = part.get_content()
            mail["rich"] = html_to_text(mail["markup"])
        mail["attachments"] = sum(1 for _ in msg.iter_attachments())
    except Exception:
        mail["plain"] = raw.decode("utf-8", "replace")
    return mail
