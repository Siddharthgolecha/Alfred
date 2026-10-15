"""Issue #15: interleaved fake-model timing for direct vs bounded handoff.

No native Cactus/Qwen inference or Jetson devices are involved. The measured
difference is Python ledger/policy/JSON orchestration overhead, NOT a latency
improvement or real model response time. Compare on the same host/runner.
"""
from pathlib import Path
import random
import tempfile
import time
from unittest.mock import Mock

from alfred.app import Alfred
from alfred.config import load_config


def percentile(items, fraction):
    values = sorted(items)
    return values[max(0, min(len(values)-1, round((len(values)-1)*fraction)))]


def main(samples=200):
    if type(samples) is not int or not 30 <= samples <= 2000:
        raise ValueError("Invalid sample count")
    with tempfile.TemporaryDirectory() as root:
        cfg=load_config()
        cfg["runtime"]["database"]=str(Path(root)/"handoff.db")
        cfg["runtime"]["logs"]=root
        cfg["runtime"]["escalation_enabled"]=True
        cfg["runtime"]["context_enabled"]=False
        cfg["runtime"]["needle_loop_enabled"]=False
        cfg["runtime"]["resource_admission_enabled"]=False
        app=Alfred(cfg, load_models=False)
        try:
            app.feedback.tell=Mock()
            app.cactus=Mock()
            app.system2.direct=Mock(return_value={
                "message":"Completed","results":[{"available":True,"gpu_temp_c":64}]})
            app.supervisor.ensure_running=Mock(return_value=True)
            app.harness=Mock()
            app.harness.plan.return_value={
                "message":"Explanation from fake model","steps":[],"save_as":None}
            durations={"direct":[],"escalation":[]}
            order=["direct","escalation"]*(samples+25)
            random.Random(15).shuffle(order)
            for i,mode in enumerate(order):
                app.cactus.route.return_value=({
                    "confidence":.99, "function_calls":[
                        {"name":"get_gpu_status","arguments":{}}]}
                    if mode=="direct" else
                    {"confidence":1.0,"function_calls":[]})
                start=time.perf_counter_ns()
                app.handle_text("GPU status" if mode=="direct"
                                else "Explain how quantum teleportation works")
                us=(time.perf_counter_ns()-start)/1000
                if i>=50:
                    durations[mode].append(us)
            assert len(durations["direct"])>100
            assert len(durations["escalation"])>100
            assert app.system2.direct.call_count==order.count("direct")
            assert app.harness.plan.call_count==order.count("escalation")
            assert app.system2.qwen.complete.call_count==0
        finally:
            app.close()
    for kind,items in durations.items():
        print(f"{kind} n={len(items)} P50={percentile(items,.5):.1f}us "
              f"P95={percentile(items,.95):.1f}us")
    print(f"handoff-minus-direct P50="
          f"{percentile(durations['escalation'],.5)-percentile(durations['direct'],.5):+.1f}us "
          f"P95={percentile(durations['escalation'],.95)-percentile(durations['direct'],.95):+.1f}us")
    print("FAKE SDK planning only: no Cordis/native models/Jetson, no device speed claim.")


if __name__=="__main__":
    main()
