"""Email Routing: settings, the DNS records it needs, and the catch-all rule."""

from ..errors import CloudflareError, NotFoundError

CLOUDFLARE_MX_SUFFIX = ".mx.cloudflare.net"


def get_settings(client, zone_id):
    """Current Email Routing state for a zone. ``None`` when never enabled."""
    try:
        return client.get("/zones/{0}/email/routing".format(zone_id))
    except NotFoundError:
        return None


def is_enabled(settings):
    return bool(settings and settings.get("enabled"))


def required_dns_records(client, zone_id):
    """The MX/SPF/DKIM records Cloudflare wants for this zone."""
    return client.get("/zones/{0}/email/routing/dns".format(zone_id)) or []


def enable(client, zone_id):
    """``POST .../email/routing/dns`` — creates the records and turns routing on."""
    return client.post("/zones/{0}/email/routing/dns".format(zone_id), json_body={})


def list_mx_records(client, zone_id):
    result = client.get(
        "/zones/{0}/dns_records".format(zone_id),
        params={"type": "MX", "per_page": 100},
    )
    return result or []


def foreign_mx_records(records):
    """MX records that do not point at Cloudflare Email Routing.

    These are the signal that the zone already receives mail somewhere else;
    enabling Email Routing would take that over.
    """
    foreign = []
    for record in records:
        content = (record.get("content") or "").strip().rstrip(".").lower()
        if not content.endswith(CLOUDFLARE_MX_SUFFIX):
            foreign.append(record)
    return foreign


def get_catch_all(client, zone_id):
    try:
        return client.get("/zones/{0}/email/routing/rules/catch_all".format(zone_id))
    except NotFoundError:
        return None


def catch_all_worker(rule):
    """Return the Worker name a catch-all rule routes to, if it does."""
    if not rule:
        return None
    for action in rule.get("actions") or []:
        if action.get("type") == "worker":
            values = action.get("value") or []
            if values:
                return values[0]
    return None


def catch_all_description(rule):
    """A short human description of what the current catch-all does."""
    if not rule:
        return "not configured"
    if not rule.get("enabled"):
        return "disabled"
    parts = []
    for action in rule.get("actions") or []:
        kind = action.get("type")
        values = action.get("value") or []
        if kind == "worker":
            parts.append("worker:{0}".format(values[0] if values else "?"))
        elif kind == "forward":
            parts.append("forward:{0}".format(", ".join(values) or "?"))
        elif kind == "drop":
            parts.append("drop")
        else:
            parts.append(str(kind))
    return ", ".join(parts) or "no action"


def set_catch_all_to_worker(client, zone_id, worker_name):
    """Point the zone's catch-all at our Email Worker."""
    body = {
        "name": "tempmail catch-all",
        "enabled": True,
        "matchers": [{"type": "all"}],
        "actions": [{"type": "worker", "value": [worker_name]}],
    }
    try:
        return client.put(
            "/zones/{0}/email/routing/rules/catch_all".format(zone_id), json_body=body
        )
    except CloudflareError as exc:
        raise CloudflareError(
            "Could not point the catch-all rule at Worker {0}: {1}".format(
                worker_name, exc.message
            ),
            status=exc.status,
            errors=exc.errors,
            hint="Confirm the Worker was deployed to the same account as the zone.",
        )
