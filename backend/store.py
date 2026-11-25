"""SQLite storage."""
import os
import secrets
import sqlite3
import string
import threading
import time

from extract import extract_for

DEFAULT_SCENARIO = "default"
# Raised when the layout changes in a way existing files cannot be upgraded to.
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL, token TEXT NOT NULL,
    collector TEXT NOT NULL UNIQUE, alias_domain TEXT NOT NULL DEFAULT '', alias_format TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, mode TEXT NOT NULL,
    pattern TEXT NOT NULL, sample TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    template_id INTEGER, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scenario_domains (
    scenario_id INTEGER NOT NULL, domain_id INTEGER NOT NULL, PRIMARY KEY (scenario_id, domain_id));
CREATE TABLE IF NOT EXISTS scenario_sources (
    scenario_id INTEGER NOT NULL, source_id INTEGER NOT NULL, PRIMARY KEY (scenario_id, source_id));
CREATE TABLE IF NOT EXISTS mailboxes (
    address TEXT PRIMARY KEY, domain TEXT NOT NULL, scenario_id INTEGER NOT NULL, source_id INTEGER,
    remote_id TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
    deleted_at REAL);
CREATE TABLE IF NOT EXISTS mails (
    id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL, sender TEXT, subject TEXT, body TEXT,
    alt TEXT NOT NULL DEFAULT '', headers TEXT NOT NULL DEFAULT '', code TEXT, received_at REAL NOT NULL,
    read_at REAL, deleted_at REAL);
CREATE INDEX IF NOT EXISTS idx_mails_address ON mails(address, received_at);
CREATE TABLE IF NOT EXISTS sent_mails (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, recipients TEXT NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
    provider_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'send', created_at REAL NOT NULL,
    deleted_at REAL);
