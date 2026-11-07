#!/usr/bin/env python3
"""Read Kimi Code subscription quotas without changing official CLI credentials."""

from datetime import datetime, timezone
import http.client
import json
import math
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

HOSTS = {"mainland-cn": "api.kimi.com", "global": "api.kimi.ai"}
WINDOWS = (
    ("limit_5h", "Session (5-hour)"),
    ("limit_7d", "Weekly (7-day)"),
    ("limit_month_total", "Monthly"),
    ("limit_month_code", "Code monthly"),
)
LOGIN_HELP = "Sign in with the official Kimi Code CLI (`kimi login`). Credentials are read-only."


def credential_home() -> Path:
    return Path(os.environ.get("KIMI_CODE_HOME") or Path.home() / ".kimi-code")


def read_bounded(path: Path, cap: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    if len(raw) > cap:
        raise ValueError("File exceeds size limit")
    return raw


def valid_header(value) -> bool:
    return isinstance(value, str) and bool(value) and all(33 <= ord(c) <= 126 for c in value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials to a redirect target.
        return None


def request_json(region: str, token: str, device: str) -> dict:
    if region not in HOSTS:
        raise ValueError("Unsupported Kimi region")
    headers = {
        "Authorization": "Bearer " + token,
        "User-Agent": "omarchy-agent-collectors",
        "X-Msh-Platform": "kimi_code",
    }
    if device:
        headers["X-Msh-Device-Id"] = device
    request = urllib.request.Request(
        f"https://{HOSTS[region]}/coding/v1/usages", headers=headers,
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise
    if len(raw) > 1024 * 1024:
        raise ValueError("Response exceeds size limit")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def unavailable(status: str, help_text: str) -> dict:
    return {"limits": [], "usageStatusText": status, "authHelpText": help_text}


def parse_usage(payload: dict) -> dict:
    limits = []
    usages = payload.get("usages") if isinstance(payload, dict) else None
    if isinstance(usages, dict):
        for key, label in WINDOWS:
            entry = usages.get(key)
            if not isinstance(entry, dict):
                continue
            value = entry.get("used_ratio")
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                continue
            try:
                ratio = float(value)
            except (ValueError, OverflowError):
                continue
            if not math.isfinite(ratio) or ratio < 0:
                continue
            row = {"label": label, "percent": min(ratio, 1)}
            reset = entry.get("reset_time")
            if isinstance(reset, str):
                try:
                    stamp = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                    if stamp.utcoffset() is not None:
                        row["resetsAt"] = stamp.astimezone(timezone.utc).isoformat()
                except (ValueError, OverflowError):
                    pass
            limits.append(row)
    if not limits:
        return unavailable("Kimi usage unavailable", "Kimi returned no recognized subscription quota windows.")
    return {"limits": limits, "usageStatusText": "", "authHelpText": ""}


def collect() -> dict:
    home = credential_home()
    try:
        auth = json.loads(read_bounded(home / "credentials" / "kimi-code.json", 65536))
        token = auth.get("access_token") if isinstance(auth, dict) else None
        if not valid_header(token):
            raise ValueError("Invalid access token")
    except (OSError, ValueError, RecursionError):
        return unavailable("Kimi sign-in required", LOGIN_HELP)
    try:
        region = read_bounded(home / "region", 64).decode().strip()
        if region not in HOSTS:
            raise ValueError("Unknown region")
    except (OSError, ValueError):
        return unavailable("Kimi region unavailable", "Set a known region through `kimi login --region mainland-cn` or `--region global`.")
    try:
        device = read_bounded(home / "device_id", 256).decode().strip()
        if not valid_header(device):
            device = ""
    except (OSError, ValueError):
        device = ""
    try:
        return parse_usage(request_json(region, token, device))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return unavailable("Kimi authorization unavailable", LOGIN_HELP)
        if error.code == 429:
            return unavailable("Kimi checks rate limited", "Wait for the next scheduled refresh.")
        return unavailable("Kimi usage unavailable", f"Kimi returned HTTP {error.code}; try again later.")
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return unavailable("Kimi usage unavailable", "Could not read Kimi usage; check the connection and retry later.")


if __name__ == "__main__":
    result = collect()
    if not result["limits"]:
        print(f"Kimi: {result['usageStatusText']}. {result['authHelpText']}", file=sys.stderr)
    print(json.dumps(result, allow_nan=False))
