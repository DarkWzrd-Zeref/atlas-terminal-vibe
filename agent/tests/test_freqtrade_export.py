"""Credential-free checks for the inert Vibe-to-Freqtrade artifact boundary."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("freqtrade_export", Path(__file__).resolve().parents[1] / "src/api/freqtrade_export.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.runs = Path(self.folder.name)
        self.artifact = self.runs / "run_123/artifacts/freqtrade"
        self.artifact.mkdir(parents=True)
        self.meta = {"strategy_class": "ExampleStrategy", "timeframe": "15m", "entrypoint": "ExampleStrategy.py", "semantic_notes": "Explicit example, no conversion claimed.", "dependencies": []}
        (self.artifact / "strategy.json").write_text(json.dumps(self.meta))
        self.source = b"raise RuntimeError('must never import during export')\n"
        (self.artifact / "ExampleStrategy.py").write_bytes(self.source)
        (self.artifact / "parameters.json").write_text('{"lookback": 20}')

    def test_preserves_files_and_hashes_without_executing(self):
        result = exporter.export_freqtrade(self.runs, "run_123")
        self.assertFalse(result["executed"])
        files = {f["path"]: base64.b64decode(f["content_base64"]) for f in result["files"]}
        self.assertEqual(files["ExampleStrategy.py"], self.source)
        self.assertIn("parameters.json", files)
        canonical = json.dumps(result["manifest"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertEqual(result["sha256"], hashlib.sha256(canonical).hexdigest())
        (self.artifact / "parameters.json").write_text('{"lookback": 21}')
        self.assertNotEqual(result["sha256"], exporter.export_freqtrade(self.runs, "run_123")["sha256"])

    def test_rejects_traversal_missing_file_and_missing_semantics(self):
        for run in ("../run_123", "run_123/..", "/run_123"):
            with self.assertRaises(exporter.ExportError):
                exporter.export_freqtrade(self.runs, run)
        self.meta["entrypoint"] = "missing.py"
        (self.artifact / "strategy.json").write_text(json.dumps(self.meta))
        with self.assertRaises(exporter.ExportError):
            exporter.export_freqtrade(self.runs, "run_123")
        self.meta["semantic_notes"] = ""
        (self.artifact / "strategy.json").write_text(json.dumps(self.meta))
        with self.assertRaises(exporter.ExportError):
            exporter.export_freqtrade(self.runs, "run_123")

    def test_rejects_oversized_file_without_reading_it(self):
        with (self.artifact / "large.bin").open("wb") as handle:
            handle.truncate(exporter.MAX_FILE + 1)
        with self.assertRaises(exporter.ExportError):
            exporter.export_freqtrade(self.runs, "run_123")

    def test_rejects_symlink_to_other_run(self):
        outside = self.runs / "private.txt"
        outside.write_text("test-only-secret-sentinel")
        try:
            (self.artifact / "escape.txt").symlink_to(outside)
        except OSError:
            self.skipTest("This Windows account cannot create symlinks")
        with self.assertRaises(exporter.ExportError):
            exporter.export_freqtrade(self.runs, "run_123")


if __name__ == "__main__":
    unittest.main()
