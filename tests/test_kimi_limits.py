"""Offline Kimi Code quota tests; no real credential files or model requests."""

import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ADAPTER = Path(__file__).resolve().parents[1] / "adapters" / "kimi"


class KimiTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ADAPTER / "limits.py").is_file(), "Kimi hook is missing")
        spec = importlib.util.spec_from_file_location("kimi_limits", ADAPTER / "limits.py")
        self.kimi = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.kimi)


class TestParsing(KimiTest):
    def test_present_windows_and_separate_monthly_allocations(self):
        result = self.kimi.parse_usage({"usages": {
            "limit_5h": {"used_ratio": 0, "reset_time": "2026-12-01T00:00:00Z"},
            "limit_month_total": {"used_ratio": "0.2"},
            "limit_month_code": {"used_ratio": "0.2"},
        }})
        self.assertEqual(result["limits"], [
            {"label": "Session (5-hour)", "percent": 0.0, "resetsAt": "2026-12-01T00:00:00+00:00"},
            {"label": "Monthly", "percent": 0.2},
            {"label": "Code monthly", "percent": 0.2},
        ])
        self.assertNotIn("Weekly (7-day)", [x["label"] for x in result["limits"]])

    def test_weekly_and_over_quota(self):
        result = self.kimi.parse_usage({"usages": {"limit_7d": {"used_ratio": 1.2}}})
        self.assertEqual(result["limits"], [{"label": "Weekly (7-day)", "percent": 1}])

    def test_invalid_ratios_windows_and_payloads(self):
        for value in [None, True, False, "", " ", "NaN", "inf", -0.1, [], {}, float("nan"), float("inf"), 10**400]:
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(self.kimi.parse_usage({"usages": {"limit_5h": {"used_ratio": value}}})["limits"], [])
        for payload in [[], None, {}, {"usages": []}, {"usages": {"limit_5h": []}},
                        {"usages": {"unknown_window": {"used_ratio": 0.5}}}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.kimi.parse_usage(payload)["limits"], [])

    def test_invalid_or_naive_reset_is_omitted(self):
        for reset in [None, 0, True, [], {}, "bad", "2026-12-01T00:00:00"]:
            with self.subTest(reset=reset):
                result = self.kimi.parse_usage({"usages": {"limit_5h": {"used_ratio": 0.5, "reset_time": reset}}})
                self.assertEqual(result["limits"], [{"label": "Session (5-hour)", "percent": 0.5}])


