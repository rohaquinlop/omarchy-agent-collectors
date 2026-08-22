"""Tests for bin/agent-collectors (stdlib unittest). Run: python3 -m unittest discover tests"""

import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_PATH = os.path.join(REPO, "bin", "agent-collectors")

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec

loader = SourceFileLoader("agent_collectors", ENGINE_PATH)
spec = importlib.util.spec_from_loader("agent_collectors", loader)
ac = module_from_spec(spec)
loader.exec_module(ac)


def ev(ts, session="s1", model="m", kind="completion", inp=10, out=5, cr=2, cw=1):
    if kind == "prompt":
        inp = out = cr = cw = 0
    return ac.make_event(ts, session, model, kind, inp, out, cr, cw)


class TestAccumulator(unittest.TestCase):
    def test_totals_today_recent_models(self):
        now = datetime(2026, 8, 21, 18, 0, 0)
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
        self.assertEqual([d["date"] for d in rec["recentDays"]],
                         ["2026-08-15", "2026-08-16", "2026-08-17", "2026-08-18",
                          "2026-08-19", "2026-08-20", "2026-08-21"])
        by_date = {d["date"]: d["messageCount"] for d in rec["recentDays"]}
        self.assertEqual(by_date["2026-08-21"], 18)  # tokens, not messages
        self.assertEqual(by_date["2026-08-15"], 18)
        mu = rec["modelUsage"]["alpha"]
        self.assertEqual(mu, {"inputTokens": 20, "outputTokens": 10,
                              "cacheReadInputTokens": 4, "cacheCreationInputTokens": 2})
        self.assertEqual(rec["todayTokensByModel"], {"alpha": 18})

    def test_incremental_merge_matches_full_aggregation(self):
        """Merging new events run by run equals merging everything at once."""
        now = datetime(2026, 8, 21, 18, 0, 0)
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
        ac.merge_events(stats, [ev("2026-08-20T10:00:00Z", "a", "alpha")],
                        now=datetime(2026, 8, 20, 18, 0, 0))
        ac.merge_events(stats, [ev("2026-08-21T10:00:00Z", "b", "beta")],
                        now=datetime(2026, 8, 21, 18, 0, 0))
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

    def test_model_cap_routes_to_other(self):
        stats = ac.fresh_stats()
        for i in range(ac.MODEL_CAP + 3):
            ac.merge_events(stats, [ev("2026-08-21T10:00:00Z", "s", f"model{i}")])
        # MODEL_CAP distinct models plus the "other" bucket
        self.assertEqual(len(stats["modelUsage"]), ac.MODEL_CAP + 1)
        self.assertIn("other", stats["modelUsage"])

    def test_bad_timestamp_dropped(self):
        self.assertIsNone(ev("not-a-date"))


