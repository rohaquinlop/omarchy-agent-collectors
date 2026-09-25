#!/usr/bin/python3
"""Limits hook for the ollama agent-collectors adapter.

Fetches Ollama Cloud account usage and prints the panel limits array.

- API:  GET  https://ollama.com/api/usage (Bearer OLLAMA_API_KEY)
- Plan: POST https://ollama.com/api/me    -> {"Plan": "max"}
- Units: the API reports usage as a fraction where 1.0 = 100% of the
  allowance (see vault: Data-archive/Ollama Cloud Max - Usage Limits
  Calibration). The panel also expects a 0..1 fraction, so no scaling.
- Per-model requests: the same response carries
  limits.<window>.models[] = {name, request_count}. The stock record
  contract has no field for them, so they ride on the limit row as the
  extra key `requestModels` (the engine passes limit rows through
  verbatim; the stock panel ignores keys it does not know). Our panel
  fork renders them as "MODELS USED THIS WEEK".
- Resets: ollama.com has no reset timestamp in the API, and its settings
  page shows a countdown (so the windows are a fixed cadence, not a
  rolling sum). This hook learns the cadence instead of guessing:
  every run appends a sample; a sample that drops to ~0 marks a window
  boundary; the boundary plus the documented period (5h / 7d) predicts
  the next one. The estimate is published only while it keeps being
  corroborated (see `learn_window`) and is flagged with
  `resetsEstimated: true`, which our panel renders as "Resets in ~2h 10m".
  Until a boundary has been observed the countdown stays empty, because a
  wrong countdown is worse than none.
- Plan label: the engine reads tierLabel only from manifest.json, so this
  hook refreshes that key from /api/me when the plan changes.
- Key: OLLAMA_API_KEY from the environment, else the canonical agent
  secrets file. Never printed.

Contract: prints a JSON array on stdout; exit 1 means "no data".

Debugging: `limits.sh --status` prints the learned state instead.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API_URL = "https://ollama.com/api/usage"
ME_URL = "https://ollama.com/api/me"
TIMEOUT_S = 20

# Window name -> (period seconds, reset countdown grace seconds). The period
# is documented and measured (vault: Ollama Cloud Max - Usage Limits
# Calibration); the grace is how late a boundary may land before the anchor
# is treated as contradicted rather than merely mis-sampled.
WINDOWS = {"session": 5 * 3600, "weekly": 7 * 86400}
GRACE_S = {"session": 3000, "weekly": 3600}

# Windows are read from whatever the response carries: paid accounts answer with
# session/weekly, free ones with `monthly` alone. Only the rolling windows are
# learned — a monthly pool resets on a calendar day, so it is computed from the
# account's signup date instead.
TITLES = {"session": "Session", "weekly": "Weekly", "monthly": "Monthly"}
LABELS = {
    "session": "Session (5-hour)",
    "weekly": "Weekly (7-day)",
    "monthly": "Monthly",
}
LEARNED_WINDOWS = ("session", "weekly")

SPAN_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

# A boundary can only be pinned to a sample when the samples are close
# enough together; a drop seen across a suspend or a long idle tells us a
# reset happened somewhere in the gap, not when.
MAX_GAP_S = 2700

# A seeded anchor (a countdown read off the ollama.com dashboard, which
# states whole hours/days) is coarse on purpose, so it gets a wider grace
# than an observed boundary before it is treated as contradicted.
GRACE_SEED_S = {"session": 2 * 3600, "weekly": 12 * 3600}

# A zero sample that turns back into usage means the window restarted while
# nothing was being used. The anchor's phase is unverifiable from there on,
# so the estimate is withdrawn — unless that zero is the boundary itself,
# which is the case for a while after an observed drop.
RESUME_TRUST_S = 2 * MAX_GAP_S

# A drop is a boundary only if it is large in both absolute and relative
# terms and lands near zero: usage that merely rolls off a window would not.
MIN_DROP = 0.01
MIN_DROP_FRACTION = 0.5
MAX_POST_DROP = 0.15
ZERO = 0.0005
RESUMED = 0.002


def account_key() -> str:
    """Which account this run is for, as a file-name-safe key.

    One Ollama account per key, so the account is what the cache must be keyed
    by: a shared cache would let a failed fetch for account B show account A's
    meters under B's label. `OLLAMA_USAGE_ACCOUNT` names it explicitly (what a
    multi-account registration sets); otherwise the variable that supplied the
    key is the best identity available.
    """
    explicit = os.environ.get("OLLAMA_USAGE_ACCOUNT") or ""
    if explicit:
        return re.sub(r"[^A-Za-z0-9._-]", "-", explicit)[:48]
    return _slug(_KEY_SOURCE or account_sources()[0])


def _slug(source: str) -> str:
    """A file-name-safe identity from a key source.

    The env-file path is part of the identity: two accounts may both use a file
    called ollama.env in different directories, and the digest keeps those
    apart without depending on how much of the path fits.
    """
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:10]
    return re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(source))[:24] + "-" + digest


def account_sources() -> list[str]:
    """Every identity this run could be for, best first.

    Needed when no key was found: the identity cannot come from the key then,
    and a constant fallback bucket would never match the cache a working run
    wrote. These are the same candidates the key search walks, so one of them
    is the account whose cache is on disk.
    """
    variable = os.environ.get("OLLAMA_USAGE_VAR") or "OLLAMA_API_KEY"
    sources = []
    if _KEY_SOURCE:
        sources.append(_KEY_SOURCE)
    for candidate in key_files():
        sources.append(os.path.expanduser(candidate) + ":" + variable)
    # A key that came from the environment wrote its cache under an
    # env-sourced identity, and in the no-key path `_KEY_SOURCE` is empty by
    # definition — without this the meters of such an account vanish instead of
    # saying "sign in again". One bucket per variable, so it cannot collide
    # with a file-sourced account's cache.
    sources.append("env:" + variable)
    return sources or ["default"]


def last_limits_path() -> str:
    return os.path.join(
        os.path.dirname(state_path()), f"last-limits-{account_key()}.json"
    )


def read_last_limits(reason: str = "unreachable") -> list:
    """Last good meters for *this* account, for a run that cannot reach the API.

    Refuses a cache written by another account, and refuses one that records
    no account at all: a wrong number is worse than a missing one.
    """
    if os.environ.get("OLLAMA_USAGE_ACCOUNT"):
        candidates = [last_limits_path()]
    else:
        directory = os.path.dirname(state_path())
        candidates = [
            os.path.join(directory, f"last-limits-{_slug(source)}.json")
            for source in account_sources()
        ]
    for candidate in candidates:
        rows = _cached_rows(candidate, reason)
        if rows:
            return rows
    return []


def _cached_rows(path: str, reason: str) -> list:
    """Rows from one cache file, or [] when missing or another account's."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    if str(data.get("account") or "") not in ({_slug(s) for s in account_sources()} | {account_key()}):
        return []
    rows = data.get("limits")
    if not isinstance(rows, list):
        return []
    # The engine stamps a record's own updatedAt with the clock of *this*
    # run, so the rows have to carry when they were really fetched or the
    # panel would certify stale meters as current.
    fetched = str(data.get("updatedAt") or "")
    return [
        {**row, "stale": True, "staleReason": reason, "staleFetchedAt": fetched}
        for row in rows
        if isinstance(row, dict)
    ]


