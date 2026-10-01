"""The `tempmail` command line.

Design rule for every command: stdout carries the answer, stderr carries the
narration. That is what makes the scripting flow in the README work, and what
lets an agent consume `--json` without filtering noise out of it.
"""

import contextlib
import json
import os
import sys
import time

import click

from . import __version__, output, provision, store as store_module
from .cloudflare import CloudflareClient
from .config import ENV_API_TOKEN, Settings, default_config_path
from .errors import ConfigError, NotFoundError, TempmailError, UsageError
from .naming import generate_local_part, normalize_domain, normalize_email
from .ttl import parse_ttl

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 96}


def _confirm(prompt):
    """Ask before anything destructive, and refuse by default when not a TTY."""
    if not sys.stdin.isatty():
        return False
    return click.confirm("    {0}".format(prompt), default=False, err=True)


@contextlib.contextmanager
def _client(settings):
    client = CloudflareClient(settings.api_token)
    try:
        yield client
    finally:
        client.close()


def _open_store(settings, client):
    if not settings.account_id or not settings.database_id:
        raise ConfigError(
            "No provisioned backend found in {0}".format(settings.path),
            hint="Run `tempmail setup <domain>` first.",
        )
    return store_module.Store(client, settings.account_id, settings.database_id)


@contextlib.contextmanager
def _session(settings):
    with _client(settings) as client:
        yield client, _open_store(settings, client)


def _resolve_target(store, email_or_domain, use_current):
    """Accept either an address or, with --current, a domain."""
    if use_current:
        domain = normalize_domain(email_or_domain)
        row = store.current_active(domain)
        if not row:
            raise NotFoundError(
                "No active address for {0}".format(domain),
                hint="Create one with `tempmail create {0}`".format(domain),
            )
        return row
    email, _, _ = normalize_email(email_or_domain)
    return store.require_address(email)


@click.group(context_settings=CONTEXT_SETTINGS)
@click.option("--config", "config_path", default=None,
              help="Path to settings.json (default: {0}).".format(default_config_path()))
@click.version_option(__version__, "-V", "--version", prog_name="tempmail")
@click.pass_context
def cli(ctx, config_path):
    """Disposable email addresses on your own domain, via Cloudflare.

    Configuration lives in ~/.config/tempmail/settings.json. The environment
    variables CLOUDFLARE_API_TOKEN and SLACK_WEBHOOK_URL override it when set.
    """
    settings = Settings.load(config_path)
    warning = settings.permissions_warning()
    if warning:
        output.warn(warning)
    ctx.obj = {"settings": settings}


# --------------------------------------------------------------- configuration


@cli.command()
@click.option("--api-token", default=None, help="Cloudflare API token.")
@click.option("--slack-webhook", default=None, help="Slack Incoming Webhook URL.")
@click.option("--account-id", default=None, help="Pin the Cloudflare account id.")
@click.option("--force", is_flag=True, help="Overwrite an existing settings file.")
@click.pass_context
def init(ctx, api_token, slack_webhook, account_id, force):
    """Create ~/.config/tempmail/settings.json (mode 600)."""
    settings = ctx.obj["settings"]

    if settings.exists and not force:
        output.note("Settings file already exists: {0}".format(settings.path))
        output.note("Pass --force to overwrite, or edit the file directly.")
        return

    if api_token is None and sys.stdin.isatty():
        api_token = click.prompt(
            "Cloudflare API token", hide_input=True, default="", show_default=False
        )
    if slack_webhook is None and sys.stdin.isatty():
        slack_webhook = click.prompt(
            "Slack webhook URL (optional)", default="", show_default=False
        )

    if api_token:
        settings.data["cloudflare"]["api_token"] = api_token.strip()
    if account_id:
        settings.account_id = account_id.strip()
    if slack_webhook:
        settings.data["slack"]["webhook_url"] = slack_webhook.strip()

    path = settings.save()
    output.step("Wrote {0} (mode 600)".format(path))

    if not settings.data["cloudflare"]["api_token"]:
        output.note(
            "No token stored. Export {0} before running setup.".format(ENV_API_TOKEN)
        )
    output.echo(path)


@cli.group()
def config():
    """Inspect the configuration."""


@config.command("show")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def config_show(ctx, as_json):
    """Print the settings with every secret masked."""
    settings = ctx.obj["settings"]
    payload = settings.redacted()
    payload["_path"] = settings.path
    payload["_exists"] = settings.exists
    configured = bool(
        settings.data["cloudflare"].get("api_token") or os.environ.get(ENV_API_TOKEN)
    )
    payload["_token_source"] = settings.api_token_source if configured else "unset"

    if as_json:
        output.dump_json(payload)
        return

    output.echo("Config: {0}".format(settings.path))
    output.echo("")
    output.echo(json.dumps(payload, indent=2))


