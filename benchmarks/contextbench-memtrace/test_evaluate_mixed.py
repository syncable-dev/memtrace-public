import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("evaluate_mixed.py")
SPEC = importlib.util.spec_from_file_location("evaluate_mixed", MODULE_PATH)
assert SPEC and SPEC.loader
evaluator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evaluator
SPEC.loader.exec_module(evaluator)


class MixedEvaluatorTests(unittest.TestCase):
    def test_image_names_match_official_runners(self):
        self.assertEqual(
            evaluator.task_image(
                {
                    "instance_id": "Multi-SWE-Bench__go__x",
                    "original_inst_id": "cli__cli-5973",
                },
                {},
            ),
            "mswebench/cli_m_cli:pr-5973",
        )
        self.assertEqual(
            evaluator.task_image(
                {
                    "instance_id": "SWE-PolyBench__python__x",
                    "original_inst_id": "org__repo-1",
                },
                {},
            ),
            "ghcr.io/timesler/swe-polybench.eval.x86_64.org__repo-1:latest",
        )
        self.assertEqual(
            evaluator.task_image(
                {
                    "instance_id": "SWE-Bench-Pro__python__x",
                    "original_inst_id": "instance-1",
                },
                {"dockerhub_tag": "org.repo-tag"},
            ),
            "jefzda/sweap-images:org.repo-tag",
        )

    def test_parse_list_accepts_dataset_string(self):
        self.assertEqual(evaluator.parse_list("['a.py', 'b.py']"), ["a.py", "b.py"])


if __name__ == "__main__":
    unittest.main()
