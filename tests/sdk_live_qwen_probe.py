"""Opt-in REAL local Qwen + official Cordis planning smoke (never a fake model).

Run only with a Qwen server already on loopback. This never executes a proposed
tool or stages approvals. Requires pinned official SDK/Cordis distributions.
Not part of portable CI: no model download, GPU or inference hardware there.
"""
from __future__ import annotations
import argparse
import json
from alfred.cognitive.harness_sdk import LocalHarness
from alfred.inference_admission import InferenceAdmission
from alfred.runtime import TOOL_SCHEMAS


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",required=True,
                        help="Local HTTP origin of existing Qwen server")
    parser.add_argument("--model",required=True,
                        help="Exact model ID advertised by local server")
    args=parser.parse_args(argv)
    admission=InferenceAdmission(enabled=True)
    client=LocalHarness(args.url,args.model,timeout=90,max_tokens=512,
                        admission=admission)
    try:
        audit=client.audit_installation()
        client.require_ready()
        result=client.plan("Check the current GPU status", "a"*32,
                           tools=TOOL_SCHEMAS,max_steps=3)
        # Assert actual *model generation* rather than the fabricated
        # stub's unconditional success. Reject empty/refusal/wrong tool plans.
        if (type(result) is not dict
                or set(result)!={"message","steps","save_as"}
                or type(result["steps"]) is not list
                or len(result["steps"])!=1
                or result["steps"][0] != {"tool":"get_gpu_status","args":{}}
                or result["save_as"] is not None):
            raise AssertionError(
                "Real Qwen did not return expected safe GPU-status proposal")
        print(json.dumps({"status":"real_qwen_plan_pass",
                          "sdk_version":audit["sdk_version"],
                          "cordis_plugins":audit["plugin_audit"],
                          "tool_proposal":"get_gpu_status",
                          "tool_executed":False,
                          "hardware_validated":False}))
        return 0
    finally:
        client.close()
        admission.close()


if __name__=="__main__":
    raise SystemExit(main())
