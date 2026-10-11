"""Tests for adapters/opencode/limits.py (stdlib unittest)."""

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "opencode_limits", os.path.join(REPO, "adapters", "opencode", "limits.py")
)
ol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ol)

UTC = timezone.utc
NOW = datetime(2026, 10, 11, 16, 0, tzinfo=UTC)  # a Sunday


def ms(dt):
    return int(dt.timestamp() * 1000)


class OpencodeLimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = os.path.join(self.tmp.name, "data")
        os.makedirs(os.path.join(data, "opencode"))
        self.db = sqlite3.connect(os.path.join(data, "opencode", "opencode.db"))
        self.db.execute("CREATE TABLE message (id INTEGER PRIMARY KEY, time_created INTEGER, data TEXT)")
        self.config_dir = os.path.join(self.tmp.name, "config")
        env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": data, "XDG_CONFIG_HOME": self.config_dir})
        env.start()
        self.addCleanup(env.stop)

    def add(self, when, model, provider="opencode-go", tin=0, tout=0, reason=0, cread=0, cwrite=0, cost=0.0):
        data = {"role": "assistant", "providerID": provider, "modelID": model, "cost": cost,
                "tokens": {"input": tin, "output": tout, "reasoning": reason,
                           "cache": {"read": cread, "write": cwrite}}}
        self.db.execute("INSERT INTO message (time_created, data) VALUES (?, ?)", (ms(when), json.dumps(data)))
        self.db.commit()

    def configure(self, cfg):
        os.makedirs(os.path.join(self.config_dir, "omarchy", "agent-collectors"), exist_ok=True)
        with open(os.path.join(self.config_dir, "omarchy", "agent-collectors", "opencode.json"), "w") as fh:
            json.dump(cfg, fh)

    def run_hook(self, now=NOW):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ol.main(now=now)
        bars = {b["title"]: b for b in json.loads(out.getvalue())} if code == 0 else {}
        return code, bars

    def test_no_go_usage_means_no_meters(self):
        self.add(datetime(2026, 10, 5, tzinfo=UTC), "claude-opus-5-5", provider="opencode", tin=1000, cost=0.5)
        self.assertEqual(self.run_hook()[0], 1)

    def test_missing_database_means_no_meters(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.join(self.tmp.name, "nowhere")}):
            self.assertEqual(self.run_hook()[0], 1)

    def test_long_context_requests_bill_at_the_higher_tier(self):
        # 300K cached context is over gpt-6-luna's 272K tier: cache reads cost
        # 0.02 instead of 0.01 per 1M -> $0.006, 0.04% of a $15 month.
        self.configure({"go": {"renewsAt": "2026-10-31T14:26:00Z"}})
        self.add(datetime(2026, 10, 9, tzinfo=UTC), "gpt-6-luna", cread=300_000, cost=0.003)
        _, bars = self.run_hook()
        self.assertAlmostEqual(bars["Go monthly"]["percent"], 0.0004, places=4)

    def test_reasoning_bills_as_output_and_plan_selects_the_limit(self):
        # qwen3.8-flash: 1M output-equivalent tokens at $0.47 -> 1.57% of $30 (Go), 0.52% of $90 (Go Plus)
        self.add(datetime(2026, 10, 9, tzinfo=UTC), "qwen3.8-flash", tout=400_000, reason=600_000)
        self.configure({"go": {"renewsAt": "2026-10-31T14:26:00Z"}})
        self.assertAlmostEqual(self.run_hook()[1]["Go monthly"]["percent"], 0.0157, places=4)
        self.configure({"go": {"renewsAt": "2026-10-31T14:26:00Z", "plan": "go-plus"}})
        self.assertAlmostEqual(self.run_hook()[1]["Go monthly"]["percent"], 0.0052, places=4)

    def test_deepseek_peak_hours_cost_double(self):
        self.configure({"go": {"renewsAt": "2026-10-31T14:26:00Z"}})
        monday_peak = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
        monday_off = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
        self.add(monday_peak, "deepseek-v4-pro", tin=1_000_000)  # $1.32 of $15
        self.add(monday_off, "deepseek-v4-pro", tin=1_000_000)  # $0.66 of $15
        self.assertAlmostEqual(self.run_hook()[1]["Go monthly"]["percent"], round(1.98 / 15, 4), places=4)

    def test_windows_follow_the_console(self):
        self.configure({"go": {"renewsAt": "2026-10-31T14:26:00Z"}})
        # $1.5 of kimi-k3's $15 monthly limit, spent before this week (Mon Oct 5 is last week)
        self.add(datetime(2026, 10, 4, 12, tzinfo=UTC), "kimi-k3", tin=500_000)
        # $0.3 this week, inside the current 5-hour block that opened at 13:00
        self.add(datetime(2026, 10, 11, 13, tzinfo=UTC), "kimi-k3", tin=100_000)
        _, bars = self.run_hook()
        self.assertAlmostEqual(bars["Go monthly"]["percent"], 0.12, places=4)  # 1.8/15
        self.assertAlmostEqual(bars["Go weekly"]["percent"], 0.04, places=4)  # 0.3/15/0.5
        self.assertAlmostEqual(bars["Go 5-hour"]["percent"], 0.1, places=4)  # 0.3/15/0.2
        self.assertEqual(bars["Go weekly"]["resetsAt"], "2026-10-12T00:00:00+00:00")
        self.assertEqual(bars["Go 5-hour"]["resetsAt"], "2026-10-11T18:00:00+00:00")
        self.assertEqual(bars["Go monthly"]["resetsAt"], "2026-10-31T14:26:00+00:00")

    def test_idle_block_reads_zero_and_month_without_renewal_is_estimated(self):
        self.add(datetime(2026, 9, 30, 1, 43, tzinfo=UTC), "kimi-k3", tin=100_000)
        _, bars = self.run_hook()
        self.assertEqual(bars["Go 5-hour"]["percent"], 0.0)
        self.assertIn("Go monthly · est.", bars)
        self.assertEqual(bars["Go monthly · est."]["resetsAt"], "2026-10-30T01:43:00+00:00")

    def test_zen_budget_is_opt_in_and_calendar_monthly(self):
        self.add(datetime(2026, 9, 28, tzinfo=UTC), "claude-opus-5-5", provider="opencode", cost=5.0)
        self.add(datetime(2026, 10, 2, tzinfo=UTC), "claude-opus-5-5", provider="opencode", cost=2.0)
        self.assertEqual(self.run_hook()[0], 1)
        self.configure({"zen": {"monthlyBudget": 40}})
        _, bars = self.run_hook()
        self.assertEqual(bars["Zen budget ($40)"]["percent"], 0.05)
        self.assertEqual(bars["Zen budget ($40)"]["resetsAt"], "2026-11-01T00:00:00+00:00")

    def test_period_steps_by_month_and_clamps_the_day(self):
        anchor = datetime(2026, 10, 31, 14, 26, tzinfo=UTC)
        start, end = ol.period(anchor, NOW)
        self.assertEqual((start.month, start.day, end.month, end.day), (9, 30, 10, 31))
        start, end = ol.period(anchor, datetime(2027, 2, 28, 15, tzinfo=UTC))
        self.assertEqual((start.month, start.day, end.month, end.day), (2, 28, 3, 31))

    def test_model_table_is_well_formed(self):
        with open(os.path.join(REPO, "adapters", "opencode", "go-models.json")) as fh:
            table = json.load(fh)
        for model, spec in table["models"].items():
            self.assertEqual(len(spec["limit"]), 2, model)
            for tier in spec["tiers"] + spec.get("peakTiers", []):
                self.assertEqual(len(tier), 5, model)
            self.assertIsNone(spec["tiers"][-1][0], f"{model}: last tier must be open-ended")


if __name__ == "__main__":
    unittest.main()
