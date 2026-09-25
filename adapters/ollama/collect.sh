#!/usr/bin/python3
"""Collect hook for the ollama adapter: local tokens for ollama-served models.

The account API (`/api/usage`) reports request counts per window and nothing
about tokens or days, so the panel's TOKENS BY DAY / TOKENS BY MODEL sections
can only come from this machine's agent logs:

- **opencode** messages whose model id is provider-qualified
  (`ollama-cloud/deepseek-v4.1-flash`) — unambiguous, no attribution needed.
- **Pi** session records, attributing every message to the provider in force
  at that point, because Pi logs the provider on `model_change` events and not
  on messages (`aperture-ollama`, `ollama-cloud`, ... match).

These are LOCAL tokens: usage from another machine, or from a tool that does
not log here, cannot appear — the account's request counts and these token
numbers answer different questions and will not agree in size.

Emits one canonical event per line on stdout (`{"ts","session","model",
"kind","input","output","cacheRead","cacheWrite"}`); the engine dedupes by
fingerprint and does the aggregation, so re-emitting the full history every
run is by contract. Nothing is written and no key is read.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime

PI_SESSIONS_GLOB = "~/.pi/agent/sessions/**/*.jsonl"
OPENCODE_DB = "~/.local/share/opencode/opencode.db"
OLLAMA_MODEL_PREFIX = "ollama-cloud/"

# How far back to emit. The engine's per-day window is 8 days and its all-time
# counters are cumulative in its own state, so a fortnight covers the chart
# with slack while keeping the hook well inside the engine's caps. Two of those
# caps bite silently: stdout is killed at 100k lines / 16 MB (and our emission
# is oldest-first, so the newest events would be the ones lost), and the
# engine keeps only 50k event fingerprints per adapter, evicting oldest-first —
# past that an emitted event looks new again and gets counted twice. So the
# hook also bounds itself, below both ceilings, and says so on stderr.
EMIT_DAYS = float(os.environ.get("OLLAMA_COLLECT_DAYS") or 14)
CUTOFF_S = time.time() - EMIT_DAYS * 86400
MAX_LINES = 40_000
MAX_BYTES = 12_000_000

_kept: deque[bytes] = deque()
_kept_bytes = 0
_dropped = 0
_filtered = 0

OPENCODE_QUERY = """
SELECT session_id, time_created,
       json_extract(data, '$.role') AS role,
       json_extract(data, '$.modelID') AS model,
       json_extract(data, '$.tokens.input') AS tin,
       json_extract(data, '$.tokens.output') AS tout,
       json_extract(data, '$.tokens.cache.read') AS tcr,
       json_extract(data, '$.tokens.cache.write') AS tcw
FROM message
WHERE json_extract(data, '$.modelID') LIKE ?
ORDER BY time_created
"""


def model_name(model: str | None, provider: str | None) -> str:
    """Provider-qualified ids become plain model ids.

    The account API calls the model `deepseek-v4.1-flash`, and the panel's
    TOKENS BY MODEL sits next to the account's request counts, so the
    `ollama-cloud/` qualification is dropped (it names the provider, not the
    model). Tags such as `:31b` stay: they are part of the model's identity.
    """
    text = str(model or "").strip()
    if text.startswith(OLLAMA_MODEL_PREFIX):
        text = text[len(OLLAMA_MODEL_PREFIX) :]
    return text or str(provider or "unknown")


def emit(ts, session, model, kind, tokens=(0, 0, 0, 0)) -> None:
    """Queue one canonical event, keeping the newest when the budget runs out.

    Timestamps are normalized to **epoch seconds**: the engine's hook parser
    has no per-line unit (only declarative sources can declare one), so a
    millisecond value would be read as seconds, land in a year ~58000, and be
    silently dropped from the day buckets while still inflating the model and
    prompt totals. Sources differ: Pi logs ISO strings, opencode logs ms.
    """
    global _kept_bytes, _dropped, _filtered
    if isinstance(ts, (int, float)) and ts > 1e11:
        ts = ts / 1000.0
    if not ts:
        _filtered += 1
        return
    if isinstance(ts, (int, float)):
        if ts < CUTOFF_S:
            _filtered += 1
            return
    else:
        try:
            if datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() < CUTOFF_S:
                _filtered += 1
                return
        except ValueError:
            _filtered += 1
            return
    line = json.dumps(
        {
            "ts": ts,
            "session": session,
            "model": model,
            "kind": kind,
            "input": int(tokens[0] or 0),
            "output": int(tokens[1] or 0),
            "cacheRead": int(tokens[2] or 0),
            "cacheWrite": int(tokens[3] or 0),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    _kept.append(line)
    _kept_bytes += len(line)
    while _kept and (len(_kept) > MAX_LINES or _kept_bytes > MAX_BYTES):
        _kept_bytes -= len(_kept.popleft())
        _dropped += 1


def is_ollama(provider: str | None) -> bool:
    return bool(provider) and "ollama" in str(provider).lower()


def collect_pi() -> int:
    """Pi sessions: provider tracking decides which messages are ollama's."""
    emitted = 0
    for path in sorted(glob.glob(os.path.expanduser(PI_SESSIONS_GLOB), recursive=True)):
        provider = None
        session = os.path.basename(path).rsplit(".", 1)[0]
        try:
            handle = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle as fh:
            for line in fh:
                if '"message"' not in line and '"model_change"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, RecursionError):
                    continue
                kind = record.get("type")
                if kind == "model_change":
                    provider = record.get("provider")
                    continue
                if kind != "message" or not is_ollama(provider):
                    continue
                message = record.get("message") or {}
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role == "user":
                    # A prompt carries no tokens but is what the panel counts
                    # ("today: N prompts · M sessions").
                    emit(record.get("timestamp"), session, None, "prompt")
                    emitted += 1
                elif role == "assistant":
                    usage = message.get("usage") or {}
                    emit(
                        record.get("timestamp"),
                        session,
                        model_name(message.get("model"), provider),
                        "completion",
                        (
                            usage.get("input"),
                            usage.get("output"),
                            usage.get("cacheRead"),
                            usage.get("cacheWrite"),
                        ),
                    )
                    emitted += 1
    return emitted


