#!/usr/bin/env -S python3 -I
"""OpenCode limits hook: OpenCode Go allowances, plus an optional Zen budget.

OpenCode Go bills each model against its own monthly dollar limit; the
5-hour and weekly allowances are 20% / 50% of it (go-models.json, copied
from opencode.ai/docs/go). Each request therefore uses cost / limit of the
monthly allowance. The windows follow the console's Go page:

  - 5-hour: a block that starts with the first request after the previous
    block ended ("starts on first use");
  - weekly: fixed, from Monday 00:00 UTC;
  - monthly: the subscription period. Set `go.renewsAt` (the console's
    "Renews …" date) for an exact boundary; without it the period is
    anchored to the first Go request on record and the bar says "est.".

Cost is re-priced from each message's tokens rather than read from
opencode.db's `cost`, which always uses a model's base tier: long-context
requests (e.g. GPT 6 Luna over 272K) bill at the higher tier, and DeepSeek
bills peak hours higher. Reasoning tokens bill as output.

The Zen bar is opt-in (`zen.monthlyBudget`, the workspace budget policy in
the console's Controls): recorded Zen spend this calendar month (UTC) over
that budget. OpenCode's small-model calls (titles) are billed but not
stored, so it reads slightly low.

Optional user config: $XDG_CONFIG_HOME/omarchy/agent-collectors/opencode.json

  {"go": {"plan": "go" | "go-plus", "renewsAt": "2026-10-31T14:26:00Z"},
   "zen": {"monthlyBudget": 40}}

Prints a limits array; exits 1 (no meters) when there is nothing to show.
The console's own numbers sit behind a browser session cookie, so they are
deliberately not used.
"""
import calendar
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
UTC = timezone.utc
GO_PROVIDER = "opencode-go"
ZEN_PROVIDER = "opencode"
QUERY = (
    "SELECT time_created, json_extract(data,'$.providerID'), json_extract(data,'$.modelID'), "
    "json_extract(data,'$.tokens.input'), json_extract(data,'$.tokens.output'), "
    "json_extract(data,'$.tokens.reasoning'), json_extract(data,'$.tokens.cache.read'), "
    "json_extract(data,'$.tokens.cache.write'), json_extract(data,'$.cost') "
    "FROM message WHERE json_extract(data,'$.role')='assistant' "
    "AND json_extract(data,'$.providerID') IN (?, ?) ORDER BY time_created"
)


def db_path():
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
    return os.path.join(data, "opencode", "opencode.db")


def user_config():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    try:
        with open(os.path.join(base, "omarchy", "agent-collectors", "opencode.json")) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def add_months(dt, n, day):
    y, m = divmod(dt.month - 1 + n, 12)
    y, m = dt.year + y, m + 1
    return dt.replace(year=y, month=m, day=min(day, calendar.monthrange(y, m)[1]))


def period(anchor, now):
    """[start, end) of the monthly period containing now, stepping from anchor."""
    day, n = anchor.day, 0
    while add_months(anchor, n, day) > now:
        n -= 1
    while add_months(anchor, n + 1, day) <= now:
        n += 1
    return add_months(anchor, n, day), add_months(anchor, n + 1, day)


def is_peak(ts, hours):
    t = datetime.fromtimestamp(ts, UTC)
    return t.weekday() < 5 and any(a <= t.hour < b for a, b in hours)


def go_cost(table, model, ts, tin, tout, cread, cwrite, recorded):
    """USD for one request at Go prices; unknown models keep opencode's cost."""
    spec = table["models"].get(model)
    if not spec:
        return recorded
    tiers = spec["tiers"]
    if spec.get("peakTiers") and is_peak(ts, table["peakHoursUTC"]):
        tiers = spec["peakTiers"]
    ctx = tin + cread + cwrite
    _, pin, pout, pread, pwrite = next((t for t in tiers if t[0] is None or ctx <= t[0]), tiers[-1])
    return (tin * pin + tout * pout + cread * pread + cwrite * (pin if pwrite is None else pwrite)) / 1e6


def bar(title, percent, resets):
    return {"label": title, "title": title, "percent": round(min(max(percent, 0.0), 1.0), 4),
            "resetsAt": resets.isoformat()}


def go_bars(table, cfg, msgs, now):
    """msgs: [(ts, share of the monthly allowance)], oldest first."""
    win = table["windows"]
    renews = cfg.get("renewsAt")
    try:
        anchor = datetime.fromisoformat(str(renews).replace("Z", "+00:00")) if renews else None
    except ValueError:
        anchor = None
    estimated = anchor is None
    if estimated:
        anchor = datetime.fromtimestamp(msgs[0][0], UTC)
    p_start, p_end = period(anchor, now)
    week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    block_start, block = None, 0.0
    for ts, share in msgs:
        if block_start is None or ts >= block_start + 5 * 3600:
            block_start, block = ts, 0.0
        block += share
    block_end = datetime.fromtimestamp(block_start + 5 * 3600, UTC)
    active = block_end > now

    weekly = sum(s for ts, s in msgs if ts >= week.timestamp())
    monthly = sum(s for ts, s in msgs if ts >= p_start.timestamp())
    return [
        # An idle block starts on first use: a past resetsAt shows no countdown.
        bar("Go 5-hour", block / win["fiveHour"] if active else 0.0, block_end if active else now),
        bar("Go weekly", weekly / win["weekly"], week + timedelta(days=7)),
        bar("Go monthly · est." if estimated else "Go monthly", monthly / win["monthly"], p_end),
    ]


def main(now=None):
    now = now or datetime.now(UTC)
    with open(os.path.join(HERE, "go-models.json")) as fh:
        table = json.load(fh)
    cfg = user_config()
    go_cfg, zen_cfg = cfg.get("go") or {}, cfg.get("zen") or {}
    path = db_path()
    if not os.path.exists(path):
        return 1
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        rows = con.execute(QUERY, (GO_PROVIDER, ZEN_PROVIDER)).fetchall()
        con.close()
    except sqlite3.Error:
        return 1

    col = 1 if go_cfg.get("plan") == "go-plus" else 0
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    go_msgs, zen_spend = [], 0.0
    for ms, provider, model, tin, tout, reason, cread, cwrite, cost in rows:
        ts = ms / 1000.0
        if provider == ZEN_PROVIDER:
            if ts >= month.timestamp():
                zen_spend += cost or 0
            continue
        usd = go_cost(table, model, ts, tin or 0, (tout or 0) + (reason or 0),
                      cread or 0, cwrite or 0, cost or 0)
        lim = ((table["models"].get(model) or {}).get("limit") or table["defaultLimit"])[col]
        go_msgs.append((ts, usd / lim if lim else 0.0))  # null limit = unlimited

    out = go_bars(table, go_cfg, go_msgs, now) if go_msgs else []
    budget = zen_cfg.get("monthlyBudget")
    if isinstance(budget, (int, float)) and budget > 0:
        out.append(bar(f"Zen budget (${budget:g})", zen_spend / budget, add_months(month, 1, 1)))
    if not out:
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
