"""D1: database provisioning and parameterised queries over the REST API."""

from ..errors import CloudflareError, NotFoundError


def list_databases(client, account_id):
    return client.get(
        "/accounts/{0}/d1/database".format(account_id), params={"per_page": 100}
    ) or []


def find_database(client, account_id, name):
    for database in list_databases(client, account_id):
        if database.get("name") == name:
            return database
    return None


def create_database(client, account_id, name):
    return client.post(
        "/accounts/{0}/d1/database".format(account_id), json_body={"name": name}
    )


def get_database(client, account_id, database_id):
    try:
        return client.get(
            "/accounts/{0}/d1/database/{1}".format(account_id, database_id)
        )
    except NotFoundError:
        return None


def _check(results, sql):
    """D1 returns a per-statement success flag inside a successful envelope."""
    for entry in results or []:
        if isinstance(entry, dict) and entry.get("success") is False:
            raise CloudflareError(
                "D1 statement failed: {0}".format(
                    (entry.get("error") or "unknown error")
                ),
                hint="Statement: {0}".format(sql[:200]),
            )
    return results


def query(client, account_id, database_id, sql, params=None):
    """Run one statement. Returns the list of per-statement result objects."""
    body = {"sql": sql}
    if params is not None:
        body["params"] = list(params)
    results = client.post(
        "/accounts/{0}/d1/database/{1}/query".format(account_id, database_id),
        json_body=body,
    )
    return _check(results, sql)


def batch(client, account_id, database_id, statements):
    """Run several statements in one round trip.

    ``statements`` is a list of ``(sql, params)`` pairs. D1 applies a batch as a
    single transaction, which is what makes `rotate` atomic: the old address can
    never end up revoked without the new one existing.
    """
    payload = []
    for sql, params in statements:
        item = {"sql": sql}
        if params is not None:
            item["params"] = list(params)
        payload.append(item)

    results = client.post(
        "/accounts/{0}/d1/database/{1}/query".format(account_id, database_id),
        json_body={"batch": payload},
    )
    return _check(results, statements[0][0] if statements else "")


def rows(results, index=0):
    """Pull the row list out of the result object at ``index``."""
    if not results or index >= len(results):
        return []
    return (results[index] or {}).get("results") or []


def first_row(results, index=0):
    found = rows(results, index)
    return found[0] if found else None
