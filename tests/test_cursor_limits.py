"""Offline tests for Cursor's subscription quota adapter (no real credentials)."""

import importlib.util
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ADAPTER = Path(__file__).resolve().parents[1] / "adapters" / "cursor"


class CursorTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ADAPTER / "limits.py").is_file(), "Cursor hook is missing")
        spec = importlib.util.spec_from_file_location("cursor_limits", ADAPTER / "limits.py")
        self.cursor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cursor)


class TestParsing(CursorTest):
    def test_fraction_and_weekly_reset_without_absolute_spend(self):
        result = self.cursor.parse_usage(
            {"enabled": True, "planUsage": {"totalPercentUsed": 42}},
            {"planInfo": {"planName": "Premium", "includedUsagePeriod": "INCLUDED_USAGE_PERIOD_WEEKLY",
                          "includedUsageResetsAt": "1794475789552"}},
        )
        self.assertEqual(result["tierLabel"], "Premium")
        self.assertEqual(result["limits"], [{"label": "Weekly", "percent": 0.42,
                                            "resetsAt": "2026-11-12T09:29:49.552000+00:00"}])

    def test_malformed_plan_metadata_does_not_crash(self):
        for period in ([], {}, True):
            with self.subTest(period=period):
                result = self.cursor.parse_usage(
                    {"planUsage": {"totalPercentUsed": 10}},
                    {"planInfo": {"includedUsagePeriod": period, "planName": []}},
                )
                self.assertEqual(result["limits"], [{"label": "Subscription", "percent": 0.1}])
                self.assertEqual(result["tierLabel"], "")

    def test_valid_zero_numeric_strings_and_over_quota(self):
        for value, expected in [(0, 0), ("0", 0), ("25.5", 0.255), (100, 1), (120, 1)]:
            with self.subTest(value=value):
                result = self.cursor.parse_usage({"planUsage": {"totalPercentUsed": value}}, {})
                self.assertEqual(result["limits"][0]["percent"], expected)

    def test_missing_invalid_percentage_is_not_zero(self):
        for value in [None, True, False, "", " ", "NaN", "inf", float("nan"), float("inf"), -1, [], {}]:
            with self.subTest(value=value):
                self.assertEqual(self.cursor.parse_usage({"planUsage": {"totalPercentUsed": value}}, {})["limits"], [])
        for payload in [{}, {"planUsage": None}, [], {"enabled": False, "planUsage": {"totalPercentUsed": 10}}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.cursor.parse_usage(payload, {})["limits"], [])

    def test_no_invented_reset_or_monthly_period(self):
        for plan in [{}, None, [], {"planInfo": None}, {"planInfo": {"includedUsageResetsAt": "invalid"}},
                     {"planInfo": {"includedUsageResetsAt": -1}},
                     {"planInfo": {"includedUsageResetsAt": True}}]:
            with self.subTest(plan=plan):
                result = self.cursor.parse_usage({"billingCycleEnd": "1794475789552", "planUsage": {"totalPercentUsed": 10}}, plan)
                self.assertEqual(result["limits"], [{"label": "Subscription", "percent": 0.1}])
        monthly = self.cursor.parse_usage({"planUsage": {"totalPercentUsed": 10}},
                                         {"planInfo": {"includedUsagePeriod": "INCLUDED_USAGE_PERIOD_MONTHLY"}})
        self.assertEqual(monthly["limits"][0]["label"], "Monthly")


