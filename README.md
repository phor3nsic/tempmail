# tempmail

Disposable email addresses on your own domain, using Cloudflare Email Routing,
an Email Worker and D1 — plus Slack notifications and a readable inbox in the
terminal.

```console
$ tempmail setup example.com
$ tempmail create example.com
Temporary email created:

  p7x9km2q@example.com

Status:  ACTIVE
Domain:  example.com
Created: 2026-09-30 18:30
```

The address works immediately. When mail arrives, an Email Worker checks that
the address is still active, parses the message, strips it of anything
executable, and both stores it for `tempmail read` and posts it to Slack.

No SMTP server, no always-on process, no build step. Cloudflare is the whole
receiving infrastructure.

---

## Install

```bash
pipx install tempmail-cf
```

```bash
pipx install git+https://github.com/phor3nsic/tempmail.git
```

From a checkout:

```bash
pipx install .
```

Requires Python 3.9+. Node is **not** required to use the CLI; it is only used
to run the Worker's own test suite.

---

## Cloudflare API token

Create a token at **My Profile → API Tokens → Create Token → Custom token**
with exactly these permissions:

| Scope   | Permission            | Access | Why |
|---------|-----------------------|--------|-----|
| Account | Workers Scripts       | Edit   | upload and update the Email Worker |
| Account | D1                    | Edit   | create the database and read/write addresses |
| Zone    | Zone                  | Read   | resolve the zone id from the domain name |
| Zone    | Zone Settings         | Edit   | read Email Routing status and enable it |
| Zone    | Email Routing Rules   | Edit   | read and set the catch-all rule |
| Zone    | DNS                   | Read   | check for MX records pointing at another provider |

**Zone Settings is the one people miss.** Cloudflare puts the Email Routing
*settings* endpoints (reading whether routing is on, and turning it on) under
`Zone Settings`, not under `Email Routing Rules` — that permission only covers
the routing rules themselves. Without it, setup stops at
`GET /zones/.../email/routing`.

`DNS` only needs **Read**: tempmail never writes DNS records itself. The
MX/SPF/DKIM records are created by Cloudflare when Email Routing is enabled; the
read is used to warn you if the domain already points at another mail provider.

Under **Zone Resources**, include only the domains you intend to use.

`tempmail setup` probes all six before changing anything and, if any are
missing, names every one of them in a single error rather than failing one at a
time. `tempmail doctor <domain>` reports the same check.

That is the full set. The CLI never needs Global API Key, account-wide admin,
or anything that can read other zones. If a call is refused, the error names the
specific permission that is missing.

---

## Configure

```bash
tempmail init
```

This writes `~/.config/tempmail/settings.json` with mode `600` inside a `700`
directory, and prompts for the token and Slack webhook. Non-interactively:

```bash
tempmail init --api-token "$CF_TOKEN" --slack-webhook "$SLACK_URL"
```

Environment variables override the file whenever they are set, which is what CI
and secret managers should use:

```bash
export CLOUDFLARE_API_TOKEN="..."
export SLACK_WEBHOOK_URL="..."
```

Inspect the configuration at any time — every secret is masked:

```console
$ tempmail config show
Config: /Users/you/.config/tempmail/settings.json
{
  "cloudflare": { "api_token": "****k3f9", ... }
}
```

A token is never written to a log, an error message or a traceback. See
`.env.example` for the full list of supported variables.

### Slack is optional

Without a webhook, messages are still stored and readable with
`tempmail inbox` / `tempmail read`. Set one up at
**Slack → Your apps → Incoming Webhooks** if you want push notifications.

---

## Set up a domain

```console
$ tempmail setup example.com
[+] API token verified
[+] Domain found: example.com
[+] Zone ID discovered
[+] Email Routing enabled
[+] MX records configured (3 records)
[+] D1 database created: tempmail
[+] Schema applied
[+] Email Worker deployed: tempmail-router
[+] Catch-all routing configured
[+] Slack integration configured

Ready. Create an address with: tempmail create example.com
```

`setup` is idempotent — running it again detects the existing state and changes
nothing. It also refuses to silently take over a domain that already receives
mail: if the zone has MX records pointing elsewhere, or a catch-all that
forwards to a real mailbox, it stops and asks. Use `--force` to proceed without
a prompt.

Useful options:

