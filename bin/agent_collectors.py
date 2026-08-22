#!/usr/bin/env python3
"""agent-collectors — usage records for AI coding agents, for Omarchy's agents panel.

One engine, many adapters. Each adapter describes one agent's local session
data; the engine normalizes it into canonical events, aggregates them, and
writes one stock-contract JSON record per agent into
~/.local/state/omarchy/agents/usage/ where the omarchy.agents panel watches.

Memory model: bounded and streamed. State stores per-file append cursors,
per-source last-timestamp cursors, and per-adapter counter blocks only.
Collectors yield events one at a time into the accumulator; no event list is
materialized in memory, regardless of history size.

Usage:
  agent-collectors [--force] [--except <id>] ... [<id> ...]
  agent-collectors --validate [--adapters-dir <dir>]

Exit codes: 0 ok, 1 at least one adapter failed.
"""

from __future__ import annotations

import argparse
import base64
import bisect
import copy
import glob as globmod
import hashlib
import json
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- paths

HOME = os.path.expanduser("~")
STATE_HOME = os.environ.get(
    "XDG_STATE_HOME", os.path.join(HOME, ".local", "state")
)
CACHE_HOME = os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache"))
USAGE_DIR = os.path.join(STATE_HOME, "omarchy", "agents", "usage")
STATE_FILE = os.path.join(
    CACHE_HOME, "omarchy", "agent-collectors", "state.json"
)
USER_ADAPTERS_DIR = os.path.join(
    HOME, ".config", "omarchy", "agent-collectors", "adapters"
)
SHELL_JSON = os.path.join(HOME, ".config", "omarchy", "shell.json")
OMARCHY_BIN = os.path.join(
    os.environ.get("OMARCHY_PATH", "/usr/share/omarchy"), "bin"
)
BUILTIN_ADAPTERS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "adapters")
)

SCHEMA_VERSION = 1  # record contract version (what the panel consumes)
STATE_SCHEMA_VERSION = 3  # engine state version

# ---------------------------------------------------------------- caps

# Persistent state and per-run memory stay bounded by these, never by history.
SESSION_SET_CAP = 10_000  # unique session ids kept per adapter
MODEL_CAP = 50  # model buckets kept per adapter (+ "other")
ACTIVE_DATES_CAP = 730  # distinct active days kept per adapter
DAY_WINDOW = 8  # per-day token counters kept (today + 7)
STATE_LOAD_GUARD = 64 * 1024 * 1024  # state files larger than this are ignored
HOOK_TIMEOUT_S: int = 120  # collect hook timeout
LIMITS_TIMEOUT_S: int = 30  # limits hook timeout
HOOK_EVENT_CAP: int = 100_000  # max stdout lines read from a collect hook
HOOK_STDOUT_CAP: int = (
    1024 * 1024
)  # max stdout bytes read from a hook (lines or doc)
HOOK_OUTPUT_BYTES_CAP: int = (
    16 * 1024 * 1024
)  # max cumulative bytes of collect-hook lines kept
HOOK_STDERR_CAP: int = 1024 * 1024  # max stderr bytes spooled from a hook
HOOK_FP_CAP: int = 50_000  # max hook event fingerprints kept per adapter
BOUNDARY_FP_CAP: int = 10_000  # max boundary-timestamp fingerprints per sqlite source
DETECT_TIMEOUT_S: int = 15  # detect command timeout
PARTIAL_CAP = 8192  # max straddle-line tail bytes kept per jsonl file
JSONL_FILES_CAP: int = 10_000  # max files matched per jsonl source per run
JSONL_LINE_CAP: int = 1024 * 1024  # max bytes of a single jsonl line parsed
SQLITE_ROW_BYTES_CAP: int = 1024 * 1024  # max bytes of string fields in a sqlite row
SQLITE_FIELD_CAP: int = 256  # max chars retained for a session/model string
HEAD_LEN = 64  # first bytes fingerprinted to detect file replacement


def log(msg: str) -> None:
    print(f"agent-collectors: {msg}", file=sys.stderr)


def expand(path: str) -> str:
    return os.path.expanduser(path)


# ---------------------------------------------------------------- helpers