def write_last_limits(limits: list) -> None:
    path = last_limits_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".limits.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "account": account_key(),
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


_KEY_SOURCE: str = ""


def state_path() -> str:
    """Where this account's samples live.

    The learned phase is per account: each window anchor comes from that
    account's own observed boundary, so one shared file would have every run
    compare its percent against whichever account sampled last — losing real
    boundaries and inventing others. Unaccounted runs keep the legacy
    filename, which is what the single-account set-up has always used.
    """
    root = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    directory = os.path.join(root, "omarchy", "ollama-usage")
    if os.environ.get("OLLAMA_USAGE_ACCOUNT"):
        return os.path.join(directory, f"state-{account_key()}.json")
    return os.path.join(directory, "state.json")


def key_files() -> list[str]:
    """Where to look for a KEY=value file, in order.

    `OLLAMA_USAGE_ENV` may hold a colon-separated list, the way PATH does, so
    a machine that keeps one shared secrets file can point at it without
    changing this adapter. Defaults: the omarchy config dir, then a shared
    `agent-secrets` file.
    """
    configured = os.environ.get("OLLAMA_USAGE_ENV")
    if configured:
        return [part for part in configured.split(":") if part]
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return [
        os.path.join(config, "omarchy", "agents", "ollama.env"),
        os.path.join(config, "agent-secrets", ".env"),
    ]


