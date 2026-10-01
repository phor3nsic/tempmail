"""Email Worker behaviour, exercised through its own JavaScript.

Node is only a runtime here: `worker_harness.mjs` calls the Worker's functions
and prints JSON, and the assertions stay in pytest. The CLI itself never needs
Node, so the whole module skips when it is unavailable.
"""

import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "worker_harness.mjs")
FIXTURES = os.path.join(HERE, "fixtures")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def harness(mode, argument):
    result = subprocess.run(
        ["node", HARNESS, mode, argument],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def fixture(name):
    return os.path.join(FIXTURES, name)


class TestParsing(object):
    def test_plain_text_message(self):
        parsed = harness("parse", fixture("simple.eml"))

        assert parsed["subject"] == "Verify your email"
        assert parsed["from"]["email"] == "noreply@service.test"
        assert "839201" in parsed["text"]

    def test_rfc2047_subjects_in_both_encodings(self):
        parsed = harness("parse", fixture("multipart.eml"))

        # B-encoded subject, Q-encoded display name.
        assert parsed["subject"] == "Verificação de acesso"
        assert parsed["from"]["name"] == "Serviço Segurança"

    def test_quoted_printable_and_multipart_alternative(self):
        parsed = harness("parse", fixture("multipart.eml"))

        assert "código de acesso é: 493821" in parsed["text"]
        assert "<b>493821</b>" in parsed["html"]

    def test_base64_body_in_a_legacy_charset(self):
        parsed = harness("parse", fixture("base64-latin1.eml"))
        assert "771903" in parsed["text"]

    def test_attachments_are_described_never_decoded(self):
        parsed = harness("parse", fixture("hostile.eml"))

        assert len(parsed["attachments"]) == 1
        attachment = parsed["attachments"][0]
        assert attachment["name"] == "invoice.pdf"
        assert attachment["type"] == "application/pdf"
        assert "content" not in attachment and "data" not in attachment

    def test_malformed_mime_does_not_raise(self):
        # The Worker falls back to headers only; the harness still has to parse
        # without throwing.
        parsed = harness("parse", fixture("malformed.eml"))
        assert "dangling boundary" in parsed["subject"]


class TestSanitization(object):
    def test_scripts_and_styles_never_reach_the_body(self):
        record = harness("record", fixture("hostile.eml"))

        assert "alert(" not in record["body"]
        assert "<script" not in record["body"]
        assert "document.cookie" not in record["body"]

    def test_bidi_and_zero_width_are_stripped_from_headers(self):
        record = harness("record", fixture("hostile.eml"))

        # A right-to-left override makes a spoofed domain render as the real
        # one. Once stripped, the deception is visible instead of hidden.
        for field in ("from", "from_name", "subject"):
            assert "‮" not in record[field]
            assert "​" not in record[field]
        assert record["subject"] == "Urgent: account locked"
        assert "moc.lrepyap" in record["from"]

    def test_control_characters_are_removed(self):
        record = harness("record", fixture("hostile.eml"))
        assert "\x07" not in record["subject"]

    def test_tracking_pixels_survive_in_the_audit_copy(self):
        record = harness("record", fixture("multipart.eml"))

        # The filtered body is for reading; the HTML copy is the evidence.
        assert "tracker.test" not in record["body"]
        assert "tracker.test" in record["body_html"]
        assert "<script>" in record["body_html"]

    def test_links_are_extracted_from_markup_too(self):
        record = harness("record", fixture("hostile.eml"))

        assert "https://phish.test/login" in record["links"]
        # A Slack-style <url|label> must not fold the label into the URL.
        assert all("|" not in link for link in record["links"])

    def test_codes_are_found_and_ranked(self):
        assert harness("record", fixture("simple.eml"))["codes"][0] == "839201"
        assert harness("record", fixture("multipart.eml"))["codes"][0] == "493821"
        assert harness("record", fixture("base64-latin1.eml"))["codes"][0] == "771903"


class TestSlackPayload(object):
    def test_untrusted_fields_are_plain_text_blocks(self):
        payload = harness("slack", fixture("hostile.eml"))

        for block in payload["blocks"]:
            text = block.get("text")
            if text and text["type"] == "mrkdwn":
                # The only mrkdwn we emit is the code line, built from digits.
                assert "Code" in text["text"]
                assert "<!channel>" not in text["text"]

    def test_slack_control_sequences_cannot_be_injected(self):
        payload = harness("slack", fixture("hostile.eml"))
        body_blocks = [
            b for b in payload["blocks"]
            if b.get("text", {}).get("type") == "plain_text"
        ]

        # The payload still contains the characters; what matters is that they
        # sit in plain_text objects, which Slack does not interpret.
        assert any("<!channel>" in b["text"]["text"] for b in body_blocks)
        assert all(b["text"]["type"] == "plain_text" for b in body_blocks)

    def test_unfurling_is_disabled(self):
        payload = harness("slack", fixture("hostile.eml"))

        assert payload["unfurl_links"] is False
        assert payload["unfurl_media"] is False

    def test_links_are_defanged(self):
        payload = harness("slack", fixture("hostile.eml"))
        rendered = json.dumps(payload)

        assert "hxxps://phish[.]test/login" in rendered
        assert "https://phish.test/login" not in rendered


class TestRecipientResolution(object):
    def test_plus_tags_resolve_to_the_base_address(self):
        results = harness(
            "recipient", "x7k2p9+netflix@example.com,X7K2P9@EXAMPLE.COM,bad,@example.com"
        )
        by_input = {item["input"]: item["resolved"] for item in results}

        assert by_input["x7k2p9+netflix@example.com"]["email"] == "x7k2p9@example.com"
        assert by_input["bad"] is None
        assert by_input["@example.com"] is None

    def test_uppercase_envelope_is_normalized(self):
        results = harness("recipient", "X7K2P9@EXAMPLE.COM")
        # The envelope is lowercased before it reaches resolveRecipient; this
        # asserts the function does not reintroduce case sensitivity.
        assert results[0]["resolved"]["domain"] == "example.com"


class TestLimits(object):
    def test_every_unbounded_field_is_capped(self):
        limits = harness("limits", "-")

        assert limits["truncated"] < 1100
        assert limits["sanitizedLength"] < 2600
        assert limits["manyLinks"] == 5
        assert limits["deepCodes"] == 3
