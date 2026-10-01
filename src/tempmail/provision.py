"""Idempotent provisioning of a domain, and the read-only diagnostics behind
`tempmail doctor`.

Every mutating step first asks Cloudflare what the current state is, so running
`setup` twice is a no-op rather than a second set of resources. The one step
that always runs is the Worker upload, which is idempotent by nature: same
script name, same content, new version.
"""

import json
import os

from . import store as store_module
from .cloudflare import d1, email_routing, workers, zones
from .errors import AuthError, ConfigError, TempmailError
from .naming import normalize_domain
from .output import note, step, warn

WORKER_FILENAME = "tempmail_worker.mjs"

# settings.json uses snake_case; the Worker's CONFIG binding uses camelCase.
_CONFIG_KEYS = {
    "max_message_bytes": "maxMessageBytes",
    "rate_limit_per_hour": "rateLimitPerHour",
    "strip_plus_tag": "stripPlusTag",
    "slack_timeout_ms": "slackTimeoutMs",
    "store_messages": "storeMessages",
    "max_stored_body_chars": "maxStoredBodyChars",
    "store_html": "storeHtml",
    "max_stored_html_chars": "maxStoredHtmlChars",
    "retention_hours": "retentionHours",
}


def load_worker_source():
    """Read the Worker module shipped as package data."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker", WORKER_FILENAME)
    try:
        with open(path, "r") as handle:
            return handle.read()
    except OSError as exc:
        raise ConfigError(
            "Could not read the Worker source at {0}: {1}".format(path, exc.strerror),
            hint="Reinstall the package: pipx reinstall tempmail-cf",
        )


def webhook_binding_name(domain):
    """Mirror of the Worker's lookup: example.com -> SLACK_WEBHOOK_EXAMPLE_COM."""
    slug = "".join(ch if ch.isalnum() else "_" for ch in domain).upper()
    return "SLACK_WEBHOOK_{0}".format(slug)


def _section(source, keys):
    out = {}
    for snake, camel in _CONFIG_KEYS.items():
        if snake in keys and source.get(snake) is not None:
            out[camel] = source[snake]
    return out


def worker_config_payload(settings):
    """The CONFIG binding: global defaults plus any per-domain overrides."""
    payload = {"defaults": _section(settings.defaults, set(_CONFIG_KEYS)), "domains": {}}
    for domain, entry in (settings.data.get("domains") or {}).items():
        overrides = _section(entry or {}, set(_CONFIG_KEYS))
        if overrides:
            payload["domains"][domain] = overrides
    return payload


def worker_bindings(settings, database_id):
    """Bindings re-sent on every upload.

    A PUT replaces the Worker's whole configuration, so leaving a binding out
    would delete it. Every value is already in settings.json, which is why we
    never need to read a secret back out of Cloudflare.
    """
    bindings = [
        workers.d1_binding("DB", database_id),
        workers.plain_binding("CONFIG", json.dumps(worker_config_payload(settings))),
    ]

    global_webhook = settings.slack_webhook()
    if global_webhook:
        bindings.append(workers.secret_binding("SLACK_WEBHOOK_URL", global_webhook))

    for domain, entry in (settings.data.get("domains") or {}).items():
        override = (entry or {}).get("slack_webhook_url")
        if override:
            bindings.append(
                workers.secret_binding(webhook_binding_name(domain), override)
            )
    return bindings


# Each probe is a cheap read that exercises exactly one permission group. A
# token cannot be asked what it is allowed to do, so the only way to report
# every missing permission at once is to try each area before mutating anything.
def _probe(client, description, path, params=None):
    try:
        client.get(path, params=params)
    except AuthError:
        return description
    except TempmailError:
        # 404 or "not configured yet" means authorised but absent, which is
        # exactly the state setup is about to fix.
        return None
    return None


def preflight(client, zone_id, account_id, worker_name):
    """Return the token permissions that are missing, as display strings."""
    probes = (
        ("Zone > Zone Settings : Edit",
         "/zones/{0}/email/routing".format(zone_id), None),
        ("Zone > Email Routing Rules : Edit",
         "/zones/{0}/email/routing/rules/catch_all".format(zone_id), None),
        ("Zone > DNS : Read",
         "/zones/{0}/dns_records".format(zone_id), {"type": "MX", "per_page": 1}),
        ("Account > D1 : Edit",
         "/accounts/{0}/d1/database".format(account_id), {"per_page": 1}),
        ("Account > Workers Scripts : Edit",
         "/accounts/{0}/workers/scripts/{1}".format(account_id, worker_name), None),
    )

    missing = []
    for description, path, params in probes:
        result = _probe(client, description, path, params)
        if result:
            missing.append(result)
    return missing


