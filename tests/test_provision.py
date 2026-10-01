"""Setup behaviour: it provisions, it is idempotent, and it refuses to quietly
take over a domain that already receives mail."""

import json

import pytest

from tempmail import provision
from tempmail.errors import NotFoundError, TempmailError

ALWAYS_YES = lambda prompt: True
ALWAYS_NO = lambda prompt: False


def run_setup(settings, client, domain="example.com", **kwargs):
    kwargs.setdefault("confirm", ALWAYS_NO)
    return provision.provision(settings, client, domain, **kwargs)


class TestProvision(object):
    def test_creates_the_whole_stack(self, settings, client, api):
        result = run_setup(settings, client)

        assert result["domain"] == "example.com"
        assert api.routing_enabled[result["zone_id"]] is True
        assert settings.worker_name in api.scripts
        assert api.databases, "a D1 database should have been created"

        catch_all = api.catch_all[result["zone_id"]]
        assert catch_all["enabled"] is True
        assert catch_all["actions"] == [
            {"type": "worker", "value": [settings.worker_name]}
        ]
        assert catch_all["matchers"] == [{"type": "all"}]

    def test_schema_is_applied_so_addresses_can_be_stored(self, settings, client, api):
        run_setup(settings, client)

        tables = {
            row[0]
            for row in api.db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"domains", "addresses", "messages"} <= tables

        columns = {row[1] for row in api.db.execute("PRAGMA table_info(messages)")}
        assert "body_html" in columns, "the audit copy column must exist"

    def test_is_idempotent(self, settings, client, api):
        run_setup(settings, client)
        first_pass = list(api.mutations)
        api.calls = []

        run_setup(settings, client)
        second_pass = api.mutations

        # Nothing is created twice: no second database, no second enable call,
        # no rewrite of a catch-all that already points at our Worker.
        # (Matched exactly, so the D1 /query calls the schema makes do not
        # count as "creating a database".)
        assert api.mutations_matching(r"/d1/database$") == []
        assert api.mutations_matching(r"/email/routing/dns$") == []
        assert api.mutations_matching(r"/catch_all$") == []
        assert len(api.databases) == 1

        # The Worker upload and the domains upsert do repeat, and both are
        # idempotent by construction.
        assert api.mutations_touching("/workers/scripts")
        assert len(second_pass) < len(first_pass)

        rows = list(api.db.execute("SELECT COUNT(*) FROM domains"))
        assert rows[0][0] == 1

    def test_worker_is_deployed_before_mail_is_routed_to_it(self, settings, client, api):
        run_setup(settings, client)

        paths = [path for method, path, _ in api.calls if method == "PUT"]
        worker_index = next(i for i, p in enumerate(paths) if "/workers/scripts" in p)
        catch_all_index = next(i for i, p in enumerate(paths) if "catch_all" in p)
        assert worker_index < catch_all_index


class TestSafety(object):
    def test_refuses_to_hijack_existing_mx_without_consent(self, settings, client, api):
        zone_id = api.zones["example.com"]
        api.mx_records[zone_id] = [
            {"type": "MX", "content": "ASPMX.L.GOOGLE.COM", "priority": 1}
        ]

        with pytest.raises(TempmailError) as excinfo:
            run_setup(settings, client, confirm=ALWAYS_NO)

        assert "already receives mail elsewhere" in excinfo.value.message
        assert api.routing_enabled.get(zone_id) is not True
        assert "--force" in excinfo.value.hint

    def test_force_takes_over_existing_mx(self, settings, client, api):
        zone_id = api.zones["example.com"]
        api.mx_records[zone_id] = [{"type": "MX", "content": "ASPMX.L.GOOGLE.COM"}]

        run_setup(settings, client, force=True)
        assert api.routing_enabled[zone_id] is True

    def test_consent_at_the_prompt_proceeds(self, settings, client, api):
        zone_id = api.zones["example.com"]
        api.mx_records[zone_id] = [{"type": "MX", "content": "ASPMX.L.GOOGLE.COM"}]

        run_setup(settings, client, confirm=ALWAYS_YES)
        assert api.routing_enabled[zone_id] is True

    def test_refuses_to_replace_a_forwarding_catch_all(self, settings, client, api):
        zone_id = api.zones["example.com"]
        api.routing_enabled[zone_id] = True
        api.catch_all[zone_id] = {
            "enabled": True,
            "matchers": [{"type": "all"}],
            "actions": [{"type": "forward", "value": ["me@personal.test"]}],
        }

        with pytest.raises(TempmailError) as excinfo:
            run_setup(settings, client, confirm=ALWAYS_NO)

        assert "catch-all rule left untouched" in excinfo.value.message
        assert api.catch_all[zone_id]["actions"][0]["type"] == "forward"

    def test_subdomain_gets_an_actionable_error(self, settings, client, api):
        with pytest.raises(NotFoundError) as excinfo:
            run_setup(settings, client, domain="mail.example.com")

        assert "not a zone" in excinfo.value.message
        assert "Email Routing only works at a zone apex" in excinfo.value.hint

    def test_unknown_domain_is_reported_clearly(self, settings, client):
        with pytest.raises(NotFoundError) as excinfo:
            run_setup(settings, client, domain="nothere.test")
        assert "not found in this Cloudflare account" in excinfo.value.message


