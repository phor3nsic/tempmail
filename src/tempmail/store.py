"""D1-backed state: which addresses exist, and what each one is allowed to do.

D1 was chosen over KV for two reasons. It is strongly consistent, so an address
is live the instant `tempmail create` returns — KV's eventual propagation would
leave a window where mail to a brand new address is dropped. And `list`,
`status` and `rotate --current` are ordinary SQL instead of prefix scans.

What is stored: addresses, and the sanitized text of delivered messages so the
`inbox`, `read` and `wait` commands can serve them back. Never the raw MIME and
never executable HTML — the Worker stores only what it already rendered safe,
truncated to a configured cap, and rows age out after a retention window.
"""

import time

from .cloudflare import d1
from .errors import CloudflareError, NotFoundError, UsageError

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"
STATUSES = (STATUS_ACTIVE, STATUS_REVOKED, STATUS_EXPIRED)

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS domains (
      domain TEXT PRIMARY KEY,
      zone_id TEXT NOT NULL,
      worker_name TEXT NOT NULL,
      max_message_bytes INTEGER,
      rate_limit_per_hour INTEGER,
      strip_plus_tag INTEGER NOT NULL DEFAULT 1,
      configured_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS addresses (
      email TEXT PRIMARY KEY,
      domain TEXT NOT NULL,
      local_part TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('active','revoked','expired')),
      created_at INTEGER NOT NULL,
      expires_at INTEGER,
      revoked_at INTEGER,
      label TEXT,
      rotated_from TEXT,
      msg_count INTEGER NOT NULL DEFAULT 0,
      last_msg_at INTEGER,
      rate_window_start INTEGER,
      rate_window_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_addr_domain ON addresses(domain, status, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT NOT NULL,
      rcpt TEXT,
      from_addr TEXT,
      from_name TEXT,
      subject TEXT,
      received_at INTEGER NOT NULL,
      sent_at INTEGER,
      outcome TEXT NOT NULL,
      raw_size INTEGER,
      body_text TEXT,
      body_html TEXT,
      links TEXT,
      codes TEXT,
      attachments TEXT,
      read_at INTEGER,
      slack_status TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_email ON messages(email, received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at)",
)

# Outcomes recorded for every inbound message, delivered or not.
OUTCOME_DELIVERED = "delivered"
OUTCOME_UNKNOWN = "dropped_unknown"
OUTCOME_REVOKED = "dropped_revoked"
OUTCOME_EXPIRED = "dropped_expired"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_TOO_LARGE = "too_large"
OUTCOME_PARSE_FALLBACK = "parse_fallback"

# Outcomes that carry readable content.
READABLE_OUTCOMES = (OUTCOME_DELIVERED, OUTCOME_PARSE_FALLBACK)

_MESSAGE_COLUMNS = (
    "id, email, rcpt, from_addr, from_name, subject, received_at, sent_at, "
    "outcome, raw_size, links, codes, attachments, read_at, slack_status"
)

# Columns added after the first release. Applied one at a time and tolerantly,
# because `setup` is expected to run against databases created by any earlier
# version. SQLite has no ADD COLUMN IF NOT EXISTS.
MIGRATION_STATEMENTS = (
    "ALTER TABLE messages ADD COLUMN body_html TEXT",
)

_ADDRESS_COLUMNS = (
    "email, domain, local_part, status, created_at, expires_at, revoked_at, "
    "label, rotated_from, msg_count, last_msg_at"
)


def now():
    return int(time.time())


def effective_status(row, at=None):
    """The status a user should see.

    A row can sit at ``active`` past its expiry until some delivery flips it;
    the CLI must not call that active.
    """
    if not row:
        return None
    status = row.get("status")
    if status == STATUS_ACTIVE and row.get("expires_at"):
        if int(row["expires_at"]) <= (at if at is not None else now()):
            return STATUS_EXPIRED
    return status


class Store(object):
    def __init__(self, client, account_id, database_id):
        self.client = client
        self.account_id = account_id
        self.database_id = database_id

    def _query(self, sql, params=None):
        return d1.query(self.client, self.account_id, self.database_id, sql, params)

    def _batch(self, statements):
        return d1.batch(self.client, self.account_id, self.database_id, statements)

    # ---- schema ----------------------------------------------------------

    def ensure_schema(self):
        """Apply the schema, then any migrations. Safe to run on every setup."""
        self._batch([(sql.strip(), None) for sql in SCHEMA_STATEMENTS])
        for sql in MIGRATION_STATEMENTS:
            try:
                self._query(sql)
            except CloudflareError as exc:
                # Already applied: the column exists. Anything else is real.
                if "duplicate column" not in str(exc).lower():
                    raise

    def healthy(self):
        self._query("SELECT 1 AS ok")
        return True

    # ---- domains ---------------------------------------------------------

    def upsert_domain(self, domain, zone_id, worker_name, max_message_bytes,
                      rate_limit_per_hour, strip_plus_tag=True):
        self._query(
            """
            INSERT INTO domains (domain, zone_id, worker_name, max_message_bytes,
                                 rate_limit_per_hour, strip_plus_tag, configured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
              zone_id = excluded.zone_id,
              worker_name = excluded.worker_name,
              max_message_bytes = excluded.max_message_bytes,
              rate_limit_per_hour = excluded.rate_limit_per_hour,
              strip_plus_tag = excluded.strip_plus_tag,
              configured_at = excluded.configured_at
            """,
            [
                domain, zone_id, worker_name, max_message_bytes,
                rate_limit_per_hour, 1 if strip_plus_tag else 0, now(),
            ],
        )

    def get_domain(self, domain):
        return d1.first_row(
            self._query("SELECT * FROM domains WHERE domain = ?", [domain])
        )

    def list_domains(self):
        return d1.rows(self._query("SELECT * FROM domains ORDER BY domain"))

    def require_domain(self, domain):
        entry = self.get_domain(domain)
        if not entry:
            raise NotFoundError(
                "Domain is not set up: {0}".format(domain),
                hint="Run `tempmail setup {0}` first.".format(domain),
            )
        return entry

    # ---- addresses -------------------------------------------------------

    def create_address(self, email, domain, local_part, expires_at=None, label=None,
                       rotated_from=None):
        created = now()
        self._query(
            """
            INSERT INTO addresses (email, domain, local_part, status, created_at,
                                   expires_at, label, rotated_from)
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            [email, domain, local_part, created, expires_at, label, rotated_from],
        )
        return {
            "email": email,
            "domain": domain,
            "local_part": local_part,
            "status": STATUS_ACTIVE,
            "created_at": created,
            "expires_at": expires_at,
            "revoked_at": None,
            "label": label,
            "rotated_from": rotated_from,
            "msg_count": 0,
            "last_msg_at": None,
        }

    def get_address(self, email):
        return d1.first_row(
            self._query(
                "SELECT {0} FROM addresses WHERE email = ?".format(_ADDRESS_COLUMNS),
                [email],
            )
        )

    def require_address(self, email):
        row = self.get_address(email)
        if not row:
            raise NotFoundError("Unknown address: {0}".format(email))
        return row

    def exists(self, email):
        return self.get_address(email) is not None

    def list_addresses(self, domain=None, status=None, limit=200):
        clauses = []
        params = []
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if status:
            if status not in STATUSES:
                raise UsageError(
                    "Invalid status filter: {0} (use one of {1})".format(
                        status, ", ".join(STATUSES)
                    )
                )
            clauses.append("status = ?")
            params.append(status)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        return d1.rows(
            self._query(
                "SELECT {0} FROM addresses{1} "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?".format(
                    _ADDRESS_COLUMNS, where
                ),
                params,
            )
        )

    def current_active(self, domain):
        """The newest address for a domain that is still usable right now."""
        return d1.first_row(
            self._query(
                """
                SELECT {0} FROM addresses
                WHERE domain = ? AND status = 'active'
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """.format(_ADDRESS_COLUMNS),
                [domain, now()],
            )
        )

    def revoke(self, email):
        """Mark an address revoked. Idempotent, and never un-revokes anything."""
        row = self.require_address(email)
        if row.get("status") == STATUS_REVOKED:
            return row, False

        self._query(
            "UPDATE addresses SET status = 'revoked', revoked_at = ? WHERE email = ?",
            [now(), email],
        )
        row["status"] = STATUS_REVOKED
        row["revoked_at"] = now()
        return row, True

    def rotate(self, old_email, new_email, domain, new_local_part, expires_at=None,
               label=None):
        """Revoke the old address and create the new one in one D1 transaction.

        Batched so the pair cannot half-apply: there is no state where the old
        address is still live but the new one is missing, or vice versa.
        """
        revoked_at = now()
        created_at = revoked_at
        self._batch(
            [
                (
                    "UPDATE addresses SET status = 'revoked', revoked_at = ? "
                    "WHERE email = ?",
                    [revoked_at, old_email],
                ),
                (
                    "INSERT INTO addresses (email, domain, local_part, status, "
                    "created_at, expires_at, label, rotated_from) "
                    "VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
                    [
                        new_email, domain, new_local_part, created_at, expires_at,
                        label, old_email,
                    ],
                ),
            ]
        )
        return {
            "email": new_email,
            "domain": domain,
            "local_part": new_local_part,
            "status": STATUS_ACTIVE,
            "created_at": created_at,
            "expires_at": expires_at,
            "revoked_at": None,
            "label": label,
            "rotated_from": old_email,
            "msg_count": 0,
            "last_msg_at": None,
        }

    # ---- messages --------------------------------------------------------

    def list_messages(self, email=None, limit=20, unread_only=False,
                      readable_only=True, since_id=None):
        """Message headers, newest first. Bodies are left out: `read` fetches those."""
        clauses = []
        params = []
        if email:
            clauses.append("email = ?")
            params.append(email)
        if unread_only:
            clauses.append("read_at IS NULL")
        if readable_only:
            clauses.append(
                "outcome IN ({0})".format(
                    ", ".join("?" for _ in READABLE_OUTCOMES)
                )
            )
            params.extend(READABLE_OUTCOMES)
        if since_id is not None:
            clauses.append("id > ?")
            params.append(int(since_id))

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        return d1.rows(
            self._query(
                "SELECT {0} FROM messages{1} ORDER BY received_at DESC, id DESC "
                "LIMIT ?".format(_MESSAGE_COLUMNS, where),
                params,
            )
        )

    def get_message(self, message_id, email=None):
        """One message including its body."""
        sql = "SELECT {0}, body_text, body_html FROM messages WHERE id = ?".format(
            _MESSAGE_COLUMNS
        )
        params = [int(message_id)]
        if email:
            sql += " AND email = ?"
            params.append(email)
        return d1.first_row(self._query(sql, params))

    def latest_message(self, email, readable_only=True):
        clauses = ["email = ?"]
        params = [email]
        if readable_only:
            clauses.append(
                "outcome IN ({0})".format(", ".join("?" for _ in READABLE_OUTCOMES))
            )
            params.extend(READABLE_OUTCOMES)
        return d1.first_row(
            self._query(
                "SELECT {0}, body_text, body_html FROM messages WHERE {1} "
                "ORDER BY received_at DESC, id DESC LIMIT 1".format(
                    _MESSAGE_COLUMNS, " AND ".join(clauses)
                ),
                params,
            )
        )

    def require_message(self, message_id, email=None):
        row = self.get_message(message_id, email=email)
        if not row:
            raise NotFoundError("Message not found: #{0}".format(message_id))
        return row

    def mark_read(self, message_id):
        self._query(
            "UPDATE messages SET read_at = ? WHERE id = ? AND read_at IS NULL",
            [now(), int(message_id)],
        )

    def unread_count(self, email):
        row = d1.first_row(
            self._query(
                "SELECT COUNT(*) AS n FROM messages WHERE email = ? AND read_at IS NULL "
                "AND outcome IN ({0})".format(
                    ", ".join("?" for _ in READABLE_OUTCOMES)
                ),
                [email] + list(READABLE_OUTCOMES),
            )
        )
        return int((row or {}).get("n") or 0)

    def purge_messages(self, older_than_seconds=None, email=None):
        """Delete aged-out messages. Returns the number of rows removed."""
        clauses = []
        params = []
        if older_than_seconds is not None:
            clauses.append("received_at < ?")
            params.append(now() - int(older_than_seconds))
        if email:
            clauses.append("email = ?")
            params.append(email)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        results = self._query("DELETE FROM messages" + where, params)
        meta = (results[0] or {}).get("meta") or {} if results else {}
        return int(meta.get("changes") or 0)
