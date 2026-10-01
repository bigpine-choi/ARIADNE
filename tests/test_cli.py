"""Verify separate Python processes preserve data without network or model access."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import runpy
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


class CliWorkflowTests(unittest.TestCase):
    def invoke(self, database, *arguments):
        result = subprocess.run(
            [sys.executable, "-m", "ariadne", "--db", str(database), *arguments],
            cwd=ROOT, capture_output=True, encoding="utf-8", timeout=15,
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_demo_restart_and_backup_in_separate_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "operational.sqlite3"
            restored = Path(directory) / "restore.sqlite3"
            self.invoke(database, "demo")
            first = json.loads(self.invoke(database, "list"))
            self.assertEqual("[가상] 펀드 A 날인본 수령·검토", first[0]["title"])
            self.assertEqual("waiting_external", first[0]["status"])
            self.invoke(database, "demo")
            second = json.loads(self.invoke(database, "list"))
            self.assertEqual(first, second)
            self.invoke(database, "backup", str(restored))
            self.assertEqual(first, json.loads(self.invoke(restored, "list")))

    def test_doctor_reports_pending_external_checks_without_creating_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "should-not-exist.sqlite3"
            report = json.loads(self.invoke(database, "doctor"))
            self.assertTrue(report["pythonSupported"])
            self.assertEqual("disabled", report["externalAi"])
            self.assertEqual("not-configured", report["graph"])
            self.assertFalse(database.exists())

    def test_engine_harness_uses_its_own_ephemeral_service(self):
        harness = runpy.run_path(str(ROOT / "scripts" / "godot_smoke.py"))
        with tempfile.TemporaryDirectory() as directory:
            process = harness["start_service"](Path(directory) / "disposable.sqlite3")
            try:
                endpoint = __import__("os").environ["ARIADNE_API_BASE"]
                self.assertTrue(endpoint.startswith("http://127.0.0.1:"))
                with urlopen(endpoint + "/api/work-items", timeout=5) as response:
                    item = json.loads(response.read().decode("utf-8"))["items"][0]
                self.assertEqual("synthetic-task-001", item["id"])
            finally:
                harness["stop_service"](process)

