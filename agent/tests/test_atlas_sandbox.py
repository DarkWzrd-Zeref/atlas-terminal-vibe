"""Broker contract tests; Linux isolation is additionally verified at startup.

Run without optional project test dependencies:
    python -m unittest discover -s agent/tests -p test_atlas_sandbox.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

AGENT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("atlas_sandbox", AGENT / "atlas_sandbox.py")
sandbox = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sandbox
spec.loader.exec_module(sandbox)


class BrokerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.runs = self.root / "runs"
        self.run = self.runs / "one"
        self.run.mkdir(parents=True)
        (self.run / "config.json").write_text("{}")

    def tearDown(self):
        self.temp.cleanup()

    def test_current_native_run_is_accepted(self):
        self.assertEqual(sandbox.validate_run(str(self.run), (self.runs,)), self.run)

    def test_parent_and_sibling_private_state_are_rejected(self):
        private = self.root / "sessions"
        private.mkdir()
        for path in (private, self.runs, self.runs / ".." / "sessions"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                sandbox.validate_run(str(path), (self.runs,))

    def test_linked_secret_is_rejected(self):
        secret = self.root / "dummy-secret"
        secret.write_text("not-an-actual-credential")
        try:
            (self.run / "input").symlink_to(secret)
        except OSError:
            self.skipTest("The host does not permit symlink creation")
        with self.assertRaises(ValueError):
            sandbox.validate_run(str(self.run), (self.runs,))

    def test_hard_linked_secret_is_rejected(self):
        secret = self.root / "dummy-secret"
        secret.write_text("not-an-actual-credential")
        os.link(secret, self.run / "input")
        with self.assertRaises(ValueError):
            sandbox.validate_run(str(self.run), (self.runs,))

    def test_symlinked_run_root_cannot_expand_trust(self):
        outside = self.root / "private"
        outside.mkdir()
        nested = outside / "one"
        nested.mkdir()
        link = self.root / "mapped-runs"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("The host does not permit symlink creation")
        with self.assertRaises(ValueError):
            sandbox.validate_run(str(nested), (link,))

    def payload(self, **updates):
        result = {"operation": "backtest", "run_dir": str(self.run), "timeout": 300, "env": {}}
        result.update(updates)
        return result

    def test_arbitrary_command_and_interpreter_fields_rejected(self):
        for update in ({"operation": "shell"}, {"command": "anything"}, {"python": "/tmp/python"},
                       {"timeout": 0}, {"timeout": 901}, {"timeout": True}, {"env": {"KEY": 1}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                sandbox.validate_request(self.payload(**update))

    def test_loader_environment_cannot_reintroduce_service_secrets(self):
        hostile = {"OPENAI_API_KEY": "dummy", "API_AUTH_KEY": "dummy", "ATLAS_VIBE_API_KEY": "dummy",
                   "LD_PRELOAD": "/tmp/evil.so", "PYTHONPATH": "/tmp", "HOME": "/data/home",
                   "VIBE_TRADING_HOME": "/data/home", "VIBE_TRADING_ALLOWED_RUN_ROOTS": "/data",
                   "FINNHUB_API_KEY": "allowed-read-only-data-key"}
        with patch.object(sandbox, "validate_run", return_value=self.run):
            _, _, loader = sandbox.validate_request(self.payload(env=hostile))
        self.assertEqual(loader, {"FINNHUB_API_KEY": "allowed-read-only-data-key"})
        child = sandbox._child_env(self.run, self.root / "temporary-home", hostile)
        for key in ("OPENAI_API_KEY", "API_AUTH_KEY", "ATLAS_VIBE_API_KEY", "LD_PRELOAD", "PYTHONPATH"):
            self.assertNotIn(key, child)
        self.assertEqual(child["VIBE_TRADING_ALLOWED_RUN_ROOTS"], str(self.run))
        self.assertEqual(child["ATLAS_SANDBOX_CHILD"], "1")
        self.assertEqual(child["PYTHON_DOTENV_DISABLED"], "1")
        self.assertEqual(child["HOME"], str(self.root / "temporary-home"))

    def test_missing_broker_fails_closed(self):
        with patch.object(sandbox, "SOCKET", self.root / "missing.sock"):
            with self.assertRaisesRegex(RuntimeError, "execution was refused"):
                sandbox.request_backtest(self.run, 1, {})

    def test_unavailable_kernel_boundary_fails_closed(self):
        with patch.object(sandbox.sys, "platform", "not-linux"):
            with self.assertRaises(RuntimeError):
                sandbox.restrict_filesystem([], [])


class ChildSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict
        except ImportError:
            raise unittest.SkipTest("Install the pinned pydantic-settings dependency for settings tests")
        cls.source = DotEnvSettingsSource

        class DependencySettings(BaseSettings):
            model_config = SettingsConfigDict(env_file=".env", env_prefix="ATLAS_TEST_")
            secret: str = "not-loaded"
            data_key: str = "default-data"

        cls.settings = DependencySettings

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp.name) / ".env"
        self.env_file.write_text("ATLAS_TEST_SECRET=synthetic-credential\n", encoding="utf-8")
        self.source_patch = patch.object(self.source, "_read_env_files", self.source._read_env_files)
        self.source_patch.start()

    def tearDown(self):
        self.source_patch.stop()
        self.temp.cleanup()

    def test_trusted_settings_keep_their_file_source(self):
        with patch.dict(os.environ, {"ATLAS_SANDBOX_CHILD": "", "PYTHON_DOTENV_DISABLED": "1"}):
            original = self.source._read_env_files
            sandbox._disable_child_dotenv()
            self.assertIs(self.source._read_env_files, original)
            self.assertEqual(self.settings(_env_file=self.env_file).secret, "synthetic-credential")

    def test_child_never_reads_dotenv_but_keeps_filtered_environment_and_init_values(self):
        with patch.dict(os.environ, {"ATLAS_SANDBOX_CHILD": "1", "PYTHON_DOTENV_DISABLED": "1",
                                    "ATLAS_TEST_DATA_KEY": "allowed-data"}), \
             patch("pydantic_settings.sources.providers.dotenv.dotenv_values",
                   side_effect=PermissionError("synthetic private settings")) as read:
            # Demonstrate why PYTHON_DOTENV_DISABLED alone is insufficient.
            with self.assertRaises(PermissionError):
                self.settings(_env_file=self.env_file)
            read.reset_mock()
            sandbox._disable_child_dotenv()
            for env_file in (None, self.env_file, [self.env_file, self.env_file]):
                with self.subTest(env_file=env_file):
                    value = self.settings(_env_file=env_file)
                    self.assertEqual(value.secret, "not-loaded")
                    self.assertEqual(value.data_key, "allowed-data")
            self.assertEqual(self.settings().secret, "not-loaded")
            self.assertEqual(self.settings(data_key="explicit").data_key, "explicit")
            self.assertEqual(self.source(self.settings, env_file=self.env_file)(), {})
            read.assert_not_called()


class RunnerBrokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Only Runner's optional presentation dependency is stubbed; the real
        # execution, failure, and artifact-collection code runs unchanged.
        presentation = types.ModuleType("rich.console")
        presentation.Console = lambda **kwargs: types.SimpleNamespace(print=lambda *args, **kwargs: None)
        spec = importlib.util.spec_from_file_location("atlas_test_native_runner", AGENT / "src/core/runner.py")
        cls.runner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.runner
        with patch.dict(sys.modules, {"rich.console": presentation}):
            spec.loader.exec_module(cls.runner)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"ATLAS_REQUIRE_ISOLATION": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_non_native_script_is_never_dispatched(self):
        with patch.object(sandbox, "request_backtest") as broker:
            with self.assertRaisesRegex(RuntimeError, "only the native"):
                self.runner.Runner().execute(self.run / "custom.py", self.run, cwd=AGENT, cli_args=[str(self.run)])
        broker.assert_not_called()

    def test_missing_broker_produces_failed_run_without_local_retry(self):
        with patch.object(sandbox, "request_backtest", side_effect=RuntimeError("broker unavailable")), \
             patch.object(self.runner.subprocess, "run") as direct:
            result = self.runner.Runner().execute(AGENT / "backtest/runner.py", self.run, cwd=AGENT, cli_args=[str(self.run)])
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 126)
        self.assertIn("broker unavailable", result.stderr)
        direct.assert_not_called()

    def test_native_outputs_still_surface_in_runner_result(self):
        (self.run / "artifacts").mkdir()
        (self.run / "artifacts/metrics.csv").write_text("trade_count\n2\n")
        native = subprocess.CompletedProcess("native backtest", 0, "completed", "")
        with patch.object(sandbox, "request_backtest", return_value=native):
            result = self.runner.Runner().execute(AGENT / "backtest/runner.py", self.run, cwd=AGENT, cli_args=[str(self.run)])
        self.assertTrue(result.success)
        self.assertEqual(result.artifacts["metrics"], self.run / "artifacts/metrics.csv")
        self.assertEqual((self.run / "logs/runner_stdout.txt").read_text(), "completed")

    def test_timeout_preserves_native_tool_failure_contract(self):
        with patch.object(sandbox, "request_backtest", side_effect=subprocess.TimeoutExpired("native", 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.runner.Runner().execute(AGENT / "backtest/runner.py", self.run, cwd=AGENT, cli_args=[str(self.run)])


if __name__ == "__main__":
    unittest.main()
