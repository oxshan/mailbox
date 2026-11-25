"""Check that a domain's MX record leads to this server."""
import secrets
import socket
import struct
import urllib.request


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