```bash
tempmail setup example.com --slack-webhook https://hooks.slack.com/...  # per-domain channel
tempmail setup example.com --rate-limit 5        # max messages/hour per address
tempmail setup example.com --retention 24h       # how long to keep messages
tempmail setup example.com --no-store            # Slack only; store nothing
```

### Domain requirements

Email Routing works at a **zone apex** only (outside Enterprise plans). So
`example.com` works, and `mail.example.com` works only if it is itself a zone in
your Cloudflare account. The CLI detects the subdomain case and tells you so,
rather than failing further down.

Multiple domains are supported and share one Worker and one database:

```bash
tempmail setup example.com
tempmail setup lab.example.net
```

---

## Daily use

```bash
tempmail create example.com                 # new address
tempmail create example.com --ttl 1h        # expires after an hour
tempmail create example.com --label signup  # note to yourself

tempmail list example.com                   # all addresses
tempmail list example.com --status active
tempmail status p7x9km2q@example.com

tempmail revoke p7x9km2q@example.com        # stop accepting mail
tempmail rotate p7x9km2q@example.com        # revoke and issue a new one
tempmail rotate example.com --current       # rotate the newest active address
```

Rotation output:

```text
OLD
  p7x9km2q@example.com  [REVOKED]

NEW
  m2q8vx7k@example.com  [ACTIVE]
```

The revoked address never becomes valid again.

---

## Reading mail from the terminal

```console
$ tempmail inbox p7x9km2q@example.com
 ID  RECEIVED          FROM                  CODE      SUBJECT
* 4  2026-09-30 18:42  noreply@service.test  493821    Verification

* = unread.  Read one with: tempmail read <email> --id <ID>

$ tempmail read p7x9km2q@example.com
From:     Service <noreply@service.test>
To:       p7x9km2q@example.com
Subject:  Verification
Received: 2026-09-30 18:42
Codes:    493821

Links (defanged, not clickable):
  hxxps://service[.]test/confirm?id=9f2

--- body ---
Your verification code is:

493821
```

`read` shows the newest message by default; pass `--id` for a specific one.

### Auditing the original HTML

`--no-filter` prints the message's HTML with its tags intact, so you can see
what the sender actually tried to render — tracking pixels, masked anchors,
hidden preheader text:

```console
$ tempmail read p7x9km2q@example.com --no-filter
--- original HTML (unrendered, for audit) ---
<html><body><p>Your login code is <b>493821</b>.</p>
<a href="https://phish.test/login">Verify your account</a>
<img src="https://tracker.test/open.gif?u=p7x9km2q" width="1" height="1">
</body></html>
```

It is printed as inert text. Nothing is ever rendered, fetched or executed.

### Waiting for mail (automation)

`wait` blocks until a *new* message arrives and prints the detected code — it
anchors on what is already in the inbox, so it never returns a stale one.

```bash
EMAIL=$(tempmail create example.com --quiet)

# ... trigger the signup that sends the code ...

OTP=$(tempmail wait "$EMAIL" --otp --quiet --timeout 120)
echo "$OTP"    # 493821
```

Every command takes `--json` for structured output, which is the easier surface
for an agent or a script:

```bash
tempmail create example.com --json
tempmail inbox "$EMAIL" --json
tempmail read "$EMAIL" --json --no-filter   # includes the raw HTML
tempmail read "$EMAIL" --codes-only         # just the codes, one per line
```

Scripted end to end:

```bash
EMAIL=$(tempmail create example.com --quiet)
OTP=$(tempmail wait "$EMAIL" --otp --quiet)
tempmail rotate "$EMAIL" --quiet
```

---

## Maintenance

```bash
tempmail doctor example.com     # read-only check of the whole setup
tempmail worker deploy          # re-upload the Worker after upgrading the CLI
tempmail purge --older-than 24h # delete stored messages
```

`doctor` verifies the token, the zone, Email Routing, the MX records, the
catch-all target, the Worker and the database, and exits non-zero if anything is
off:

```console
$ tempmail doctor example.com
[+] api_token       active
[+] zone            0a1b2c...
[+] email_routing   enabled
[+] mx_records      3 MX record(s)
[!] catch_all       forward:me@personal.test
    -> Run `tempmail setup example.com` to repoint it.
```

After upgrading the CLI, run `tempmail worker deploy` to push the new Worker.

---

## How it works

