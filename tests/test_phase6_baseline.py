"""Phase 6 frozen baseline: source-compatible, model-free security regressions."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts.bench_phase6 import (
    FIXTURES, PARTITIONS, load_corpus, replay, run, validate_case,
)


class Phase6BaselineTests(unittest.TestCase):
    def test_frozen_corpus_and_partition_separation(self):
        manifest, cases = load_corpus()
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["baseline_main_sha"],
                         "88876d00836787a7a1e75806c1cda10809e8bb41")
        self.assertEqual(len(cases), 12)
        self.assertEqual([sum(c["partition"] == p for c in cases)
                          for p in PARTITIONS], [8, 4])
        self.assertTrue(all(c["expected_phase6"]["evidence"] == "unknown"
                            for c in cases))

    def test_actual_phase5_replay_matches_frozen_expectations(self):
        outcome = run()
        self.assertTrue(outcome["verified_against_frozen_expectations"])
        self.assertEqual(len(outcome["cases"]), 12)
        self.assertTrue(all(not row["consequential_effects"]
                            for row in outcome["cases"]))

    def test_repeat_run_is_byte_deterministic(self):
        a = json.dumps(run(), sort_keys=True, separators=(",", ":"))
        b = json.dumps(run(), sort_keys=True, separators=(",", ":"))
        self.assertEqual(a, b)

    def test_committed_baseline_snapshot_is_exact(self):
        frozen = json.loads((FIXTURES / "baseline_outputs_v1.json").read_text(
            encoding="utf-8"))
        self.assertEqual(run(), frozen)
        self.assertEqual(len(frozen["cases"]), 12)

    def test_heldout_cannot_be_silently_modified(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            for path in FIXTURES.glob("*.json"):
                shutil.copy2(path, target / path.name)
            heldout = target / "heldout.json"
            payload = heldout.read_text(encoding="utf-8")
            heldout.write_text(payload.replace("hold_gpu_status", "hold_gpu_revise"),
                               encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                load_corpus(target)

    def test_undeclared_fields_and_privileged_fixtures_rejected(self):
        _, cases = load_corpus()
        candidate = deepcopy(cases[0])
        candidate["secret"] = "none"
        with self.assertRaisesRegex(ValueError, "fields"):
            validate_case(candidate, "development")
        candidate = deepcopy(cases[0])
        candidate["capability_snapshot"]["native_model"] = "native"
        with self.assertRaisesRegex(ValueError, "capability"):
            validate_case(candidate, "development")
        candidate = deepcopy(cases[0])
        candidate["intention"] = "https://example.com/remote"
        with self.assertRaisesRegex(ValueError, "intention"):
            validate_case(candidate, "development")

    def test_known_unsafe_proposals_never_call_write_tool(self):
        _, cases = load_corpus()
        by_id = {case["id"]: case for case in cases}
        for key in ("dev_bad_args", "dev_service_denied",
                    "dev_service_staged", "hold_duplicate_write",
                    "hold_untrusted_injection"):
            with self.subTest(key=key):
                row = replay(by_id[key])
                self.assertFalse(row["consequential_effects"])
                self.assertEqual(row["executed_reads"], [])
        self.assertEqual(replay(by_id["dev_service_staged"])["outcome"],
                         "pending_approval")


if __name__ == "__main__":
    unittest.main()
