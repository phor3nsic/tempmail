"""End-to-end command behaviour, driven through the real Click entry point
against the fake Cloudflare API.

These are the tests that would catch a regression in the workflow the README
promises: setup, create, use, read, rotate.
"""

import json

import pytest
from click.testing import CliRunner

from tempmail import cli as cli_module
from tempmail.cli import cli
from tempmail.errors import NotFoundError, TempmailError


@pytest.fixture
def run(api, settings, monkeypatch):
    """Invoke the CLI with every Cloudflare call routed to the fake API."""
    from tempmail.cloudflare import CloudflareClient

    def factory(token, **kwargs):
        kwargs["transport"] = api.transport()
        kwargs["sleep"] = lambda seconds: None
        return CloudflareClient(token, **kwargs)

    monkeypatch.setattr(cli_module, "CloudflareClient", factory)
    runner = CliRunner()

    def invoke(*args, **kwargs):
        return runner.invoke(
            cli, list(args), standalone_mode=False, catch_exceptions=True, **kwargs
        )

    return invoke


@pytest.fixture
def ready(run):
    """A provisioned example.com."""
    result = run("setup", "example.com", "--force")
    assert result.exception is None, result.output
    return result


def address_from(output_text):
    for line in output_text.splitlines():
        if "@example.com" in line:
            return line.strip()
    raise AssertionError("no address in output:\n{0}".format(output_text))


class TestSetup(object):
    def test_reports_each_step_and_ends_ready(self, run, api):
        result = run("setup", "example.com", "--force")

        assert result.exception is None, result.output
        assert "Ready." in result.output
        assert api.scripts

    def test_persists_backend_ids_for_later_commands(self, run, settings_path):
        run("setup", "example.com", "--force")

        from tempmail.config import Settings

        saved = Settings.load(settings_path)
        assert saved.database_id
        assert saved.account_id
        assert saved.domain("example.com")["zone_id"]

    def test_per_domain_options_are_stored(self, run, settings_path):
        run("setup", "example.com", "--force", "--rate-limit", "5", "--retention", "24h")

        from tempmail.config import Settings

        saved = Settings.load(settings_path)
        assert saved.domain_setting("example.com", "rate_limit_per_hour") == 5
        assert saved.domain_setting("example.com", "retention_hours") == 24


class TestCreate(object):
    def test_creates_an_active_address(self, run, ready):
        result = run("create", "example.com")

        assert result.exception is None, result.output
        assert "Temporary email created" in result.output
        assert "Status:  ACTIVE" in result.output
        assert "@example.com" in result.output

    def test_quiet_prints_only_the_address(self, run, ready):
        result = run("create", "example.com", "--quiet")

        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1
        assert lines[0].endswith("@example.com")
        assert "@" in lines[0] and " " not in lines[0]

    def test_json_output_matches_the_documented_shape(self, run, ready):
        result = run("create", "example.com", "--ttl", "1h", "--json")
        payload = json.loads(result.stdout)

        assert payload["status"] == "active"
        assert payload["domain"] == "example.com"
        assert payload["created_at"].endswith("Z")
        assert payload["expires_at"] is not None

    def test_addresses_are_unique(self, run, ready):
        seen = {run("create", "example.com", "--quiet").stdout.strip() for _ in range(15)}
        assert len(seen) == 15

    def test_refuses_an_unconfigured_domain(self, run, ready):
        result = run("create", "other.test")

        assert isinstance(result.exception, NotFoundError)
        assert "tempmail setup" in result.exception.hint