CREATE TABLE IF NOT EXISTS forwards (
    id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
"""


def retire_old_database(path):
    """Move a database from before SCHEMA_VERSION aside; it is kept, not converted. Returns its new path."""
    if not os.path.isfile(path):
        return None
    db = sqlite3.connect(path)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        empty = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] == 0
        if version == SCHEMA_VERSION or empty:
            return None
        if version > SCHEMA_VERSION:
            raise RuntimeError("%s was written by a newer version of mailbox" % path)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    backup = "%s.v%d-%s" % (path, version, time.strftime("%Y%m%d-%H%M%S"))
    os.rename(path, backup)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.rename(path + suffix, backup + suffix)
    return backup


class Store:
    def __init__(self, path, seed_domains=()):
        self.retired = retire_old_database(path)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("PRAGMA journal_mode=WAL;" + _SCHEMA + "PRAGMA user_version = %d;" % SCHEMA_VERSION)
        self.execute(
            "INSERT OR IGNORE INTO scenarios (key, name, created_at) VALUES (?, '默认场景', ?)",
            (DEFAULT_SCENARIO, time.time()),
        )
        self.default_scenario_id = self.query("SELECT id FROM scenarios WHERE key = ?", (DEFAULT_SCENARIO,))[0]["id"]
        if not self.query("SELECT id FROM domains LIMIT 1"):
            for name in seed_domains:
                self.execute("INSERT OR IGNORE INTO domains (name, created_at) VALUES (?, ?)", (name, time.time()))

    def add_column(self, table, column, definition):
        if column not in [row["name"] for row in self.query("PRAGMA table_info(%s)" % table)]:
            self.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))

    def query(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args).rowcount

    def insert(self, sql, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args).lastrowid

    def domain_names(self):
        return {row["name"] for row in self.query("SELECT name FROM domains")}

    def usable_domains(self, scenario_id):
        """Enabled domains a scenario may use: its linked ones, or every enabled domain when none are linked."""
        linked = self.query(
            "SELECT d.name, d.enabled FROM scenario_domains sd JOIN domains d ON d.id = sd.domain_id"
            " WHERE sd.scenario_id = ?",
            (scenario_id,),
        )
        rows = linked or self.query("SELECT name, enabled FROM domains")
        return [row["name"] for row in rows if row["enabled"]]

    def linked_sources(self, scenario_id):
        return self.query(
            "SELECT s.* FROM scenario_sources ss JOIN sources s ON s.id = ss.source_id"
            " WHERE ss.scenario_id = ? ORDER BY s.id",
            (scenario_id,),
        )

    def source(self, source_id):
        rows = self.query("SELECT * FROM sources WHERE id = ?", (source_id,))
        return rows[0] if rows else None

    def collectors(self):
        """Collector address -> source, for every source."""
        return {row["collector"]: row for row in self.query("SELECT * FROM sources")}

    def mailbox(self, address):
        rows = self.query("SELECT * FROM mailboxes WHERE address = ? AND deleted_at IS NULL", (address,))
        return rows[0] if rows else None

    def is_disabled(self, address):
        box = self.mailbox(address)
        return bool(box) and not box["active"]

    def add_mailbox(self, address, scenario_id, source_id=None, remote_id=""):
        """Record a mailbox. Returns False when the address is already taken."""
        return bool(
            self.execute(
                "INSERT OR IGNORE INTO mailboxes (address, domain, scenario_id, source_id, remote_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (address, address.rsplit("@", 1)[1], scenario_id, source_id, remote_id, time.time()),
            )
        )

    def create_local_mailbox(self, domain, scenario_id):
        alphabet = string.ascii_lowercase + string.digits
        while True:
            local = secrets.choice(string.ascii_lowercase) + "".join(secrets.choice(alphabet) for _ in range(9))
            address = "%s@%s" % (local, domain)
            if self.add_mailbox(address, scenario_id):
                return address

    def resolve_alias(self, source, recipients, mentions):
        """Find which alias of a source a mail delivered to its collector address was sent to, or None.

        An address we already know as a mailbox of the source wins. Otherwise the first address in the
        recipient headers that could be one of its aliases is taken: the mail went to an alias made elsewhere.
        """
        candidates = [address for address in recipients + mentions if address != source["collector"]]
        if not candidates:
            return None
        marks = ",".join("?" * len(candidates))
        known = {
            row["address"]
            for row in self.query(
                "SELECT address FROM mailboxes WHERE source_id = ? AND address IN (%s)" % marks,
                [source["id"]] + candidates,
            )
        }
        for address in candidates:
            if address in known:
                return address
        own = self.domain_names()
        outside = [a for a in recipients if a != source["collector"] and a.rsplit("@", 1)[1] not in own]
        if source["kind"] == "duck":
            outside = [a for a in outside if a.endswith("@duck.com")]
        else:
            domains = {row["domain"] for row in self.query(
                "SELECT DISTINCT domain FROM mailboxes WHERE source_id = ?", (source["id"],)
            )}
            domains.add(source["alias_domain"])
            outside = [a for a in outside if a.rsplit("@", 1)[1] in domains] or outside
        return outside[0] if outside else None

    def template_for(self, address):
        rows = self.query(
            "SELECT t.mode, t.pattern FROM mailboxes b JOIN scenarios s ON s.id = b.scenario_id"
            " JOIN templates t ON t.id = s.template_id WHERE b.address = ?",
            (address,),
        )
        return rows[0] if rows else None

    def add_mail(self, address, mail, code, source_id=None):
        """Store a received mail, creating its mailbox in the default scenario when it is new."""
        now = time.time()
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO mailboxes (address, domain, scenario_id, source_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (address, address.rsplit("@", 1)[1], self.default_scenario_id, source_id, now),
            )
            self.db.execute("UPDATE mailboxes SET deleted_at = NULL WHERE address = ?", (address,))
            self.db.execute(
                "INSERT INTO mails (address, sender, subject, body, alt, headers, code, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    address,
                    mail["sender"],
                    mail["subject"],
                    mail["plain"] or mail["rich"],
                    mail["rich"] if mail["plain"] else "",
                    mail["headers"],
                    code,
                    now,
                ),
            )

    def reparse(self, scenario_ids):
        """Re-extract codes for every mail in the given scenarios after their template changed."""
        for scenario_id in scenario_ids:
            rows = self.query(
                "SELECT t.mode, t.pattern FROM scenarios s JOIN templates t ON t.id = s.template_id WHERE s.id = ?",
                (scenario_id,),
            )
            template = rows[0] if rows else None
            mails = self.query(
                "SELECT m.id, m.subject, m.body, m.alt, m.code FROM mails m JOIN mailboxes b ON b.address = m.address"
                " WHERE b.scenario_id = ? AND m.deleted_at IS NULL",
                (scenario_id,),
            )
            for mail in mails:
                code = extract_for(template, mail["subject"], mail["body"], mail["alt"])
                if code != mail["code"]:
                    self.execute("UPDATE mails SET code = ? WHERE id = ?", (code, mail["id"]))

    def latest_code(self, address, since=0.0):
        rows = self.query(
            "SELECT code, sender, subject, received_at FROM mails"
            " WHERE address = ? AND code IS NOT NULL AND received_at >= ? AND deleted_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (address, since),
        )
        return rows[0] if rows else None

    def remove_mailbox(self, address):
        """Drop a mailbox the user deleted; its mails are hidden like expired ones."""
        now = time.time()
        self.execute("UPDATE mails SET deleted_at = ? WHERE address = ? AND deleted_at IS NULL", (now, address))
        return bool(self.execute("DELETE FROM mailboxes WHERE address = ?", (address,)))

    def purge(self, max_age_seconds):
        """Soft-delete expired rows: they are flagged with deleted_at and hidden, never removed.

        Only catch-all mailboxes on our own domains expire. A mailbox of an external source stays, because
        its alias keeps working there, and so does a disabled one, which would otherwise accept mail again.
        """
        now = time.time()
        cutoff = now - max_age_seconds
        self.execute("UPDATE mails SET deleted_at = ? WHERE received_at < ? AND deleted_at IS NULL", (now, cutoff))
        self.execute("UPDATE sent_mails SET deleted_at = ? WHERE created_at < ? AND deleted_at IS NULL", (now, cutoff))
        self.execute(
            "UPDATE mailboxes SET deleted_at = ? WHERE created_at < ? AND deleted_at IS NULL"
            " AND source_id IS NULL AND active = 1"
            " AND address NOT IN (SELECT DISTINCT address FROM mails WHERE deleted_at IS NULL)",
            (now, cutoff),
        )
