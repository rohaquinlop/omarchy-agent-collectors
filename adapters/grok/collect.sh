#!/usr/bin/python3
"""Collect hook for the grok adapter: local tokens for SuperGrok usage.

The billing endpoint behind `limits.sh` reports the weekly allowance and
nothing about tokens or days, so the panel's TOKENS BY DAY / TOKENS BY MODEL
sections are filled from what this machine can observe of the same
subscription:

- **The Grok CLI's own accounting** (`~/.grok/sessions/*/*/updates.jsonl`,
  `turn_completed` events) — the authoritative per-turn usage, including a
  `usage.modelUsage` breakdown per model.
- **Pi** sessions whose provider is a grok one (`aperture-grok`, which runs on
  the same SuperGrok credentials), attributed per message because Pi logs the
  provider on `model_change` events and not on messages.
- **opencode** messages whose model id is qualified `grok-sub/` — that is the
  SuperGrok-backed id. `opencode-go/...` is a different plan and is left out.
- **Prompts** from `~/.grok/sessions/*/prompt_history.jsonl`, so the panel's
  prompt/session counts cover the CLI too.

These are LOCAL tokens: other machines, and the Grok web app, cannot appear.
Emits one canonical event per line on stdout; the engine dedupes by
fingerprint and aggregates. No key is read and nothing is written.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from collections import deque
from datetime import datetime

PI_SESSIONS_GLOB = "~/.pi/agent/sessions/**/*.jsonl"
OPENCODE_DB = "~/.local/share/opencode/opencode.db"
GROK_MODEL_PREFIXES = ("grok-sub/",)


def grok_home() -> str:
    """The Grok CLI's home: $GROK_HOME when set, else ~/.grok.

    The limits hook reads the OIDC session from the same place, and the
    manifest's detect gate resolves it the same way, so all three agree.
    """
    return os.environ.get("GROK_HOME") or os.path.join(os.path.expanduser("~"), ".grok")


def grok_glob(pattern: str) -> str:
    return os.path.join(grok_home(), "sessions", pattern)

# See the ollama adapter's collect.sh for the reasoning behind each bound: the
# engine kills stdout at 100k lines / 16 MB and evicts event fingerprints
# oldest-first past 50k, after which re-emitted events are counted twice. Both
# failures are silent, so the hook stays well below them and reports itself.
EMIT_DAYS = float(os.environ.get("GROK_COLLECT_DAYS") or 14)
CUTOFF_S = time.time() - EMIT_DAYS * 86400
MAX_LINES = 40_000
MAX_BYTES = 12_000_000

_kept: deque[bytes] = deque()
_kept_bytes = 0
_dropped = 0
_filtered = 0


def emit(ts, session, model, kind, tokens=(0, 0, 0, 0)) -> None:
    """Queue one canonical event with an epoch-seconds timestamp."""
    global _kept_bytes, _dropped, _filtered
    if isinstance(ts, (int, float)) and ts > 1e11:
        ts = ts / 1000.0
    if isinstance(ts, (int, float)):
        if ts < CUTOFF_S:
            _filtered += 1
            return
    else:
        try:
            if datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() < CUTOFF_S:
                _filtered += 1
                return
        except (ValueError, TypeError):
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


def plain_model(model: str | None) -> str:
    """Drop a provider qualification; model ids are what the panel lists."""
    text = str(model or "").strip()
    for prefix in GROK_MODEL_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text or "grok"


def usage_tokens(usage: dict) -> tuple[int, int, int, int]:
    return (
        usage.get("inputTokens") or usage.get("input") or 0,
        usage.get("outputTokens") or usage.get("output") or 0,
        usage.get("cachedReadTokens") or usage.get("cacheRead") or 0,
        usage.get("cacheCreationTokens") or usage.get("cacheWrite") or 0,
    )


def collect_grok_cli() -> int:
    """The CLI's own turn accounting: one event per model per finished turn."""
    emitted = 0
    for path in sorted(glob.glob(grok_glob("*/*/updates.jsonl"))):
        session = os.path.basename(os.path.dirname(path))
        fallback = None
        try:
            handle = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle as fh:
            for line in fh:
                if "turn_completed" not in line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, RecursionError):
                    continue
                update = (record.get("params") or {}).get("update") or {}
                if update.get("sessionUpdate") != "turn_completed":
                    continue
                usage = update.get("usage")
                if not isinstance(usage, dict):
                    continue
                per_model = usage.get("modelUsage")
                if isinstance(per_model, dict) and per_model:
                    names = list(per_model.keys())
                    fallback = fallback or names[0]
                else:
                    # A turn the CLI did not break down by model: attribute it
                    # to the model this session has already been using, which
                    # is what the surrounding turns report.
                    per_model = {fallback or "grok": usage}
                for name, bucket in per_model.items():
                    if not isinstance(bucket, dict):
                        continue
                    emit(
                        record.get("timestamp"),
                        session,
                        plain_model(name),
                        "completion",
                        usage_tokens(bucket),
                    )
                    emitted += 1
    return emitted