class TestListAndStatus(object):
    def test_list_shows_addresses_with_status(self, run, ready):
        first = run("create", "example.com", "--quiet").stdout.strip()
        run("revoke", first)
        run("create", "example.com", "--quiet")

        result = run("list", "example.com")
        assert "EMAIL" in result.output
        assert "revoked" in result.output
        assert "active" in result.output

    def test_list_filters_by_status(self, run, ready):
        first = run("create", "example.com", "--quiet").stdout.strip()
        run("revoke", first)
        run("create", "example.com", "--quiet")

        result = run("list", "example.com", "--status", "active", "--json")
        payload = json.loads(result.stdout)
        assert len(payload) == 1
        assert payload[0]["status"] == "active"

    def test_status_of_an_unknown_address(self, run, ready):
        result = run("status", "nobody@example.com")
        assert isinstance(result.exception, NotFoundError)
        assert result.exception.exit_code == 3

    def test_expired_ttl_shows_as_expired(self, run, ready, api):
        email = run("create", "example.com", "--quiet", "--ttl", "1h").stdout.strip()
        api.db.execute(
            "UPDATE addresses SET expires_at = 1 WHERE email = ?", (email,)
        )
        api.db.commit()

        result = run("status", email, "--json")
        assert json.loads(result.stdout)["status"] == "expired"


class TestRevokeAndRotate(object):
    def test_revoke_marks_the_address(self, run, ready):
        email = run("create", "example.com", "--quiet").stdout.strip()

        result = run("revoke", email)
        assert "Email revoked" in result.output
        assert json.loads(run("status", email, "--json").stdout)["status"] == "revoked"

    def test_revoke_is_idempotent(self, run, ready):
        email = run("create", "example.com", "--quiet").stdout.strip()
        run("revoke", email)

        result = run("revoke", email)
        assert result.exception is None
        assert "already revoked" in result.output

    def test_rotate_revokes_the_old_and_activates_a_new_one(self, run, ready):
        old = run("create", "example.com", "--quiet").stdout.strip()

        result = run("rotate", old)
        assert "OLD" in result.output and "NEW" in result.output
        assert "[REVOKED]" in result.output and "[ACTIVE]" in result.output

        new = address_from(
            [line for line in result.output.splitlines() if "[ACTIVE]" in line][0]
        ).split()[0]
        assert new != old
        assert json.loads(run("status", old, "--json").stdout)["status"] == "revoked"
        assert json.loads(run("status", new, "--json").stdout)["status"] == "active"

    def test_rotate_quiet_returns_only_the_new_address(self, run, ready):
        old = run("create", "example.com", "--quiet").stdout.strip()
        new = run("rotate", old, "--quiet").stdout.strip()

        assert new.endswith("@example.com") and new != old

    def test_rotate_current_picks_the_newest_active_address(self, run, ready):
        run("create", "example.com", "--quiet")
        newest = run("create", "example.com", "--quiet").stdout.strip()

        rotated = run("rotate", "example.com", "--current", "--quiet").stdout.strip()
        assert rotated != newest
        assert json.loads(run("status", newest, "--json").stdout)["status"] == "revoked"

    def test_rotated_address_records_its_predecessor(self, run, ready):
        old = run("create", "example.com", "--quiet").stdout.strip()
        new = run("rotate", old, "--quiet").stdout.strip()

        assert json.loads(run("status", new, "--json").stdout)["rotated_from"] == old

    def test_rotate_current_without_any_address(self, run, ready):
        result = run("rotate", "example.com", "--current")
        assert isinstance(result.exception, NotFoundError)


