"""Error hierarchy. Every error carries the process exit code it should produce.

Nothing in here ever renders a secret: :func:`mask` is the single place allowed to
turn a credential into something printable.
"""


def mask(secret):
    """Render a secret as ``****`` plus its last 4 characters.

    Short or empty values collapse to ``****`` so we never leak a whole token by
    accident when one is unexpectedly tiny.
    """
    if not secret or len(secret) < 8:
        return "****"
    return "****" + secret[-4:]


class TempmailError(Exception):
    """Base class for every expected failure. Message is shown to the user as-is."""

    exit_code = 1

    def __init__(self, message, hint=None):
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(TempmailError):
    exit_code = 2


class ConfigError(TempmailError):
    exit_code = 2


class NotFoundError(TempmailError):
    exit_code = 3


class AuthError(TempmailError):
    exit_code = 4


class CloudflareError(TempmailError):
    """A Cloudflare API call returned success=false or a non-2xx status."""

    def __init__(self, message, status=None, errors=None, hint=None):
        super().__init__(message, hint=hint)
        self.status = status
        self.errors = errors or []


class SlackError(TempmailError):
    pass
