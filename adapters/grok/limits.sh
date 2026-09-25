#!/usr/bin/python3
"""Limits hook for the grok agent-collectors adapter.

Uses the same CLI-proxy billing endpoint the public Omarchy Grok collectors
use (calmasacow/omarchy-grok-usage, documented in their README):

  GET https://cli-chat-proxy.grok.com/v1/billing?format=credits

Auth is the SuperGrok OIDC session in ~/.grok/auth.json (grok login).
API-key entries are skipped — those are console keys, not the weekly pool.

Never prints tokens. Exit 1 means "no data".
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BILLING_HOST = "cli-chat-proxy.grok.com"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
TOKEN_AUTH = "xai-grok-cli"
MAX_AUTH_BYTES = 64 * 1024
MAX_VERSION_BYTES = 8 * 1024
MAX_REMOTE_BYTES = 1024 * 1024
TIMEOUT_S = 15

PRODUCT_TITLES = {
    "GrokBuild": "Grok Build",
    "GrokChat": "Chat",
    "Chat": "Chat",
    "GrokImagine": "Imagine",
    "Imagine": "Imagine",
    "GrokVoice": "Voice",
    "Voice": "Voice",
    "GrokAPI": "API",
    "API": "API",
}

CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-xai-token-auth",
        "x-userid",
        "x-email",
    }
)


def grok_home() -> Path:
    raw = os.environ.get("GROK_HOME")
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw)))
    return Path.home() / ".grok"


def origin_of(url: str) -> tuple[str, str, int] | None:
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, host, port


def allowed_request_url(url: str) -> bool:
    return origin_of(url) == ("https", BILLING_HOST, 443)


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that would send the Grok token off-origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_request_url(newurl):
            raise urllib.error.HTTPError(
                newurl, code, "cross-origin redirect refused", headers, fp
            )
        nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nxt is None:
            return None
        if origin_of(req.full_url) != origin_of(nxt.full_url):
            for name in list(nxt.headers):
                if name.lower() in CREDENTIAL_HEADERS:
                    del nxt.headers[name]
        return nxt


def opener() -> urllib.request.OpenerDirector:
    handle = urllib.request.OpenerDirector()
    for handler in (
        SameOriginRedirectHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.UnknownHandler(),
    ):
        handle.add_handler(handler)
    return handle


_OPENER = opener()


def read_json_object(path: Path, max_bytes: int) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        size = path.stat().st_size
        if size <= 0 or size > max_bytes:
            return None
        data = json.loads(path.read_bytes().decode("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def parse_iso(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def client_version() -> str:
    data = read_json_object(grok_home() / "version.json", MAX_VERSION_BYTES)
    if isinstance(data, dict):
        value = str(data.get("version") or "").strip()
        if value:
            return value
    return os.environ.get("GROK_CLIENT_VERSION", "1.0.13")


def oauth_login() -> dict[str, Any] | None:
    data = read_json_object(grok_home() / "auth.json", MAX_AUTH_BYTES)
    if not isinstance(data, dict):
        return None
    now = datetime.now(timezone.utc)
    best: dict[str, Any] | None = None
    best_expiry: datetime | None = None
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        mode = str(entry.get("auth_mode") or "")
        if mode not in ("oidc", "oauth", ""):
            continue
        key = str(entry.get("key") or "").strip()
        if not key or key.startswith("sk-") or key.startswith("xai-"):
            continue
        expires = parse_iso(entry.get("expires_at"))
        if best is None:
            best, best_expiry = entry, expires
            continue
        if expires and (best_expiry is None or expires > best_expiry):
            best, best_expiry = entry, expires
        elif expires is None and best_expiry is not None and best_expiry < now:
            best, best_expiry = entry, expires
    return best


def auth_headers(login: dict[str, Any]) -> dict[str, str]:
    token = str(login.get("key") or "").strip()
    version = client_version()
    headers = {
        "Authorization": "Bearer " + token,
        "X-XAI-Token-Auth": TOKEN_AUTH,
        "Accept": "application/json",
        "x-grok-client-version": version,
        "x-grok-client-identifier": "grok-shell",
        "User-Agent": f"grok-shell/{version}",
    }
    user_id = str(login.get("user_id") or "").strip()
    if user_id:
        headers["x-userid"] = user_id
    email = str(login.get("email") or "").strip()
    if email:
        headers["x-email"] = email
    return headers


def fetch_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    if not allowed_request_url(url):
        raise ValueError("refusing unexpected billing host")
    request = urllib.request.Request(url, headers=headers, method="GET")
    with _OPENER.open(request, timeout=TIMEOUT_S) as response:
        if not allowed_request_url(getattr(response, "geturl", lambda: url)()):
            raise ValueError("redirected off the billing host")
        raw = response.read(MAX_REMOTE_BYTES + 1)
    if len(raw) > MAX_REMOTE_BYTES:
        raise ValueError("response too large")
    payload = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError("invalid JSON object")
    return payload


def parse_percent_points(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return min(1.0, max(0.0, float(raw) / 100.0))
    except (TypeError, ValueError):
        return None


def period_label(period_type: str) -> str:
    text = period_type.upper()
    if "WEEK" in text:
        return "Weekly"
    if "MONTH" in text:
        return "Monthly"
    if "DAY" in text:
        return "Daily"
    return "Usage"


def product_title(product: str) -> str:
    raw = str(product or "").strip()
    if raw in PRODUCT_TITLES:
        return PRODUCT_TITLES[raw]
    spaced = "".join(
        (" " + ch if ch.isupper() and i else ch) for i, ch in enumerate(raw)
    ).strip()
    if spaced.lower().startswith("grok chat"):
        return "Chat"
    return spaced or raw or "Product"


def cache_path() -> str:
    root = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(root, "omarchy", "grok-usage", "last-limits.json")


def read_cached_limits(reason: str = "unreachable") -> list:
    """Last good meters, marked stale, for a run that cannot reach billing."""
    try:
        with open(cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("limits") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    fetched = str(data.get("updatedAt") or "") if isinstance(data, dict) else ""
    return [
        {**row, "stale": True, "staleReason": reason, "staleFetchedAt": fetched}
        for row in rows
        if isinstance(row, dict)
    ]


def write_cached_limits(limits: list) -> None:
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "account": "grok-cli",
                    "updatedAt": datetime.now(timezone.utc)
                    .astimezone()
                    .isoformat(timespec="seconds"),
                    "limits": limits,
                },
                fh,
            )
        os.replace(tmp, path)
    except OSError:
        return


def parse_limits(config: dict[str, Any]) -> list[dict[str, Any]]:
    limits: list[dict[str, Any]] = []
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    period_type = str((period or {}).get("type") or "")
    resets_at = str((period or {}).get("end") or config.get("billingPeriodEnd") or "")
    label = period_label(period_type) if period_type else (
        "Weekly" if "week" in str(config.get("billingPeriodEnd") or "").lower() else "Usage"
    )

    percent: float | None = None
    raw_percent = config.get("creditUsagePercent")
    if raw_percent is not None:
        percent = parse_percent_points(raw_percent)
    elif period or config.get("billingPeriodEnd"):
        percent = 0.0

    if percent is not None:
        title = "Weekly" if label == "Weekly" else label
        limits.append(
            {
                "label": f"{label} SuperGrok Limit" if label == "Weekly" else f"{label} limit",
                "title": title,
                "percent": percent,
                "resetsAt": resets_at,
            }
        )

    rows = config.get("productUsage")
    if isinstance(rows, list):
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            product = str(entry.get("product") or "").strip()
            if not product:
                continue
            product_percent = parse_percent_points(entry.get("usagePercent"))
            if product_percent is None:
                continue
            limits.append(
                {
                    "label": product,
                    "title": product_title(product),
                    "percent": product_percent,
                    "resetsAt": resets_at,
                }
            )
    return limits


def main() -> int:
    login = oauth_login()
    if login is None or not str(login.get("key") or "").strip():
        # Keep the meters, but say why they are old: an empty record hides the
        # section and would leave the footer claiming a fresh run.
        print("grok limits: the SuperGrok session is missing or expired "
              "(run: grok login)", file=sys.stderr)
        cached = read_cached_limits("auth")
        if cached:
            print(json.dumps(cached))
            return 0
        return 1
    try:
        payload = fetch_json(BILLING_URL, auth_headers(login))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ValueError, OSError, TimeoutError):
        cached = read_cached_limits()
        if cached:
            print(json.dumps(cached))
            return 0
        return 1
    config = payload.get("config")
    if not isinstance(config, dict):
        print("grok limits: billing response carried no config — shape may have changed",
              file=sys.stderr)
        cached = read_cached_limits("unrefreshed")
        if cached:
            print(json.dumps(cached))
            return 0
        return 1
    out = parse_limits(config)
    if not out:
        print("grok limits: billing response parsed to no windows — shape may have changed",
              file=sys.stderr)
        cached = read_cached_limits("unrefreshed")
        if cached:
            print(json.dumps(cached))
            return 0
        return 1
    write_cached_limits(out)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
