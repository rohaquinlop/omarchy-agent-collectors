"""Tests for bin/agent-collectors (stdlib unittest). Run: python3 -m unittest discover tests"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from typing import ClassVar

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "bin"))

import fcntl

import agent_collectors as ac  # engine lives in bin/agent_collectors.py; bin/agent-collectors is a launcher


# Collectors are generators; tests consume them eagerly.
def cj(*a, **k):
    return list(ac.collect_jsonl_lines(*a, **k))


def cs(*a, **k):
    return list(ac.collect_sqlite_query(*a, **k))


def ch(*a, **k):
    return list(ac.collect_hook(*a, **k))


def ev(
    ts, session="s1", model="m", kind="completion", inp=10, out=5, cr=2, cw=1
):
    if kind == "prompt":
        inp = out = cr = cw = 0
    return ac.make_event(ts, session, model, kind, inp, out, cr, cw)


class TestAccumulator(unittest.TestCase):
    def test_totals_today_recent_models(self):
        now = datetime(2026, 8, 21, 18, 0, 0, tzinfo=timezone.utc)
        events = [
            ev("2026-08-21T10:00:00Z", "a", "alpha"),
            ev("2026-08-21T11:00:00Z", "b", "beta", "prompt"),
            ev("2026-08-15T09:00:00Z", "c", "alpha"),  # 6 days back
            ev("2026-07-01T09:00:00Z", "d", "gamma"),  # outside week
        ]
        stats = ac.fresh_stats()
        ac.merge_events(stats, events, now=now)
        rec = ac.build_record("pi", "Pi", stats, [], now=now)
        self.assertEqual(rec["todayPrompts"], 1)
        self.assertEqual(rec["todaySessions"], 2)
        # completion tokens today: 10+5+2+1 = 18
        self.assertEqual(rec["todayTotalTokens"], 18)
        self.assertEqual(rec["totalPrompts"], 1)
        self.assertEqual(rec["totalSessions"], 4)
        self.assertEqual(rec["activeDays"], 3)
        self.assertEqual(
            [d["date"] for d in rec["recentDays"]],
            [
                "2026-08-15",
                "2026-08-16",
                "2026-08-17",
                "2026-08-18",
                "2026-08-19",
                "2026-08-20",
                "2026-08-21",
            ],
        )
        by_date = {d["date"]: d["messageCount"] for d in rec["recentDays"]}
        self.assertEqual(by_date["2026-08-21"], 18)  # tokens, not messages
        self.assertEqual(by_date["2026-08-15"], 18)
        mu = rec["modelUsage"]["alpha"]
        self.assertEqual(
            mu,
            {
                "inputTokens": 20,
                "outputTokens": 10,
                "cacheReadInputTokens": 4,
                "cacheCreationInputTokens": 2,
            },
        )
        self.assertEqual(rec["todayTokensByModel"], {"alpha": 18})

    def test_incremental_merge_matches_full_aggregation(self):
        """Merging new events run by run equals merging everything at once."""
        now = datetime(2026, 8, 21, 18, 0, 0, tzinfo=timezone.utc)
        all_events = [
            ev("2026-08-21T10:00:00Z", "a", "alpha"),
            ev("2026-08-21T11:00:00Z", "b", "beta", "prompt"),
            ev("2026-08-20T09:00:00Z", "c", "gamma"),
            ev("2026-08-15T09:00:00Z", "d", "alpha"),
        ]
        one_shot = ac.fresh_stats()
        ac.merge_events(one_shot, all_events, now=now)

        stepped = ac.fresh_stats()
        for chunk in (all_events[:1], all_events[1:3], all_events[3:]):
            ac.merge_events(stepped, chunk, now=now)
        a = ac.build_record("x", "X", one_shot, [], now=now)
        b = ac.build_record("x", "X", stepped, [], now=now)
        a.pop("updatedAt")
        b.pop("updatedAt")
        self.assertEqual(a, b)

    def test_day_rollover_resets_today(self):
        stats = ac.fresh_stats()
        ac.merge_events(
            stats,
            [ev("2026-08-20T10:00:00Z", "a", "alpha")],
            now=datetime(2026, 8, 20, 18, 0, 0, tzinfo=timezone.utc),
        )
        ac.merge_events(
            stats,
            [ev("2026-08-21T10:00:00Z", "b", "beta")],
            now=datetime(2026, 8, 21, 18, 0, 0, tzinfo=timezone.utc),
        )
        rec = ac.build_record("x", "X", stats, [])
        self.assertEqual(rec["todayPrompts"], 0)
        self.assertEqual(rec["todaySessions"], 1)
        self.assertEqual(rec["todayTotalTokens"], 18)
        self.assertEqual(rec["totalPrompts"], 0)
        self.assertEqual(rec["totalSessions"], 2)
        self.assertEqual(rec["activeDays"], 2)

    def test_session_set_cap_is_approximate_but_bounded(self):
        stats = ac.fresh_stats()
        n = ac.SESSION_SET_CAP + 500
        events = [ev("2026-08-21T10:00:00Z", f"sess{i}", "m") for i in range(n)]
        ac.merge_events(stats, events)
        self.assertEqual(len(stats["sessions"]), ac.SESSION_SET_CAP)
        self.assertEqual(stats["sessionsEvicted"], 500)
        rec = ac.build_record("x", "X", stats, [])
        self.assertEqual(rec["totalSessions"], n)

    def test_days_map_stays_in_window(self):
        now = datetime(2026, 8, 21, 18, 0, 0, tzinfo=timezone.utc)
        stats = ac.fresh_stats()
        ac.merge_events(
            stats,
            [
                ev("2020-01-01T10:00:00Z", "old", "m"),  # before the window
                ev("2030-01-01T10:00:00Z", "future", "m"),  # after today
                ev("2026-08-21T10:00:00Z", "now", "m"),  # today
            ],
            now=now,
        )
        self.assertEqual(stats["days"], {"2026-08-21": 18})
        rec = ac.build_record("x", "X", stats, [], now=now)
        self.assertEqual(rec["totalSessions"], 3)  # counters still count

    def test_model_cap_routes_to_other(self):
        stats = ac.fresh_stats()
        for i in range(ac.MODEL_CAP + 3):
            ac.merge_events(
                stats, [ev("2026-08-21T10:00:00Z", "s", f"model{i}")]
            )
        # MODEL_CAP distinct models plus the "other" bucket
        self.assertEqual(len(stats["modelUsage"]), ac.MODEL_CAP + 1)
        self.assertIn("other", stats["modelUsage"])

    def test_bad_timestamp_dropped(self):
        self.assertIsNone(ev("not-a-date"))


class TestRecordContract(unittest.TestCase):
    CONTRACT_KEYS: ClassVar[set[str]] = {
        "schemaVersion",
        "id",
        "name",
        "updatedAt",
        "ready",
        "hasLocalStats",
        "todayPrompts",
        "todaySessions",
        "todayTotalTokens",
        "todayTokensByModel",
        "recentDays",
        "totalPrompts",
        "totalSessions",
        "activeDays",
        "activeDates",
        "modelUsage",
        "limits",
        "tierLabel",
        "usageStatusText",
        "authHelpText",
    }

    def test_record_has_full_contract(self):
        rec = ac.build_record("pi", "Pi", ac.fresh_stats(), [])
        missing = self.CONTRACT_KEYS - set(rec)
        self.assertEqual(missing, set())

    def test_matches_live_records_when_present(self):
        """Key sets must be a superset of the stock collectors' live output."""
        usage_dir = os.path.expanduser("~/.local/state/omarchy/agents/usage")
        rec = ac.build_record("x", "X", ac.fresh_stats(), [])
        for name in ("claude.json", "codex.json"):
            path = os.path.join(usage_dir, name)
            if not os.path.exists(path):
                self.skipTest(f"{name} not present")
            with open(path) as fh:
                live = json.load(fh)
            self.assertFalse(
                set(live) - set(rec),
                f"record missing live keys: {set(live) - set(rec)}",
            )