def api_key() -> str | None:
    """The API key from the environment or a KEY=value file. Never printed."""
    global _KEY_SOURCE
    variable = os.environ.get("OLLAMA_USAGE_VAR") or "OLLAMA_API_KEY"
    key = os.environ.get(variable)
    if key:
        _KEY_SOURCE = "env:" + variable
        return key
    prefix = variable + "="
    for candidate in key_files():
        try:
            with open(os.path.expanduser(candidate), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith(prefix):
                        _KEY_SOURCE = os.path.expanduser(candidate) + ":" + variable
                        value = line.split("=", 1)[1].strip()
                        return value.strip('"').strip("'")
        except OSError:
            continue
    return None


def fetch(key: str, url: str, method: str = "GET") -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + key}, method=method
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.load(resp)


def fraction(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return min(number, 1.0)


def predicted_reset(anchor_s: float, now_s: float, period: int) -> float:
    """The next boundary at or after `now` for a cadence anchored at `anchor`."""
    if now_s <= anchor_s:
        return anchor_s + period
    steps = int((now_s - anchor_s) // period) + 1
    return anchor_s + steps * period


def learn_window(
    state: dict, name: str, value: float, now_s: float
) -> tuple[str, bool, bool]:
    """Fold one sample into the window state.

    Returns (resetsAt ISO or "", estimated, provisional). A window's phase is
    anchored by an observed restart, and four things can happen to that
    anchor:

    - A drop to ~0 means the window just restarted. Pinned to this sample,
      which is up to 15 minutes late, so every countdown built on it is
      published as "~".
    - The same drop seen across a gap too long to pin (a suspend, a long
      idle) still holds if the cadence put a boundary inside that gap.
    - A window that restarted while nothing was being used is invisible to
      us, and a window that restarts on first use instead of on a clock
      would have moved its phase by then. Either way the phase is no longer
      verifiable, so the estimate is withdrawn until the next drop.
    - If a period passes without the drop the anchor predicts, the cadence
      assumption itself failed: withdraw rather than keep drawing a
      countdown that no longer describes the account.

    Before any boundary has been seen the ollama.com dashboard can state the
    phase itself ("Resets in 3 hours" / "Resets in 3 days"). `seed_window`
    turns that into an anchor, which this function then treats as an estimate
    with a wider grace until a boundary is actually observed and replaces it.
    Failing even that, one true thing remains: usage exists now, so the window
    started at or before the first sample that showed it, and the reset cannot
    be later than that first sight plus the period. That upper bound is
    published as `provisional` and survives only until a boundary is observed.
    Read it as "no later than": the reset can only come sooner, so it never
    promises time the account may not have — and it is the loosest useful
    statement, which is why the learned estimate replaces it at the first
    boundary.

    The anchor is deliberately sticky while it is trusted: only the first
    and second cases move it. The last two withdraw the estimate *and* the
    anchor, so a phase that failed is never revived by a later unverifiable
    drop. A countdown that has been wrong once is worth less than no
    countdown at all.
    """
    period = WINDOWS[name]
    prev = state.get("value")
    prev_at = state.get("atMs")
    # Anchor and boundary are seconds (the sample clock); atMs is the only
    # millisecond value here, as its name says.
    anchor = state.get("anchorS")
    active = bool(state.get("active")) and isinstance(anchor, (int, float))
    seeded = state.get("seeded") is True and isinstance(anchor, (int, float))
    boundary_s = (
        state["boundaryMs"] / 1000.0
        if isinstance(state.get("boundaryMs"), (int, float))
        else None
    )
    first_seen = state.get("firstSeenS")
    if not isinstance(first_seen, (int, float)):
        first_seen = None
    dropped = False

    if isinstance(prev, (int, float)) and isinstance(prev_at, (int, float)):
        prev_at_s = prev_at / 1000.0
        gap = now_s - prev_at_s
        drop = prev - value
        threshold = max(MIN_DROP, MIN_DROP_FRACTION * prev)
        # The boundary the cadence says fell at or after the previous sample
        # (the second of slack keeps a boundary landing exactly on that
        # sample from being skipped as "strictly after").
        due = (
            predicted_reset(anchor, prev_at_s - 1, period)
            if isinstance(anchor, (int, float))
            else None
        )
        if drop >= threshold and value <= MAX_POST_DROP:
            # Any drop means the window restarted inside this gap, so the
            # current run of usage began at or after this sample.
            dropped = True
            if 0 < gap <= MAX_GAP_S:
                # A boundary pinned to this sample is fresh evidence whatever
                # the state was: it re-anchors even after a withdrawal, and
                # it replaces a seeded phase with a precise one.
                anchor = boundary_s = now_s
                seeded = False
                active = True
            elif seeded and (
                (due is None) or prev_at_s <= due <= now_s
                or abs(now_s - due) <= GRACE_SEED_S[name]
            ):
                # A coarse seeded phase that the drop roughly bears out: keep
                # the anchor and, with it, the wider seeded grace, until a
                # pinned drop replaces it.
                if due is not None and prev_at_s <= due <= now_s:
                    boundary_s = due
                active = True
            elif active and due is not None and prev_at_s <= due <= now_s:
                # Unpinnable drop, but the live cadence already placed a
                # boundary in that gap: keep the phase, remember where it was.
                boundary_s = due
                active = True
            else:
                active = False
        elif seeded:
            # Trust the stated phase until its first boundary is due; if even
            # the wide seeded grace passes with no reset, the statement was
            # wrong and the estimate goes away.
            due_seed = predicted_reset(anchor, prev_at_s - 1, period)
            if now_s > due_seed + GRACE_SEED_S[name]:
                seeded = False
                active = False
        elif (
            active
            and due is not None
            and value > ZERO
            and now_s > due + GRACE_S[name]
        ):
            active = False
        elif (
            prev <= ZERO
            and value > RESUMED
            and (boundary_s is None or now_s - boundary_s > RESUME_TRUST_S)
        ):
            active = False

        # A withdrew estimate takes its anchor with it. Keeping a phase the
        # rules above just rejected would let a later unpinnable drop revive
        # it (the corroboration branch predicts from the anchor), which is
        # exactly the wrong countdown this hook is meant never to publish.
        if not active:
            anchor = boundary_s = None
            seeded = False

    anchor = float(anchor) if isinstance(anchor, (int, float)) else None

    # Provisional upper bound, for the stretch before the first observed
    # boundary: the first sample of the current unbroken non-zero run is the
    # latest the window can have started (a sample showing zero cannot have
    # been part of it), and one period after that is the latest it can end.
    # Both paths agree on the direction, so `≤` is always the right prefix.
    if active or value <= ZERO:
        first_seen = None
    elif dropped or first_seen is None:
        first_seen = now_s

    state.update(
        {
            "value": value,
            "atMs": int(now_s * 1000),
            "anchorS": anchor,
            "firstSeenS": first_seen,
            "boundaryMs": int(boundary_s * 1000) if boundary_s is not None else None,
            "seeded": bool(seeded and anchor is not None),
            "active": bool(active and anchor is not None),
        }
    )

    if state["active"]:
        reset = predicted_reset(state["anchorS"], now_s, period)
        return (
            datetime.fromtimestamp(reset, timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds"),
            True,
            False,
        )
    if first_seen is not None and now_s < first_seen + period:
        # A full period of non-zero usage with no restart would disprove the
        # cadence, so the bound stops rather than roll forward for ever.
        return (
            datetime.fromtimestamp(first_seen + period, timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds"),
            False,
            True,
        )
    return "", False, False


def request_models(window) -> list[dict]:
    """Per-model request counts for one window, heaviest first."""
    rows = (window or {}).get("models") if isinstance(window, dict) else None
    out = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            count = row.get("request_count")
            name = row.get("name")
            if isinstance(count, int) and isinstance(name, str) and name:
                out.append({"name": name, "requests": count})
    out.sort(key=lambda item: item["requests"], reverse=True)
    return out[:12]


def sync_tier_label(plan: str) -> None:
    """Keep manifest.json's tierLabel equal to the live plan.

    The engine only ever reads the plan label from the manifest, so the
    manifest is where a changed plan has to land. Only that one key is
    touched, and only when it actually differs.
    """
    label = plan.strip().capitalize()
    if not label:
        return
    # The caller's adapter dir, not this script's: the per-account shims share
    # this implementation, and each plan label belongs to its own manifest.
    directory = os.environ.get("OLLAMA_USAGE_ADAPTER_DIR") or os.path.dirname(
        os.path.abspath(__file__)
    )
    path = os.path.join(directory, "manifest.json")
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return
    if manifest.get("tierLabel") == label:
        return
    manifest["tierLabel"] = label
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".manifest.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def seed_window(win: dict, name: str, remaining_s: float, now_s: float) -> float:
    """Anchor a window from a countdown stated by the ollama.com dashboard.

    The dashboard reports whole hours and days ("Resets in 3 hours"), so this
    phase is coarse by construction. It is published as an estimate, trusted
    until its first boundary is due (with the wider GRACE_SEED_S), and
    replaced by a precise anchor the moment a boundary is observed.
    """
    period = WINDOWS[name]
    anchor = now_s - (period - remaining_s)
    win.update(
        {
            "anchorS": anchor,
            "seeded": True,
            "active": True,
            "firstSeenS": None,
            "boundaryMs": None,
        }
    )
    return anchor


def parse_span(text: str) -> float | None:
    """'3h', '2h30m', '3 days', '90 minutes' -> seconds, or None."""
    total = 0.0
    found = False
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", text.lower()):
        if unit not in SPAN_UNITS:
            return None
        total += float(number) * SPAN_UNITS[unit]
        found = True
    return total if found and total > 0 else None


def seed(resets: list[str]) -> int:
    """CLI: `--seed session=3h weekly=3d`."""
    now_s = datetime.now(timezone.utc).timestamp()
    wanted: list[tuple[str, float]] = []
    for item in resets:
        name, _, text = item.partition("=")
        name = name.strip().lower()
        seconds = parse_span(text)
        if name not in WINDOWS:
            print(f"unknown window {name!r}; want one of {sorted(WINDOWS)}", file=sys.stderr)
            return 2
        if seconds is None:
            print(f"cannot read a countdown out of {text!r}", file=sys.stderr)
            return 2
        if seconds > WINDOWS[name]:
            print(
                f"{name}: {text!r} is longer than the {WINDOWS[name] // 3600}h period",
                file=sys.stderr,
            )
            return 2
        wanted.append((name, seconds))
    if not wanted:
        print("usage: limits.sh --seed session=3h weekly=3d", file=sys.stderr)
        return 2

    state = load_state()
    for name, seconds in wanted:
        seed_window(state.setdefault(name, {}), name, seconds, now_s)
        print(
            f"{name}: seeded reset at "
            + datetime.fromtimestamp(now_s + seconds, timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds")
            + f" (in {seconds / 3600:.2f}h, from the stated countdown)"
        )
    save_state(state)
    return 0


def load_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".state.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def next_monthly_reset(created_at: str, now_s: float) -> str:
    """The next occurrence of the signup day-of-month, or "".

    ollama.com/pricing: "On the Free plan, usage resets monthly from the date
    you signed up" (paid plans: the subscription start day). The usage response
    carries no monthly reset field, so the day is derived from the account's
    own CreatedAt rather than reported, and the row is marked estimated so it
    never reads as a reported timestamp.
    """
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(created_at or ""))
    if not match:
        return ""
    day = int(match.group(3))
    now = datetime.fromtimestamp(now_s, timezone.utc).astimezone()
    year, month = now.year, now.month
    for _ in range(2):
        try:
            candidate = datetime(year, month, day, tzinfo=now.tzinfo)
        except ValueError:
            # A 31st signup in a 30-day month: the last day of that month.
            nxt = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=now.tzinfo)
            candidate = nxt - timedelta(days=1)
        if candidate > now:
            return candidate.isoformat(timespec="seconds")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return ""


def build_limits(data: dict, state: dict, now_s: float, created_at: str = "") -> list[dict]:
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        return []
    out = []
    for name in ("session", "weekly", "monthly"):
        entry = TITLES[name]
        window = limits.get(name)
        percent = fraction((window or {}).get("usage"))
        if percent is None:
            continue
        if name in LEARNED_WINDOWS:
            resets_at, estimated, provisional = learn_window(
                state.setdefault(name, {}), name, percent, now_s
            )
        else:
            resets_at, estimated, provisional = next_monthly_reset(created_at, now_s), True, False
        row = {
            "label": LABELS[name],
            "title": entry,
            "percent": percent,
            "resetsAt": resets_at,
        }
        if estimated:
            row["resetsEstimated"] = True
        if provisional:
            row["resetsProvisional"] = True
        models = request_models(window)
        if models:
            row["requestModels"] = models
        out.append(row)
    return out


def main() -> int:
    now_s = datetime.now(timezone.utc).timestamp()
    key = api_key()
    if not key:
        # No key at all: this adapter is normally skipped by its detect gate,
        # so say why rather than leaving an empty record behind.
        print("ollama limits: no OLLAMA_API_KEY found", file=sys.stderr)
        cached = read_last_limits("auth")
        if cached:
            print(json.dumps(cached))
            return 0
        return 1
    try:
        data = fetch(key, API_URL)
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError) as exc:
        # A rejected key is not the same problem as an unreachable host, and
        # the chip should say which one it is rather than nothing.
        rejected = isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403)
        if rejected:
            print(
                "ollama limits: the API key was rejected — check OLLAMA_API_KEY",
                file=sys.stderr,
            )
        cached = read_last_limits("auth" if rejected else "unreachable")
        if cached:
            print(json.dumps(cached))
            return 0
        return 1

    # One writer at a time: the engine's 15-minute run and a manual run can
    # overlap, and both read-modify-write the same sample state.
    lock_path = state_path() + ".lock"
    lock_fd = None
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError:
        lock_fd = None

    state = load_state()
    try:
        account = fetch(key, ME_URL, "POST")
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError):
        account = {}
    sync_tier_label(str(account.get("Plan") or ""))
    limits = build_limits(data, state, now_s, str(account.get("CreatedAt") or ""))
    if not limits:
        # A 200 that parses to no windows means the response shape moved. The
        # cached meters are not wrong, just unrefreshed — and an empty array
        # would hide the section while the footer still claimed a fresh run.
        print("ollama limits: no windows in the response — shape may have changed",
              file=sys.stderr)
        cached = read_last_limits("unrefreshed")
        if cached:
            print(json.dumps(cached))
            return 0
        return 1
    if lock_fd is not None:
        save_state(state)

    write_last_limits(limits)
    print(json.dumps(limits))
    return 0


if __name__ == "__main__":
    if "--status" in sys.argv:
        print(json.dumps(load_state(), indent=2, sort_keys=True))
        sys.exit(0)
    if "--seed" in sys.argv:
        sys.exit(seed(sys.argv[sys.argv.index("--seed") + 1 :]))
    sys.exit(main())