def get_path(obj, dotted):
    """Follow a dot-separated path into nested dicts/lists; None when absent."""
    cur = obj
    for part in (dotted or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def to_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def parse_ts(value, unit="s") -> float | None:
    """Parse an ISO timestamp or an epoch number into epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts / 1000.0 if unit == "ms" and ts > 1e11 else ts
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        ts = float(text)
        return ts / 1000.0 if unit == "ms" and ts > 1e11 else ts
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def day_key(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(epoch_seconds))


def make_event(
    ts, session, model, kind, inp=0, out=0, cread=0, cwrite=0, unit="s"
) -> dict | None:
    ts = parse_ts(ts, unit)
    if ts is None:
        return None
    return {
        "ts": ts,
        "session": str(session or ""),
        "model": str(model or "unknown"),
        "kind": kind,
        "input": to_int(inp),
        "output": to_int(out),
        "cacheRead": to_int(cread),
        "cacheWrite": to_int(cwrite),
    }


def file_ends_newline(path: str, size: int) -> bool:
    """True when the file's last byte is a newline (no partial final line)."""
    if size <= 0:
        return False
    try:
        with open(path, "rb") as fh:
            fh.seek(size - 1)
            return fh.read(1) == b"\n"
    except OSError:
        return False


def file_partial(path: str, size: int, cap: int = PARTIAL_CAP) -> str:
    """Unterminated tail: text after the last newline ("" when none)."""
    if size <= 0:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - cap))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    idx = data.rfind("\n")
    return data[idx + 1 :] if idx != -1 else data


def file_head(path: str, size: int) -> str:
    """Base64 of the first HEAD_LEN bytes; detects file replacement."""
    if size <= 0:
        return ""
    try:
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read(HEAD_LEN)).decode()
    except OSError:
        return ""


def event_fp(ev: dict) -> str:
    """Fingerprint of a canonical event; used for hook dedupe."""
    raw = json.dumps(
        [
            ev["ts"],
            ev["session"],
            ev["model"],
            ev["kind"],
            ev["input"],
            ev["output"],
            ev["cacheRead"],
            ev["cacheWrite"],
        ]
    )
    return hashlib.sha1(raw.encode()).hexdigest()


# ------------------------------------------------- jsonl-lines collector


def jsonl_line_to_event(obj: dict, cfg: dict, path: str) -> dict | None:
    """One parsed jsonl line -> canonical event, or None when filtered."""
    if cfg["kinds"] and get_path(obj, cfg["kindPath"]) not in cfg["kinds"]:
        return None
    role = get_path(obj, cfg["rolePath"])
    sid = get_path(obj, cfg["sessionPath"]) if cfg["sessionPath"] else None
    if not sid:
        sid = os.path.splitext(path)[0]
    if role == cfg["promptRole"]:
        return make_event(get_path(obj, cfg["tsPath"]), sid, None, "prompt")
    if role == cfg["completionRole"]:
        t = cfg["tokens"]
        return make_event(
            get_path(obj, cfg["tsPath"]),
            sid,
            get_path(obj, cfg["modelPath"]),
            "completion",
            get_path(obj, t.get("input", "")) if t.get("input") else 0,
            get_path(obj, t.get("output", "")) if t.get("output") else 0,
            get_path(obj, t.get("cacheRead", "")) if t.get("cacheRead") else 0,
            get_path(obj, t.get("cacheWrite", ""))
            if t.get("cacheWrite")
            else 0,
        )
    return None


def iter_bounded_lines(fh, cap: int = JSONL_LINE_CAP):
    """Yield lines up to cap bytes; oversized lines are drained and yield None.

    Each readline call allocates at most cap + 1 bytes, so no physical line
    can force unbounded allocation before parsing.
    """
    while True:
        line = fh.readline(cap + 1)
        if not line:
            return
        if not line.endswith("\n") and len(line) > cap:
            while line and not line.endswith("\n"):
                line = fh.readline(cap + 1)
            yield None
            continue
        yield line