def ensure_database(settings, client, account_id):
    """Find or create the D1 database, caching its id in settings.json."""
    database_id = settings.database_id
    if database_id and d1.get_database(client, account_id, database_id):
        return database_id, False

    existing = d1.find_database(client, account_id, settings.database_name)
    if existing:
        settings.database_id = existing.get("uuid") or existing.get("id")
        return settings.database_id, False

    created = d1.create_database(client, account_id, settings.database_name)
    settings.database_id = created.get("uuid") or created.get("id")
    if not settings.database_id:
        raise TempmailError("Cloudflare did not return an id for the new D1 database")
    return settings.database_id, True


def deploy_worker(settings, client, account_id, database_id):
    workers.upload_script(
        client,
        account_id,
        settings.worker_name,
        load_worker_source(),
        worker_bindings(settings, database_id),
        settings.compatibility_date,
    )
    return settings.worker_name


def ensure_email_routing(client, zone_id, domain, force, confirm):
    """Enable Email Routing, refusing to hijack an existing mail setup silently."""
    current = email_routing.get_settings(client, zone_id)
    if email_routing.is_enabled(current):
        return False

    foreign = email_routing.foreign_mx_records(
        email_routing.list_mx_records(client, zone_id)
    )
    if foreign and not force:
        hosts = ", ".join(sorted(set(r.get("content", "?") for r in foreign)))
        warn("{0} already has MX records pointing elsewhere: {1}".format(domain, hosts))
        warn("Enabling Email Routing will take over inbound mail for this domain.")
        if not confirm("Enable Email Routing anyway?"):
            raise TempmailError(
                "Aborted: {0} already receives mail elsewhere".format(domain),
                hint="Re-run with --force to proceed non-interactively.",
            )

    email_routing.enable(client, zone_id)
    return True


def ensure_catch_all(client, zone_id, worker_name, force, confirm):
    """Point the catch-all at our Worker, asking first if it forwards somewhere."""
    current = email_routing.get_catch_all(client, zone_id)
    if email_routing.catch_all_worker(current) == worker_name and current.get("enabled"):
        return False

    if current and current.get("enabled"):
        existing = email_routing.catch_all_description(current)
        if "forward" in existing and not force:
            warn("The catch-all rule currently does: {0}".format(existing))
            if not confirm("Replace it with the tempmail Worker?"):
                raise TempmailError(
                    "Aborted: catch-all rule left untouched",
                    hint="Re-run with --force to proceed non-interactively.",
                )

    email_routing.set_catch_all_to_worker(client, zone_id, worker_name)
    return True


def provision(settings, client, domain, slack_webhook=None, force=False, confirm=None):
    """Run the whole setup for one domain. Returns a summary dict."""
    confirm = confirm or (lambda _prompt: False)
    domain = normalize_domain(domain)

    client.verify_token()
    step("API token verified")

    zone = zones.resolve_zone(client, domain)
    zone_id = zone["id"]
    step("Domain found: {0}".format(domain))

    account_id = settings.account_id or zones.account_id_for_zone(zone)
    if not account_id:
        raise ConfigError(
            "Could not determine the Cloudflare account id for {0}".format(domain),
            hint="Set it in settings.json under cloudflare.account_id, or export "
            "CLOUDFLARE_ACCOUNT_ID.",
        )
    settings.account_id = account_id
    step("Zone ID discovered")

    missing = preflight(client, zone_id, account_id, settings.worker_name)
    if missing:
        raise AuthError(
            "The API token is missing {0} permission(s): {1}".format(
                len(missing), "; ".join(missing)
            ),
            hint="Add them at My Profile > API Tokens > your token > Edit, and make "
            "sure Zone Resources includes {0}. See README > Cloudflare API "
            "token.".format(domain),
        )
    step("Token permissions verified")

    if slack_webhook:
        settings.set_domain(domain, slack_webhook_url=slack_webhook)

    enabled = ensure_email_routing(client, zone_id, domain, force, confirm)
    step("Email Routing {0}".format("enabled" if enabled else "already enabled"))

    records = email_routing.required_dns_records(client, zone_id)
    missing = [r for r in records if r.get("required") and not _record_ok(r)]
    if missing:
        warn(
            "{0} DNS record(s) still propagating or missing; mail may bounce for a "
            "few minutes.".format(len(missing))
        )
    step("MX records configured ({0} records)".format(len(records)))

    database_id, created_db = ensure_database(settings, client, account_id)
    step(
        "D1 database {0}: {1}".format(
            "created" if created_db else "ready", settings.database_name
        )
    )

    store = store_module.Store(client, account_id, database_id)
    store.ensure_schema()
    step("Schema applied")

    # The Worker is deployed before the catch-all points at it, so there is no
    # window where mail is routed to a script that does not exist yet.
    worker_name = deploy_worker(settings, client, account_id, database_id)
    step("Email Worker deployed: {0}".format(worker_name))

    changed = ensure_catch_all(client, zone_id, worker_name, force, confirm)
    step("Catch-all routing {0}".format("configured" if changed else "already correct"))

    store.upsert_domain(
        domain,
        zone_id,
        worker_name,
        settings.domain_setting(domain, "max_message_bytes"),
        settings.domain_setting(domain, "rate_limit_per_hour"),
        settings.domain_setting(domain, "strip_plus_tag"),
    )
    settings.set_domain(domain, zone_id=zone_id)

    if settings.slack_webhook(domain):
        step("Slack integration configured")
    else:
        note("No Slack webhook set: messages will be stored for `tempmail inbox` only.")

    return {
        "domain": domain,
        "zone_id": zone_id,
        "account_id": account_id,
        "database_id": database_id,
        "worker_name": worker_name,
        "slack": bool(settings.slack_webhook(domain)),
    }


