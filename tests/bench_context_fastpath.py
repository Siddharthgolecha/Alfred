"""Interleaved context-off vs context-on direct fast-path overhead (mock inference).

Measures Python/SQLite/context policy only, *not* Needle/Qwen/Jetson latency.
No new model calls should be added by ContextManager.
"""
import random
import statistics
from pathlib import Path
import tempfile
import time
from unittest.mock import Mock

from alfred.app import Alfred
from alfred.config import load_config


def percentile(data, frac):
    seq=sorted(data)
    return seq[min(len(seq)-1,round((len(seq)-1)*frac))]


def run(samples=160):
    with tempfile.TemporaryDirectory() as tmp:
        apps={}
        for enabled in (False,True):
            cfg=load_config()
            cfg["runtime"]["database"]=str(Path(tmp)/("on.db" if enabled else "off.db"))
            cfg["runtime"]["logs"]=tmp
            cfg["runtime"]["context_enabled"]=enabled
            cfg["runtime"]["needle_loop_enabled"]=False
            app=Alfred(cfg,load_models=False)
            app.cactus=Mock()
            app.cactus.route.return_value={
                "confidence":.98,
                "function_calls":[{"name":"get_gpu_status","arguments":{}}]}
            app.system2.direct=Mock(return_value={
                "message":"Done","results":[{"available":True,"gpu_temp_c":65}]})
            app.feedback.tell=Mock()
            apps[enabled]=app
        try:
            order=[False,True]*(samples+30)
            random.Random(160).shuffle(order)
            data={False:[],True:[]}
            for i,on in enumerate(order):
                app=apps[on]
                begin=time.perf_counter_ns()
                app.handle_text("Check GPU temperature")
                us=(time.perf_counter_ns()-begin)/1000
                if i>=60:
                    data[on].append(us)
            for enabled,app in apps.items():
                assert app.cactus.route.call_count==order.count(enabled)
                assert app.system2.direct.call_count==order.count(enabled)
                # The shared ContextManager has not caused a single Qwen call.
                assert app.cactus.route_feedback.call_count==0
            for enabled in (False,True):
                v=data[enabled]
                print(f"context_{'enabled' if enabled else 'baseline'} n={len(v)} "
                      f"P50={percentile(v,.50):.1f}us P95={percentile(v,.95):.1f}us")
            print(f"context-minus-baseline P50="
                  f"{percentile(data[True],.50)-percentile(data[False],.50):+.1f}us "
                  f"P95={percentile(data[True],.95)-percentile(data[False],.95):+.1f}us")
            print("Mock-only, no local model or Jetson hardware.")
        finally:
            for app in apps.values():
                app.close()


if __name__=="__main__":
    run()
