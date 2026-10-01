"""The settings file: ``~/.config/tempmail/settings.json``.

The file is the canonical source of configuration (the ProjectDiscovery model:
one file you edit once per machine). Environment variables stay supported and
take precedence when present, so CI and secret managers can inject credentials
without writing anything to disk.

Resolution order for every credential: environment variable, then settings file.
"""

import json
import os
import stat
import tempfile

from .errors import ConfigError, mask

ENV_API_TOKEN = "CLOUDFLARE_API_TOKEN"
ENV_ACCOUNT_ID = "CLOUDFLARE_ACCOUNT_ID"
ENV_SLACK_WEBHOOK = "SLACK_WEBHOOK_URL"
ENV_CONFIG_PATH = "TEMPMAIL_CONFIG"

# Workers runtime behaviour is pinned to this date so a future runtime change
# cannot silently alter how a deployed Worker behaves.
DEFAULT_COMPATIBILITY_DATE = "2026-09-01"
DEFAULT_WORKER_NAME = "tempmail-router"
DEFAULT_DATABASE_NAME = "tempmail"

# 1 MiB. Cloudflare accepts up to 25 MiB, but a disposable address exists to
# receive a verification code, and parsing megabytes of attachments on the
# Workers free tier is how you hit EXCEEDED_CPU.
DEFAULT_MAX_MESSAGE_BYTES = 1048576
DEFAULT_RATE_LIMIT_PER_HOUR = 20
DEFAULT_SLACK_TIMEOUT_MS = 5000

# How much sanitized body text to keep per message for `tempmail read`, and how
# long to keep messages at all. A disposable inbox that never forgets is just a
# long-lived archive of other people's verification codes.
DEFAULT_MAX_STORED_BODY_CHARS = 16384
DEFAULT_RETENTION_HOURS = 168

# The unmodified HTML is kept as an audit copy for `tempmail read --no-filter`.
# It is stored and printed as inert text; nothing ever renders it.
DEFAULT_MAX_STORED_HTML_CHARS = 65536

_SECRET_KEYS = frozenset({"api_token", "webhook_url", "slack_webhook_url"})


def default_config_path():
    """Resolve the settings file path, honouring ``TEMPMAIL_CONFIG`` and XDG."""
    override = os.environ.get(ENV_CONFIG_PATH)
    if override:
        return os.path.abspath(os.path.expanduser(override))

    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "tempmail", "settings.json")


def default_settings():
    return {
        "cloudflare": {"api_token": "", "account_id": ""},
        "slack": {"webhook_url": ""},
        "worker": {
            "name": DEFAULT_WORKER_NAME,
            "compatibility_date": DEFAULT_COMPATIBILITY_DATE,
        },
        "d1": {"database_name": DEFAULT_DATABASE_NAME, "database_id": ""},
        "defaults": {
            "ttl": None,
            "max_message_bytes": DEFAULT_MAX_MESSAGE_BYTES,
            "rate_limit_per_hour": DEFAULT_RATE_LIMIT_PER_HOUR,
            "strip_plus_tag": True,
            "slack_timeout_ms": DEFAULT_SLACK_TIMEOUT_MS,
            "store_messages": True,
            "max_stored_body_chars": DEFAULT_MAX_STORED_BODY_CHARS,
            "store_html": True,
            "max_stored_html_chars": DEFAULT_MAX_STORED_HTML_CHARS,
            "retention_hours": DEFAULT_RETENTION_HOURS,
        },
        "domains": {},
    }