def _record_ok(record):
    return str(record.get("status") or "").lower() in ("", "ok", "valid", "active")


def diagnose(settings, client, domain):
    """Read-only checks for `tempmail doctor`. Returns a list of check dicts."""
    domain = normalize_domain(domain)
    checks = []

    def record(name, ok, detail, hint=None):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "hint": hint})

    try:
        client.verify_token()
        record("api_token", True, "active")
    except TempmailError as exc:
        record("api_token", False, exc.message, exc.hint)
        return checks

    try:
        zone = zones.resolve_zone(client, domain)
    except TempmailError as exc:
        record("zone", False, exc.message, exc.hint)
        return checks
    record("zone", True, zone["id"])

    account_id = settings.account_id or zones.account_id_for_zone(zone)

    missing = preflight(client, zone["id"], account_id, settings.worker_name)
    record(
        "permissions",
        not missing,
        "all required" if not missing else "missing: {0}".format("; ".join(missing)),
        None if not missing else "Add them to the API token, then re-run.",
    )
    if missing:
        # Every remaining check would fail for the same reason; saying so once
        # is more useful than a wall of derived failures.
        return checks

    try:
        _inspect_domain(settings, client, zone, account_id, domain, record)
    except TempmailError as exc:
        # doctor is a diagnostic: it reports problems, it does not raise them.
        record("diagnostics", False, exc.message, exc.hint)
    return checks


def _inspect_domain(settings, client, zone, account_id, domain, record):
    routing = email_routing.get_settings(client, zone["id"])
    record(
        "email_routing",
        email_routing.is_enabled(routing),
        "enabled" if email_routing.is_enabled(routing) else "not enabled",
        None if email_routing.is_enabled(routing) else "Run `tempmail setup {0}`".format(domain),
    )

    mx = email_routing.list_mx_records(client, zone["id"])
    foreign = email_routing.foreign_mx_records(mx)
    record(
        "mx_records",
        bool(mx) and not foreign,
        "{0} MX record(s){1}".format(
            len(mx),
            "; {0} point elsewhere".format(len(foreign)) if foreign else "",
        ),
        "Mail for this domain may be delivered to another provider." if foreign else None,
    )

    catch_all = email_routing.get_catch_all(client, zone["id"])
    target = email_routing.catch_all_worker(catch_all)
    record(
        "catch_all",
        target == settings.worker_name and bool(catch_all and catch_all.get("enabled")),
        email_routing.catch_all_description(catch_all),
        "Run `tempmail setup {0}` to repoint it.".format(domain)
        if target != settings.worker_name
        else None,
    )

    if account_id:
        exists = workers.script_exists(client, account_id, settings.worker_name)
        record(
            "worker",
            exists,
            settings.worker_name if exists else "not deployed",
            None if exists else "Run `tempmail worker deploy`.",
        )

    database_id = settings.database_id
    if not database_id:
        record("database", False, "no database id in settings", "Run `tempmail setup`.")
    else:
        try:
            store_module.Store(client, account_id, database_id).healthy()
            record("database", True, database_id)
        except TempmailError as exc:
            record("database", False, exc.message, exc.hint)

    has_webhook = bool(settings.slack_webhook(domain))
    record(
        "slack",
        True,
        "configured" if has_webhook else "not configured (inbox-only mode)",
        None,
    )
