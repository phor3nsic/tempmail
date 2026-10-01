"""Rendering. Human output goes to stdout; progress and warnings go to stderr.

The split matters for scripting: `EMAIL=$(tempmail create example.com --quiet)`
must capture the address and nothing else, even while the command narrates what
it is doing.
"""

import json
import sys
import time

from . import store
from .ttl import format_duration

STATUS_LABEL = {
    store.STATUS_ACTIVE: "ACTIVE",
    store.STATUS_REVOKED: "REVOKED",
    store.STATUS_EXPIRED: "EXPIRED",
}


def echo(message="", err=False):
    stream = sys.stderr if err else sys.stdout
    stream.write("{0}\n".format(message))
    stream.flush()


def step(message):
    """A `[+] ...` progress line, on stderr so it never pollutes piped output."""
    echo("[+] {0}".format(message), err=True)


def warn(message):
    echo("[!] {0}".format(message), err=True)


def note(message):
    echo("[*] {0}".format(message), err=True)


def dump_json(payload):
    echo(json.dumps(payload, indent=2, sort_keys=False, default=str))


def fmt_datetime(timestamp):
    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(timestamp)))


def fmt_clock(timestamp):
    if not timestamp:
        return "-"
    return time.strftime("%H:%M", time.localtime(int(timestamp)))


def fmt_expiry(row, at=None):
    expires_at = row.get("expires_at")
    if not expires_at:
        return "never"

    remaining = int(expires_at) - int(at if at is not None else time.time())
    if remaining <= 0:
        return "{0} (expired)".format(fmt_datetime(expires_at))
    return "{0} (in {1})".format(fmt_datetime(expires_at), format_duration(remaining))


def address_payload(row):
    """The JSON shape for an address, as documented in the README."""
    status = store.effective_status(row)
    return {
        "email": row.get("email"),
        "domain": row.get("domain"),
        "status": status,
        "created_at": iso(row.get("created_at")),
        "expires_at": iso(row.get("expires_at")),
        "revoked_at": iso(row.get("revoked_at")),
        "label": row.get("label"),
        "rotated_from": row.get("rotated_from"),
        "messages": row.get("msg_count") or 0,
        "last_message_at": iso(row.get("last_msg_at")),
    }


def iso(timestamp):
    if not timestamp:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(timestamp)))


def render_created(row):
    status = store.effective_status(row)
    lines = [
        "",
        "Temporary email created:",
        "",
        "  {0}".format(row["email"]),
        "",
        "Status:  {0}".format(STATUS_LABEL.get(status, status)),
        "Domain:  {0}".format(row["domain"]),
        "Created: {0}".format(fmt_datetime(row.get("created_at"))),
    ]
    if row.get("expires_at"):
        lines.append("Expires: {0}".format(fmt_expiry(row)))
    if row.get("label"):
        lines.append("Label:   {0}".format(row["label"]))
    lines.append("")
    return "\n".join(lines)


def render_list(rows):
    if not rows:
        return "No addresses yet."

    width = max([len(row.get("email") or "") for row in rows] + [len("EMAIL")])
    lines = ["{0}  {1}  {2}".format("EMAIL".ljust(width), "STATUS ".ljust(8), "CREATED")]
    for row in rows:
        status = store.effective_status(row)
        lines.append(
            "{0}  {1}  {2}".format(
                (row.get("email") or "").ljust(width),
                (status or "?").ljust(8),
                fmt_clock(row.get("created_at")),
            )
        )
    return "\n".join(lines)


def render_status(row, unread=None):
    status = store.effective_status(row)
    lines = [
        "",
        "  {0}".format(row["email"]),
        "",
        "Status:   {0}".format(STATUS_LABEL.get(status, status)),
        "Domain:   {0}".format(row.get("domain")),
        "Created:  {0}".format(fmt_datetime(row.get("created_at"))),
        "Expires:  {0}".format(fmt_expiry(row)),
        "Messages: {0}{1}".format(
            row.get("msg_count") or 0,
            " ({0} unread)".format(unread) if unread else "",
        ),
    ]
    if row.get("last_msg_at"):
        lines.append("Last mail: {0}".format(fmt_datetime(row.get("last_msg_at"))))
    if row.get("revoked_at"):
        lines.append("Revoked:  {0}".format(fmt_datetime(row.get("revoked_at"))))
    if row.get("rotated_from"):
        lines.append("Rotated from: {0}".format(row.get("rotated_from")))
    lines.append("")
    return "\n".join(lines)


