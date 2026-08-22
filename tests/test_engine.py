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


class TestAggregation(unittest.TestCase):
    def test_totals_today_recent_models(self):
        now = datetime(2026, 8, 21, 18, 0, 0)
        events = [
            ev("2026-08-21T10:00:00Z", "a", "alpha"),
            ev("2026-08-21T11:00:00Z", "b", "beta", "prompt"),
            ev("2026-08-15T09:00:00Z", "c", "alpha"),  # 6 days back
            ev("2026-07-01T09:00:00Z", "d", "gamma"),  # outside week
        ]
        agg = ac.aggregate(events, now=now)
        self.assertEqual(agg["todayPrompts"], 1)
        self.assertEqual(agg["todaySessions"], 2)
        # completion tokens today: 10+5+2+1 = 18
        self.assertEqual(agg["todayTotalTokens"], 18)
        self.assertEqual(agg["totalPrompts"], 1)
        self.assertEqual(agg["totalSessions"], 4)
        self.assertEqual(agg["activeDays"], 3)
        self.assertEqual([d["date"] for d in agg["recentDays"]],
                         ["2026-08-15", "2026-08-16", "2026-08-17", "2026-08-18",
                          "2026-08-19", "2026-08-20", "2026-08-21"])
        by_date = {d["date"]: d["messageCount"] for d in agg["recentDays"]}
        self.assertEqual(by_date["2026-08-21"], 18)  # tokens, not messages
        self.assertEqual(by_date["2026-08-15"], 18)
        mu = agg["modelUsage"]["alpha"]
        self.assertEqual(mu, {"inputTokens": 20, "outputTokens": 10,
                              "cacheReadInputTokens": 4, "cacheCreationInputTokens": 2})
        self.assertEqual(agg["todayTokensByModel"], {"alpha": 18})

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
        rec = ac.build_record("pi", "Pi", [ev("2026-08-21T10:00:00Z")], [])
        missing = self.CONTRACT_KEYS - set(rec)
        self.assertEqual(missing, set())

    def test_matches_live_records_when_present(self):
        """Key sets must be a superset of the stock collectors' live output."""
        usage_dir = os.path.expanduser("~/.local/state/omarchy/agents/usage")
        rec = ac.build_record("x", "X", [ev(time.time())], [])
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

    def write_session(self, lines):
        p = os.path.join(self.tmp, "sess1.jsonl")
        with open(p, "w") as fh:
            fh.write("\n".join(json.dumps(l) for l in lines) + "\n")
        return p

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

    def test_parse_and_incremental_cache(self):
        self.write_session([{"type": "session"}, self.PI_LINE])
        events = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["model"], "mimo-v2.5-pro")
        self.assertEqual(events[0]["input"], 100)

        # unchanged rerun served from cache
        again = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(again), 1)

        # append a second assistant message -> new event picked up
        with open(os.path.join(self.tmp, "sess1.jsonl"), "a") as fh:
            fh.write(json.dumps(dict(self.PI_LINE, id="m2")) + "\n")
        grown = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        self.assertEqual(len(grown), 2)

    def test_user_message_counts_as_prompt(self):
        self.write_session([
            {"type": "message", "timestamp": "2026-08-21T10:00:00Z",
             "message": {"role": "user", "content": []}},
            self.PI_LINE,
        ])
        events = ac.collect_jsonl_lines(self.source(), self.tmp, self.state, force=False)
        kinds = sorted(e["kind"] for e in events)
        self.assertEqual(kinds, ["completion", "prompt"])


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
                      " json_extract(data,'$.tokens.cache.write') AS tcw FROM message"),
            "columns": {"ts": "time_created", "sessionId": "session_id", "role": "role",
                        "model": "model", "input": "tin", "output": "tout",
                        "cacheRead": "tcr", "cacheWrite": "tcw"},
        }

    def test_events_from_db_and_sig_cache(self):
        state = {}
        events = ac.collect_sqlite_query(self.source(), self.tmp, state, force=False)
        self.assertEqual(len(events), 2)
        kinds = sorted(e["kind"] for e in events)
        self.assertEqual(kinds, ["completion", "prompt"])
        cached = ac.collect_sqlite_query(self.source(), self.tmp, state, force=False)
        self.assertEqual(len(cached), 2)

    def test_readonly_connection_wal_tolerant(self):
        # mode=ro must not fail on an existing -wal sidecar
        open(self.db + "-wal", "a").close()
        events = ac.collect_sqlite_query(self.source(), self.tmp, {}, force=True)
        self.assertEqual(len(events), 2)


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
