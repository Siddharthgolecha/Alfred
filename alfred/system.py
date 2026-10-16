"""Shared foundation for Alfred's two cognitive systems."""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any


class System:
    """Common admission-aware base for System1 and System2.

    Both systems can share Alfred's optional inference gate while retaining
    their separate routing and planning responsibilities.
    """

    def __init__(self, admission: Any | None = None) -> None:
        self.admission = admission

    def inference_claim(self, kind: str):
        """Return the shared inference claim, or a no-op when unconfigured."""
        return self.admission.claim(kind) if self.admission is not None else nullcontext()
