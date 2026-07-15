import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "aws" / "full_preflight_runner.py"
SPEC = importlib.util.spec_from_file_location("full_preflight_runner", MODULE_PATH)
RUNNER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RUNNER)


class FullPreflightRunnerTests(unittest.TestCase):
    def test_task_query_is_deterministic_and_bounded(self):
        query = RUNNER.task_query("  alpha\n beta  " + "x" * 1000)
        self.assertTrue(query.startswith("alpha beta"))
        self.assertEqual(len(query), 600)

    def test_manifest_loader_preserves_explicit_order(self):
        import pandas as pd

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "full.parquet"
            manifest = root / "manifest.json"
            pd.DataFrame({"instance_id": ["a", "b"]}).to_parquet(dataset)
            manifest.write_text(json.dumps(["b", "a"]))
            self.assertEqual(RUNNER.load_manifest(dataset, manifest), ["b", "a"])

    def test_pin_shim_preserves_real_binary_and_queues(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "memtrace"
            binary.write_bytes(b"\x7fELFfake")
            binary.chmod(0o755)
            real = RUNNER.install_pin_shim(binary)
            self.assertEqual(real.read_bytes(), b"\x7fELFfake")
            text = binary.read_text()
            self.assertIn(RUNNER.PIN_SHIM_MARKER, text)
            self.assertIn("MEMTRACE_PIN_WAIT_SECONDS", text)
            RUNNER.install_pin_shim(binary)
            self.assertEqual(real.read_bytes(), b"\x7fELFfake")


if __name__ == "__main__":
    unittest.main()
