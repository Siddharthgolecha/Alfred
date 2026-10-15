"""Cactus Needle/Whistle and schema-constrained multi-backend local reasoning."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)

def request_json(url, data=None, timeout=4):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(url, data=body, headers={"Content-Type": "application/json"} if body else {})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)

# Backward-compatible import for older callers and tests.
from .backends import ModelSupervisor
LlamaSupervisor = ModelSupervisor

# llama.cpp's JSON Schema response_format constrains plan structure;
# Python still performs semantic validation and tool authorization.
PLAN_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "maxLength": 800},
        "steps": {
            "type": "array", "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "args": {"type": "object"},
                    "when": {
                        "type": "object",
                        "properties": {
                            "step": {"type": "integer", "minimum": 0},
                            "field": {"type": "string"},
                            "op": {"type": "string", "enum": ["gt","gte","lt","lte","eq","neq"]},
                            "value": {"type": ["string","number","boolean"]}
                        },
                        "required": ["step","field","op","value"],
                        "additionalProperties": False
                    }
                },
                "required": ["tool","args"],
                "additionalProperties": False
            }
        },
        "save_as": {"type": ["string","null"]}
    },
    "required": ["message","steps","save_as"],
    "additionalProperties": False
}

class QwenPlanResponseError(ValueError):
    """The inference backend did not return a complete, schema-shaped plan."""


class Qwen:
    """Schema-constrained System 2 planner across compatible local services."""
    def __init__(self, cfg, supervisor):
        self.cfg = cfg
        self.supervisor = supervisor

    def _response_format(self, name, schema):
        # llama.cpp accepts the historical inline 'schema'; Ollama and LM
        # Studio use the OpenAI-style 'json_schema' envelope.
        if getattr(self.supervisor, "backend", None) in ("ollama", "lm_studio"):
            return {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}
        return {"type": "json_schema", "schema": schema}

    def _endpoint(self):
        # Older code/tests supply supervisors with no URL attribute.
        url = getattr(self.supervisor, "url", None)
        return url if isinstance(url, str) else self.cfg["url"].rstrip("/")

    def complete(self, transcript, tool_catalog, allowed_services=(), named_tasks=()):
        self.supervisor.ensure_running()
        model = self.supervisor.model_id()
        # Maintain compatibility with older callers which supply names only.
        schemas = [
            s if isinstance(s,dict) else {"name":str(s),"description":"","parameters":{}}
            for s in tool_catalog
        ]
        descriptions = json.dumps(schemas,ensure_ascii=True,separators=(",",":"))
        prompt = (
            "You are Alfred's local System 2 planner. Output strictly JSON following response_format. "
            "You are NOT a shell, system administrator, or executable Python interpreter. "
            "Return message, steps, save_as (null when not saving). "
            "Only use the exact provided tool names and argument keys/types. "
            "Do not invent a target service, script, timestamp, or observation. "
            "Allowlisted services: " + json.dumps(sorted(allowed_services)) + ". "
            "Allowlisted named tasks: " + json.dumps(sorted(named_tasks)) + ". "
            "Tools and their argument schemas: " + descriptions + ". "
            "Plan a maximum of five steps. Each step must have tool and args. "
            "Optional when has step (index of a strictly EARLIER result), field "
            "(one top-level result key), op (gt/gte/lt/lte/eq/neq), value (scalar). "
            "Only propose when the field exists in the tool's known result. "
            "No nested plans, loops, tool names outside catalogue, arbitrary commands, "
            "or unsupported event watches. "
            "Use steps=[] for an ordinary explanation or if the request is unclear. "
            "Use message to ask a specific clarification when details are missing. "
            "For observed system conditions, call read-only tools instead of asserting measurements. "
            "Use save_as only when asked explicitly to save/create a named workflow; "
            "saving does not execute the steps. "
            "Do not claim an action was completed in message; actions execute afterward "
            "and may require a separate local approval."
        )
        payload = {
            "messages": [{"role":"system","content":prompt},{"role":"user","content":transcript}],
            "temperature": 0, "max_tokens": int(self.cfg["max_tokens"]),
            "response_format": self._response_format("alfred_plan", PLAN_RESPONSE_SCHEMA),
            "stream": False
        }
        if model:
            payload["model"] = model
        # Qwen3.5 can spend its entire generation budget thinking, and some
        # llama.cpp versions fail to enforce JSON grammar in thinking mode.
        # Both forms are best-effort for differently aged llama.cpp servers.
        # Do not send backend-specific options to Ollama or LM Studio.
        if getattr(self.supervisor, "backend", None) in ("llama_cpp", "llama_server"):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload["reasoning_effort"] = "none"

        # A retry is generation-only: no returned text can become a tool call
        # until it passes System2.parse_plan and preflight. Never reinterpret a
        # failed plan as a successful schedule or silently discard its steps.
        for attempt in range(2):
            out = request_json(self._endpoint() + "/v1/chat/completions",
                               payload, timeout=90)
            try:
                return self._parse_plan_response(out)
            except QwenPlanResponseError as exc:
                if attempt:
                    raise QwenPlanResponseError(
                        "Qwen could not produce a valid plan after one retry. "
                        "No actions were executed; check the model chat template "
                        "and qwen.max_tokens."
                    ) from exc
                LOG.warning("Invalid Qwen plan (%s); retrying once", exc)
                payload["messages"][0]["content"] += (
                    " RETRY: Your previous reply was incomplete or malformed. "
                    "Return ONLY one JSON object with exactly these three keys: "
                    'message (string), steps (array), save_as (string or null). '
                    "For a missing schedule detail, ask in message and set "
                    "steps=[] and save_as=null. No markdown or reasoning text."
                )

    @staticmethod
    def _parse_plan_response(response):
        """Validate the envelope and top-level fields before any action path."""
        try:
            choice = response["choices"][0]
            finish_reason = choice.get("finish_reason")
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise QwenPlanResponseError("Missing chat completion content") from exc
        if finish_reason == "length":
            raise QwenPlanResponseError("Qwen completion ran out of output tokens")
        if not isinstance(content, str) or not content.strip():
            raise QwenPlanResponseError("Empty plan content (possibly thinking-only)")
        try:
            plan = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise QwenPlanResponseError("Plan content is not JSON") from exc
        if (not isinstance(plan, dict)
                or set(plan) != {"message", "steps", "save_as"}
                or not isinstance(plan["message"], str)
                or len(plan["message"]) > 800
                or not isinstance(plan["steps"], list)
                or not (plan["save_as"] is None or isinstance(plan["save_as"], str))):
            raise QwenPlanResponseError("Qwen response missing required plan fields")
        return plan

    def explain(self, user_request, observed_results):
        """Second bounded pass: interpret observed tool output; never return executable calls."""
        self.supervisor.ensure_running()
        model = self.supervisor.model_id()
        observations = json.dumps(observed_results,default=str,ensure_ascii=True)[:7000]
        payload = {
            "messages": [
                {"role":"system","content":
                    "You are Alfred, a concise local diagnostic assistant. "
                    "Summarize actual tool observations for the user; distinguish measurements "
                    "from possible causes. Do not claim facts absent from the provided tool results. "
                    "Do not issue instructions to execute actions, claim an action has succeeded, "
                    "or invent hardware readings. If measurements are insufficient, say so."},
                {"role":"user","content":
                    "User question: " + user_request[:1200] + "\nObserved read-only tool results:\n" + observations}
            ],
            "temperature":0,
            "max_tokens":min(512,int(self.cfg["max_tokens"])),
            "response_format":self._response_format("alfred_explanation", {
                "type":"object","properties":{"answer":{"type":"string","maxLength":1400}},
                "required":["answer"],"additionalProperties":False
            }),
            "stream":False
        }
        if model:
            payload["model"]=model
        out=request_json(self._endpoint() + "/v1/chat/completions",
                         payload,timeout=90)
        content=out["choices"][0]["message"].get("content") or ""
        try:
            answer=json.loads(content)["answer"]
        except (ValueError,TypeError,KeyError) as exc:
            raise ValueError("Qwen analysis did not return a valid answer") from exc
        if not isinstance(answer,str) or not answer.strip():
            raise ValueError("Qwen analysis answer is empty")
        return answer[:1400]

class Cactus:
    """One long-lived Needle instance; native inference kept on calling thread."""
    def __init__(self, schemas):
        import needle
        self.needle = needle
        self.agent = needle.Needle(tools=schemas)
        self.whistle = needle.Whistle()

    def route(self, transcript):
        self.agent.reset()
        return self.agent.complete(transcript, max_new_tokens=384)

    def route_feedback(self, observation_json):
        """Continue the current isolated task without reset; input is trusted scalar JSON.

        Needle.complete() interprets a JSON result as tool feedback. Never use
        Needle.run(), which would execute model-named Python functions itself.
        """
        return self.agent.complete(observation_json, max_new_tokens=384)

    def transcribe_stream(self, frames, on_partial=None):
        # Whistle commits segments as it receives streaming samples.
        pieces = []
        pending = ""
        for event in self.whistle.stream(frames, language="en", keywords=["Alfred", "Jetson", "Sony"]):
            committed = str(event.get("text") or "").strip()
            pending = str(event.get("pending") or "")
            if committed:
                pieces.append(committed)
            if on_partial:
                confirmed=" ".join(pieces).strip()
                provisional=pending.strip()
                # Some streaming providers return a pending transcript that
                # already includes the confirmed prefix; do not echo it twice.
                if provisional and confirmed and provisional.lower().startswith(confirmed.lower()):
                    shown=provisional
                else:
                    shown=" ".join(part for part in (confirmed,provisional) if part)
                on_partial(shown)
        return " ".join(pieces).strip()