class TestJsonlCollector(unittest.TestCase):
    PI_LINE: ClassVar[dict] = {
        "type": "message",
        "id": "m1",
        "timestamp": "2026-08-21T10:00:00Z",
        "message": {
            "role": "assistant",
            "model": "mimo-v2.5-pro",
            "usage": {
                "input": 100,
                "output": 20,
                "cacheRead": 50,
                "cacheWrite": 7,
            },
        },
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = {}

    def write_session(self, lines, name="sess1.jsonl", newline=True):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as fh:
            fh.write("\n".join(json.dumps(l) for l in lines))
            if newline:
                fh.write("\n")
        return p

    def append(self, lines, name="sess1.jsonl"):
        with open(os.path.join(self.tmp, name), "a") as fh:
            fh.write("\n".join(json.dumps(l) for l in lines) + "\n")

    def source(self):
        return {
            "format": "jsonl-lines",
            "glob": os.path.join(self.tmp, "**/*.jsonl"),
            "kindPath": "type",
            "kinds": ["message"],
            "rolePath": "message.role",
            "promptRole": "user",
            "completionRole": "assistant",
            "timestampPath": "timestamp",
            "modelPath": "message.model",
            "tokens": {
                "input": "message.usage.input",
                "output": "message.usage.output",
                "cacheRead": "message.usage.cacheRead",
                "cacheWrite": "message.usage.cacheWrite",
            },
        }

    def test_parse_and_incremental_cursor(self):
        self.write_session([{"type": "session"}, self.PI_LINE])
        events = cj(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["model"], "mimo-v2.5-pro")
        self.assertEqual(events[0]["input"], 100)

        # unchanged rerun: no new events, cursor kept
        again = cj(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(again, [])

        # append a second assistant message -> only the new event is returned
        self.append([dict(self.PI_LINE, id="m2")])
        grown = cj(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(grown), 1)
        self.assertEqual(grown[0]["input"], 100)

    def test_user_message_counts_as_prompt(self):
        self.write_session(
            [
                {
                    "type": "message",
                    "timestamp": "2026-08-21T10:00:00Z",
                    "message": {"role": "user", "content": []},
                },
                self.PI_LINE,
            ]
        )
        events = cj(self.source(), self.tmp, self.state, force=False)
        kinds = sorted(e["kind"] for e in events)
        self.assertEqual(kinds, ["completion", "prompt"])

    def test_truncated_file_rescans(self):
        self.write_session([self.PI_LINE, dict(self.PI_LINE, id="m2")])
        first = cj(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(first), 2)
        # rotate: shorter file with different content
        self.write_session([dict(self.PI_LINE, id="m3")])
        after = cj(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(after), 1)

    def test_unterminated_tail_reattached(self):
        # file ends without newline; the partial line is unparseable and skipped
        p = os.path.join(self.tmp, "sess1.jsonl")
        with open(p, "w") as fh:
            fh.write(json.dumps(self.PI_LINE) + "\n")
            fh.write(
                '{"type": "message", "timestamp": "2026-08-2'
            )  # partial line, no newline
        first = cj(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(first), 1)
        # continuation arrives: the stored tail is re-attached and parsed once
        with open(p, "a") as fh:
            fh.write(
                '1T10:00:00Z", "message": {"role": "assistant", "model": "m", "usage": {}}}'
            )
            fh.write("\n")
            fh.write(json.dumps(dict(self.PI_LINE, id="m2")) + "\n")
        after = cj(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(after), 2)
        self.assertEqual(
            after[0]["model"], "m"
        )  # the re-attached straddle line
        self.assertEqual(after[1]["input"], 100)  # the appended message
        # nothing re-read on a third run
        self.assertEqual(
            cj(self.source(), self.tmp, self.state, force=False), []
        )

    def test_rotated_and_regrown_file_rescans(self):
        self.write_session([self.PI_LINE, dict(self.PI_LINE, id="m2")])
        first = cj(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(first), 2)
        size_before = os.path.getsize(os.path.join(self.tmp, "sess1.jsonl"))
        # replace with different content that grows past the old offset
        lines = [
            dict(self.PI_LINE, id="m3"),
            dict(self.PI_LINE, id="m4"),
            dict(self.PI_LINE, id="m5", timestamp="2026-08-21T11:00:00Z"),
        ]
        self.write_session(lines)
        self.assertGreater(
            os.path.getsize(os.path.join(self.tmp, "sess1.jsonl")), size_before
        )
        after = cj(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(after), 3)  # head mismatch forced a full rescan

    def test_force_rescans_and_returns_everything(self):
        self.write_session([self.PI_LINE])
        cj(self.source(), self.tmp, self.state, force=False)
        again = cj(self.source(), self.tmp, self.state, force=True)
        self.assertEqual(len(again), 1)

    def test_giant_session_id_truncated(self):
        src = self.source()
        src["sessionIdPath"] = "message.sessionId"
        self.write_session(
            [
                {
                    "type": "message",
                    "timestamp": "2026-08-21T10:00:00Z",
                    "message": {
                        "role": "assistant",
                        "model": "m",
                        "sessionId": "s" * 500,
                        "usage": {},
                    },
                }
            ]
        )
        events = cj(src, self.tmp, self.state, force=False)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["session"]), ac.EVENT_FIELD_CAP)

    def test_deeply_nested_line_skipped_not_fatal(self):
        self.write_session(
            [self.PI_LINE]
            + [dict(self.PI_LINE, id="m2")]
        )
        with open(os.path.join(self.tmp, "sess1.jsonl"), "a") as fh:
            fh.write("[" * 100000 + "]" * 100000 + "\n")  # RecursionError
        events = cj(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(events), 2)  # both valid lines parsed; junk skipped
        self.assertEqual(cj(self.source(), self.tmp, self.state, force=False), [])

    def test_oversized_line_skipped(self):
        p = os.path.join(self.tmp, "sess1.jsonl")
        with open(p, "w") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "message",
                        "timestamp": "2026-08-21T10:00:00Z",
                        "message": {
                            "role": "assistant",
                            "model": "m",
                            "usage": {},
                        },
                    }
                )
                + "\n"
            )
            fh.write("x" * (ac.JSONL_LINE_CAP + 100) + "\n")
            fh.write(json.dumps(dict(self.PI_LINE, id="m2")) + "\n")
        events = cj(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(events), 2)  # giant line skipped, cursor stays sane
        # cursor advanced past the giant line: rerun yields nothing
        self.assertEqual(cj(self.source(), self.tmp, self.state, force=False), [])

    def test_file_count_capped(self):
        for i in range(3):
            self.write_session(
                [dict(self.PI_LINE, id=f"m{i}")], name=f"s{i}.jsonl"
            )
        old = ac.JSONL_FILES_CAP
        ac.JSONL_FILES_CAP = 2
        import builtins
        from unittest import mock

        real_open = builtins.open
        with mock.patch("builtins.open") as mo, mock.patch.object(
            ac, "log"
        ) as ml:
            mo.side_effect = real_open
            try:
                events = cj(self.source(), self.tmp, self.state, force=False)
            finally:
                ac.JSONL_FILES_CAP = old
        opened = [c.args[0] for c in mo.call_args_list]
        logged = [str(c.args[0]) for c in ml.call_args_list]
        self.assertEqual(len(events), 2)
        # the glob scan stopped at the cap: exactly 2 files were ever opened
        self.assertEqual(
            len({p for p in opened if p.endswith(".jsonl")}), 2
        )
        self.assertTrue(any("capped at" in m for m in logged))


