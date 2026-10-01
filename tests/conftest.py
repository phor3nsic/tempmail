"""A fake Cloudflare API.

D1 is not stubbed: the /query endpoint runs the SQL against an in-memory SQLite
database, so the schema, the ON CONFLICT upserts and the batch transaction in
`rotate` are all exercised for real. Everything else returns the envelope shape
the API documents.

The fake also records every mutating request, which is what the idempotency
test asserts on.
"""

import json
import re
import sqlite3

import httpx
import pytest

API_PREFIX = "/client/v4"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

ZONE_ID = "zone0000000000000000000000000001"
ACCOUNT_ID = "acct0000000000000000000000000001"
DATABASE_ID = "db00000000-0000-0000-0000-000000000001"


def ok(result):
    return httpx.Response(200, json={"success": True, "errors": [], "messages": [],
                                     "result": result})


def fail(status, message, code=1000):
    return httpx.Response(
        status,
        json={"success": False, "errors": [{"code": code, "message": message}],
              "messages": [], "result": None},
    )


class FakeCloudflare(object):
    """In-memory stand-in for the subset of the API the CLI touches."""

    def __init__(self, zones=("example.com",)):
        self.calls = []
        # Regexes for paths this token is not allowed to touch, so a test can
        # reproduce a narrowly scoped API token.
        self.denied = []
        self.zones = {name: ZONE_ID[:-1] + str(i + 1) for i, name in enumerate(zones)}
        self.routing_enabled = {}
        self.catch_all = {}
        self.mx_records = {}
        self.databases = {}
        self.scripts = {}
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row

    # -- bookkeeping -------------------------------------------------------

    @property
    def mutations(self):
        return [(m, p) for m, p, _ in self.calls if m in MUTATING]

    def mutations_touching(self, needle):
        return [(m, p) for m, p in self.mutations if needle in p]

    def mutations_matching(self, pattern):
        """Mutations whose path matches a regex — use this to tell a resource
        being created apart from it merely being queried."""
        return [(m, p) for m, p in self.mutations if re.search(pattern, p)]

    # -- dispatch ----------------------------------------------------------

    def handler(self, request):
        path = request.url.path
        if path.startswith(API_PREFIX):
            path = path[len(API_PREFIX):]

        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content[:200]
        self.calls.append((request.method, path, body))

        if request.headers.get("Authorization") != "Bearer test-token":
            return fail(401, "Invalid API token", code=10000)

        for pattern in self.denied:
            if re.search(pattern, path):
                return fail(403, "Authentication error", code=10000)

        for pattern, method, handler in self._routes():
            if request.method != method:
                continue
            match = re.match(pattern + "$", path)
            if match:
                return handler(request, body, *match.groups())
        return fail(404, "No route for {0} {1}".format(request.method, path))

    def _routes(self):
        return (
            (r"/user/tokens/verify", "GET", self._verify),
            (r"/zones", "GET", self._list_zones),
            (r"/zones/([^/]+)/email/routing", "GET", self._get_routing),
            (r"/zones/([^/]+)/email/routing/dns", "GET", self._get_routing_dns),
            (r"/zones/([^/]+)/email/routing/dns", "POST", self._enable_routing),
            (r"/zones/([^/]+)/dns_records", "GET", self._dns_records),
            (r"/zones/([^/]+)/email/routing/rules/catch_all", "GET", self._get_catch_all),
            (r"/zones/([^/]+)/email/routing/rules/catch_all", "PUT", self._put_catch_all),
            (r"/accounts/([^/]+)/d1/database", "GET", self._list_databases),
            (r"/accounts/([^/]+)/d1/database", "POST", self._create_database),
            (r"/accounts/([^/]+)/d1/database/([^/]+)", "GET", self._get_database),
            (r"/accounts/([^/]+)/d1/database/([^/]+)/query", "POST", self._query),
            (r"/accounts/([^/]+)/workers/scripts/([^/]+)", "GET", self._get_script),
            (r"/accounts/([^/]+)/workers/scripts/([^/]+)", "PUT", self._put_script),
        )

    # -- handlers ----------------------------------------------------------

    def _verify(self, request, body):
        return ok({"id": "token1", "status": "active"})

    def _list_zones(self, request, body):
        name = request.url.params.get("name")
        zone_id = self.zones.get(name)
        if not zone_id:
            return ok([])
        return ok([{"id": zone_id, "name": name, "account": {"id": ACCOUNT_ID}}])

    def _get_routing(self, request, body, zone_id):
        return ok({"enabled": self.routing_enabled.get(zone_id, False),
                   "status": "ready", "name": "tempmail"})

    def _get_routing_dns(self, request, body, zone_id):
        return ok([
            {"type": "MX", "name": "example.com", "content": "route1.mx.cloudflare.net",
             "priority": 1, "required": True, "status": "ok"},
            {"type": "TXT", "name": "example.com", "content": "v=spf1 include:_spf.mx.cloudflare.net ~all",
             "required": True, "status": "ok"},
        ])

    def _enable_routing(self, request, body, zone_id):
        self.routing_enabled[zone_id] = True
        self.mx_records.setdefault(zone_id, []).append(
            {"type": "MX", "content": "route1.mx.cloudflare.net"}
        )
        return ok({"enabled": True})

    def _dns_records(self, request, body, zone_id):
        return ok(self.mx_records.get(zone_id, []))

    def _get_catch_all(self, request, body, zone_id):
        return ok(self.catch_all.get(zone_id,
                                     {"enabled": False, "matchers": [], "actions": []}))

    def _put_catch_all(self, request, body, zone_id):
        self.catch_all[zone_id] = body
        return ok(body)

    def _list_databases(self, request, body, account_id):
        return ok(list(self.databases.values()))

    def _create_database(self, request, body, account_id):
        entry = {"uuid": DATABASE_ID, "name": body["name"]}
        self.databases[DATABASE_ID] = entry
        return ok(entry)

    def _get_database(self, request, body, account_id, database_id):
        entry = self.databases.get(database_id)
        return ok(entry) if entry else fail(404, "database not found", code=7404)

    def _query(self, request, body, account_id, database_id):
        if database_id not in self.databases:
            return fail(404, "database not found", code=7404)

        statements = body.get("batch") or [body]
        results = []
        try:
            for statement in statements:
                cursor = self.db.execute(
                    statement["sql"], tuple(statement.get("params") or [])
                )
                rows = [dict(row) for row in cursor.fetchall()]
                results.append({
                    "success": True,
                    "results": rows,
                    "meta": {"changes": cursor.rowcount if cursor.rowcount > 0 else 0,
                             "last_row_id": cursor.lastrowid or 0},
                })
            self.db.commit()
        except sqlite3.Error as exc:
            return ok([{"success": False, "error": str(exc), "results": [], "meta": {}}])
        return ok(results)

    def _get_script(self, request, body, account_id, name):
        if name not in self.scripts:
            return fail(404, "script not found", code=10007)
        return ok({"id": name})

    def _put_script(self, request, body, account_id, name):
        # The real endpoint takes multipart; record that it was called and with
        # roughly the right content type.
        self.scripts[name] = {"content_type": request.headers.get("content-type", "")}
        return ok({"id": name, "etag": "etag1"})

    # -- helpers for tests -------------------------------------------------

    def transport(self):
        return httpx.MockTransport(self.handler)

    def insert_message(self, email, **fields):
        """Pretend the Worker delivered a message."""
        values = {
            "email": email,
            "rcpt": email,
            "from_addr": "noreply@service.test",
            "from_name": "Service",
            "subject": "Verification",
            "received_at": fields.pop("received_at", 1759200000),
            "sent_at": None,
            "outcome": "delivered",
            "raw_size": 2048,
            "body_text": "Your verification code is:\n\n839201",
            "body_html": "<html><body>code <b>839201</b><img src=\"https://t.test/p.gif\"></body></html>",
            "links": json.dumps(["https://service.test/verify"]),
            "codes": json.dumps(["839201"]),
            "attachments": json.dumps([]),
        }
        values.update(fields)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        cursor = self.db.execute(
            "INSERT INTO messages ({0}) VALUES ({1})".format(columns, placeholders),
            tuple(values.values()),
        )
        self.db.commit()
        return cursor.lastrowid


@pytest.fixture
def api():
    return FakeCloudflare()


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    path = tmp_path / "config" / "settings.json"
    monkeypatch.setenv("TEMPMAIL_CONFIG", str(path))
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    return str(path)


@pytest.fixture
def settings(settings_path):
    from tempmail.config import Settings

    instance = Settings.load(settings_path)
    instance.data["cloudflare"]["api_token"] = "test-token"
    instance.data["slack"]["webhook_url"] = "https://hooks.slack.com/services/T/B/XYZ"
    instance.save()
    return instance


@pytest.fixture
def client(api):
    from tempmail.cloudflare import CloudflareClient

    instance = CloudflareClient("test-token", transport=api.transport(),
                                sleep=lambda seconds: None)
    yield instance
    instance.close()