def _merge(base, overlay):
    """Recursively overlay ``overlay`` onto ``base`` so new defaults appear in
    settings files written by an older version."""
    result = dict(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class Settings(object):
    def __init__(self, data=None, path=None, exists=False):
        self.path = path or default_config_path()
        self.exists = exists
        self.data = _merge(default_settings(), data or {})

    # ---- persistence -----------------------------------------------------

    @classmethod
    def load(cls, path=None):
        path = path or default_config_path()
        if not os.path.exists(path):
            return cls(path=path, exists=False)

        try:
            with open(path, "r") as handle:
                data = json.load(handle)
        except ValueError as exc:
            raise ConfigError(
                "Settings file is not valid JSON: {0}".format(path),
                hint="Fix the syntax or delete the file and run `tempmail init`. "
                "Details: {0}".format(exc),
            )
        except OSError as exc:
            raise ConfigError(
                "Cannot read settings file: {0} ({1})".format(path, exc.strerror)
            )

        if not isinstance(data, dict):
            raise ConfigError("Settings file must contain a JSON object: {0}".format(path))
        return cls(data=data, path=path, exists=True)

    def save(self):
        """Write the file atomically with 0600, inside a 0700 directory.

        The temp file is created in the destination directory so the rename is
        atomic, and it is born with restrictive permissions — the secret is
        never briefly world-readable.
        """
        directory = os.path.dirname(self.path)
        if directory:
            try:
                os.makedirs(directory, 0o700)
            except OSError:
                if not os.path.isdir(directory):
                    raise
            else:
                os.chmod(directory, 0o700)

        payload = json.dumps(self.data, indent=2, sort_keys=False) + "\n"
        handle_fd, temp_path = tempfile.mkstemp(dir=directory or ".", prefix=".settings-")
        try:
            os.fchmod(handle_fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(handle_fd, "w") as handle:
                handle.write(payload)
            os.replace(temp_path, self.path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        self.exists = True
        return self.path

    def permissions_warning(self):
        """Return a warning when the settings file is readable by others."""
        if not os.path.exists(self.path):
            return None
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            return (
                "Settings file {0} is mode {1:04o}; it holds credentials. "
                "Run: chmod 600 {0}".format(self.path, mode)
            )
        return None

    # ---- credentials -----------------------------------------------------

    @property
    def api_token(self):
        token = os.environ.get(ENV_API_TOKEN) or self.data["cloudflare"].get("api_token")
        if not token:
            raise ConfigError(
                "No Cloudflare API token configured",
                hint="Run `tempmail init`, or export {0}".format(ENV_API_TOKEN),
            )
        return token.strip()

    @property
    def api_token_source(self):
        return "env" if os.environ.get(ENV_API_TOKEN) else "settings"

    @property
    def account_id(self):
        """Account id, if pinned. Empty means: discover it from the zone."""
        return (
            os.environ.get(ENV_ACCOUNT_ID)
            or self.data["cloudflare"].get("account_id")
            or ""
        ).strip()

    @account_id.setter
    def account_id(self, value):
        self.data["cloudflare"]["account_id"] = value or ""

    def slack_webhook(self, domain=None):
        """Per-domain webhook when configured, else the global one.

        Environment wins over both so a one-off run can redirect notifications
        without touching the file.
        """
        if domain:
            override = (self.domain(domain) or {}).get("slack_webhook_url")
            if override:
                return override.strip()
        value = os.environ.get(ENV_SLACK_WEBHOOK) or self.data["slack"].get("webhook_url")
        return (value or "").strip()

    # ---- sections --------------------------------------------------------

    @property
    def worker_name(self):
        return self.data["worker"].get("name") or DEFAULT_WORKER_NAME

    @property
    def compatibility_date(self):
        return self.data["worker"].get("compatibility_date") or DEFAULT_COMPATIBILITY_DATE

    @property
    def database_name(self):
        return self.data["d1"].get("database_name") or DEFAULT_DATABASE_NAME

    @property
    def database_id(self):
        return (self.data["d1"].get("database_id") or "").strip()

    @database_id.setter
    def database_id(self, value):
        self.data["d1"]["database_id"] = value or ""

    @property
    def defaults(self):
        return self.data["defaults"]

    def domain(self, name):
        return self.data["domains"].get(name)

    def set_domain(self, name, **fields):
        entry = dict(self.data["domains"].get(name) or {})
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.data["domains"][name] = entry
        return entry

    def domain_setting(self, domain, key):
        """Per-domain value falling back to the global default."""
        entry = self.domain(domain) or {}
        if entry.get(key) is not None:
            return entry[key]
        return self.defaults.get(key)

    # ---- display ---------------------------------------------------------

    def redacted(self):
        """A deep copy with every secret replaced by its masked form."""

        def walk(node, key=None):
            if isinstance(node, dict):
                return dict((k, walk(v, k)) for k, v in node.items())
            if isinstance(node, list):
                return [walk(item) for item in node]
            if key in _SECRET_KEYS and isinstance(node, str) and node:
                return mask(node)
            return node

        return walk(self.data)