class TestSqliteCollector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE message (id text PRIMARY KEY, session_id text, time_created integer, data text)"
        )
        rows = [
            ("u1", "s1", 1755770400000, json.dumps({"role": "user"})),
            (
                "a1",
                "s1",
                1755770401000,
                json.dumps(
                    {
                        "role": "assistant",
                        "modelID": "qwen",
                        "tokens": {
                            "input": 9,
                            "output": 3,
                            "cache": {"read": 4, "write": 2},
                        },
                    }
                ),
            ),
        ]
        con.executemany("INSERT INTO message VALUES (?,?,?,?)", rows)
        con.commit()
        con.close()

    def source(self):
        return {
            "format": "sqlite-query",
            "database": self.db,
            "timestampUnit": "ms",
            "promptRole": "user",
            "completionRole": "assistant",
            "query": (
                "SELECT session_id, time_created, json_extract(data,'$.role') AS role,"
                " json_extract(data,'$.modelID') AS model,"
                " json_extract(data,'$.tokens.input') AS tin,"
                " json_extract(data,'$.tokens.output') AS tout,"
                " json_extract(data,'$.tokens.cache.read') AS tcr,"
                " json_extract(data,'$.tokens.cache.write') AS tcw FROM message"
                " ORDER BY time_created"
            ),
            "columns": {
                "ts": "time_created",
                "sessionId": "session_id",
                "role": "role",
                "model": "model",
                "input": "tin",
                "output": "tout",
                "cacheRead": "tcr",
                "cacheWrite": "tcw",
            },
        }

    def test_events_from_db_and_incremental_last_ts(self):
        state = {}
        events = cs(self.source(), self.tmp, state, force=False)
        self.assertEqual(len(events), 2)
        kinds = sorted(e["kind"] for e in events)
        self.assertEqual(kinds, ["completion", "prompt"])

        # unchanged db: no new events
        cached = cs(self.source(), self.tmp, state, force=False)
        self.assertEqual(cached, [])

        # append one row -> only the new row is returned
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO message VALUES (?,?,?,?)",
            (
                "a2",
                "s2",
                1755770402000,
                json.dumps(
                    {
                        "role": "assistant",
                        "modelID": "qwen",
                        "tokens": {
                            "input": 1,
                            "output": 2,
                            "cache": {"read": 3, "write": 4},
                        },
                    }
                ),
            ),
        )
        con.commit()
        con.close()
        grown = cs(self.source(), self.tmp, state, force=False)
        self.assertEqual(len(grown), 1)
        self.assertEqual(grown[0]["session"], "s2")

    def test_readonly_connection_wal_tolerant(self):
        # mode=ro must not fail on an existing -wal sidecar
        open(self.db + "-wal", "a").close()
        events = cs(self.source(), self.tmp, {}, force=True)
        self.assertEqual(len(events), 2)

    def test_boundary_ties_merged_once(self):
        state = {}
        first = cs(self.source(), self.tmp, state, force=False)
        self.assertEqual(len(first), 2)
        # append a row with the SAME ts as the last seen row
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO message VALUES (?,?,?,?)",
            (
                "a3",
                "s3",
                1755770401000,
                json.dumps(
                    {
                        "role": "assistant",
                        "modelID": "qwen",
                        "tokens": {
                            "input": 5,
                            "output": 5,
                            "cache": {"read": 0, "write": 0},
                        },
                    }
                ),
            ),
        )
        con.commit()
        con.close()
        grown = cs(self.source(), self.tmp, state, force=False)
        self.assertEqual(len(grown), 1)
        self.assertEqual(grown[0]["session"], "s3")
        # boundary rows re-fetched but skipped
        self.assertEqual(cs(self.source(), self.tmp, state, force=False), [])

    def test_non_select_query_fails_cleanly(self):
        src = self.source()
        src["query"] = "DELETE FROM message"
        with self.assertRaises(RuntimeError):
            cs(src, self.tmp, {}, force=False)

    def test_oversized_row_skipped(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO message VALUES (?,?,?,?)",
            (
                "big",
                "s9",
                1755770403000,
                json.dumps(
                    {"role": "x" * (ac.SQLITE_ROW_BYTES_CAP + 100)}
                ),
            ),
        )
        con.commit()
        con.close()
        events = cs(self.source(), self.tmp, {}, force=True)
        self.assertEqual(len(events), 2)  # the giant-field row is skipped

    def test_session_string_truncated(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO message VALUES (?,?,?,?)",
            ("u9", "s" * 400, 1755770404000, json.dumps({"role": "user"})),
        )
        con.commit()
        con.close()
        events = cs(self.source(), self.tmp, {}, force=True)
        big = [e for e in events if e["ts"] == 1755770404.0]
        self.assertEqual(len(big), 1)
        self.assertEqual(len(big[0]["session"]), ac.EVENT_FIELD_CAP)