class TestCollect(KimiTest):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.auth = self.home / "credentials" / "kimi-code.json"
        self.auth.parent.mkdir()
        self.auth.write_text(json.dumps({"access_token": "synthetic-secret", "refresh_token": "do-not-touch"}))
        (self.home / "region").write_text("mainland-cn\n")
        (self.home / "device_id").write_text("synthetic-device")
        env = patch.dict(os.environ, {"KIMI_CODE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def test_current_home_override_read_only(self):
        before = self.auth.read_bytes(), self.auth.stat().st_mtime_ns
        with patch.object(self.kimi, "request_json", return_value={"usages": {"limit_5h": {"used_ratio": 0.1}}}) as request:
            result = self.kimi.collect()
        self.assertEqual(result["limits"][0]["percent"], 0.1)
        self.assertEqual(request.call_args.args, ("mainland-cn", "synthetic-secret", "synthetic-device"))
        self.assertEqual(before, (self.auth.read_bytes(), self.auth.stat().st_mtime_ns))
        with patch.dict(os.environ, {"KIMI_CODE_HOME": ""}), patch.object(Path, "home", return_value=self.home):
            self.assertEqual(self.kimi.credential_home(), self.home / ".kimi-code")

    def test_unknown_or_missing_region_never_sends_credentials(self):
        path = self.home / "region"
        for region in [None, "unknown", "https://untrusted.invalid", " " * 100]:
            with self.subTest(region=region):
                if region is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(region)
                with patch.object(self.kimi, "request_json") as request:
                    result = self.kimi.collect()
                request.assert_not_called()
                self.assertEqual(result["limits"], [])
                self.assertIn("region", result["authHelpText"].lower())

    def test_missing_malformed_or_oversized_auth(self):
        for raw in [None, "bad", "[]", "{}", '{"access_token": true}', '{"access_token": "bad\\nheader"}', " " * 65537]:
            with self.subTest(raw_type=type(raw).__name__):
                if raw is None:
                    self.auth.unlink(missing_ok=True)
                else:
                    self.auth.write_text(raw)
                with patch.object(self.kimi, "request_json") as request:
                    result = self.kimi.collect()
                request.assert_not_called()
                self.assertEqual(result["limits"], [])
                self.assertIn("login", result["authHelpText"])

    def test_failures_do_not_expose_tokens(self):
        errors = [urllib.error.HTTPError("https://api.kimi.com/", status, "synthetic-secret", {}, io.BytesIO())
                  for status in (401, 403, 429, 500)]
        errors += [urllib.error.URLError("synthetic-secret"), TimeoutError("synthetic-secret"),
                   ValueError("synthetic-secret"), http.client.IncompleteRead(b"synthetic-secret")]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                if isinstance(error, urllib.error.HTTPError):
                    self.addCleanup(error.close)
                with patch.object(self.kimi, "request_json", side_effect=error):
                    result = self.kimi.collect()
                self.assertEqual(result["limits"], [])
                self.assertTrue(result["usageStatusText"])
                self.assertNotIn("synthetic-secret", json.dumps(result))

    def test_cli_error_has_safe_json_and_diagnostic(self):
        self.auth.unlink()
        result = subprocess.run([sys.executable, str(ADAPTER / "limits.py")], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["limits"], [])
        self.assertIn("login", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class TestTransport(KimiTest):
    def test_known_region_hosts_and_no_redirects(self):
        for region, host in [("mainland-cn", "api.kimi.com"), ("global", "api.kimi.ai")]:
            with self.subTest(region=region), patch.object(self.kimi.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = io.BytesIO(b'{"usages": {}}')
                self.kimi.request_json(region, "synthetic-secret", "synthetic-device")
                request = build.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url, f"https://{host}/coding/v1/usages")
                self.assertEqual(request.get_method(), "GET")
                self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-secret")
                self.assertEqual(request.get_header("X-msh-device-id"), "synthetic-device")
                self.assertLessEqual(build.return_value.open.call_args.kwargs["timeout"], 10)
                redirect = next(h for h in build.call_args.args if isinstance(h, urllib.request.HTTPRedirectHandler))
                self.assertIsNone(redirect.redirect_request(request, None, 302, "Found", {}, "https://untrusted.invalid"))
        with self.assertRaises(ValueError):
            self.kimi.request_json("https://untrusted.invalid", "synthetic-secret", "synthetic-device")

    def test_invalid_oversized_and_truncated_replies(self):
        for raw in [b"bad", b"[]", b"\xff", b" " * (1048576 + 1)]:
            with self.subTest(length=len(raw)), patch.object(self.kimi.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = io.BytesIO(raw)
                with self.assertRaises(ValueError):
                    self.kimi.request_json("mainland-cn", "synthetic-secret", "")
        with patch.object(self.kimi.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = http.client.IncompleteRead(b"partial")
            with self.assertRaises(http.client.IncompleteRead):
                self.kimi.request_json("mainland-cn", "synthetic-secret", "")

    def test_manifest_detects_current_cli_home(self):
        manifest = json.loads((ADAPTER / "manifest.json").read_text())
        self.assertEqual(manifest["id"], "kimi")
        self.assertNotIn("collect", manifest)
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "KIMI_CODE_HOME": directory, "PATH": "/usr/bin:/bin"}
            command = manifest["detect"][0]["command"]
            self.assertNotEqual(subprocess.run(command, shell=True, env=env).returncode, 0)
            path = Path(directory) / "credentials"
            path.mkdir()
            (path / "kimi-code.json").write_text("{}")
            self.assertEqual(subprocess.run(command, shell=True, env=env).returncode, 0)


if __name__ == "__main__":
    unittest.main()