class TestWorkerBindings(object):
    def test_bindings_carry_db_config_and_webhook(self, settings, client, api):
        run_setup(settings, client)
        bindings = provision.worker_bindings(settings, "db-1")
        by_name = {b["name"]: b for b in bindings}

        assert by_name["DB"]["type"] == "d1"
        assert by_name["SLACK_WEBHOOK_URL"]["type"] == "secret_text"
        assert by_name["CONFIG"]["type"] == "plain_text"

        config = json.loads(by_name["CONFIG"]["text"])
        assert config["defaults"]["maxMessageBytes"] == 1048576
        assert config["defaults"]["storeHtml"] is True

    def test_per_domain_overrides_reach_the_worker(self, settings, client):
        settings.set_domain("example.com", rate_limit_per_hour=3,
                            slack_webhook_url="https://hooks.slack.com/per-domain")
        bindings = provision.worker_bindings(settings, "db-1")
        by_name = {b["name"]: b for b in bindings}

        assert by_name["SLACK_WEBHOOK_EXAMPLE_COM"]["text"].endswith("per-domain")
        config = json.loads(by_name["CONFIG"]["text"])
        assert config["domains"]["example.com"]["rateLimitPerHour"] == 3

    def test_binding_name_matches_the_worker_lookup(self):
        # The Worker computes this name at runtime; if the two ever diverge the
        # per-domain webhook silently stops being found.
        assert provision.webhook_binding_name("lab.example.net") == \
            "SLACK_WEBHOOK_LAB_EXAMPLE_NET"


class TestDoctor(object):
    def test_reports_healthy_after_setup(self, settings, client, api):
        run_setup(settings, client)
        checks = {c["check"]: c for c in provision.diagnose(settings, client, "example.com")}

        assert checks["email_routing"]["ok"]
        assert checks["catch_all"]["ok"]
        assert checks["worker"]["ok"]
        assert checks["database"]["ok"]

    def test_flags_a_catch_all_that_was_changed_behind_our_back(self, settings, client, api):
        run_setup(settings, client)
        zone_id = api.zones["example.com"]
        api.catch_all[zone_id] = {
            "enabled": True, "matchers": [{"type": "all"}],
            "actions": [{"type": "drop"}],
        }

        checks = {c["check"]: c for c in provision.diagnose(settings, client, "example.com")}
        assert not checks["catch_all"]["ok"]
        assert "tempmail setup" in checks["catch_all"]["hint"]


class TestPermissionPreflight(object):
    """Regression cover for a real failure: the token carried Email Routing
    Rules but not Zone Settings, and setup died on GET /email/routing after
    already reporting three successful steps."""

    def test_missing_zone_settings_is_named_correctly(self, settings, client, api):
        # Email Routing *settings* live under Zone Settings, not under the
        # Email Routing Rules permission that covers the rules endpoints.
        api.denied = [r"/email/routing$"]

        with pytest.raises(TempmailError) as excinfo:
            run_setup(settings, client)

        assert "Zone > Zone Settings : Edit" in excinfo.value.message
        assert "Email Routing Rules" not in excinfo.value.message

    def test_rules_and_settings_are_distinguished(self, settings, client, api):
        api.denied = [r"/email/routing/rules"]

        with pytest.raises(TempmailError) as excinfo:
            run_setup(settings, client)

        assert "Zone > Email Routing Rules : Edit" in excinfo.value.message
        assert "Zone Settings" not in excinfo.value.message

    def test_every_missing_permission_is_reported_at_once(self, settings, client, api):
        api.denied = [r"/email/routing$", r"/dns_records", r"/workers/scripts"]

        with pytest.raises(TempmailError) as excinfo:
            run_setup(settings, client)

        message = excinfo.value.message
        assert "missing 3 permission" in message
        for expected in ("Zone Settings", "DNS : Read", "Workers Scripts"):
            assert expected in message

    def test_preflight_runs_before_anything_is_changed(self, settings, client, api):
        api.denied = [r"/email/routing$"]

        with pytest.raises(TempmailError):
            run_setup(settings, client)

        assert api.mutations == [], "no resource may be touched before the check"

    def test_absent_resources_are_not_mistaken_for_denied_ones(self, settings, client, api):
        # The Worker does not exist yet on a first run: a 404 means authorised
        # but absent, which is the state setup exists to fix.
        assert provision.preflight(
            client, api.zones["example.com"], "acct1", "tempmail-router"
        ) == []

    def test_doctor_surfaces_the_same_finding(self, settings, client, api):
        api.denied = [r"/email/routing$"]
        checks = {c["check"]: c for c in provision.diagnose(settings, client, "example.com")}

        assert not checks["permissions"]["ok"]
        assert "Zone Settings" in checks["permissions"]["detail"]