class TestHookBounding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.script = os.path.join(self.tmp, "collect.sh")
        open(self.script, "w").close()
        os.chmod(self.script, 0o755)

    def test_collect_hook_streams_events(self):
        with open(self.script, "w") as fh:
            fh.write(
                '#!/bin/sh\necho \'{"ts": "2026-08-21T10:00:00Z", "session": "s1", "kind": "completion", "input": 1}\'\n'
            )
        events = ch(
            {"id": "x", "collect": "collect.sh"}, self.tmp, force=False
        )
        self.assertEqual(len(events), 1)

    def test_collect_hook_output_capped(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\n")
            fh.writelines(
                'echo \'{"ts": "2026-08-21T10:00:00Z", "session": "s", "kind": "completion"}\'\n'
                for _ in range(ac.HOOK_EVENT_CAP + 50)
            )
        events = ch(
            {"id": "x", "collect": "collect.sh"}, self.tmp, force=False
        )
        self.assertEqual(len(events), ac.HOOK_EVENT_CAP)

    def test_collect_hook_timeout_kills(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\nsleep 30\n")
        old = ac.HOOK_TIMEOUT_S
        ac.HOOK_TIMEOUT_S = 2
        try:
            with self.assertRaises(RuntimeError):
                ch(
                    {"id": "x", "collect": "collect.sh"},
                    self.tmp,
                    force=False,
                )
        finally:
            ac.HOOK_TIMEOUT_S = old

    def test_collect_hook_failure_reported(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\necho oops >&2\nexit 3\n")
        with self.assertRaises(RuntimeError) as ctx:
            ch(
                {"id": "x", "collect": "collect.sh"},
                self.tmp,
                force=False,
            )
        self.assertIn("oops", str(ctx.exception))

    def test_limits_hook_capped_and_optional(self):
        with open(self.script, "w") as fh:
            fh.write('#!/bin/sh\necho \'[{"kind": "tier"}]\'\n')
        limits = ac.run_limits_hook(
            {"id": "x", "limits": "collect.sh"}, self.tmp, force=False
        )
        self.assertEqual(limits, [{"kind": "tier"}])
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        self.assertEqual(
            ac.run_limits_hook(
                {"id": "x", "limits": "collect.sh"}, self.tmp, force=False
            ),
            [],
        )

    def test_single_giant_line_capped(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\n")
            fh.write("head -c 2097152 /dev/zero | tr '\\0' 'a'\n")
            fh.write(
                'echo \'{"ts": "2026-08-21T10:00:00Z", "session": "s", "kind": "completion"}\'\n'
            )
        events = ch(
            {"id": "x", "collect": "collect.sh"}, self.tmp, force=False
        )
        self.assertEqual(events, [])

    def test_collect_hook_cumulative_bytes_capped(self):
        line = json.dumps(
            {
                "ts": "2026-08-21T10:00:00Z",
                "session": "s",
                "kind": "completion",
                "pad": "x" * 60,
            },
            separators=(",", ":"),
        )
        keep = 1000 // len(line) + 1  # crossing line is kept, then killed
        old = ac.HOOK_OUTPUT_BYTES_CAP
        ac.HOOK_OUTPUT_BYTES_CAP = 1000
        try:
            with open(self.script, "w") as fh:
                fh.write("#!/bin/sh\n")
                fh.writelines(f"echo '{line}'\n" for _ in range(50))
            events = ch(
                {"id": "x", "collect": "collect.sh"},
                self.tmp,
                force=False,
            )
        finally:
            ac.HOOK_OUTPUT_BYTES_CAP = old
        self.assertGreater(keep, 0)
        self.assertLess(keep, 50)
        self.assertEqual(len(events), keep)

    def test_daemonized_hook_does_not_hang(self):
        # daemon holds the stderr pipe open after the hook exits
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\n")
            fh.write("setsid sh -c 'sleep 4' >/dev/null &\n")
            fh.write(
                'echo \'{"ts": "2026-08-21T10:00:00Z", "session": "s", "kind": "completion"}\'\n'
            )
        t0 = time.monotonic()
        events = ch(
            {"id": "x", "collect": "collect.sh"}, self.tmp, force=False
        )
        self.assertEqual(len(events), 1)
        self.assertLess(time.monotonic() - t0, 10)

    def test_hook_giant_session_truncated(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\n")
            fh.write(
                'echo \'{"ts": "2026-08-21T10:00:00Z", "session": "'
                + "s" * 500
                + '", "kind": "completion"}\'\n'
            )
        events = ch(
            {"id": "x", "collect": "collect.sh"}, self.tmp, force=False
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["session"]), ac.EVENT_FIELD_CAP)

    def test_hook_deeply_nested_line_skipped(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\n")
            fh.write(
                'echo \'{"ts": "2026-08-21T10:00:00Z", "session": "s", "kind": "completion"}\'\n'
            )
            fh.write("python3 -c \"print('['*100000 + ']'*100000)\"\n")
            fh.write(
                'echo \'{"ts": "2026-08-21T10:00:01Z", "session": "s", "kind": "completion"}\'\n'
            )
        events = ch(
            {"id": "x", "collect": "collect.sh"}, self.tmp, force=False
        )
        self.assertEqual(len(events), 2)


class TestHookDedupe(unittest.TestCase):
    def test_duplicate_events_merged_once(self):
        stats = ac.fresh_stats()
        events = [
            ev("2026-08-21T10:00:00Z", "s1", "m"),
            ev("2026-08-21T10:00:01Z", "s2", "m"),
        ]
        self.assertEqual(ac.merge_hook_events(stats, events), 2)
        self.assertEqual(
            ac.merge_hook_events(stats, events), 0
        )  # re-emitted history
        self.assertEqual(
            ac.merge_hook_events(
                stats, [ev("2026-08-21T10:00:02Z", "s3", "m")]
            ),
            1,
        )
        rec = ac.build_record("x", "X", stats, [])
        self.assertEqual(rec["totalSessions"], 3)

    def test_fingerprint_list_capped(self):
        stats = ac.fresh_stats()
        old = ac.HOOK_FP_CAP
        ac.HOOK_FP_CAP = 10
        try:
            events = [
                ev(f"2026-08-21T10:00:0{i}Z", f"s{i}", "m") for i in range(15)
            ]
            self.assertEqual(ac.merge_hook_events(stats, events), 15)
            self.assertEqual(len(stats["hookFp"]), 10)
        finally:
            ac.HOOK_FP_CAP = old


class TestFailureIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_state = ac.STATE_FILE
        self.old_builtin = ac.BUILTIN_ADAPTERS_DIR
        self.old_user = ac.USER_ADAPTERS_DIR
        self.old_collectors = dict(ac.COLLECTORS)
        ac.STATE_FILE = os.path.join(self.tmp, "state.json")
        ac.BUILTIN_ADAPTERS_DIR = os.path.join(self.tmp, "none")
        ac.USER_ADAPTERS_DIR = os.path.join(self.tmp, "none2")
        self.ad = os.path.join(self.tmp, "adapters", "bad")
        os.makedirs(self.ad)
        with open(os.path.join(self.ad, "manifest.json"), "w") as fh:
            json.dump(
                {
                    "schemaVersion": 1,
                    "id": "bad",
                    "name": "Bad",
                    "sources": [
                        {
                            "format": "jsonl-lines",
                            "glob": os.path.join(self.tmp, "*.jsonl"),
                            "kindPath": "type",
                            "kinds": ["message"],
                            "rolePath": "message.role",
                            "promptRole": "user",
                            "completionRole": "assistant",
                            "timestampPath": "timestamp",
                            "modelPath": "message.model",
                            "tokens": {
                                "input": "message.usage.input",
                                "output": "message.usage.output",
                                "cacheRead": "message.usage.cacheRead",
                                "cacheWrite": "message.usage.cacheWrite",
                            },
                        }
                    ],
                },
                fh,
            )
        self.usage = os.path.join(self.tmp, "usage")
        os.makedirs(self.usage)
        self.args = [
            "--adapters-dir",
            os.path.join(self.tmp, "adapters"),
            "--usage-dir",
            self.usage,
        ]

    def tearDown(self):
        ac.STATE_FILE = self.old_state
        ac.BUILTIN_ADAPTERS_DIR = self.old_builtin
        ac.USER_ADAPTERS_DIR = self.old_user
        ac.COLLECTORS.clear()
        ac.COLLECTORS.update(self.old_collectors)

    def test_failed_run_commits_nothing(self):
        def boom(source, adapter_dir, state, force):
            yield ev("2026-08-21T10:00:00Z")
            raise RuntimeError("mid-stream failure")

        ac.COLLECTORS["jsonl-lines"] = boom
        self.assertEqual(ac.main(self.args), 1)
        self.assertFalse(os.path.exists(os.path.join(self.usage, "bad.json")))
        with open(ac.STATE_FILE) as fh:
            self.assertNotIn("bad", json.load(fh).get("stats", {}))
        # repeated failures never change totals
        self.assertEqual(ac.main(self.args), 1)
        with open(ac.STATE_FILE) as fh:
            self.assertNotIn("bad", json.load(fh).get("stats", {}))

    def test_successful_run_commits(self):
        def ok(source, adapter_dir, state, force):
            yield ev("2026-08-21T10:00:00Z")

        ac.COLLECTORS["jsonl-lines"] = ok
        self.assertEqual(ac.main(self.args), 0)
        with open(ac.STATE_FILE) as fh:
            state = json.load(fh)
        self.assertEqual(state["stats"]["bad"]["promptsTotal"], 0)
        self.assertEqual(state["stats"]["bad"]["sessions"], ["s1"])
        self.assertTrue(os.path.exists(os.path.join(self.usage, "bad.json")))

    def test_repeat_runs_merge_only_new_events(self):
        """Regression: committed cursors must be visible to the next run."""
        ac.COLLECTORS.update(self.old_collectors)
        with open(os.path.join(self.tmp, "sess.jsonl"), "w") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "message",
                        "timestamp": "2026-08-21T10:00:00Z",
                        "message": {
                            "role": "assistant",
                            "model": "m",
                            "usage": {
                                "input": 1,
                                "output": 2,
                                "cacheRead": 3,
                                "cacheWrite": 4,
                            },
                        },
                    }
                )
                + "\n"
            )
        self.assertEqual(ac.main(self.args), 0)
        with open(ac.STATE_FILE) as fh:
            state = json.load(fh)
        sessions_before = state["stats"]["bad"]["sessions"]
        self.assertEqual(len(sessions_before), 1)
        # second run: cursor committed, nothing new to merge
        self.assertEqual(ac.main(self.args), 0)
        with open(ac.STATE_FILE) as fh:
            state = json.load(fh)
        self.assertEqual(state["stats"]["bad"]["sessions"], sessions_before)
        self.assertEqual(
            state["stats"]["bad"]["modelUsage"]["m"]["inputTokens"], 1
        )


class TestStateLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_file = ac.STATE_FILE
        ac.STATE_FILE = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        ac.STATE_FILE = self.old_file

    def test_v1_state_ignored_and_rebuilt(self):
        with open(ac.STATE_FILE, "w") as fh:
            json.dump({"jsonl:old": {"f": {"sig": 1, "events": [1, 2, 3]}}}, fh)
        self.assertEqual(ac.load_state(), {})

    def test_v2_state_ignored_and_rebuilt(self):
        with open(ac.STATE_FILE, "w") as fh:
            json.dump(
                {"schemaVersion": 2, "stats": {"pi": {"promptsTotal": 9}}}, fh
            )
        self.assertEqual(ac.load_state(), {})

    def test_state_roundtrip(self):
        state = {"schemaVersion": 2, "stats": {"pi": {"promptsTotal": 7}}}
        ac.save_state(state)
        loaded = ac.load_state()
        self.assertEqual(loaded["stats"]["pi"]["promptsTotal"], 7)

    def test_oversized_state_ignored(self):
        with open(ac.STATE_FILE, "w") as fh:
            fh.seek(ac.STATE_LOAD_GUARD + 1)
            fh.write("{}")
        self.assertEqual(ac.load_state(), {})

    def test_state_never_contains_event_lists(self):
        """Cursors and counters only: no key named 'events' anywhere."""
        state = {"schemaVersion": 2}
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "t.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE m (ts integer, role text)")
        con.execute("INSERT INTO m VALUES (1, 'user')")
        con.commit()
        con.close()
        src = {
            "format": "sqlite-query",
            "database": db,
            "promptRole": "user",
            "completionRole": "assistant",
            "query": "SELECT ts, role FROM m",
            "columns": {"ts": "ts", "role": "role"},
        }
        cs(src, "", state, force=False)
        self.assertNotIn("events", json.dumps(state))
        self.assertEqual(
            set(state),
            {"schemaVersion"} | {k for k in state if k.startswith("sqlite:")},
        )


class TestStreaming(unittest.TestCase):
    def source(self, tmp):
        return {
            "format": "jsonl-lines",
            "glob": os.path.join(tmp, "**/*.jsonl"),
            "kindPath": "type",
            "kinds": ["message"],
            "rolePath": "message.role",
            "promptRole": "user",
            "completionRole": "assistant",
            "timestampPath": "timestamp",
            "modelPath": "message.model",
            "tokens": {
                "input": "message.usage.input",
                "output": "message.usage.output",
                "cacheRead": "message.usage.cacheRead",
                "cacheWrite": "message.usage.cacheWrite",
            },
        }

    def test_large_history_streams_without_materializing(self):
        tmp = tempfile.mkdtemp()
        n = 20000
        line = json.dumps(
            {
                "type": "message",
                "timestamp": "2026-08-21T10:00:00Z",
                "message": {
                    "role": "assistant",
                    "model": "m",
                    "usage": {
                        "input": 1,
                        "output": 2,
                        "cacheRead": 3,
                        "cacheWrite": 4,
                    },
                },
            }
        )
        with open(os.path.join(tmp, "big.jsonl"), "w") as fh:
            fh.writelines(line + "\n" for _ in range(n))
        state = {}
        gen = ac.collect_jsonl_lines(self.source(tmp), tmp, state, force=False)
        self.assertIsInstance(gen, types.GeneratorType)
        stats = ac.fresh_stats()
        merged = ac.merge_events(stats, gen)
        self.assertEqual(merged, n)
        rec = ac.build_record("x", "X", stats, [])
        self.assertEqual(rec["totalSessions"], 1)
        self.assertEqual(rec["totalPrompts"], 0)
        # cursor advanced: rerun yields nothing
        self.assertEqual(cj(self.source(tmp), tmp, state, force=False), [])
        # state holds cursors only, never events
        self.assertNotIn("events", json.dumps(state))

    def test_sqlite_streams_row_by_row(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "t.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE m (ts integer, role text)")
        con.executemany(
            "INSERT INTO m VALUES (?,?)", [(i, "user") for i in range(100)]
        )
        con.commit()
        con.close()
        src = {
            "format": "sqlite-query",
            "database": db,
            "promptRole": "user",
            "completionRole": "assistant",
            "query": "SELECT ts, role FROM m",
            "columns": {"ts": "ts", "role": "role"},
        }
        state = {}
        gen = ac.collect_sqlite_query(src, tmp, state, force=False)
        self.assertIsInstance(gen, types.GeneratorType)
        merged = ac.merge_events(ac.fresh_stats(), gen)
        self.assertEqual(merged, 100)
        self.assertEqual(cs(src, tmp, state, force=False), [])


class TestManifestValidation(unittest.TestCase):
    def base(self, **over):
        m = {
            "schemaVersion": 1,
            "id": "x",
            "name": "X",
            "sources": [{"format": "jsonl-lines", "glob": "~/**/*.jsonl"}],
        }
        m.update(over)
        return m

    def test_valid_manifest_passes(self):
        self.assertEqual(ac.validate_manifest(self.base()), [])

    def test_oversized_manifest_skipped(self):
        old_builtin = ac.BUILTIN_ADAPTERS_DIR
        old_user = ac.USER_ADAPTERS_DIR
        ac.BUILTIN_ADAPTERS_DIR = "/nonexistent/adapters"
        ac.USER_ADAPTERS_DIR = "/nonexistent/user-adapters"
        try:
            with tempfile.TemporaryDirectory() as d:
                ad = os.path.join(d, "adapters", "big")
                os.makedirs(ad)
                with open(os.path.join(ad, "manifest.json"), "w") as fh:
                    fh.seek(ac.MANIFEST_CAP + 1)
                    fh.write("{}")
                adapters, warnings = ac.load_adapters(
                    [os.path.join(d, "adapters")]
                )
        finally:
            ac.BUILTIN_ADAPTERS_DIR = old_builtin
            ac.USER_ADAPTERS_DIR = old_user
        self.assertEqual(adapters, [])
        self.assertTrue(any("exceeds" in w for w in warnings))

    def test_problems_detected(self):
        self.assertTrue(ac.validate_manifest({"schemaVersion": 2}))
        bad = self.base(sources=[{"format": "nope"}])
        self.assertTrue(
            any("unknown format" in p for p in ac.validate_manifest(bad))
        )
        no_source = self.base(sources=[])
        self.assertTrue(
            any("collect hook" in p for p in ac.validate_manifest(no_source))
        )


class TestFilters(unittest.TestCase):
    def test_detect_missing_path_skips(self):
        self.assertFalse(
            ac.detect_ok({"detect": [{"path": "/nonexistent/path/xyz"}]})
        )
        self.assertTrue(ac.detect_ok({}))

    def test_detect_command_times_out(self):
        old = ac.DETECT_TIMEOUT_S
        ac.DETECT_TIMEOUT_S = 1
        try:
            t0 = time.monotonic()
            self.assertFalse(
                ac.detect_ok({"detect": [{"type": "command", "command": "sleep 5"}]})
            )
            self.assertLess(time.monotonic() - t0, 4)
        finally:
            ac.DETECT_TIMEOUT_S = old

    def test_superseded_uses_omarchy_bin(self):
        old = ac.OMARCHY_BIN
        try:
            with tempfile.TemporaryDirectory() as d:
                ac.OMARCHY_BIN = d
                self.assertFalse(ac.superseded("pi"))
                open(os.path.join(d, "omarchy-agent-usage-pi"), "w").close()
                self.assertTrue(ac.superseded("pi"))
        finally:
            ac.OMARCHY_BIN = old

    def test_oversized_shell_json_treated_as_enabled(self):
        old = ac.SHELL_JSON
        try:
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "shell.json")
                ac.SHELL_JSON = p
                with open(p, "w") as fh:
                    fh.seek(ac.SHELL_JSON_GUARD + 1)
                    fh.write("{}")
                self.assertFalse(ac.provider_disabled("pi"))
        finally:
            ac.SHELL_JSON = old


class TestRunLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_state = ac.STATE_FILE
        ac.STATE_FILE = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        ac.STATE_FILE = self.old_state

    def test_lock_is_exclusive(self):
        fd1 = ac.lock_state()
        try:
            with self.assertRaises(BlockingIOError):
                fd2 = os.open(ac.STATE_FILE + ".lock", os.O_RDWR)
                try:
                    fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd2)
        finally:
            os.close(fd1)

    def test_lock_survives_state_replacement(self):
        # save_state() replaces the state pathname with a new inode; the
        # lock must sit on a stable inode so a third run cannot slip in
        fd1 = ac.lock_state()
        try:
            ac.save_state({"schemaVersion": 3})
            with self.assertRaises(BlockingIOError):
                fd2 = os.open(ac.STATE_FILE + ".lock", os.O_RDWR)
                try:
                    fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd2)
        finally:
            os.close(fd1)

    def test_concurrent_runs_serialize(self):
        ad = os.path.join(self.tmp, "adapters", "ok")
        os.makedirs(ad)
        manifest = {
            "schemaVersion": 1,
            "id": "ok",
            "name": "OK",
            "sources": [
                {
                    "format": "jsonl-lines",
                    "glob": os.path.join(self.tmp, "*.jsonl"),
                    "kindPath": "type",
                    "kinds": ["message"],
                    "rolePath": "message.role",
                    "promptRole": "user",
                    "completionRole": "assistant",
                    "timestampPath": "timestamp",
                    "modelPath": "message.model",
                    "tokens": {
                        "input": "message.usage.input",
                        "output": "message.usage.output",
                        "cacheRead": "message.usage.cacheRead",
                        "cacheWrite": "message.usage.cacheWrite",
                    },
                }
            ],
        }
        with open(os.path.join(ad, "manifest.json"), "w") as fh:
            json.dump(manifest, fh)
        with open(os.path.join(self.tmp, "sess.jsonl"), "w") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "message",
                        "timestamp": "2026-08-21T10:00:00Z",
                        "message": {
                            "role": "assistant",
                            "model": "m",
                            "usage": {"input": 1, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        },
                    }
                )
                + "\n"
            )
        usage = os.path.join(self.tmp, "usage")
        os.makedirs(usage)
        args = [
            "--adapters-dir",
            os.path.join(self.tmp, "adapters"),
            "--usage-dir",
            usage,
        ]
        threads = [
            threading.Thread(target=ac.main, args=(args,)) for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(ac.STATE_FILE) as fh:
            state = json.load(fh)
        # serialized: the second run saw the committed cursor and merged nothing
        self.assertEqual(
            state["stats"]["ok"]["modelUsage"]["m"]["inputTokens"], 1
        )
        self.assertEqual(
            state["stats"]["ok"]["sessions"],
            [os.path.join(self.tmp, "sess")],
        )


if __name__ == "__main__":
    unittest.main()
