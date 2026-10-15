"""Re-export shim — routing helpers live in alfred.router as of Sub-Task 1 refactor."""
from __future__ import annotations

from .router import adjust_status_calls, explicit_gpu_status_request

__all__ = ["explicit_gpu_status_request", "adjust_status_calls"]
