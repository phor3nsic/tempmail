"""Generation and validation of the random local part of an address."""

import re
import secrets

from .errors import UsageError

# Base32-ish alphabet with the characters that get misread out loud or in a
# terminal removed (0/o, 1/l/i). 28 symbols: 8 chars give ~38 bits of entropy,
# which is far beyond what a catch-all scanner can walk through.
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
DEFAULT_LENGTH = 8
MIN_LENGTH = 6

# Addresses an operator would not expect to be handed out to a random service.
RESERVED = frozenset(
    {
        "abuse", "admin", "administrator", "billing", "contact", "dmarc",
        "hostmaster", "info", "mail", "mailer-daemon", "noc", "noreply",
        "no-reply", "postmaster", "root", "security", "support", "sysadmin",
        "webmaster",
    }
)

# RFC 5321 caps the local part at 64 octets and the whole path at 254.
MAX_LOCAL_PART = 64
MAX_EMAIL = 254

_LOCAL_PART_RE = re.compile(r"^[a-z0-9]([a-z0-9._+-]*[a-z0-9])?$")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def generate_local_part(length=DEFAULT_LENGTH):
    """Return a cryptographically random local part.

    ``secrets.choice`` draws from the system CSPRNG, so the result is neither
    sequential nor predictable from any previously issued address.
    """
    if length < MIN_LENGTH:
        raise UsageError("Local part length must be at least {0}".format(MIN_LENGTH))

    while True:
        candidate = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if candidate not in RESERVED:
            return candidate


def generate_email(domain, length=DEFAULT_LENGTH):
    return "{0}@{1}".format(generate_local_part(length), normalize_domain(domain))


def normalize_domain(domain):
    """Lowercase, strip a trailing dot, and validate the shape of a domain."""
    if not domain:
        raise UsageError("Domain is required")

    text = str(domain).strip().lower().rstrip(".")
    if text.startswith("@"):
        text = text[1:]
    if not _DOMAIN_RE.match(text):
        raise UsageError("Invalid domain: {0!r}".format(domain))
    return text


def normalize_email(email):
    """Lowercase and validate an address, returning ``(email, local, domain)``."""
    if not email:
        raise UsageError("Email address is required")

    text = str(email).strip().lower()
    if text.count("@") != 1:
        raise UsageError("Invalid email address: {0!r}".format(email))

    local, domain = text.split("@", 1)
    if not local or len(local) > MAX_LOCAL_PART or not _LOCAL_PART_RE.match(local):
        raise UsageError("Invalid email address: {0!r}".format(email))

    domain = normalize_domain(domain)
    text = "{0}@{1}".format(local, domain)
    if len(text) > MAX_EMAIL:
        raise UsageError("Email address is too long: {0!r}".format(email))
    return text, local, domain


def strip_plus_tag(local_part):
    """``x7k2p9+netflix`` -> ``x7k2p9``, so sub-addressing resolves to the base."""
    return local_part.split("+", 1)[0]