class TestRecordContract(unittest.TestCase):
    CONTRACT_KEYS = {
        "schemaVersion", "id", "name", "updatedAt", "ready", "hasLocalStats",
        "todayPrompts", "todaySessions", "todayTotalTokens", "todayTokensByModel",
        "recentDays", "totalPrompts", "totalSessions", "activeDays", "activeDates",
        "modelUsage", "limits", "tierLabel", "usageStatusText", "authHelpText",
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
            self.assertFalse(set(live) - set(rec),
                             f"record missing live keys: {set(live) - set(rec)}")


class TestJsonlCollector(unittest.TestCase):
    PI_LINE = {
        "type": "message", "id": "m1", "timestamp": "2026-08-21T10:00:00Z",
        "message": {"role": "assistant", "model": "mimo-v2.5-pro",
                    "usage": {"input": 100, "output": 20, "cacheRead": 50, "cacheWrite": 7}},
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
            "kindPath": "type", "kinds": ["message"],
            "rolePath": "message.role", "promptRole": "user", "completionRole": "assistant",
            "timestampPath": "timestamp", "modelPath": "message.model",
            "tokens": {"input": "message.usage.input", "output": "message.usage.output",
                       "cacheRead": "message.usage.cacheRead", "cacheWrite": "message.usage.cacheWrite"},
        }

    def test_parse_and_incremental_cursor(self):
        self.write_session([{"type": "session"}, self.PI_LINE])
        events = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["model"], "mimo-v2.5-pro")
        self.assertEqual(events[0]["input"], 100)

        # unchanged rerun: no new events, cursor kept
        again = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(again, [])

        # append a second assistant message -> only the new event is returned
        self.append([dict(self.PI_LINE, id="m2")])
        grown = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(grown), 1)
        self.assertEqual(grown[0]["input"], 100)

    def test_user_message_counts_as_prompt(self):
        self.write_session([
            {"type": "message", "timestamp": "2026-08-21T10:00:00Z",
             "message": {"role": "user", "content": []}},
            self.PI_LINE,
        ])
        events = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        kinds = sorted(e["kind"] for e in events)
        self.assertEqual(kinds, ["completion", "prompt"])

    def test_truncated_file_rescans(self):
        self.write_session([self.PI_LINE, dict(self.PI_LINE, id="m2")])
        first = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(first), 2)
        # rotate: shorter file with different content
        self.write_session([dict(self.PI_LINE, id="m3")])
        after = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(after), 1)

    def test_unterminated_tail_continuation_skipped(self):
        # file ends without newline; the partial line is unparseable and skipped
        p = os.path.join(self.tmp, "sess1.jsonl")
        with open(p, "w") as fh:
            fh.write(json.dumps(self.PI_LINE) + "\n")
            fh.write('{"type": "message", "timestamp": "2026-08-2')  # partial line, no newline
        first = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(first), 1)
        # continuation arrives: first line of the new chunk is a continuation -> skipped
        with open(p, "a") as fh:
            fh.write('1T10:00:00Z", "message": {"role": "assistant", "model": "m", "usage": {}}}')
            fh.write("\n")
            fh.write(json.dumps(dict(self.PI_LINE, id="m2")) + "\n")
        after = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["input"], 100)

    def test_force_rescans_and_returns_everything(self):
        self.write_session([self.PI_LINE])
        ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        again = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=True)
        self.assertEqual(len(again), 1)


class TestSqliteCollector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE message (id text PRIMARY KEY, session_id text, time_created integer, data text)")
        rows = [
            ("u1", "s1", 1755770400000, json.dumps({"role": "user"})),
            ("a1", "s1", 1755770401000, json.dumps({"role": "assistant", "modelID": "qwen",
                                                    "tokens": {"input": 9, "output": 3,
                                                               "cache": {"read": 4, "write": 2}}})),
        ]
        con.executemany("INSERT INTO message VALUES (?,?,?,?)", rows)
        con.commit()
        con.close()

    def source(self):
        return {
            "format": "sqlite-query",
            "database": self.db,
            "timestampUnit": "ms",
            "promptRole": "user", "completionRole": "assistant",
            "query": ("SELECT session_id, time_created, json_extract(data,'$.role') AS role,"
                      " json_extract(data,'$.modelID') AS model,"
                      " json_extract(data,'$.tokens.input') AS tin,"
                      " json_extract(data,'$.tokens.output') AS tout,"
                      " json_extract(data,'$.tokens.cache.read') AS tcr,"
                      " json_extract(data,'$.tokens.cache.write') AS tcw FROM message"
                      " ORDER BY time_created"),
            "columns": {"ts": "time_created", "sessionId": "session_id", "role": "role",
                        "model": "model", "input": "tin", "output": "tout",
                        "cacheRead": "tcr", "cacheWrite": "tcw"},
        }

    def test_events_from_db_and_incremental_last_ts(self):
        state = {}
        events = ac.collect_sqlite_query(self.source(), self.tmp, state, force=False)
        self.assertEqual(len(events), 2)
        kinds = sorted(e["kind"] for e in events)
        self.assertEqual(kinds, ["completion", "prompt"])

        # unchanged db: no new events
        cached = ac.collect_sqlite_query(self.source(), self.tmp, state, force=False)
        self.assertEqual(cached, [])

        # append one row -> only the new row is returned
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO message VALUES (?,?,?,?)",
                    ("a2", "s2", 1755770402000, json.dumps({"role": "assistant", "modelID": "qwen",
                                                            "tokens": {"input": 1, "output": 2,
                                                                       "cache": {"read": 3, "write": 4}}})))
        con.commit()
        con.close()
        grown = ac.collect_sqlite_query(self.source(), self.tmp, state, force=False)
        self.assertEqual(len(grown), 1)
        self.assertEqual(grown[0]["session"], "s2")

    def test_readonly_connection_wal_tolerant(self):
        # mode=ro must not fail on an existing -wal sidecar
        open(self.db + "-wal", "a").close()
        events = ac.collect_sqlite_query(self.source(), self.tmp, {}, force=True)
        self.assertEqual(len(events), 2)


