"""Re-export shim — routing logic inlined into alfred.app as of the Sub-Task 7 refactor.

Import from alfred.app or alfred.routing; both work.
"""
from .app import explicit_gpu_status_request, adjust_status_calls  # noqa: F401

__all__ = ["explicit_gpu_status_request", "adjust_status_calls"]