def collect_jsonl_lines(
    source: dict, adapter_dir: str, state: dict, force: bool
):
    """Yield events new since the last run. Cache holds cursors only.

    Cache entry per file: {"sig", "offset", "newline", "partial", "head"}.
    Unchanged file -> skipped. Grown file -> only the appended bytes are parsed
    (append-only assumption; a mid-file rewrite that grows the file is not
    detected, unless the head fingerprint changes, which forces a full
    rescan). Shrunk/rotated file -> full rescan from offset 0. A stored
    straddle tail is re-attached to the first line of the new chunk. Events
    are yielded one at a time; nothing accumulates.
    """
    files: list[str] = []
    seen: set[str] = set()
    overflow = 0
    for p in globmod.iglob(expand(source["glob"]), recursive=True):
        if p in seen:
            continue
        seen.add(p)
        if len(files) >= JSONL_FILES_CAP:
            overflow += 1
            continue
        files.append(p)
    if overflow:
        log(
            f"{source['glob']}: more than {JSONL_FILES_CAP} files matched;"
            f" {overflow} extra skipped"
        )
    files.sort()
    cache_key = f"jsonl:{source['glob']}"
    cache = state.get(cache_key)
    if not isinstance(cache, dict):
        cache = {}
    new_cache = {}
    cfg = {
        "kinds": set(source.get("kinds") or []),
        "kindPath": source.get("kindPath", ""),
        "rolePath": source.get("rolePath", ""),
        "promptRole": source.get("promptRole", "user"),
        "completionRole": source.get("completionRole", "assistant"),
        "tsPath": source.get("timestampPath", "timestamp"),
        "tsUnit": source.get("timestampUnit", "iso"),
        "modelPath": source.get("modelPath", ""),
        "sessionPath": source.get("sessionIdPath", ""),
        "tokens": source.get("tokens") or {},
    }
    for path in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        cached = cache.get(path)
        if not force and isinstance(cached, dict) and cached.get("sig") == sig:
            new_cache[path] = cached
            continue
        append_only = (
            not force
            and isinstance(cached, dict)
            and st.st_size >= cached.get("offset", 0)
            and file_head(path, st.st_size) == cached.get("head", None)
        )
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                partial = ""
                skip_first = False
                if append_only:
                    fh.seek(cached.get("offset", 0))
                    if "partial" in cached:
                        partial = cached["partial"]
                    else:  # legacy entry: fall back to the skip heuristic
                        skip_first = cached.get(
                            "offset", 0
                        ) > 0 and not cached.get("newline", True)
                first = True
                for line in iter_bounded_lines(fh):
                    if line is None:
                        log(
                            f"{path}: line exceeds {JSONL_LINE_CAP} bytes; skipped"
                        )
                        if first:
                            partial = ""  # straddle tail belongs to that line
                        continue
                    if first:
                        first = False
                        if partial:
                            if len(partial) + len(line) > JSONL_LINE_CAP:
                                log(
                                    f"{path}: re-attached line exceeds"
                                    f" {JSONL_LINE_CAP} bytes; skipped"
                                )
                                partial = ""
                                while not line.endswith("\n"):
                                    line = fh.readline(JSONL_LINE_CAP + 1)
                                    if not line:
                                        break
                                continue
                            line = partial + line  # re-attach the straddle tail
                            partial = ""
                        elif (
                            skip_first
                        ):  # legacy: continuation of an unterminated last line
                            continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ev = jsonl_line_to_event(obj, cfg, path)
                    if ev:
                        yield ev
        except OSError as e:
            log(f"{path}: {e}")
            if isinstance(cached, dict):
                new_cache[path] = cached  # retry next run
            continue
        new_cache[path] = {
            "sig": sig,
            "offset": st.st_size,
            "newline": file_ends_newline(path, st.st_size),
            "partial": file_partial(path, st.st_size),
            "head": file_head(path, st.st_size),
        }

    state[cache_key] = new_cache


# ---------------------------------------------- sqlite-query collector


def db_sig(db_path: str):
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    sig = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    wal = db_path + "-wal"
    try:
        if os.path.exists(wal):
            ws = os.stat(wal)
            sig["wal"] = [ws.st_size, ws.st_mtime_ns]
    except OSError:
        pass  # WAL checkpointed between exists() and stat()
    return sig