def render_rotation(old_row, new_row):
    old_status = store.effective_status(old_row)
    return "\n".join(
        [
            "",
            "OLD",
            "  {0}  [{1}]".format(old_row["email"], STATUS_LABEL.get(old_status, old_status)),
            "",
            "NEW",
            "  {0}  [ACTIVE]".format(new_row["email"]),
            "",
        ]
    )


def json_field(value, default):
    if not value:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def message_payload(row, include_body=False, include_html=False):
    payload = {
        "id": row.get("id"),
        "email": row.get("email"),
        "to": row.get("rcpt") or row.get("email"),
        "from": row.get("from_addr"),
        "from_name": row.get("from_name"),
        "subject": row.get("subject"),
        "received_at": iso(row.get("received_at")),
        "sent_at": iso(row.get("sent_at")),
        "outcome": row.get("outcome"),
        "size": row.get("raw_size"),
        "codes": json_field(row.get("codes"), []),
        "links": json_field(row.get("links"), []),
        "attachments": json_field(row.get("attachments"), []),
        "read": bool(row.get("read_at")),
    }
    if include_body:
        payload["body"] = row.get("body_text") or ""
    if include_html:
        payload["html"] = row.get("body_html") or ""
    return payload


def render_inbox(rows, email=None):
    if not rows:
        return "Inbox is empty{0}.".format(" for {0}".format(email) if email else "")

    id_width = max([len(str(row.get("id") or "")) for row in rows] + [2])
    from_width = min(
        32, max([len(row.get("from_addr") or "") for row in rows] + [len("FROM")])
    )

    lines = [
        "{0}  {1}  {2}  {3}  {4}".format(
            "ID".rjust(id_width),
            "RECEIVED".ljust(16),
            "FROM".ljust(from_width),
            "CODE".ljust(8),
            "SUBJECT",
        )
    ]
    for row in rows:
        codes = json_field(row.get("codes"), [])
        marker = " " if row.get("read_at") else "*"
        lines.append(
            "{0}{1} {2}  {3}  {4}  {5}".format(
                marker,
                str(row.get("id") or "").rjust(id_width - 1 if id_width > 1 else 1),
                fmt_datetime(row.get("received_at")).ljust(16),
                (row.get("from_addr") or "-")[:from_width].ljust(from_width),
                (codes[0] if codes else "-")[:8].ljust(8),
                (row.get("subject") or "(no subject)")[:60],
            )
        )
    lines.append("")
    lines.append("* = unread.  Read one with: tempmail read <email> --id <ID>")
    return "\n".join(lines)


def render_message(row, no_filter=False):
    codes = json_field(row.get("codes"), [])
    links = json_field(row.get("links"), [])
    attachments = json_field(row.get("attachments"), [])

    lines = [
        "",
        "From:     {0}{1}".format(
            "{0} ".format(row["from_name"]) if row.get("from_name") else "",
            "<{0}>".format(row.get("from_addr") or "unknown"),
        ),
        "To:       {0}".format(row.get("rcpt") or row.get("email")),
        "Subject:  {0}".format(row.get("subject") or "(no subject)"),
        "Received: {0}".format(fmt_datetime(row.get("received_at"))),
        "Size:     {0} bytes".format(row.get("raw_size") or 0),
    ]
    if codes:
        lines.append("Codes:    {0}".format("  ".join(codes)))
    if row.get("outcome") == "parse_fallback":
        lines.append("Note:     body could not be parsed; headers only")

    if links:
        lines.append("")
        lines.append("Links (defanged, not clickable):")
        for link in links:
            lines.append("  {0}".format(link.replace("http", "hxxp", 1).replace(".", "[.]")))

    if attachments:
        lines.append("")
        lines.append("Attachments (metadata only, never downloaded):")
        for item in attachments:
            lines.append(
                "  {0} ({1}, {2} bytes)".format(
                    item.get("name"), item.get("type"), item.get("size")
                )
            )

    lines.append("")
    if no_filter:
        html = row.get("body_html")
        if html:
            lines.append("--- original HTML (unrendered, for audit) ---")
            lines.append(html)
        else:
            lines.append("--- no HTML part; showing text body ---")
            lines.append(row.get("body_text") or "(empty)")
    else:
        lines.append("--- body ---")
        lines.append(row.get("body_text") or "(empty)")

    lines.append("")
    return "\n".join(lines)
