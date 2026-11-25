"""Minimal SMTP receiver."""
import asyncio
import re
import socket

_ADDRESS = re.compile(r"<([^<>]*)>")


class SmtpSession:
    TIMEOUT = 60
    MAX_RECIPIENTS = 20

    def __init__(self, app, reader, writer):
        self.app, self.reader, self.writer = app, reader, writer

    async def reply(self, line):
        self.writer.write(line.encode() + b"\r\n")
        await self.writer.drain()

    async def readline(self):
        return await asyncio.wait_for(self.reader.readline(), self.TIMEOUT)

    async def run(self):
        cfg = self.app.cfg
        host = socket.gethostname() or "localhost"
        sender, recipients = "", []
        await self.reply("220 %s ESMTP mailbox-%s" % (host, self.app.instance_id))
        while True:
            line = await self.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            verb, _, arg = text.partition(" ")
            verb = verb.upper()
            if verb == "EHLO":
                await self.reply("250-%s\r\n250-SIZE %d\r\n250 8BITMIME" % (host, cfg.max_mail_bytes))
            elif verb == "HELO":
                await self.reply("250 %s" % host)
            elif verb == "MAIL":
                match = _ADDRESS.search(arg)
                sender, recipients = (match.group(1) if match else ""), []
                await self.reply("250 OK")
            elif verb == "RCPT":
                match = _ADDRESS.search(arg)
                address = (match.group(1) if match else arg.partition(":")[2]).strip().lower()
                local, _, domain = address.rpartition("@")
                if not local or domain not in self.app.store.domain_names():
                    await self.reply("550 No such user here")
                elif self.app.store.is_disabled(address):
                    await self.reply("550 Mailbox disabled")
                elif len(recipients) >= self.MAX_RECIPIENTS:
                    await self.reply("452 Too many recipients")
                else:
                    recipients.append(address)
                    await self.reply("250 OK")
            elif verb == "DATA":
                if not recipients:
                    await self.reply("503 Need RCPT first")
                    continue
                await self.reply("354 End data with <CR><LF>.<CR><LF>")
                raw = await self.read_data()
                if raw is None:
                    await self.reply("552 Message too large")
                else:
                    loop = asyncio.get_event_loop()
                    try:
                        await loop.run_in_executor(None, self.app.deliver, sender, list(recipients), raw)
                    except Exception as exc:
                        print("deliver failed: %r" % exc, flush=True)
                        await self.reply("451 Local error, try again later")
                    else:
                        await self.reply("250 OK")
                sender, recipients = "", []
            elif verb == "RSET":
                sender, recipients = "", []
                await self.reply("250 OK")
            elif verb == "NOOP":
                await self.reply("250 OK")
            elif verb == "QUIT":
                await self.reply("221 Bye")
                return
            else:
                await self.reply("502 Command not implemented")

    async def read_data(self):
        chunks, size = [], 0
        while True:
            line = await self.readline()
            if not line:
                raise ConnectionError("client closed during DATA")
            if line in (b".\r\n", b".\n"):
                break
            if line.startswith(b"."):
                line = line[1:]
            size += len(line)
            if size <= self.app.cfg.max_mail_bytes:
                chunks.append(line)
        return None if size > self.app.cfg.max_mail_bytes else b"".join(chunks)