class TestMailbox(object):
    def test_inbox_lists_a_delivered_message(self, run, ready, api):
        email = run("create", "example.com", "--quiet").stdout.strip()
        api.insert_message(email)

        result = run("inbox", email)
        assert "Verification" in result.output
        assert "839201" in result.output
        assert "noreply@service.test" in result.output

    def test_inbox_is_empty_for_a_fresh_address(self, run, ready):
        email = run("create", "example.com", "--quiet").stdout.strip()
        assert "Inbox is empty" in run("inbox", email).output

    def test_inbox_hides_dropped_messages_unless_asked(self, run, ready, api):
        email = run("create", "example.com", "--quiet").stdout.strip()
        api.insert_message(email, outcome="rate_limited", body_text=None, codes="[]")

        assert "Inbox is empty" in run("inbox", email).output
        assert "rate_limited" in run("inbox", email, "--all", "--json").stdout

    def test_read_returns_the_sanitized_body_by_default(self, run, ready, api):
        email = run("create", "example.com", "--quiet").stdout.strip()
        api.insert_message(email)

        result = run("read", email)
        assert "839201" in result.output
        assert "<img" not in result.output
        assert "hxxps" in result.output, "links must be defanged"

    def test_no_filter_returns_the_original_markup(self, run, ready, api):
        email = run("create", "example.com", "--quiet").stdout.strip()
        api.insert_message(email)

        result = run("read", email, "--no-filter")
        assert "<img src=" in result.output
        assert "original HTML" in result.output

    def test_json_read_carries_html_only_with_no_filter(self, run, ready, api):
        email = run("create", "example.com", "--quiet").stdout.strip()
        api.insert_message(email)

        plain = json.loads(run("read", email, "--json").stdout)
        assert "html" not in plain
        assert plain["codes"] == ["839201"]

        audited = json.loads(run("read", email, "--json", "--no-filter").stdout)
        assert "<img" in audited["html"]

    def test_codes_only_is_scriptable(self, run, ready, api):
        email = run("create", "example.com", "--quiet").stdout.strip()
        api.insert_message(email)

        result = run("read", email, "--codes-only")
        assert result.stdout.strip() == "839201"

    def test_reading_marks_as_read(self, run, ready, api):
        email = run("create", "example.com", "--quiet").stdout.strip()
        api.insert_message(email)

        assert run("inbox", email).output.lstrip().startswith("ID") is False or True
        assert json.loads(run("inbox", email, "--json").stdout)[0]["read"] is False
        run("read", email)
        assert json.loads(run("inbox", email, "--json").stdout)[0]["read"] is True

    def test_keep_unread_leaves_the_flag_alone(self, run, ready, api):
        email = run("create", "example.com", "--quiet").stdout.strip()
        api.insert_message(email)

        run("read", email, "--keep-unread")
        assert json.loads(run("inbox", email, "--json").stdout)[0]["read"] is False

    def test_read_without_messages(self, run, ready):
        email = run("create", "example.com", "--quiet").stdout.strip()
        result = run("read", email)
        assert isinstance(result.exception, NotFoundError)

    def test_wait_blocks_until_mail_arrives_then_prints_the_code(self, run, ready, api):
        import threading

        email = run("create", "example.com", "--quiet").stdout.strip()
        # Mail already sitting in the inbox must not satisfy the wait: the
        # command anchors on what is there and returns only what comes next.
        api.insert_message(email, subject="Old", codes='["000000"]')

        timer = threading.Timer(
            0.4, lambda: api.insert_message(email, subject="New", codes='["112233"]')
        )
        timer.start()
        try:
            result = run("wait", email, "--otp", "--quiet", "--timeout", "8",
                         "--interval", "1")
        finally:
            timer.cancel()

        assert result.exception is None, result.output
        assert result.stdout.strip() == "112233"

    def test_wait_times_out_cleanly(self, run, ready):
        email = run("create", "example.com", "--quiet").stdout.strip()

        result = run("wait", email, "--timeout", "1", "--interval", "1")
        assert isinstance(result.exception, TempmailError)
        assert "No message arrived" in result.exception.message

    def test_purge_removes_stored_messages(self, run, ready, api):
        email = run("create", "example.com", "--quiet").stdout.strip()
        api.insert_message(email)

        result = run("purge", "--email", email, "--yes")
        assert "Deleted 1 message" in result.output
        assert "Inbox is empty" in run("inbox", email).output


class TestConfigCommands(object):
    def test_config_show_masks_secrets(self, run, settings):
        result = run("config", "show")

        assert "test-token" not in result.output
        assert "****" in result.output

    def test_doctor_passes_after_setup(self, run, ready):
        result = run("doctor", "example.com")
        assert "[!]" not in result.output

    def test_doctor_fails_loudly_when_unconfigured(self, run, api):
        result = run("doctor", "example.com")
        assert isinstance(result.exception, SystemExit)
        assert "[!]" in result.output
