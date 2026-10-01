"""Worker script upload.

``PUT /accounts/{account_id}/workers/scripts/{name}`` with multipart/form-data
creates a version and deploys it in one call, so there is no separate deploy
step. Every binding is re-sent on each upload: a PUT replaces the script's whole
configuration, and the CLI already holds every value in settings.json, so we
never have to read a secret back out of Cloudflare.
"""

from ..errors import NotFoundError

MODULE_FILENAME = "tempmail_worker.mjs"
MODULE_CONTENT_TYPE = "application/javascript+module"


def script_exists(client, account_id, name):
    try:
        client.get("/accounts/{0}/workers/scripts/{1}".format(account_id, name))
        return True
    except NotFoundError:
        return False


def kv_binding(name, namespace_id):
    return {"type": "kv_namespace", "name": name, "namespace_id": namespace_id}


def d1_binding(name, database_id):
    return {"type": "d1", "name": name, "id": database_id}


def secret_binding(name, value):
    return {"type": "secret_text", "name": name, "text": value}


def plain_binding(name, value):
    return {"type": "plain_text", "name": name, "text": value}


def upload_script(client, account_id, name, script_source, bindings, compatibility_date):
    """Upload and deploy the Worker. Returns the API result payload."""
    metadata = {
        "main_module": MODULE_FILENAME,
        "compatibility_date": compatibility_date,
        "bindings": list(bindings),
    }
    files = {
        "metadata": (None, client.dump_json(metadata), "application/json"),
        MODULE_FILENAME: (
            MODULE_FILENAME,
            script_source.encode("utf-8"),
            MODULE_CONTENT_TYPE,
        ),
    }
    return client.put(
        "/accounts/{0}/workers/scripts/{1}".format(account_id, name), files=files
    )
