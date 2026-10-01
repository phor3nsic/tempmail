"""Zone lookup.

Email Routing is configured per zone and, outside Enterprise plans, only at the
zone apex. So ``mail.example.com`` works only when it is itself a zone in the
account; otherwise we say exactly that instead of failing somewhere downstream.
"""

from ..errors import NotFoundError


def find_zone(client, domain):
    """Return the zone whose name matches ``domain`` exactly, or ``None``."""
    result = client.get("/zones", params={"name": domain, "per_page": 1})
    for zone in result or []:
        if zone.get("name") == domain:
            return zone
    return None


def _parent_candidates(domain):
    labels = domain.split(".")
    return [".".join(labels[i:]) for i in range(1, len(labels) - 1)]


def resolve_zone(client, domain):
    """Resolve ``domain`` to a zone, raising a actionable error when it is not one."""
    zone = find_zone(client, domain)
    if zone:
        return zone

    for parent in _parent_candidates(domain):
        if find_zone(client, parent):
            raise NotFoundError(
                "{0} is not a zone in this Cloudflare account "
                "(its parent {1} is)".format(domain, parent),
                hint=(
                    "Email Routing only works at a zone apex outside Enterprise plans. "
                    "Either run `tempmail setup {0}`, or add {1} as its own zone in "
                    "Cloudflare and delegate NS to it.".format(parent, domain)
                ),
            )

    raise NotFoundError(
        "Domain not found in this Cloudflare account: {0}".format(domain),
        hint="Check the spelling, and that the API token's Zone Resources include it.",
    )


def account_id_for_zone(zone):
    return ((zone or {}).get("account") or {}).get("id") or ""