def hashlib_key(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def collect_sqlite_query(
    source: dict, adapter_dir: str, state: dict, force: bool
):
    """Yield rows new since the last run, streamed row by row.

    Cache entry: {"sig", "lastTs", "boundary"} — never event lists. With a ts
    column the user query is wrapped as SELECT * FROM (<query>) WHERE "<ts>"
    >= ? so only new rows are fetched; rows sharing the boundary timestamp are
    deduplicated by fingerprint. Requires ascending ts order in the query
    (adapter queries SHOULD ORDER BY the timestamp column). Without a ts
    column a full streaming rescan runs on every signature change. Rows are
    yielded one at a time; nothing accumulates.
    """
    db_path = expand(source["database"])
    query = source["query"]
    cols = source.get("columns") or {}
    unit = source.get("timestampUnit", "s")

    cache_key = f"sqlite:{source['database']}:{hashlib_key(query)}"
    cached = state.get(cache_key)
    if not isinstance(cached, dict):
        cached = {}
    sig = db_sig(db_path)
    if not force and cached.get("sig") == sig:
        return []

    ts_col = cols.get("ts", "")
    last_ts = (
        cached.get("lastTs")
        if (not force and isinstance(cached, dict))
        else None
    )
    boundary_fps = set(cached.get("boundary") or []) if not force else set()
    run_query, params = query, ()
    if ts_col and last_ts is not None:
        quoted = ts_col.replace('"', '""')
        run_query = f'SELECT * FROM ({query}) WHERE "{quoted}" >= ?'
        params = (last_ts,)

    max_raw = last_ts
    new_boundary: list[str] = []
    uri = f"file:{db_path}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            cur = con.execute(run_query, params)
            if cur.description is None:
                raise RuntimeError(
                    f"sqlite {db_path}: query returned no columns"
                )
            names = [d[0] for d in cur.description]
            row_warned = False
            for row in cur:  # streamed; the result set is never materialized
                row = dict(zip(names, row))
                if any(
                    isinstance(v, str) and len(v) > SQLITE_ROW_BYTES_CAP
                    for v in row.values()
                ):
                    if not row_warned:
                        log(
                            f"sqlite {db_path}: row exceeds"
                            f" {SQLITE_ROW_BYTES_CAP} bytes; skipped"
                        )
                        row_warned = True
                    continue
                if ts_col:
                    raw = row.get(ts_col)
                    if raw is not None and (max_raw is None or raw > max_raw):
                        max_raw = raw
                role = row.get(cols.get("role", ""))
                sid = row.get(cols.get("sessionId", ""))
                if isinstance(sid, str) and len(sid) > SQLITE_FIELD_CAP:
                    sid = sid[:SQLITE_FIELD_CAP]
                model = row.get(cols.get("model", ""))
                if isinstance(model, str) and len(model) > SQLITE_FIELD_CAP:
                    model = model[:SQLITE_FIELD_CAP]
                kind = (
                    "prompt"
                    if role == source.get("promptRole", "user")
                    else (
                        "completion"
                        if role == source.get("completionRole", "assistant")
                        else None
                    )
                )
                if kind is None:
                    continue
                ev = make_event(
                    row.get(cols.get("ts", "")),
                    sid,
                    model,
                    kind,
                    row.get(cols.get("input", ""), 0)
                    if kind == "completion"
                    else 0,
                    row.get(cols.get("output", ""), 0)
                    if kind == "completion"
                    else 0,
                    row.get(cols.get("cacheRead", ""), 0)
                    if kind == "completion"
                    else 0,
                    row.get(cols.get("cacheWrite", ""), 0)
                    if kind == "completion"
                    else 0,
                    unit,
                )
                if not ev:
                    continue
                if boundary_fps and event_fp(ev) in boundary_fps:
                    continue  # already merged at the boundary timestamp
                if (
                    ts_col
                    and raw is not None
                    and max_raw is not None
                    and raw == max_raw
                ):
                    new_boundary.append(event_fp(ev))
                    if len(new_boundary) > BOUNDARY_FP_CAP:
                        new_boundary.pop(0)
                yield ev
        finally:
            con.close()
    except sqlite3.Error as e:
        raise RuntimeError(f"sqlite {db_path}: {e}")

    state[cache_key] = {
        "sig": sig,
        "lastTs": max_raw,
        "boundary": new_boundary if ts_col else [],
    }


# ------------------------------------------------------- hook collector


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the hook and any process it spawned (runaway-guard)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _hook_run(
    script: str,
    adapter_dir: str,
    timeout: float,
    env_extra: dict | None,
    max_lines: int | None = None,
    max_bytes: int | None = None,
):
    """Run a hook with bounded output capture.

    stdout is read with a deadline and capped (by lines plus a cumulative
    byte budget for collect hooks, by bytes for limits hooks); the process is
    killed on cap or timeout. stderr is
    spooled to a memory-bounded temp file (rolls to disk past the cap).
    Returns (status, output, stderr_tail, stderr_capped, returncode) where
    status is "ok", "capped", or "timeout" and output is a list of lines or a
    byte string depending on the cap mode.
    """
    proc = subprocess.Popen(
        [os.path.join(adapter_dir, script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=adapter_dir,
        env={**os.environ, **(env_extra or {})},
        start_new_session=True,  # own process group: runaway children can be killed
    )
    assert proc.stdout is not None and proc.stderr is not None
    stdout, stderr = proc.stdout, proc.stderr
    stderr_capped = {"v": False}

    def pump_stderr():
        while True:
            chunk = stderr.read(65536)
            if not chunk:
                return
            if spool.tell() + len(chunk) > HOOK_STDERR_CAP:
                stderr_capped["v"] = True
                return
            spool.write(chunk)

    with tempfile.SpooledTemporaryFile(
        max_size=HOOK_STDERR_CAP, mode="wb"
    ) as spool:
        stderr_thread = threading.Thread(target=pump_stderr, daemon=True)
        stderr_thread.start()

        sel = selectors.DefaultSelector()
        sel.register(stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        buf = b""
        total = 0  # cumulative bytes appended to lines (collect hook budget)
        lines: list[str] = []
        raw_out = bytearray()
        status = "ok"
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_group(proc)
                    status = "timeout"
                    break
                if not sel.select(min(remaining, 1.0)):
                    continue
                chunk = os.read(stdout.fileno(), 65536)
                if not chunk:
                    break
                if max_bytes is not None:
                    if len(raw_out) + len(chunk) > max_bytes:
                        raw_out.extend(chunk[: max_bytes - len(raw_out)])
                        _kill_group(proc)
                        status = "capped"
                        break
                    raw_out.extend(chunk)
                else:
                    if len(buf) + len(chunk) > HOOK_STDOUT_CAP:
                        _kill_group(proc)
                        status = "capped"
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = raw.decode("utf-8", "replace").strip()
                        if line:
                            lines.append(line)
                            total += len(line)
                            if (
                                max_lines is not None
                                and len(lines) >= max_lines
                            ) or total > HOOK_OUTPUT_BYTES_CAP:
                                _kill_group(proc)
                                status = "capped"
                                break
                    if status == "capped":
                        break
            if max_bytes is None and buf.strip():
                tail = buf.decode("utf-8", "replace").strip()
                if total + len(tail) <= HOOK_OUTPUT_BYTES_CAP:
                    lines.append(tail)
        finally:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                proc.wait()
                status = "timeout"
            stderr_thread.join(timeout=5)
            for fd in (stdout, stderr):
                try:
                    fd.close()
                except OSError:
                    pass
            sel.close()
        spool.seek(0)
        stderr_tail = spool.read(HOOK_STDERR_CAP).decode("utf-8", "replace")
        output: list[str] | bytes = (
            lines if max_bytes is None else bytes(raw_out)
        )
        return status, output, stderr_tail, stderr_capped["v"], proc.returncode


def collect_hook(adapter: dict, adapter_dir: str, force: bool):
    """Yield events parsed from a collect hook's stdout, one at a time."""
    script = adapter.get("collect")
    if not script:
        return
    status, output, stderr_tail, stderr_capped, returncode = _hook_run(
        script,
        adapter_dir,
        HOOK_TIMEOUT_S,
        {"FORCE": "1" if force else ""},
        max_lines=HOOK_EVENT_CAP,
    )
    if stderr_capped:
        log(
            f"{adapter['id']} collect hook: stderr capped at {HOOK_STDERR_CAP} bytes"
        )
    if status == "timeout":
        raise RuntimeError(
            f"collect hook timed out: {stderr_tail.strip()[:300]}"
        )
    if status == "capped":
        log(
            f"{adapter['id']} collect hook: stdout capped at {HOOK_EVENT_CAP}"
            f" lines or {HOOK_OUTPUT_BYTES_CAP} bytes"
        )
    elif returncode != 0:
        raise RuntimeError(f"collect hook failed: {stderr_tail.strip()[:300]}")
    for line in output:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = make_event(
            obj.get("ts"),
            obj.get("session"),
            obj.get("model"),
            obj.get("kind"),
            obj.get("input"),
            obj.get("output"),
            obj.get("cacheRead"),
            obj.get("cacheWrite"),
        )
        if ev:
            yield ev


COLLECTORS = {
    "jsonl-lines": collect_jsonl_lines,
    "sqlite-query": collect_sqlite_query,
}


# ------------------------------------------------------------ accumulate


def fresh_stats() -> dict:
    return {
        "promptsTotal": 0,
        "sessions": [],
        "sessionsEvicted": 0,
        "activeDates": [],
        "activeDatesEvicted": 0,
        "modelUsage": {},
        "today": {
            "day": "",
            "prompts": 0,
            "tokens": 0,
            "sessions": [],
            "sessionsEvicted": 0,
            "tokensByModel": {},
        },
        "days": {},
        "hookFp": [],
    }


def merge_hook_events(stats: dict, events, now: datetime | None = None) -> int:
    """Merge hook events, skipping fingerprints already merged before.

    Hooks are stateless and may re-emit their full history; the per-adapter
    fingerprint list (capped) makes each distinct event count once. The
    intermediate list is bounded by the hook output caps (lines and
    cumulative bytes).
    """
    fps = set(stats["hookFp"])
    fresh = []
    for ev in events:
        fp = event_fp(ev)
        if fp in fps:
            continue
        fresh.append(ev)
        fps.add(fp)
        stats["hookFp"].append(fp)
        if len(stats["hookFp"]) > HOOK_FP_CAP:
            del stats["hookFp"][: len(stats["hookFp"]) - HOOK_FP_CAP]
            fps = set(stats["hookFp"])
    return merge_events(stats, fresh, now=now)


def _model_key(mapping: dict, model: str) -> str:
    if model in mapping or len(mapping) < MODEL_CAP:
        return model
    return "other"


def merge_events(stats: dict, events, now: datetime | None = None) -> int:
    """Merge an iterable of events into a counter block; return event count.

    Consumes the iterable inline (generators included); memory is bounded by
    caps plus one event.
    """
    now = now or datetime.now(timezone.utc).astimezone()
    today = now.strftime("%Y-%m-%d")
    t = stats["today"]
    if t["day"] != today:
        t.clear()
        t.update(
            {
                "day": today,
                "prompts": 0,
                "tokens": 0,
                "sessions": [],
                "sessionsEvicted": 0,
                "tokensByModel": {},
            }
        )
    cutoff = (now - timedelta(days=DAY_WINDOW - 1)).strftime("%Y-%m-%d")
    stats["days"] = {d: v for d, v in stats["days"].items() if d >= cutoff}

    sessions = stats["sessions"]
    sessions_seen = set(sessions)
    active_dates = stats["activeDates"]
    t_sessions = t["sessions"]
    t_sessions_seen = set(t_sessions)
    merged = 0

    for ev in events:
        d = day_key(ev["ts"])
        sid = ev["session"]
        tokens = ev["input"] + ev["output"] + ev["cacheRead"] + ev["cacheWrite"]
        if sid and sid not in sessions_seen:
            if len(sessions) < SESSION_SET_CAP:
                sessions.append(sid)
                sessions_seen.add(sid)
            else:
                stats["sessionsEvicted"] += 1
        if d not in active_dates:
            bisect.insort(active_dates, d)
            if len(active_dates) > ACTIVE_DATES_CAP:
                active_dates.pop(0)
                stats["activeDatesEvicted"] += 1
        stats["days"][d] = stats["days"].get(d, 0) + tokens
        if ev["kind"] == "prompt":
            stats["promptsTotal"] += 1
            if d == today:
                t["prompts"] += 1
        else:
            bucket = stats["modelUsage"].setdefault(
                _model_key(stats["modelUsage"], ev["model"]),
                {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                },
            )
            bucket["inputTokens"] += ev["input"]
            bucket["outputTokens"] += ev["output"]
            bucket["cacheReadInputTokens"] += ev["cacheRead"]
            bucket["cacheCreationInputTokens"] += ev["cacheWrite"]
            if d == today:
                t["tokens"] += tokens
                key = _model_key(t["tokensByModel"], ev["model"])
                t["tokensByModel"][key] = (
                    t["tokensByModel"].get(key, 0) + tokens
                )
        if d == today and sid and sid not in t_sessions_seen:
            if len(t_sessions) < SESSION_SET_CAP:
                t_sessions.append(sid)
            else:
                t["sessionsEvicted"] += 1
            t_sessions_seen.add(sid)
        merged += 1
    return merged


# -------------------------------------------------------------- records


def build_record(
    adapter_id: str,
    name: str,
    stats: dict,
    limits: list,
    status_text: str = "",
    auth_help: str = "",
    tier_label: str = "",
    now: datetime | None = None,
) -> dict:
    today = stats["today"]
    now = now or datetime.now(timezone.utc).astimezone()
    recent = []
    for i in range(DAY_WINDOW - 2, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        recent.append({"date": d, "messageCount": stats["days"].get(d, 0)})
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": adapter_id,
        "name": name,
        "updatedAt": now.astimezone().isoformat(),
        "ready": True,
        "hasLocalStats": True,
        "todayPrompts": today["prompts"],
        "todaySessions": len(today["sessions"]) + today["sessionsEvicted"],
        "todayTotalTokens": today["tokens"],
        "todayTokensByModel": today["tokensByModel"],
        "recentDays": recent,
        "totalPrompts": stats["promptsTotal"],
        "totalSessions": len(stats["sessions"]) + stats["sessionsEvicted"],
        "activeDays": len(stats["activeDates"]) + stats["activeDatesEvicted"],
        "activeDates": list(stats["activeDates"]),
        "modelUsage": stats["modelUsage"],
        "limits": limits or [],
        "tierLabel": tier_label,
        "usageStatusText": status_text,
        "authHelpText": auth_help,
    }


def write_record(record: dict, usage_dir: str) -> None:
    os.makedirs(usage_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=usage_dir, prefix=f".{record['id']}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(usage_dir, f"{record['id']}.json"))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ----------------------------------------------------------- discovery

REQUIRED_SOURCE_KEYS = {"format"}


def load_adapters(
    extra_dirs: list[str],
) -> tuple[list[tuple[dict, str]], list[str]]:
    """Return ([(manifest, dir)], warnings). User dirs override built-ins by id."""
    found: dict[str, tuple[dict, str]] = {}
    warnings: list[str] = []
    search_dirs = []
    for d in extra_dirs:
        search_dirs.append(("builtin", d))
    search_dirs.append(("builtin", BUILTIN_ADAPTERS_DIR))
    search_dirs.append(("user", USER_ADAPTERS_DIR))

    for origin, base in search_dirs:
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            manifest_path = os.path.join(base, entry, "manifest.json")
            if not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path) as fh:
                    m = json.load(fh)
            except (json.JSONDecodeError, OSError) as e:
                warnings.append(
                    f"{origin} adapter {base}/{entry}: bad manifest ({e})"
                )
                continue
            problems = validate_manifest(m)
            if problems:
                warnings.extend(
                    f"{origin} adapter {entry}: {p}" for p in problems
                )
                continue
            found[m["id"]] = (m, os.path.join(base, entry))  # later dirs win
    ordered = sorted(found.values(), key=lambda pair: pair[0]["id"])
    return ordered, warnings


def validate_manifest(m: dict) -> list[str]:
    problems = []
    if m.get("schemaVersion") != SCHEMA_VERSION:
        problems.append(f"schemaVersion must be {SCHEMA_VERSION}")
    if not m.get("id") or not str(m["id"]).replace("-", "").isalnum():
        problems.append("missing/invalid id")
    if not m.get("name"):
        problems.append("missing name")
    sources = m.get("sources")
    hooks = m.get("hooks") or {}
    collect_hook = m.get("collect") or hooks.get("collect")
    if not sources and not collect_hook:
        problems.append("needs sources[] or a collect hook")
    for i, s in enumerate(sources or []):
        missing = REQUIRED_SOURCE_KEYS - set(s)
        if missing:
            problems.append(f"sources[{i}] missing {sorted(missing)}")
        elif s["format"] not in COLLECTORS:
            problems.append(f"sources[{i}] unknown format {s['format']!r}")
        elif s["format"] == "jsonl-lines" and not s.get("glob"):
            problems.append(f"sources[{i}] jsonl-lines needs glob")
        elif s["format"] == "sqlite-query" and not (
            s.get("database") and s.get("query")
        ):
            problems.append(f"sources[{i}] sqlite-query needs database+query")
    return problems


# ------------------------------------------------------------ filters


def detect_ok(manifest: dict) -> bool:
    checks = manifest.get("detect") or []
    for check in checks:
        path = expand(check.get("path", ""))
        if check.get("type") == "command":
            try:
                r = subprocess.run(
                    check["command"],
                    shell=True,
                    check=False,
                    timeout=DETECT_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                log(f"detect command timed out: {check['command']}")
                return False
            if r.returncode != 0:
                return False
        elif not os.path.exists(path):
            return False
    return True


def superseded(adapter_id: str) -> bool:
    return os.path.isfile(
        os.path.join(OMARCHY_BIN, f"omarchy-agent-usage-{adapter_id}")
    )


def provider_disabled(adapter_id: str) -> bool:
    """True when omarchy.agents widget settings disable this provider."""
    try:
        with open(SHELL_JSON) as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False

    def scan(node):
        if isinstance(node, dict):
            if node.get("id") == "omarchy.agents":
                providers = node.get("providers") or {}
                entry = providers.get(adapter_id)
                return isinstance(entry, dict) and entry.get("enabled") is False
            return any(scan(v) for v in node.values())
        if isinstance(node, list):
            return any(scan(v) for v in node)
        return False

    return scan(cfg)


def run_limits_hook(manifest: dict, adapter_dir: str, force: bool) -> list:
    script = manifest.get("limits")
    if not script:
        return []
    status, output, _stderr_tail, stderr_capped, returncode = _hook_run(
        script,
        adapter_dir,
        LIMITS_TIMEOUT_S,
        None,
        max_bytes=HOOK_STDOUT_CAP,
    )
    if stderr_capped:
        log(
            f"{manifest['id']} limits hook: stderr capped at {HOOK_STDERR_CAP} bytes"
        )
    if status == "timeout":
        log(f"{manifest['id']} limits hook: timed out")
        return []
    if status == "capped":
        log(
            f"{manifest['id']} limits hook: stdout capped at {HOOK_STDOUT_CAP} bytes"
        )
    if returncode != 0:  # optional by contract: exit 1 == no data
        return []
    try:
        data = json.loads(output)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []


# ---------------------------------------------------------------- main


def load_state() -> dict:
    """Load v3 state. Older or oversized state is ignored: one full rescan
    rebuilds all counters, so no history is lost and no unbounded allocation
    happens during the upgrade."""
    try:
        if os.path.getsize(STATE_FILE) > STATE_LOAD_GUARD:
            log(f"state file exceeds {STATE_LOAD_GUARD} bytes; ignoring it")
            return {}
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        if (
            not isinstance(data, dict)
            or data.get("schemaVersion") != STATE_SCHEMA_VERSION
        ):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state["schemaVersion"] = STATE_SCHEMA_VERSION
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(STATE_FILE), prefix=".state."
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-collectors")
    parser.add_argument(
        "--force", action="store_true", help="rescan all sources, ignore caches"
    )
    parser.add_argument(
        "--except", dest="except_ids", action="append", default=[]
    )
    parser.add_argument("--usage-dir", default=USAGE_DIR)
    parser.add_argument(
        "--adapters-dir",
        action="append",
        default=[],
        help="extra adapters directory (repeatable)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="list discovered adapters and exit",
    )
    parser.add_argument("ids", nargs="*", help="only these adapter ids")
    args = parser.parse_args(argv)

    adapters, warnings = load_adapters(args.adapters_dir)
    for w in warnings:
        log(w)

    if args.validate:
        for m, d in adapters:
            print(
                f"{m['id']:20} {m['name']:20} {d}"
                + ("  [SUPERSEDED]" if superseded(m["id"]) else "")
            )
        return 0

    wanted = set(args.ids)
    excluded = set(args.except_ids)
    state = load_state()

    status = 0
    ran_any = False
    for manifest, adapter_dir in adapters:
        aid = manifest["id"]
        if wanted and aid not in wanted:
            continue
        if aid in excluded:
            continue
        if superseded(aid):
            log(f"{aid}: skipped, official omarchy-agent-usage-{aid} exists")
            continue
        if provider_disabled(aid):
            log(f"{aid}: skipped, disabled in shell.json")
            continue
        if not detect_ok(manifest):
            continue
        ran_any = True
        try:
            all_stats = state.setdefault("stats", {})
            if (
                args.force or aid not in all_stats
            ):  # force rebuilds from the full scan
                stats = fresh_stats()
            else:
                stats = copy.deepcopy(all_stats[aid])
            run_state = {"stats": state["stats"]}  # cursor caches staged here
            for key, value in state.items():
                if key != "stats":
                    run_state[key] = (
                        value  # previous cursors visible to collectors
                    )
            merged = 0
            for source in manifest.get("sources") or []:
                fn = COLLECTORS[source["format"]]
                merged += merge_events(
                    stats, fn(source, adapter_dir, run_state, args.force)
                )
            hook = manifest.get("collect")
            if hook:
                merged += merge_hook_events(
                    stats,
                    collect_hook(
                        {**manifest, "collect": hook}, adapter_dir, args.force
                    ),
                )
            record = build_record(
                aid,
                manifest["name"],
                stats,
                run_limits_hook(manifest, adapter_dir, args.force),
                status_text=manifest.get("usageStatusText", ""),
                auth_help=manifest.get("authHelpText", ""),
                tier_label=manifest.get("tierLabel", ""),
            )
            # commit: cursors + counters move together, only on success
            for key, value in run_state.items():
                if key != "stats":
                    state[key] = value
            all_stats[aid] = stats
            save_state(state)  # persist cursors+counters before the record
            write_record(record, args.usage_dir)
            print(f"{aid}: {merged} new events -> {aid}.json")
        except Exception as e:  # noqa: BLE001 — isolation: nothing committed; one adapter never blocks others
            log(f"{aid}: FAILED {e}")
            status = 1

    if ran_any or state:
        save_state(state)
    return status


if __name__ == "__main__":
    sys.exit(main())