class TestCollect(CursorTest):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "cursor"
        self.config.mkdir()
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.auth = self.config / "auth.json"
        self.auth.write_text(json.dumps({"accessToken": "synthetic-secret", "refreshToken": "do-not-touch"}))

    def test_xdg_auth_read_only_and_partial_plan_failure(self):
        before = self.auth.read_bytes(), self.auth.stat().st_mtime_ns
        with patch.object(self.cursor, "request_json", side_effect=[
            {"planUsage": {"totalPercentUsed": 10}}, urllib.error.URLError("private error detail")
        ]) as request:
            result = self.cursor.collect()
        self.assertEqual(result["limits"], [{"label": "Subscription", "percent": 0.1}])
        self.assertEqual(before, (self.auth.read_bytes(), self.auth.stat().st_mtime_ns))
        self.assertEqual(request.call_args_list[0].args, ("GetCurrentPeriodUsage", "synthetic-secret"))
        self.assertEqual(request.call_count, 2)

    def test_truncated_http_response_is_handled_for_usage_and_plan(self):
        with patch.object(self.cursor, "request_json", side_effect=http.client.IncompleteRead(b"partial")):
            result = self.cursor.collect()
        self.assertEqual(result["limits"], [])
        self.assertTrue(result["usageStatusText"])
        with patch.object(self.cursor, "request_json", side_effect=[
            {"planUsage": {"totalPercentUsed": 10}}, http.client.IncompleteRead(b"partial"),
        ]):
            result = self.cursor.collect()
        self.assertEqual(result["limits"], [{"label": "Subscription", "percent": 0.1}])

    def test_cli_emits_actionable_diagnostic_without_traceback(self):
        import subprocess
        self.auth.unlink()
        result = subprocess.run(
            [os.sys.executable, str(ADAPTER / "limits.py")],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout)["limits"], [])
        self.assertIn("cursor-agent login", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_default_config_path(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}), patch.object(Path, "home", return_value=Path(self.temp.name)):
            self.assertEqual(self.cursor.config_dir(), Path(self.temp.name) / ".config" / "cursor")

    def test_missing_malformed_oversized_credentials_and_invalid_token(self):
        for content in [None, "not json", "[]", "{}", '{"accessToken": 123}', '{"accessToken": "bad\\nheader"}', " " * 65537]:
            with self.subTest(content_type=type(content).__name__):
                if content is None:
                    self.auth.unlink(missing_ok=True)
                else:
                    self.auth.write_text(content)
                with patch.object(self.cursor, "request_json") as request:
                    result = self.cursor.collect()
                self.assertEqual(result["limits"], [])
                self.assertIn("login", result["authHelpText"])
                request.assert_not_called()

    def test_team_scope_and_bad_configuration_fail_closed(self):
        for config in [{"authInfo": {"activeTeamId": 12}}, {"authInfo": {"teamId": 12}},
                       {"authInfo": {"activeTeamId": "bad"}}, {"authInfo": []}, [], "invalid"]:
            with self.subTest(config=config):
                path = self.config / "cli-config.json"
                path.write_text(json.dumps(config) if not isinstance(config, str) else config)
                with patch.object(self.cursor, "request_json") as request:
                    result = self.cursor.collect()
                self.assertEqual(result["limits"], [])
                self.assertTrue(result["usageStatusText"])
                request.assert_not_called()

    def test_http_network_and_malformed_failures_are_sanitized(self):
        for error in [urllib.error.HTTPError("https://api2.cursor.sh/", code, "synthetic-secret", {}, None)
                      for code in (401, 403, 429, 500)] + [urllib.error.URLError("synthetic-secret"),
                                                       TimeoutError("synthetic-secret"), ValueError("synthetic-secret")]:
            with self.subTest(error=type(error).__name__):
                if isinstance(error, urllib.error.HTTPError):
                    self.addCleanup(error.close)
                with patch.object(self.cursor, "request_json", side_effect=error):
                    result = self.cursor.collect()
                self.assertEqual(result["limits"], [])
                self.assertNotIn("synthetic-secret", json.dumps(result))
                self.assertTrue(result["usageStatusText"])
                self.assertTrue(result["authHelpText"])


class TestTransport(CursorTest):
    def test_known_endpoint_headers_bounds_and_json_object(self):
        with patch.object(self.cursor.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(b'{"ok": true}')
            self.assertEqual(self.cursor.request_json("GetCurrentPeriodUsage", "synthetic-secret"), {"ok": True})
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage")
            self.assertEqual(request.data, b"{}")
            self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-secret")
            self.assertEqual(request.get_header("Connect-protocol-version"), "1")
            self.assertLessEqual(build.return_value.open.call_args.kwargs["timeout"], 10)
            handlers = build.call_args.args
            self.assertTrue(any(isinstance(h, urllib.request.HTTPRedirectHandler) for h in handlers))
            redirect = next(h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
            self.assertIsNone(redirect.redirect_request(request, None, 302, "Found", {}, "https://untrusted.invalid"))
        with self.assertRaises(ValueError):
            self.cursor.request_json("https://untrusted.invalid", "synthetic-secret")

    def test_invalid_and_oversized_responses(self):
        for payload in [b"[]", b"not json", b"\xff", b" " * (1048576 + 1)]:
            with self.subTest(length=len(payload)):
                with patch.object(self.cursor.urllib.request, "build_opener") as build:
                    build.return_value.open.return_value = io.BytesIO(payload)
                    with self.assertRaises(ValueError):
                        self.cursor.request_json("GetPlanInfo", "synthetic-secret")

    def test_manifest_detects_credentials_without_cli_on_path(self):
        import subprocess
        with self.subTest(manifest=True):
            self.assertTrue((ADAPTER / "manifest.json").is_file())
        manifest = json.loads((ADAPTER / "manifest.json").read_text())
        self.assertEqual(manifest["id"], "cursor")
        self.assertEqual(manifest["limits"], "limits.py")
        self.assertNotIn("collect", manifest)
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ, "XDG_CONFIG_HOME": temp, "PATH": "/usr/bin:/bin"}
            command = manifest["detect"][0]["command"]
            self.assertNotEqual(subprocess.run(command, shell=True, env=env).returncode, 0)
            directory = Path(temp) / "cursor"
            directory.mkdir()
            (directory / "auth.json").write_text("{}")
            self.assertEqual(subprocess.run(command, shell=True, env=env).returncode, 0)


if __name__ == "__main__":
    unittest.main()
