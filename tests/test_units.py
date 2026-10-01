"""Unit coverage for the pure helpers: naming, TTL parsing and the settings file."""

import os
import stat

import pytest

from tempmail import naming, output, store
from tempmail.config import Settings
from tempmail.errors import ConfigError, UsageError, mask
from tempmail.ttl import format_duration, parse_ttl


class TestNaming(object):
    def test_local_part_uses_unambiguous_alphabet(self):
        for _ in range(200):
            local = naming.generate_local_part()
            assert len(local) == 8
            assert not set(local) & set("01loi")

    def test_local_parts_do_not_repeat(self):
        # Not a statistical proof, but a sequential or seeded generator would
        # collide immediately here.
        generated = {naming.generate_local_part() for _ in range(500)}
        assert len(generated) == 500

    def test_never_issues_a_reserved_name(self, monkeypatch):
        # Drive the generator through a stubbed CSPRNG that spells a reserved
        # word first, proving the rejection loop actually rejects it.
        import itertools

        sequence = itertools.chain("postmaster", itertools.cycle("k8x4p2m9"))
        monkeypatch.setattr(naming.secrets, "choice", lambda alphabet: next(sequence))

        assert naming.generate_local_part(10) not in naming.RESERVED

    def test_rejects_short_length(self):
        with pytest.raises(UsageError):
            naming.generate_local_part(3)

    @pytest.mark.parametrize("value", ["not-an-email", "a@b@c.com", "@example.com",
                                       "user@", "user@invalid", "user@-bad.com"])
    def test_rejects_malformed_addresses(self, value):
        with pytest.raises(UsageError):
            naming.normalize_email(value)

    def test_normalizes_case_and_whitespace(self):
        email, local, domain = naming.normalize_email("  X7K2P9@Example.COM ")
        assert (email, local, domain) == ("x7k2p9@example.com", "x7k2p9", "example.com")

    def test_strip_plus_tag(self):
        assert naming.strip_plus_tag("x7k2p9+netflix") == "x7k2p9"
        assert naming.strip_plus_tag("x7k2p9") == "x7k2p9"

    def test_rejects_overlong_local_part(self):
        with pytest.raises(UsageError):
            naming.normalize_email("{0}@example.com".format("a" * 65))


class TestTtl(object):
    @pytest.mark.parametrize("value,expected", [
        ("30m", 1800), ("1h", 3600), ("24h", 86400), ("7d", 604800), ("90s", 90),
        ("1H", 3600), (None, None), ("", None),
    ])
    def test_parses(self, value, expected):
        assert parse_ttl(value) == expected

    @pytest.mark.parametrize("value", ["5x", "-1h", "h", "1.5h", "0m", "400d"])
    def test_rejects(self, value):
        with pytest.raises(UsageError):
            parse_ttl(value)

    def test_format_duration(self):
        assert format_duration(3600) == "1h"
        assert format_duration(9000) == "2h30m"
        assert format_duration(None) == "never"


class TestSettings(object):
    def test_file_is_created_private(self, settings_path):
        instance = Settings.load(settings_path)
        instance.data["cloudflare"]["api_token"] = "super-secret-token"
        path = instance.save()

        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700

    def test_environment_overrides_the_file(self, settings_path, monkeypatch):
        instance = Settings.load(settings_path)
        instance.data["cloudflare"]["api_token"] = "from-file"
        instance.save()

        assert Settings.load(settings_path).api_token == "from-file"
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "from-env")
        reloaded = Settings.load(settings_path)
        assert reloaded.api_token == "from-env"
        assert reloaded.api_token_source == "env"

    def test_missing_token_is_an_actionable_error(self, settings_path):
        with pytest.raises(ConfigError) as excinfo:
            Settings.load(settings_path).api_token
        assert "tempmail init" in excinfo.value.hint

    def test_redaction_hides_every_secret(self, settings_path):
        instance = Settings.load(settings_path)
        instance.data["cloudflare"]["api_token"] = "aaaaaaaaaaaaSECRET1"
        instance.data["slack"]["webhook_url"] = "https://hooks.slack.com/services/A/B/SECRET2"
        instance.set_domain("example.com", slack_webhook_url="https://hooks.slack.com/x/SECRET3")

        blob = str(instance.redacted())
        for secret in ("SECRET1", "SECRET2", "SECRET3"):
            assert secret not in blob
        assert instance.redacted()["cloudflare"]["api_token"] == "****RET1"

    def test_per_domain_webhook_wins_over_global(self, settings_path):
        instance = Settings.load(settings_path)
        instance.data["slack"]["webhook_url"] = "https://global"
        instance.set_domain("lab.test", slack_webhook_url="https://per-domain")

        assert instance.slack_webhook("lab.test") == "https://per-domain"
        assert instance.slack_webhook("other.test") == "https://global"

    def test_warns_when_file_is_group_readable(self, settings_path):
        instance = Settings.load(settings_path)
        instance.save()
        os.chmod(instance.path, 0o644)
        assert "chmod 600" in Settings.load(settings_path).permissions_warning()

    def test_corrupt_file_is_reported_not_crashed(self, settings_path):
        os.makedirs(os.path.dirname(settings_path))
        with open(settings_path, "w") as handle:
            handle.write("{not json")
        with pytest.raises(ConfigError):
            Settings.load(settings_path)


class TestMasking(object):
    def test_mask_keeps_only_a_suffix(self):
        assert mask("abcdefghijklmnop") == "****mnop"

    def test_short_values_are_fully_hidden(self):
        assert mask("abc") == "****"
        assert mask("") == "****"


class TestEffectiveStatus(object):
    def test_expired_by_clock_even_when_row_says_active(self):
        now = store.now()
        assert store.effective_status({"status": "active", "expires_at": now - 1}) == "expired"
        assert store.effective_status({"status": "active", "expires_at": now + 60}) == "active"

    def test_revoked_wins_over_expiry(self):
        assert store.effective_status({"status": "revoked", "expires_at": None}) == "revoked"


class TestOutput(object):
    def test_quiet_rendering_has_no_decoration(self):
        rendered = output.render_list([
            {"email": "a@x.test", "status": "active", "created_at": 1759200000},
        ])
        assert "a@x.test" in rendered

    def test_links_are_defanged_in_message_view(self):
        row = {
            "email": "a@x.test", "from_addr": "s@y.test", "subject": "Hi",
            "received_at": 1759200000, "raw_size": 10,
            "links": '["https://evil.test/path"]', "codes": "[]", "attachments": "[]",
            "body_text": "hello",
        }
        rendered = output.render_message(row)
        assert "https://evil.test" not in rendered
        assert "hxxps" in rendered

    def test_no_filter_shows_original_markup(self):
        row = {
            "email": "a@x.test", "from_addr": "s@y.test", "subject": "Hi",
            "received_at": 1759200000, "raw_size": 10,
            "links": "[]", "codes": "[]", "attachments": "[]",
            "body_text": "hello", "body_html": "<img src='https://t.test/p.gif'>",
        }
        assert "<img" in output.render_message(row, no_filter=True)
        assert "<img" not in output.render_message(row, no_filter=False)