class TestHookBounding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.script = os.path.join(self.tmp, "collect.sh")
        open(self.script, "w").close()
        os.chmod(self.script, 0o755)

    def test_collect_hook_streams_events(self):
        with open(self.script, "w") as fh:
            fh.write('#!/bin/sh\necho \'{"ts": "2026-08-21T10:00:00Z", "session": "s1", "kind": "completion", "input": 1}\'\n')
        events = ac.collect_hook({"id": "x", "collect": "collect.sh"}, self.tmp, {}, force=False)
        self.assertEqual(len(events), 1)

    def test_collect_hook_output_capped(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\n")
            for _ in range(ac.HOOK_EVENT_CAP + 50):
                fh.write('echo \'{"ts": "2026-08-21T10:00:00Z", "session": "s", "kind": "completion"}\'\n')
        events = ac.collect_hook({"id": "x", "collect": "collect.sh"}, self.tmp, {}, force=False)
        self.assertEqual(len(events), ac.HOOK_EVENT_CAP)

    def test_collect_hook_timeout_kills(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\nsleep 30\n")
        old = ac.HOOK_TIMEOUT_S
        ac.HOOK_TIMEOUT_S = 2
        try:
            with self.assertRaises(RuntimeError):
                ac.collect_hook({"id": "x", "collect": "collect.sh"}, self.tmp, {}, force=False)
        finally:
            ac.HOOK_TIMEOUT_S = old

    def test_collect_hook_failure_reported(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\necho oops >&2\nexit 3\n")
        with self.assertRaises(RuntimeError) as ctx:
            ac.collect_hook({"id": "x", "collect": "collect.sh"}, self.tmp, {}, force=False)
        self.assertIn("oops", str(ctx.exception))

    def test_limits_hook_capped_and_optional(self):
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\necho '[{\"kind\": \"tier\"}]'\n")
        limits = ac.run_limits_hook({"id": "x", "limits": "collect.sh"}, self.tmp, force=False)
        self.assertEqual(limits, [{"kind": "tier"}])
        with open(self.script, "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        self.assertEqual(ac.run_limits_hook({"id": "x", "limits": "collect.sh"}, self.tmp, force=False), [])


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

    def test_v2_state_roundtrip(self):
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
        src = {"format": "sqlite-query", "database": db, "promptRole": "user",
               "completionRole": "assistant",
               "query": "SELECT ts, role FROM m", "columns": {"ts": "ts", "role": "role"}}
        ac.collect_sqlite_query(src, "", state, force=False)
        self.assertNotIn("events", json.dumps(state))
        self.assertEqual(set(state),
                         {"schemaVersion"} | {k for k in state if k.startswith("sqlite:")})


class TestManifestValidation(unittest.TestCase):
    def base(self, **over):
        m = {"schemaVersion": 1, "id": "x", "name": "X",
             "sources": [{"format": "jsonl-lines", "glob": "~/**/*.jsonl"}]}
        m.update(over)
        return m

    def test_valid_manifest_passes(self):
        self.assertEqual(ac.validate_manifest(self.base()), [])

    def test_problems_detected(self):
        self.assertTrue(ac.validate_manifest({"schemaVersion": 2}))
        bad = self.base(sources=[{"format": "nope"}])
        self.assertTrue(any("unknown format" in p for p in ac.validate_manifest(bad)))
        no_source = self.base(sources=[])
        self.assertTrue(any("collect hook" in p for p in ac.validate_manifest(no_source)))


class TestFilters(unittest.TestCase):
    def test_detect_missing_path_skips(self):
        self.assertFalse(ac.detect_ok({"detect": [{"path": "/nonexistent/path/xyz"}]}))
        self.assertTrue(ac.detect_ok({}))

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


if __name__ == "__main__":
    unittest.main()