@config.command("path")
@click.pass_context
def config_path_cmd(ctx):
    """Print the settings file path."""
    output.echo(ctx.obj["settings"].path)


# ---------------------------------------------------------------------- setup


@cli.command()
@click.argument("domain")
@click.option("--slack-webhook", default=None,
              help="Slack webhook for this domain (overrides the global one).")
@click.option("--rate-limit", type=int, default=None,
              help="Max messages accepted per address per hour.")
@click.option("--max-size", type=int, default=None,
              help="Max inbound message size in bytes (Cloudflare caps at 25 MiB).")
@click.option("--retention", default=None,
              help="How long to keep stored messages, e.g. 24h or 7d.")
@click.option("--no-store", is_flag=True,
              help="Do not store message bodies; notify Slack only.")
@click.option("--force", is_flag=True,
              help="Do not ask before taking over existing email configuration.")
@click.pass_context
def setup(ctx, domain, slack_webhook, rate_limit, max_size, retention, no_store, force):
    """Provision DOMAIN end to end. Safe to run repeatedly."""
    settings = ctx.obj["settings"]
    domain = normalize_domain(domain)

    overrides = {}
    if rate_limit is not None:
        overrides["rate_limit_per_hour"] = rate_limit
    if max_size is not None:
        overrides["max_message_bytes"] = max_size
    if retention is not None:
        overrides["retention_hours"] = max(1, parse_ttl(retention) // 3600)
    if no_store:
        overrides["store_messages"] = False
    if overrides:
        settings.set_domain(domain, **overrides)

    with _client(settings) as client:
        result = provision.provision(
            settings, client, domain,
            slack_webhook=slack_webhook, force=force, confirm=_confirm,
        )

    settings.save()
    output.echo("")
    output.echo("Ready. Create an address with: tempmail create {0}".format(result["domain"]))


@cli.command()
@click.argument("domain")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def doctor(ctx, domain, as_json):
    """Check DOMAIN's configuration without changing anything."""
    settings = ctx.obj["settings"]
    with _client(settings) as client:
        checks = provision.diagnose(settings, client, domain)

    if as_json:
        output.dump_json({"domain": domain, "checks": checks})
    else:
        for check in checks:
            output.echo(
                "{0} {1:<15} {2}".format(
                    "[+]" if check["ok"] else "[!]", check["check"], check["detail"]
                )
            )
            if check["hint"] and not check["ok"]:
                output.echo("    -> {0}".format(check["hint"]))

    if any(not check["ok"] for check in checks):
        raise SystemExit(1)


@cli.group()
def worker():
    """Manage the Email Worker."""


@worker.command("deploy")
@click.pass_context
def worker_deploy(ctx):
    """Re-upload the Worker, e.g. after upgrading the CLI."""
    settings = ctx.obj["settings"]
    if not settings.account_id or not settings.database_id:
        raise ConfigError(
            "Nothing provisioned yet", hint="Run `tempmail setup <domain>` first."
        )

    with _client(settings) as client:
        name = provision.deploy_worker(
            settings, client, settings.account_id, settings.database_id
        )
    output.step("Email Worker deployed: {0}".format(name))
    output.echo(name)


# ------------------------------------------------------------------ addresses


@cli.command()
@click.argument("domain")
@click.option("--ttl", default=None, help="Expire the address after e.g. 30m, 1h, 24h.")
@click.option("--label", default=None, help="Free-form note stored with the address.")
@click.option("--length", type=int, default=8, help="Length of the random local part.")
@click.option("--quiet", "-q", is_flag=True, help="Print only the address.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def create(ctx, domain, ttl, label, length, quiet, as_json):
    """Create a new temporary address on DOMAIN."""
    settings = ctx.obj["settings"]
    domain = normalize_domain(domain)
    ttl_seconds = parse_ttl(ttl if ttl is not None else settings.domain_setting(domain, "ttl"))

    with _session(settings) as (client, store):
        store.require_domain(domain)

        expires_at = store_module.now() + ttl_seconds if ttl_seconds else None
        # A collision is vanishingly unlikely, but an address handed out twice
        # would silently cross two sessions' mail, so we check rather than hope.
        for _ in range(5):
            local_part = generate_local_part(length)
            email = "{0}@{1}".format(local_part, domain)
            if not store.exists(email):
                break
        else:
            raise TempmailError("Could not generate a free address; try --length 10")

        row = store.create_address(
            email, domain, local_part, expires_at=expires_at, label=label
        )

    if quiet:
        output.echo(row["email"])
    elif as_json:
        output.dump_json(output.address_payload(row))
    else:
        output.echo(output.render_created(row))


@cli.command("list")
@click.argument("domain", required=False)
@click.option("--status", default=None,
              help="Filter by active, revoked or expired.")
@click.option("--limit", type=int, default=50, help="Maximum rows to show.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def list_cmd(ctx, domain, status, limit, as_json):
    """List addresses, optionally for one DOMAIN."""
    settings = ctx.obj["settings"]
    domain = normalize_domain(domain) if domain else None

    with _session(settings) as (client, store):
        rows = store.list_addresses(domain=domain, status=status, limit=limit)

    if as_json:
        output.dump_json([output.address_payload(row) for row in rows])
    else:
        output.echo(output.render_list(rows))


@cli.command()
@click.argument("email")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def status(ctx, email, as_json):
    """Show the state of one address."""
    settings = ctx.obj["settings"]
    email, _, _ = normalize_email(email)

    with _session(settings) as (client, store):
        row = store.require_address(email)
        unread = store.unread_count(email)

    if as_json:
        payload = output.address_payload(row)
        payload["unread"] = unread
        output.dump_json(payload)
    else:
        output.echo(output.render_status(row, unread=unread))


@cli.command()
@click.argument("email")
@click.option("--quiet", "-q", is_flag=True, help="Print only the address.")
@click.pass_context
def revoke(ctx, email, quiet):
    """Revoke an address. Later mail for it is discarded."""
    settings = ctx.obj["settings"]
    email, _, _ = normalize_email(email)

    with _session(settings) as (client, store):
        row, changed = store.revoke(email)

    if quiet:
        output.echo(row["email"])
        return

    output.echo("")
    output.echo("Email {0}:".format("revoked" if changed else "already revoked"))
    output.echo("")
    output.echo("  {0}".format(row["email"]))
    output.echo("")


@cli.command()
@click.argument("target")
@click.option("--current", "use_current", is_flag=True,
              help="Treat TARGET as a domain and rotate its newest active address.")
@click.option("--ttl", default=None, help="TTL for the new address, e.g. 1h.")
@click.option("--label", default=None, help="Label for the new address.")
@click.option("--quiet", "-q", is_flag=True, help="Print only the new address.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def rotate(ctx, target, use_current, ttl, label, quiet, as_json):
    """Revoke an address and issue a fresh one in its place."""
    settings = ctx.obj["settings"]

    with _session(settings) as (client, store):
        old_row = _resolve_target(store, target, use_current)
        domain = old_row["domain"]
        ttl_seconds = parse_ttl(
            ttl if ttl is not None else settings.domain_setting(domain, "ttl")
        )
        expires_at = store_module.now() + ttl_seconds if ttl_seconds else None

        for _ in range(5):
            local_part = generate_local_part()
            new_email = "{0}@{1}".format(local_part, domain)
            if not store.exists(new_email):
                break
        else:
            raise TempmailError("Could not generate a free address")

        new_row = store.rotate(
            old_row["email"], new_email, domain, local_part,
            expires_at=expires_at, label=label or old_row.get("label"),
        )
        old_row["status"] = store_module.STATUS_REVOKED

    if quiet:
        output.echo(new_row["email"])
    elif as_json:
        output.dump_json(
            {
                "old": output.address_payload(old_row),
                "new": output.address_payload(new_row),
            }
        )
    else:
        output.echo(output.render_rotation(old_row, new_row))


# -------------------------------------------------------------------- mailbox


@cli.command()
@click.argument("email", required=False)
@click.option("--current", "use_current", default=None,
              help="Use the newest active address of this domain.")
@click.option("--limit", type=int, default=20, help="Maximum messages to show.")
@click.option("--unread", is_flag=True, help="Only messages not yet read.")
@click.option("--all", "show_all", is_flag=True,
              help="Include dropped messages (revoked, expired, rate limited).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def inbox(ctx, email, use_current, limit, unread, show_all, as_json):
    """List messages received by an address."""
    settings = ctx.obj["settings"]

    with _session(settings) as (client, store):
        if use_current:
            email = _resolve_target(store, use_current, True)["email"]
        elif email:
            email, _, _ = normalize_email(email)
        else:
            raise UsageError("Provide an address, or --current <domain>")

        rows = store.list_messages(
            email=email, limit=limit, unread_only=unread, readable_only=not show_all
        )

    if as_json:
        output.dump_json([output.message_payload(row) for row in rows])
    else:
        output.echo(output.render_inbox(rows, email))


@cli.command()
@click.argument("email")
@click.option("--id", "message_id", type=int, default=None,
              help="Message id from `tempmail inbox` (default: the newest).")
@click.option("--no-filter", "no_filter", is_flag=True,
              help="Show the original HTML with tags intact, for auditing. "
                   "It is printed as inert text and never rendered.")
@click.option("--codes-only", is_flag=True, help="Print only the detected codes.")
@click.option("--keep-unread", is_flag=True, help="Do not mark the message as read.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def read(ctx, email, message_id, no_filter, codes_only, keep_unread, as_json):
    """Read one message in full."""
    settings = ctx.obj["settings"]
    email, _, _ = normalize_email(email)

    with _session(settings) as (client, store):
        if message_id is not None:
            row = store.require_message(message_id, email=email)
        else:
            row = store.latest_message(email)
            if not row:
                raise NotFoundError("No messages for {0}".format(email))
        if not keep_unread and row.get("id"):
            store.mark_read(row["id"])

    if codes_only:
        for code in output.json_field(row.get("codes"), []):
            output.echo(code)
        return

    if as_json:
        output.dump_json(
            output.message_payload(row, include_body=True, include_html=no_filter)
        )
    else:
        output.echo(output.render_message(row, no_filter=no_filter))


@cli.command()
@click.argument("email", required=False)
@click.option("--current", "use_current", default=None,
              help="Wait on the newest active address of this domain.")
@click.option("--timeout", type=int, default=120, help="Seconds to wait (default 120).")
@click.option("--interval", type=int, default=3, help="Seconds between polls.")
@click.option("--otp", is_flag=True, help="Print the detected code instead of the message.")
@click.option("--no-filter", "no_filter", is_flag=True,
              help="Show the original HTML with tags intact.")
@click.option("--quiet", "-q", is_flag=True, help="Print only the result value.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def wait(ctx, email, use_current, timeout, interval, otp, no_filter, quiet, as_json):
    """Block until a new message arrives, then print it.

    Built for automation: OTP=$(tempmail wait "$EMAIL" --otp --quiet)
    """
    settings = ctx.obj["settings"]
    interval = max(1, interval)
    deadline = time.time() + max(1, timeout)

    with _session(settings) as (client, store):
        if use_current:
            email = _resolve_target(store, use_current, True)["email"]
        elif email:
            email, _, _ = normalize_email(email)
        else:
            raise UsageError("Provide an address, or --current <domain>")

        # Anchor on the newest id already present so we return only mail that
        # arrives from now on, not whatever was sitting in the inbox.
        existing = store.list_messages(email=email, limit=1)
        since_id = existing[0]["id"] if existing else 0

        if not quiet:
            output.note("Waiting up to {0}s for mail to {1} ...".format(timeout, email))

        row = None
        while True:
            found = store.list_messages(email=email, limit=1, since_id=since_id)
            if found:
                row = store.get_message(found[0]["id"], email=email)
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(interval, max(0.1, remaining)))

        if row and row.get("id"):
            store.mark_read(row["id"])

    if not row:
        raise TempmailError(
            "No message arrived for {0} within {1}s".format(email, timeout)
        )

    codes = output.json_field(row.get("codes"), [])
    if otp:
        if not codes:
            raise NotFoundError("A message arrived but no code was detected in it")
        output.echo(codes[0])
        return

    if as_json:
        output.dump_json(
            output.message_payload(row, include_body=True, include_html=no_filter)
        )
    elif quiet:
        output.echo(row.get("body_text") or "")
    else:
        output.echo(output.render_message(row, no_filter=no_filter))


@cli.command()
@click.option("--email", default=None, help="Only purge this address's messages.")
@click.option("--older-than", default=None,
              help="Only purge messages older than e.g. 24h (default: everything matched).")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_context
def purge(ctx, email, older_than, yes):
    """Delete stored messages."""
    settings = ctx.obj["settings"]
    seconds = parse_ttl(older_than) if older_than else None

    if email:
        email, _, _ = normalize_email(email)

    scope = email or "all addresses"
    window = "older than {0}".format(older_than) if older_than else "all messages"
    if not yes and not _confirm("Delete {0} for {1}?".format(window, scope)):
        raise TempmailError("Aborted")

    with _session(settings) as (client, store):
        deleted = store.purge_messages(older_than_seconds=seconds, email=email)

    output.echo("Deleted {0} message(s).".format(deleted))


def main():
    try:
        cli(standalone_mode=False)
    except TempmailError as exc:
        output.echo("Error: {0}".format(exc.message), err=True)
        if exc.hint:
            output.echo("Hint:  {0}".format(exc.hint), err=True)
        sys.exit(exc.exit_code)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.exceptions.Abort:
        output.echo("Aborted.", err=True)
        sys.exit(130)
    except KeyboardInterrupt:
        output.echo("", err=True)
        sys.exit(130)
    return 0


if __name__ == "__main__":
    main()
