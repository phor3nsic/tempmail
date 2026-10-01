"""HTTP client for the Cloudflare API.

Three jobs beyond "send a request": unwrap the ``{success, errors, result}``
envelope into either a value or a typed exception, retry the failures that are
worth retrying, and guarantee the API token never reaches a log line, an
exception message or a traceback.
"""

import json as jsonlib
import random
import time

import httpx

from ..errors import AuthError, CloudflareError, NotFoundError, mask

BASE_URL = "https://api.cloudflare.com/client/v4"

# Which token permission a 403 on a given path is complaining about. Checked in
# order, so the more specific prefixes come first: the Email Routing *rules*
# endpoints want "Email Routing Rules", while the settings and DNS-enable
# endpoints under the same prefix want "Zone Settings" instead.
_PERMISSION_HINTS = (
    ("/workers/scripts", "Account > Workers Scripts : Edit"),
    ("/d1/database", "Account > D1 : Edit"),
    ("/email/routing/rules", "Zone > Email Routing Rules : Edit"),
    ("/email/routing", "Zone > Zone Settings : Edit"),
    ("/dns_records", "Zone > DNS : Read"),
    ("/zones", "Zone > Zone : Read"),
)

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0


def _permission_hint(path):
    for prefix, permission in _PERMISSION_HINTS:
        if prefix in path:
            return (
                "The API token is missing the permission: {0}. "
                "See README > Cloudflare API token.".format(permission)
            )
    return "Check the API token permissions. See README > Cloudflare API token."


class CloudflareClient(object):
    def __init__(self, token, timeout=30.0, transport=None, max_retries=_MAX_RETRIES,
                 sleep=time.sleep):
        if not token:
            raise AuthError("No Cloudflare API token provided")
        self._token = token
        self._max_retries = max_retries
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": "Bearer {0}".format(token),
                "Accept": "application/json",
                "User-Agent": "tempmail-cf",
            },
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self):
        self._client.close()

    # ---- internals -------------------------------------------------------

    def _scrub(self, text):
        """Replace the token anywhere it might have been echoed back."""
        if not text:
            return text
        return str(text).replace(self._token, mask(self._token))

    def _retry_delay(self, response, attempt):
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), _BACKOFF_CAP)
            except ValueError:
                pass
        # Full jitter: spreads retries out when several calls fail together.
        return random.uniform(0, min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP))

    def _unwrap(self, response, method, path):
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict) and payload.get("success") and "result" in payload:
            return payload["result"]

        messages = []
        codes = []
        if isinstance(payload, dict):
            for item in payload.get("errors") or []:
                if isinstance(item, dict):
                    codes.append(item.get("code"))
                    text = item.get("message") or ""
                    chain = item.get("error_chain") or []
                    for link in chain:
                        if isinstance(link, dict) and link.get("message"):
                            text += " ({0})".format(link["message"])
                    if text:
                        messages.append(text)
                else:
                    messages.append(str(item))

        detail = "; ".join(messages) or "HTTP {0}".format(response.status_code)
        summary = "Cloudflare API {0} {1} failed: {2}".format(
            method, path, self._scrub(detail)
        )

        if response.status_code in (401, 403):
            raise AuthError(summary, hint=_permission_hint(path))
        if response.status_code == 404:
            raise NotFoundError(summary)
        raise CloudflareError(
            summary, status=response.status_code, errors=codes,
            hint="Cloudflare error codes: {0}".format(codes) if codes else None,
        )

    # ---- requests --------------------------------------------------------

    def request(self, method, path, params=None, json_body=None, files=None,
                raw_response=False):
        """Perform a request, retrying transient failures, and unwrap the result."""
        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.request(
                    method, path, params=params, json=json_body, files=files
                )
            except httpx.TimeoutException:
                last_error = CloudflareError(
                    "Cloudflare API {0} {1} timed out".format(method, path),
                    hint="Network or Cloudflare is slow; the command is safe to retry.",
                )
            except httpx.HTTPError as exc:
                last_error = CloudflareError(
                    "Cloudflare API {0} {1} failed: {2}".format(
                        method, path, self._scrub(exc)
                    )
                )
            else:
                if response.status_code in _RETRY_STATUSES and attempt < self._max_retries:
                    self._sleep(self._retry_delay(response, attempt))
                    continue
                if raw_response:
                    return response
                return self._unwrap(response, method, path)

            if attempt < self._max_retries:
                self._sleep(min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP))

        raise last_error

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, json_body=None, params=None):
        return self.request("POST", path, params=params, json_body=json_body)

    def put(self, path, json_body=None, params=None, files=None):
        return self.request("PUT", path, params=params, json_body=json_body, files=files)

    def patch(self, path, json_body=None, params=None):
        return self.request("PATCH", path, params=params, json_body=json_body)

    def delete(self, path, params=None):
        return self.request("DELETE", path, params=params)

    # ---- token -----------------------------------------------------------

    def verify_token(self):
        """``GET /user/tokens/verify``. Returns the token status payload."""
        result = self.get("/user/tokens/verify")
        status = (result or {}).get("status")
        if status and status != "active":
            raise AuthError(
                "Cloudflare API token is not active (status: {0})".format(status)
            )
        return result

    def dump_json(self, value):
        """JSON for a multipart part, with the token scrubbed defensively."""
        return self._scrub(jsonlib.dumps(value))
