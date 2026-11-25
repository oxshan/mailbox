"""Whether a mail at a collector address really came through the source's forwarding service.

Anyone can send mail to a collector address and write any alias into its To header. Only a mail the
forwarder itself handed on is filed under the alias it names; any other stays under the collector.
"""
import re
import socket

# The domains each service sends its forwarded mail from.
FORWARDER_DOMAINS = {"addy": ("anonaddy.me", "anonaddy.com", "addy.io"), "duck": ("duck.com", "duckduckgo.com")}
# Mail servers whose Authentication-Results header we take as true: the ones holding the connected accounts.
# They put their header on top, above anything the sender wrote.
TRUSTED_RECEIVERS = ("microsoft.com",)


def _within(domain, suffixes):
    domain = domain.lower().rstrip(".")
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in suffixes)


def forwarder_domains(source):
    extra = (source["alias_domain"],) if source.get("alias_domain") else ()
    return FORWARDER_DOMAINS.get(source["kind"], ()) + extra


def confirmed_host(ip):
    """The host name of an IP address when that name also resolves back to it, else ''."""
    try:
        host = socket.gethostbyaddr(ip)[0]
        if ip in {info[4][0] for info in socket.getaddrinfo(host, None)}:
            return host
    except (OSError, UnicodeError):
        pass
    return ""


def _domain_of(value):
    return value.rpartition("@")[2].strip("<>\"'").lower()


def receiver_verdict(headers):
    """The domains the top Authentication-Results header of a trusted receiver saw pass SPF or DKIM."""
    first = next((line for line in headers.split("\n") if line.lower().startswith("authentication-results:")), "")
    clauses = first.partition(":")[2].split(";")
    if not clauses or not _within(clauses[0].split()[0] if clauses[0].split() else "", TRUSTED_RECEIVERS):
        return set()
    passed = set()
    for clause in clauses[1:]:
        method = re.match(r"\s*(spf|dkim)\s*=\s*pass\b", clause, re.I)
        field = re.search(r"\b(smtp\.mailfrom|header\.d)\s*=\s*([^\s;]+)", clause, re.I)
        if method and field and (method.group(1).lower() == "spf") == (field.group(1).lower() == "smtp.mailfrom"):
            passed.add(_domain_of(field.group(2)))
    return passed


def from_forwarder(source, mail, peer):
    """Whether a mail for a source's collector was handed on by the source's service.

    `peer` is the IP address of the server that delivered it over SMTP to us. It is None for mail read
    from a connected account, which is judged by the receiving server's authentication results instead.
    """
    domains = forwarder_domains(source)
    if peer is None:
        return any(_within(domain, domains) for domain in receiver_verdict(mail["headers"]))
    host = confirmed_host(peer) if peer else ""
    return bool(host) and _within(host, domains)
