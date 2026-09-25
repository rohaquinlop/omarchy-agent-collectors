#!/usr/bin/python3
"""Sanity tests for the ollama collect hook's event stream.

The engine reads hook lines with no way to declare a timestamp unit, so a
source that logs milliseconds (opencode) and one that logs ISO strings (Pi)
must be normalized before printing or the events land in the wrong day — or in
no day at all, silently, while still inflating the model totals. That is the
class of bug these tests exist for; they assert invariants across *every*
emitted event rather than a hand-picked sample.

    python3 ~/.config/omarchy/agent-collectors/adapters/ollama/collect_test.py
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "collect.sh")
DAY = 86400.0
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else ' — ' + detail}")


def run_hook() -> list[dict]:
    proc = subprocess.run(
        [HOOK], capture_output=True, text=True, timeout=180, check=False
    )
    if proc.returncode != 0:
        FAILURES.append(f"hook exited {proc.returncode}: {proc.stderr[-300:]}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def main() -> int:
    now = time.time()
    events = run_hook()
    print(f"hook emitted {len(events)} events")
    check("hook emits events", len(events) > 0)

    stamps, bad_ts, future = [], 0, 0
    for ev in events:
        ts = ev["ts"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        if not isinstance(ts, (int, float)) or ts > 1e11:
            bad_ts += 1
            continue
        stamps.append(ts)
        if ts > now + DAY:
            future += 1

    check("every timestamp is epoch seconds, not milliseconds", bad_ts == 0, f"{bad_ts} bad")
    check("no event is dated in the future", future == 0, f"{future} future")
    if stamps:
        check(
            "timestamps sit inside the emission window",
            min(stamps) > now - 40 * DAY,
            f"oldest is {(now - min(stamps)) / DAY:.1f}d old",
        )

    kinds = collections.Counter(ev["kind"] for ev in events)
    check("prompts carry no tokens", all(
        ev["input"] == ev["output"] == ev["cacheRead"] == ev["cacheWrite"] == 0
        for ev in events if ev["kind"] == "prompt"
    ))
    check("completions carry a model and tokens", all(
        ev["model"] and ev["model"] != "unknown"
        for ev in events if ev["kind"] == "completion"
    ), f"kinds: {dict(kinds)}")
    check("model ids are not provider-qualified", not any(
        str(ev["model"]).startswith("ollama-cloud/") for ev in events
    ))
    check("sessions are named", all(ev["session"] for ev in events))

    # The day buckets the panel draws key off localtime, so the days must come
    # out as real dates (a unit slip shows up as a year ~58000).
    per_day: collections.Counter = collections.Counter()
    for ev in events:
        stamp = ev["ts"]
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        per_day[time.strftime("%Y-%m-%d", time.localtime(stamp))] += 1
    check("every event lands on a real date", all(day.startswith("20") for day in per_day))
    check("today is represented", time.strftime("%Y-%m-%d", time.localtime(now)) in per_day)
    print(f"  days: {dict(sorted(per_day.items())[-4:])}")

    print(f"\n{'all invariants hold' if not FAILURES else str(len(FAILURES)) + ' FAILURES'}")
    for failure in FAILURES:
        print(" -", failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
