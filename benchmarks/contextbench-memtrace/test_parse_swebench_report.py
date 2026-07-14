import importlib.util
import sys
import unittest
from pathlib import Path

import pandas as pd


MODULE_PATH = Path(__file__).with_name("parse_swebench_report.py")
SPEC = importlib.util.spec_from_file_location("parse_swebench_report", MODULE_PATH)
assert SPEC and SPEC.loader
parser = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = parser
SPEC.loader.exec_module(parser)


class ParseSwebenchReportTests(unittest.TestCase):
    def test_full_dataset_mode_counts_unsubmitted_rows(self):
        frame = pd.DataFrame(
            [
                {"instance_id": "context-1", "original_inst_id": "original-1"},
                {"instance_id": "context-2", "original_inst_id": "original-2"},
            ]
        )
        rows = parser.result_rows(
            frame,
            {"resolved_ids": ["original-1"], "completed_ids": ["original-1"]},
            {"original-1"},
            include_all_dataset=True,
        )
        self.assertEqual([row["resolved"] for row in rows], [True, False])
        self.assertEqual(rows[1]["status"], "not_submitted")

    def test_subset_mode_uses_only_submitted_predictions(self):
        frame = pd.DataFrame(
            [
                {"instance_id": "context-1", "original_inst_id": "original-1"},
                {"instance_id": "context-2", "original_inst_id": "original-2"},
            ]
        )
        rows = parser.result_rows(frame, {}, {"original-2"})
        self.assertEqual([row["instance_id"] for row in rows], ["context-2"])


if __name__ == "__main__":
    unittest.main()