def collect_grok_prompts() -> int:
    emitted = 0
    for path in sorted(glob.glob(grok_glob("*/prompt_history.jsonl"))):
        try:
            handle = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, RecursionError):
                    continue
                session = record.get("session_id") or "grok-cli"
                emit(record.get("timestamp"), session, None, "prompt")
                emitted += 1
    return emitted


def collect_pi() -> int:
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
                if kind != "message" or "grok" not in str(provider or "").lower():
                    continue
                message = record.get("message") or {}
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role == "user":
                    emit(record.get("timestamp"), session, None, "prompt")
                    emitted += 1
                elif role == "assistant":
                    usage = message.get("usage") or {}
                    emit(
                        record.get("timestamp"),
                        session,
                        plain_model(message.get("model")),
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
    path = os.path.expanduser(OPENCODE_DB)
    if not os.path.exists(path):
        return 0
    import sqlite3

    emitted = 0
    query = """
    SELECT session_id, time_created, json_extract(data, '$.modelID'),
           json_extract(data, '$.tokens.input'), json_extract(data, '$.tokens.output'),
           json_extract(data, '$.tokens.cache.read'), json_extract(data, '$.tokens.cache.write')
    FROM message
    WHERE json_extract(data, '$.modelID') LIKE 'grok-sub/%'
    ORDER BY time_created
    """
    try:
        with sqlite3.connect(f"file:{path}?immutable=1", uri=True) as conn:
            for session, ts, model, tin, tout, tcr, tcw in conn.execute(query):
                emit(ts, session, plain_model(model), "completion", (tin, tout, tcr, tcw))
                emitted += 1
    except sqlite3.Error:
        return emitted
    return emitted


def flush() -> None:
    if _kept:
        sys.stdout.buffer.write(b"\n".join(_kept) + b"\n")
        sys.stdout.buffer.flush()
    if _dropped:
        print(
            f"grok collect: dropped {_dropped} oldest events (self-cap "
            f"{MAX_LINES} lines / {MAX_BYTES} bytes) — past the engine's "
            "fingerprint cap re-emitted events are counted twice",
            file=sys.stderr,
        )
    print(
        f"grok collect: kept {len(_kept)} events, {_kept_bytes} bytes, "
        f"filtered {_filtered} older than {EMIT_DAYS:g}d",
        file=sys.stderr,
    )


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
    reported = 0
    for name, source in (
        ("grok-cli", collect_grok_cli),
        ("grok-prompts", collect_grok_prompts),
        ("pi", collect_pi),
        ("opencode", collect_opencode),
    ):
        try:
            reported += source()
        except Exception as exc:  # noqa: BLE001 - hook boundary
            print(f"grok collect: {name} source failed: {exc}", file=sys.stderr)
    flush()
    print(f"grok collect: {reported} events seen", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
