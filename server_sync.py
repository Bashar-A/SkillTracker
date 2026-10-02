"""Mediocre Entropia upload protocol, independent of Tk and local session files."""
import hashlib
import ipaddress
import json
import math
import socket
import ssl
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 20 * 1024 * 1024
MAX_EVENTS = 100000
SESSION_FIELDS = ("id", "mob", "mob_type", "maturity", "weapon", "planet", "notes",
                  "started_at", "ended_at", "ped_cycled")
EVENT_FIELDS = ("value_ped", "cost_ped", "items", "messages", "excluded_from_loot", "excluded_loot_items")
ITEM_FIELDS = ("value", "minTt", "maxTt", "markupType", "markup")


class UploadError(ValueError):
    """A user-readable failure with no credentials in its message."""


def normalize_host(host, allow_http=False):
    host = str(host or "").strip().rstrip("/")
    try:
        parts = urlsplit(host)
        port = parts.port
    except ValueError:
        raise UploadError("Enter a valid server URL including https://.") from None
    if (parts.scheme not in ("https", "http") or not parts.hostname or
            parts.username is not None or parts.password is not None or parts.query or parts.fragment or
            parts.path not in ("", "/", "/api")):
        raise UploadError("Use the service URL, without credentials, query, or a page path (optional /api is accepted).")
    loopback = parts.hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        pass
    if parts.scheme == "http" and not (loopback or allow_http):
        raise UploadError("Use HTTPS, or explicitly enable HTTP for your trusted local network in Settings.")
    return f"{parts.scheme}://{parts.netloc}"


def encode(body):
    try:
        return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise UploadError("Upload data contains an invalid number or JSON value.") from None


def fingerprint(body):
    return hashlib.sha256(encode(body)).hexdigest()


def number(value, label, minimum=0, maximum=1_000_000_000):
    if isinstance(value, bool):
        raise UploadError(f"{label} must be a number.")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise UploadError(f"{label} must be a number.") from None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise UploadError(f"{label} must be between {minimum} and {maximum}.")
    return result


def session_payload(session):
    """Send only import fields; local log paths, raw chat, and skill snapshots stay local."""
    if not isinstance(session, dict):
        raise UploadError("Invalid analytic session.")
    for key, limit in (("id", 128), ("mob", 160)):
        if not str(session.get(key, "") or "").strip() or len(str(session[key])) > limit:
            raise UploadError(f"Session needs a valid {key} before uploading.")
    payload = {key: session[key] for key in SESSION_FIELDS if key in session}
    dates = []
    for key in ("started_at", "ended_at"):
        try:
            value = datetime.fromisoformat(str(session.get(key, "")).replace("Z", "+00:00"))
            # Monitor session clocks are stored in local time without an offset.
            # Aware imported clocks retain their original instant.
            value = value.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            raise UploadError(f"Session needs a valid {key}; complete it before uploading.") from None
        dates.append(value)
        payload[key] = value.isoformat()
    if dates[1] < dates[0]:
        raise UploadError("Session end precedes its start.")
    payload["ped_cycled"] = number(session.get("ped_cycled"), "PED cycled", 0.00000001)
    events = session.get("loot_events")
    if not isinstance(events, list) or len(events) > 50000:
        raise UploadError("Session needs loot_events with at most 50,000 rows.")
    if any(not isinstance(row, dict) for row in events):
        raise UploadError("Invalid loot event in session.")
    payload["loot_events"] = [{key: row[key] for key in EVENT_FIELDS if key in row} for row in events]
    encode(payload)
    return payload


def batches(kind, rows):
    """Split by row count, encoded bytes, and event count before any writes."""
    if not rows:
        raise UploadError("There is no data to upload.")
    limit = 100 if kind == "sessions" else 1000
    result, batch, size, events = [], [], len(encode({kind: []})), 0
    for row in rows:
        row_size = len(encode(row)) + 1
        row_events = len(row.get("loot_events", [])) if kind == "sessions" else 0
        if row_size + len(encode({kind: []})) > MAX_BYTES or row_events > MAX_EVENTS:
            raise UploadError("One record exceeds the server upload limit; split it before uploading.")
        if batch and (len(batch) >= limit or size + row_size > MAX_BYTES or events + row_events > MAX_EVENTS):
            result.append({kind: batch})
            batch, size, events = [], len(encode({kind: []})), 0
        batch.append(row)
        size += row_size
        events += row_events
    result.append({kind: batch})
    return result


def item_rows(raw):
    """Accept ItemInput arrays, {items:[...]}, or a name-to-details catalog."""
    raw = raw.get("items", raw) if isinstance(raw, dict) else raw
    if isinstance(raw, dict):
        if any(not isinstance(details, dict) for details in raw.values()):
            raise UploadError("Each catalog item must contain an object with TT/MU fields.")
        raw = [dict(details, name=name) for name, details in raw.items()]
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise UploadError("Items JSON must contain an array or a name-to-details catalog.")
    rows, seen = [], set()
    for row in raw:
        name = str(row.get("name", "") or "").strip()
        if not name or len(name) > 200 or name.upper() in seen:
            raise UploadError("Items need unique names of 1 to 200 characters.")
        seen.add(name.upper())
        fields = {key: row[key] for key in ITEM_FIELDS if key in row}
        if not fields:
            raise UploadError(f"Item {name} has no value, TT range, or MU fields.")
        rows.append({"name": name, **fields})
    return rows