```text
sender ──SMTP──> Cloudflare MX ──> Email Routing catch-all
                                           │
                                   Email Worker (tempmail-router)
                                           │
                     ┌─────────────────────┼─────────────────────┐
                 D1 lookup            parse + sanitize       Slack webhook
            active? revoked? expired?   (no dependencies)     (Block Kit)
                                           │
                                     D1 messages table
                                   (tempmail inbox / read)
```

One catch-all rule per zone points at one Worker, which consults D1 for every
message. That is why a new address works instantly: nothing in Cloudflare's
configuration changes when you run `create` — only a row in the database. It
also avoids the 200-rules-per-domain limit entirely.

D1 rather than KV because it is strongly consistent (KV's propagation delay
would leave a window where mail to a brand new address is dropped) and because
`list`, `status` and `rotate --current` are plain SQL.

### Expiration

TTL is enforced at delivery time by the Worker, not by a scheduled job, so an
expired address stops accepting mail the moment it lapses regardless of when any
cron last ran.

---

## Security model

Every inbound message is treated as hostile.

- **Nothing is executed.** HTML is never rendered, scripts and styles are
  dropped from the readable body, and no remote resource is ever fetched.
- **Slack injection.** All untrusted fields (sender, subject, body) are sent as
  Block Kit `plain_text` objects, which Slack does not interpret as markup. The
  only `mrkdwn` the Worker emits is the code line, built from digits.
- **Link safety.** URLs are listed separately and defanged (`hxxps://`,
  `[.]`), and the payload sets `unfurl_links: false` so Slack never requests
  them.
- **Unicode spoofing.** Bidirectional overrides, zero-width characters and
  control characters are stripped from headers, so a reversed domain shows up as
  `paypal moc.lrepyap` instead of impersonating the real one.
- **Size and CPU limits.** Messages over 1 MiB (configurable) are dropped
  without parsing; part counts, nesting depth, entity expansion, body length and
  the Slack payload are all individually capped.
- **Address enumeration.** Mail to an unknown, revoked or expired address is
  accepted and discarded, never rejected — an SMTP rejection would confirm to a
  scanner which addresses are live.
- **Rate limiting.** Each address accepts a bounded number of messages per hour
  (default 20).
- **Attachments** are reported by name, type and size, and never decoded,
  stored or forwarded.
- **Secrets** live only in `settings.json` (mode 600) and in Worker secret
  bindings. The token is masked in every message the CLI can produce, and the
  Worker logs never contain the webhook URL or message content.
- **Retention.** Stored messages are deleted after the retention window
  (default 7 days); `tempmail purge` removes them on demand.

Storing message bodies is what makes `inbox`/`read` possible, and is a
deliberate trade-off: the D1 database holds sanitized text and, when enabled,
the original HTML. Use `--no-store` at setup for Slack-only operation.

---

## Configuration reference

`~/.config/tempmail/settings.json`:

```json
{
  "cloudflare": { "api_token": "", "account_id": "" },
  "slack": { "webhook_url": "" },
  "worker": { "name": "tempmail-router", "compatibility_date": "2026-09-01" },
  "d1": { "database_name": "tempmail", "database_id": "" },
  "defaults": {
    "ttl": null,
    "max_message_bytes": 1048576,
    "rate_limit_per_hour": 20,
    "strip_plus_tag": true,
    "slack_timeout_ms": 5000,
    "store_messages": true,
    "max_stored_body_chars": 16384,
    "store_html": true,
    "max_stored_html_chars": 65536,
    "retention_hours": 168
  },
  "domains": {
    "example.com": { "zone_id": "...", "slack_webhook_url": null }
  }
}
```

Anything under `defaults` can be overridden per domain inside `domains`.
`strip_plus_tag` keeps sub-addressing working: mail to
`p7x9km2q+netflix@example.com` is delivered to `p7x9km2q@example.com`.

Exit codes: `0` success, `1` error, `2` usage, `3` not found, `4` auth.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

The suite runs without network access: a fake Cloudflare API backs the D1
endpoint with in-memory SQLite, so the schema, upserts and the batched `rotate`
transaction are exercised for real. The Worker's own tests run its JavaScript
through Node against `.eml` fixtures (including a deliberately hostile one) and
skip automatically if Node is unavailable.

```text
src/tempmail/
  cli.py          commands
  config.py       settings.json, env precedence, masking
  provision.py    idempotent setup and doctor
  store.py        D1 schema and queries
  cloudflare/     API wrappers (zones, email routing, workers, d1)
  worker/tempmail_worker.mjs   the Email Worker, uploaded verbatim
```

## License

MIT
