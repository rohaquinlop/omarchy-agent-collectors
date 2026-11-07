#!/usr/bin/env python3
"""Read personal Cursor subscription quotas using official CLI credentials."""

import json
import math
import os
from pathlib import Path
from datetime import datetime, timezone
import urllib.error
import urllib.request

API = "https://api2.cursor.sh/aiserver.v1.DashboardService/"
METHODS = {"GetCurrentPeriodUsage", "GetPlanInfo"}
RESPONSE_CAP = 1024 * 1024
FILE_CAP = 64 * 1024
LOGIN_HELP = "Run `cursor-agent login` to sign in again. Credentials are read-only."


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "cursor"


def read_object(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(FILE_CAP + 1)
    if len(raw) > FILE_CAP:
        raise ValueError("File exceeds size limit")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Bearer credentials must never follow a redirect, even to another HTTPS host.
        return None


def request_json(method: str, token: str) -> dict:
    if method not in METHODS:
        raise ValueError("Unsupported Cursor method")
    request = urllib.request.Request(
        API + method,
        data=b"{}",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        },
        method="POST",
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
            raw = response.read(RESPONSE_CAP + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise
    if len(raw) > RESPONSE_CAP:
        raise ValueError("Response exceeds size limit")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


def unavailable(status: str, help_text: str) -> dict:
    return {"limits": [], "usageStatusText": status, "authHelpText": help_text}


def parse_usage(usage: dict, plan: dict) -> dict:
    missing = unavailable("Cursor usage unavailable", "Cursor returned no usable subscription quota.")
    if not isinstance(usage, dict) or usage.get("enabled") is False:
        return missing
    quota = usage.get("planUsage")
    if not isinstance(quota, dict):
        return missing
    value = quota.get("totalPercentUsed")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return missing
    try:
        percent = float(value)
    except (ValueError, OverflowError):
        return missing
    if not math.isfinite(percent) or percent < 0:
        return missing
    info = plan.get("planInfo") if isinstance(plan, dict) else None
    if not isinstance(info, dict):
        info = {}
    period = info.get("includedUsagePeriod")
    label = {
        "INCLUDED_USAGE_PERIOD_WEEKLY": "Weekly",
        "INCLUDED_USAGE_PERIOD_MONTHLY": "Monthly",
    }.get(period if isinstance(period, str) else "", "Subscription")
    limit = {"label": label, "percent": min(percent / 100, 1)}
    reset = info.get("includedUsageResetsAt")
    if not isinstance(reset, bool) and isinstance(reset, (int, float, str)):
        try:
            millis = float(reset)
            if math.isfinite(millis) and millis > 0:
                limit["resetsAt"] = datetime.fromtimestamp(millis / 1000, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    name = info.get("planName")
    tier = "".join(c for c in name[:128] if c.isprintable()) if isinstance(name, str) else ""
    return {"limits": [limit], "tierLabel": tier, "usageStatusText": "", "authHelpText": ""}


def collect() -> dict:
    directory = config_dir()
    try:
        auth = read_object(directory / "auth.json")
        token = auth.get("accessToken")
        if not isinstance(token, str) or not token.strip() or not token.isascii() or any(c.isspace() or ord(c) < 32 for c in token):
            raise ValueError("Missing or invalid access token")
    except (OSError, ValueError, RecursionError):
        return unavailable("Cursor sign-in required", LOGIN_HELP)
    try:
        try:
            config = read_object(directory / "cli-config.json")
        except FileNotFoundError:
            config = {}
        scope = config.get("authInfo")
        if scope is None:
            scope = {}
        if not isinstance(scope, dict):
            raise ValueError("Invalid auth scope")
        if any(scope.get(key) is not None for key in ("activeTeamId", "teamId")):
            return unavailable("Cursor team scope unsupported", "This adapter currently supports personal subscriptions only; no personal quota was queried.")
    except (OSError, ValueError, RecursionError):
        return unavailable("Cursor configuration unavailable", "Check the official CLI's cli-config.json; no quota was queried.")
    try:
        usage = request_json("GetCurrentPeriodUsage", token)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return unavailable("Cursor authorization unavailable", LOGIN_HELP)
        if error.code == 429:
            return unavailable("Cursor checks rate limited", "Wait for the next scheduled refresh.")
        return unavailable("Cursor usage unavailable", f"Cursor returned HTTP {error.code}; try again later.")
    except (OSError, ValueError, RecursionError):
        return unavailable("Cursor usage unavailable", "Could not read Cursor usage; check the connection and retry later.")
    try:
        plan = request_json("GetPlanInfo", token)
    except (OSError, ValueError, RecursionError):
        # A plan lookup is optional; never invent a quota reset from a billing date.
        plan = {}
    return parse_usage(usage, plan)


if __name__ == "__main__":
    print(json.dumps(collect(), allow_nan=False))