def collect_opencode() -> int:
    """opencode: the model id itself names the provider, so no tracking."""
    path = os.path.expanduser(OPENCODE_DB)
    if not os.path.exists(path):
        return 0
    emitted = 0
    try:
        with sqlite3.connect(f"file:{path}?immutable=1", uri=True) as conn:
            for session, ts, _role, model, tin, tout, tcr, tcw in conn.execute(
                OPENCODE_QUERY, (OLLAMA_MODEL_PREFIX + "%",)
            ):
                emit(ts, session, model_name(model, None), "completion", (tin, tout, tcr, tcw))
                emitted += 1
    except (sqlite3.Error, OSError):
        return emitted
    return emitted


def limits_only() -> bool:
    """True when the caller only wants fresh meters, not fresh token history.

    The panel asks for a refresh the moment it opens, and re-scanning every
    local session log for that is seconds of CPU for numbers that only move
    once a day. The panel fork sets this; the record is still rewritten from
    the engine's cumulative counters, so nothing is lost.
    """
    return os.environ.get("AGENTS_LIMITS_ONLY") == "1"


def main() -> int:
    if limits_only():
        print("collect: limits-only run, no local scan", file=sys.stderr)
        return 0
    # A hook must never fail the engine: a broken source yields no events and
    # the panel keeps whatever the account API already gives it. stdout is
    # written once, at the end, so a mid-run failure still emits whole lines.
    try:
        emitted = collect_pi()
    except Exception as exc:  # noqa: BLE001 - hook boundary
        print(f"ollama collect: pi source failed: {exc}", file=sys.stderr)
        emitted = 0
    try:
        emitted += collect_opencode()
    except Exception as exc:  # noqa: BLE001 - hook boundary
        print(f"ollama collect: opencode source failed: {exc}", file=sys.stderr)
    flush()
    print(f"ollama collect: {emitted} events", file=sys.stderr)
    return 0


def flush() -> None:
    """Write the kept events and report what the bounds did."""
    if _kept:
        sys.stdout.buffer.write(b"\n".join(_kept) + b"\n")
        sys.stdout.buffer.flush()
    if _dropped:
        print(
            f"ollama collect: dropped {_dropped} oldest events (self-cap "
            f"{MAX_LINES} lines / {MAX_BYTES} bytes) — the engine evicts its "
            "fingerprints oldest-first, so emitting past its cap double-counts",
            file=sys.stderr,
        )
    print(
        f"ollama collect: kept {len(_kept)} events, {_kept_bytes} bytes, "
        f"filtered {_filtered} older than {EMIT_DAYS:g}d",
        file=sys.stderr,
    )


if __name__ == "__main__":
    sys.exit(main())
