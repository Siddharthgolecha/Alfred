"""Cactus Needle/Whistle and schema-constrained multi-backend local reasoning."""
from __future__ import annotations

from contextlib import nullcontext

import json
import logging
import math
from urllib.request import Request, urlopen

from .constants import Limits, Timeouts, Truncation
from .prompts import (
    build_planner_system_prompt,
    CONTEXT_DATA_PREFIX,
    HANDOFF_DATA_PREFIX,
    PLANNER_RETRY_SUFFIX,
    EXPLAIN_SYSTEM_PROMPT,
)

LOG = logging.getLogger(__name__)

MAX_NEW_TOKENS = 384

def request_json(url: str, data: object = None, timeout: int = Timeouts.REQUEST_JSON) -> object:
    """POST or GET a JSON endpoint; raises on network / HTTP errors."""
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
        "message": {"type": "string", "maxLength": Limits.QWEN_MESSAGE_MAX},
        "steps": {
            "type": "array", "maxItems": Limits.QWEN_PLAN_MAX_STEPS,
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


def _unique_json_pairs(pairs):
    """Refuse ambiguous object keys at every depth, not just top-level."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise QwenPlanResponseError("Duplicate JSON key in Qwen plan")
        result[key] = value
    return result


def _reject_nonfinite_constant(value):
    """Reject non-finite JSON constants ('NaN', 'Infinity', '-Infinity')."""
    raise QwenPlanResponseError("Non-finite JSON number in Qwen plan")


def _finite_json_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise QwenPlanResponseError("Non-finite JSON number in Qwen plan")
    return parsed


class Qwen:
    """Schema-constrained System 2 planner across compatible local services."""
    def __init__(self, cfg, supervisor, admission=None):
        self.admission = admission
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

    def _run_inference(self, payload):
        """Ensure the backend is running, stamp the model name into *payload* (if known),
        and POST to the chat endpoint. Use this for single-shot inference calls."""
        self.supervisor.ensure_running()
        model = self.supervisor.model_id()
        if model:
            payload["model"] = model
        return request_json(self._endpoint() + "/v1/chat/completions", payload, timeout=90)

    def complete(self, transcript, tool_catalog, allowed_services=(), named_tasks=(), *,
                 context=None, handoff=None):
        guard = (self.admission.claim("qwen")
                 if self.admission is not None else nullcontext())
        with guard:
            return self._complete_admitted(
                transcript, tool_catalog, allowed_services, named_tasks,
                context=context, handoff=handoff)

    def _complete_admitted(self, transcript, tool_catalog,
                            allowed_services=(), named_tasks=(), *,
                            context=None, handoff=None):
        self.supervisor.ensure_running()
        model = self.supervisor.model_id()
        # Maintain compatibility with older callers which supply names only.
        schemas = [
            s if isinstance(s, dict) else {"name": str(s), "description": "", "parameters": {}}
            for s in tool_catalog
        ]
        system_prompt = build_planner_system_prompt(schemas, allowed_services, named_tasks)
        messages = [{"role": "system", "content": system_prompt}]
        if context and (context.get("slots") or context.get("task_observations")
                        or context.get("user_approved_facts")):
            messages.append({"role": "user", "content":
                CONTEXT_DATA_PREFIX + json.dumps(context, sort_keys=True, separators=(",", ":"))})
        messages.append({"role": "user", "content": transcript})
        if handoff is not None:
            messages.append({"role": "user", "content":
                HANDOFF_DATA_PREFIX + handoff.to_model_context()})
        payload = {
            "messages": messages,
            "temperature": 0, "max_tokens": int(self.cfg["max_tokens"]),
            "response_format": self._response_format("alfred_plan", PLAN_RESPONSE_SCHEMA),
            "stream": False
        }
        # Qwen3.5 can spend its entire generation budget thinking, and some
        # llama.cpp versions fail to enforce JSON grammar in thinking mode.
        # Both forms are best-effort for differently aged llama.cpp servers.
        # Do not send backend-specific options to Ollama or LM Studio.
        if getattr(self.supervisor, "backend", None) in ("llama_cpp", "llama_server"):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload["reasoning_effort"] = "none"

        if model:
            payload["model"] = model
        # A retry is generation-only: no returned text can become a tool call
        # until it passes System2.parse_plan and preflight. Never reinterpret a
        # failed plan as a successful schedule or silently discard its steps.
        for attempt in range(2):
            out = request_json(self._endpoint() + "/v1/chat/completions", payload, timeout=90)
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
                payload["messages"][0]["content"] += PLANNER_RETRY_SUFFIX

    @staticmethod
    def _parse_plan_response(response):
        """Validate the envelope and top-level fields before any action path."""
        try:
            choices = response.get("choices") if isinstance(response, dict) else None
            if not isinstance(choices, list) or not choices:
                raise QwenPlanResponseError("Missing chat completion content")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise QwenPlanResponseError("Missing chat completion content")
            message = choice.get("message")
            if not isinstance(message, dict) or "content" not in message:
                raise QwenPlanResponseError("Missing chat completion content")
            finish_reason = choice.get("finish_reason")
            content = message["content"]
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise QwenPlanResponseError("Missing chat completion content") from exc
        if finish_reason not in (None, "stop"):
            raise QwenPlanResponseError("Qwen completion did not finish successfully")
        if not isinstance(content, str) or not content.strip():
            raise QwenPlanResponseError("Empty plan content (possibly thinking-only)")
        try:
            plan = json.loads(content, object_pairs_hook=_unique_json_pairs,
                              parse_constant=_reject_nonfinite_constant,
                              parse_float=_finite_json_float)
        except (TypeError, ValueError) as exc:
            raise QwenPlanResponseError("Malformed or ambiguous Qwen JSON plan") from exc
        if (not isinstance(plan, dict)
                or set(plan) != {"message", "steps", "save_as"}
                or not isinstance(plan["message"], str)
                or len(plan["message"]) > Limits.QWEN_MESSAGE_MAX
                or not isinstance(plan["steps"], list)
                or len(plan["steps"]) > Limits.QWEN_PLAN_MAX_STEPS
                or not (plan["save_as"] is None or isinstance(plan["save_as"], str))):
            raise QwenPlanResponseError("Qwen response missing required plan fields")
        for step in plan["steps"]:
            if (not isinstance(step, dict)
                    or not {"tool", "args"} <= set(step)
                    or not set(step) <= {"tool", "args", "when"}
                    or not isinstance(step["tool"], str)
                    or not 1 <= len(step["tool"]) <= Limits.PLAN_TOOL_NAME_MAX
                    or not isinstance(step["args"], dict)):
                raise QwenPlanResponseError("Malformed Qwen plan step structure")
            if "when" in step:
                when = step["when"]
                if (not isinstance(when, dict)
                        or set(when) != {"step", "field", "op", "value"}
                        or type(when["step"]) is not int
                        or when["step"] < 0
                        or not isinstance(when["field"], str)
                        or not isinstance(when["op"], str)
                        or when["op"] not in ("gt", "gte", "lt", "lte", "eq", "neq")
                        or type(when["value"]) not in (str, int, float, bool)):
                    raise QwenPlanResponseError("Malformed Qwen step condition")
        # Tool authorization, argument semantics, references and service
        # allowlists remain exclusively in System2.parse_plan/preflight.
        # An unauthorized but structurally valid tool is NOT regenerated.
        return plan

    def explain(self, user_request, observed_results):
        """Inference-only diagnostic response, never an executable tool proposal."""
        guard=(self.admission.claim("qwen_explain")
               if self.admission is not None else nullcontext())
        with guard:
            return self._explain_admitted(user_request,observed_results)

    def _explain_admitted(self, user_request, observed_results):
        observations = json.dumps(observed_results, default=str, ensure_ascii=True)[:Truncation.EXPLAIN_OBSERVATIONS]
        payload = {
            "messages": [
                {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
                {"role": "user", "content":
                    "User question: " + user_request[:Truncation.EXPLAIN_QUESTION]
                    + "\nObserved read-only tool results:\n" + observations},
            ],
            "temperature":0,
            "max_tokens":min(512,int(self.cfg["max_tokens"])),
            "response_format":self._response_format("alfred_explanation", {
                "type":"object","properties":{"answer":{"type":"string","maxLength":Limits.QWEN_ANSWER_MAX}},
                "required":["answer"],"additionalProperties":False
            }),
            "stream":False
        }
        out=self._run_inference(payload)
        content=out["choices"][0]["message"].get("content") or ""
        try:
            answer=json.loads(content)["answer"]
        except (ValueError,TypeError,KeyError) as exc:
            raise ValueError("Qwen analysis did not return a valid answer") from exc
        if not isinstance(answer,str) or not answer.strip():
            raise ValueError("Qwen analysis answer is empty")
        return answer[:Limits.QWEN_ANSWER_MAX]

from .stt import STTEngineProtocol, WhistleSTTEngine
from .system1 import System1, System1RouterProtocol


class Cactus:
    """Composable facade unifying Needle3 router and Whistle STT engines."""

    def __init__(
        self,
        schemas: list[dict[str, Any]],
        admission: Any | None = None,
        router_engine: System1RouterProtocol | None = None,
        stt_engine: STTEngineProtocol | None = None,
    ) -> None:
        self.admission = admission
        self._router = router_engine or System1(schemas, admission=admission)
        self._stt = stt_engine or WhistleSTTEngine(admission=admission)
        self.agent = getattr(self._router, "agent", self._router)
        self.whistle = getattr(self._stt, "whistle", self._stt)

    def route(self, transcript: str) -> dict[str, Any]:
        return self._router.route(transcript)

    def route_feedback(self, observation_json: str) -> dict[str, Any]:
        return self._router.route_feedback(observation_json)

    def transcribe_stream(
        self, frames: Any, on_partial: Callable[[str], None] | None = None
    ) -> str:
        return self._stt.transcribe_stream(frames, on_partial=on_partial)