def merge_items(rows, catalog):
    """Use fresh versions and preserve unspecified server TT/MU fields."""
    defaults = {"value": 0, "minTt": 0, "maxTt": 0, "markupType": "percentage", "markup": 100}
    result = []
    for row in rows:
        existing = catalog.get(row["name"].upper(), {})
        merged = {**defaults, **{k: existing[k] for k in ITEM_FIELDS if k in existing}, **row,
                  "name": existing.get("name", row["name"]), "version": existing.get("version", 0)}
        for key in ("value", "minTt", "maxTt"):
            merged[key] = number(merged[key], key)
        if merged["minTt"] > merged["maxTt"]:
            raise UploadError(f"Item {merged['name']}: minimum TT exceeds maximum TT.")
        if merged["markupType"] not in ("percentage", "fixed"):
            raise UploadError("MU type must be percentage or fixed.")
        merged["markup"] = number(merged["markup"], "MU", 100 if merged["markupType"] == "percentage" else 0, 1000000)
        result.append(merged)
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UploadError("The server redirected the request. Configure its final URL in Settings.")


class ServerClient:
    def __init__(self, host, token, allow_http=False):
        self.host = normalize_host(host, allow_http)
        self.token = str(token or "").strip()
        if not self.token or any(c.isspace() or not 32 <= ord(c) < 127 for c in self.token):
            raise UploadError("Enter your API token in Settings.")
        self.opener = build_opener(NoRedirect())

    @property
    def destination(self):
        # Receipts are scoped to both service and token; never persist the secret.
        return fingerprint({"host": self.host, "token": self.token})

    def request(self, path, body=None, method="GET"):
        data = encode(body) if body is not None else None
        if data is not None and len(data) > MAX_BYTES:
            raise UploadError("Request exceeds 20 MB.")
        req = Request(self.host + "/api/uploads/" + path, data=data, method=method,
                      headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=120) as response:
                return json.load(response)
        except UploadError:
            raise
        except HTTPError as error:
            try:
                detail = json.loads(error.read(8192)).get("error", "")
            except (ValueError, AttributeError):
                detail = ""
            hints = {401: "Token is invalid, expired, or revoked.", 403: "Account is not approved or is disabled.",
                     404: "Endpoint not found. Deploy the server catalog update for item/MU uploads.",
                     409: "Server data changed or this session ID has different content. Review it before retrying.",
                     429: "Server rate limit reached. Wait one minute before retrying."}
            message = str(detail or hints.get(error.code, "Check the server and upload data."))[:1000]
            raise UploadError(f"HTTP {error.code}: {message.replace(self.token, '[redacted]')}") from None
        except (URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError):
            raise UploadError("Cannot reach the server. Check its URL, network, and TLS certificate. Retry safely after checking the server.") from None
        except (ValueError, TypeError):
            raise UploadError("The server returned invalid JSON. Check the service URL.") from None

    def catalog(self):
        result, page = {}, 1
        while True:
            data = self.request(f"items?page={page}")
            rows = data.get("rows") if isinstance(data, dict) else None
            if not isinstance(rows, list) or "total" not in data:
                raise UploadError("Invalid catalog response.")
            for row in rows:
                result[row["name"].upper()] = row
            if len(result) >= data["total"]:
                return result
            if not rows or page >= 10000:
                raise UploadError("Catalog changed during reading. Retry the upload.")
            page += 1

    def upload(self, kind, rows, progress=lambda *args: None):
        method = "PUT" if kind == "skills" else "POST"
        if kind == "skills":
            if not isinstance(rows, dict) or not 1 <= len(rows) <= 500:
                raise UploadError("Provide 1 to 500 current skills.")
            rows = {name: number(value, "Skill points", 0, 100000) for name, value in rows.items()}
            bodies = [{"skills": rows}]
        elif kind == "sessions":
            bodies = batches(kind, [session_payload(row) for row in rows])
        elif kind == "items":
            bodies = batches(kind, merge_items(item_rows(rows), self.catalog()))
        else:
            raise UploadError("Unknown upload type.")
        for index, body in enumerate(bodies, 1):
            response = self.request(kind, body, method)
            if not isinstance(response, dict):
                raise UploadError("Invalid upload response. Check the server before retrying.")
            if kind == "sessions":
                receipts = response.get("rows")
                expected = {row["id"] for row in body["sessions"]}
                if (not isinstance(receipts, list) or len(receipts) != len(body["sessions"]) or
                    any(not isinstance(row, dict) or row.get("sourceId") not in expected or not row.get("id") or
                        not isinstance(row.get("duplicate"), bool) for row in receipts)):
                    raise UploadError("Invalid session receipts. The server may have accepted the batch; re-uploading safely checks duplicates.")
            progress(index, len(bodies), body, response)
        return len(bodies)
