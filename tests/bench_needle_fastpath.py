"""Offline latency comparison for unchanged fast path with loop disabled/enabled.

These numbers measure Python routing/ledger/policy overhead with deterministic
fake native inference. They are NOT Needle inference latency or Jetson hardware
measurements. Run on a single host/interleaved to limit thermal/IO drift.
"""
from __future__ import annotations

from pathlib import Path
import random
import statistics
import tempfile
import time
from unittest.mock import Mock

from alfred.app import Alfred
from alfred.config import load_config


def percentile(values, q):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * q) - 1))]


def main(samples=300):
    if type(samples) is not int or not 30 <= samples <= 10000:
        raise ValueError("Invalid sample count")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = load_config()
        cfg["runtime"]["database"] = str(Path(tmp)/"latency.db")
        cfg["runtime"]["logs"] = tmp
        app = Alfred(cfg, load_models=False)
        app.cactus = Mock()
        app.cactus.route.return_value = {
            "function_calls": [{"name":"get_gpu_status","arguments":{}}],
            "confidence": .99
        }
        app.system2.direct = Mock(return_value={"message":"Completed","results":[{
            "available":True,"gpu_temp_c":60}]})
        app.feedback.tell = Mock()
        records = {"baseline": [], "loop_enabled": []}
        try:
            sequence = ["baseline", "loop_enabled"] * samples
            random.Random(14).shuffle(sequence)
            for ix, name in enumerate(sequence):
                app.cfg["runtime"]["needle_loop_enabled"] = name == "loop_enabled"
                start = time.perf_counter_ns()
                app.handle_text("GPU status")
                elapsed = (time.perf_counter_ns()-start)/1000
                if ix >= 60:
                    records[name].append(elapsed)
            assert app.cactus.route.call_count == len(sequence)
            app.cactus.route_feedback.assert_not_called()
        finally:
            app.close()
        for name, measurements in records.items():
            print(f"{name}: n={len(measurements)} P50={percentile(measurements,.5):.1f} us "
                  f"P95={percentile(measurements,.95):.1f} us")
        delta50 = percentile(records["loop_enabled"],.5) - percentile(records["baseline"],.5)
        delta95 = percentile(records["loop_enabled"],.95) - percentile(records["baseline"],.95)
        print(f"loop-minus-baseline: P50={delta50:+.1f} us P95={delta95:+.1f} us "
              "(mock inference, same-host interleaved; not a native performance claim)")


if __name__ == "__main__":
    main()
