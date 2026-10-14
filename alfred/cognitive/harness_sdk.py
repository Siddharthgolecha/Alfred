"""Optional real DeepSeek Harness SDK adapter; Alfred retains all tool authority.

The SDK's own JSON-RPC subprocess is used, not a home-grown fake wire parser.
Only a verified, tool-free sdk-minimal Cordis overlay may be started.
No transcript is saved by Alfred; temporary SDK session files are destroyed on close.
"""
from __future__ import annotations

from concurrent.futures import Future
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)
SDK_VERSION = "0.1.5rc1"
# Exact shipped sdk-minimal row IDs from the audited release family. The
# overlay deliberately disables all model-facing tools and host side effects.
DENIED_ROWS = frozenset({
    "persistent-bash", "persistent-pwsh", "terminal-bash", "terminal-pwsh",
    "pty", "subprocess", "sandbox-policy", "sandbox", "filesystem",
    "tool-fs", "attachment-local", "working-directory",
    "tool-working-directory", "mcp-resources", "jobs",
    "plugin-package-inventory-deepseek", "session-log-deepseek",
    "compaction-basic", "image-offload",
})
PATCH = Path(__file__).with_name("alfred_safe.patch.yml")
MAX_PROMPT = 4000
MAX_RESPONSE = 2000


class HarnessUnavailable(RuntimeError):
    pass


def _loopback(url):
    if not isinstance(url, str):
        raise HarnessUnavailable("Harness requires local HTTP model endpoint")
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in
            ("127.0.0.1", "localhost", "::1")
            or parsed.port is None or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise HarnessUnavailable("Harness endpoint must be loopback HTTP origin")
    return url.rstrip("/")


def check_safe_patch(path=PATCH):
    """Validate the version-specific Cordis overlay before any SDK launch.

    Rejects new/unexpected rows, JS expressions, duplicates, plugins, enabling
    rows or modifying the SDK server/model provider. Uses exact text grammar
    rather than a full yaml parser (to avoid implicit custom-tag execution).
    """
    raw = Path(path).read_bytes()
    if len(raw) > 4096:
        raise HarnessUnavailable("Unsafe Cordis overlay size")
    lines = []
    for rawline in raw.decode("utf-8").splitlines():
        line = rawline.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    if len(lines) != 2 * len(DENIED_ROWS):
        raise HarnessUnavailable("Cordis disable list incomplete")
    seen = set()
    for i in range(0, len(lines), 2):
        a, b = lines[i:i+2]
        if not re.fullmatch(r"- id: [a-z][a-z0-9-]*", a) or b != "disabled: true":
            raise HarnessUnavailable("Cordis overlay contains unauthorized syntax")
        row = a[6:]
        if row not in DENIED_ROWS or row in seen:
            raise HarnessUnavailable("Unexpected or duplicated Cordis row")
        seen.add(row)
    if seen != DENIED_ROWS:
        raise HarnessUnavailable("Unsafe Cordis overlay missing rows")
    return hashlib.sha256(raw).hexdigest()


class LocalHarness:
    """Owned reusable DeepSeek SDK runtime. No shell, tools, or cloud routes."""

    def __init__(self, model_url, model, *, timeout=45, max_tokens=256,
                 factory=None, patch=PATCH):
        self.url = _loopback(model_url)
        if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,120}", model):
            raise HarnessUnavailable("Invalid local model identifier")
        if type(timeout) not in (int, float) or not 5 <= timeout <= 120:
            raise HarnessUnavailable("Invalid timeout")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 512:
            raise HarnessUnavailable("Invalid token limit")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.patch = Path(patch).resolve()
        self.patch_sha256 = check_safe_patch(self.patch)
        self.factory = factory
        self._lock = threading.RLock()
        self._temp = None
        self._sdk = None
        self._closed = False

    def _open(self):
        if self._closed:
            raise HarnessUnavailable("Harness closed")
        if self._sdk is not None:
            return self._sdk
        if check_safe_patch(self.patch) != self.patch_sha256:
            raise HarnessUnavailable("Cordis overlay changed")
        if self.factory is None:
            try:
                version = importlib.metadata.version("deepseek-harness-sdk")
                runtime_version = importlib.metadata.version("deepseek-harness-runtime-bin")
                if version != SDK_VERSION or runtime_version != SDK_VERSION:
                    raise HarnessUnavailable("Unexpected SDK/runtime version")
                from deepseek_harness import DeepSeekHarness
            except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
                raise HarnessUnavailable("Pinned DeepSeek SDK not installed") from exc
            factory = DeepSeekHarness
        else:
            factory = self.factory
        # Isolated home, not ~/.dsh. chmod avoids other local users reading
        # SDK's JSONL history while a call is in progress.
        self._temp = tempfile.TemporaryDirectory(prefix="alfred-harness-")
        root = Path(self._temp.name)
        root.chmod(0o700)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        try:
            # The installed SDK launches one owned dsh process and closes it.
            # The original Qwen supervisor already owns the local model server.
            self._sdk = factory(
                dsh_home=str(root / "home"),
                cwd=str(workspace),
                runtime_cwd=str(workspace),
                profile="sdk-minimal",
                patches=(str(self.patch),),
                provider="deepseek-official",
                model=self.model,
                base_url=self.url + "/v1",
                api_key="alfred-local-only",  # never written into repo/logs
                max_tokens=self.max_tokens,
                initialize_timeout_seconds=min(self.timeout, 30),
                request_timeout_seconds=self.timeout,
                shutdown_timeout_seconds=1.0,
                env={"DSH_CONTEXT_WINDOW": "4096",
                     "DSH_SYSTEM_PROMPT":
                       "You are Alfred. Read-only offline assistant. No tools or actions. "
                       "Answer concisely; do not invent observations."},
            )
            return self._sdk
        except BaseException:
            self.close()
            raise

    def run(self, prompt, session_id):
        """Read-only SDK model turn with a firm parent timeout and crash fallback.

        A worker thread is necessary because SDK Session.run waits for async
        notifications without a configurable timeout. Timeout closes/kills the
        SDK-owned child. Never resubmit a tool proposal or auto-approve actions.
        """
        if not isinstance(prompt, str) or not 0 < len(prompt) <= MAX_PROMPT:
            raise HarnessUnavailable("Invalid prompt size")
        if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise HarnessUnavailable("Invalid correlated session")
        with self._lock:
            sdk = self._open()
            done = Future()

            def run_once():
                try:
                    done.set_result(sdk.run(prompt, session_id=session_id))
                except BaseException as exc:
                    done.set_exception(exc)

            t = threading.Thread(target=run_once, name="alfred-harness-turn", daemon=True)
            t.start()
            try:
                result = done.result(timeout=self.timeout)
                response = getattr(result, "final_response", None)
                finish = getattr(result, "finish_reason", None)
                if (type(response) is not str or len(response) > MAX_RESPONSE
                        or finish != "completed"):
                    raise HarnessUnavailable("Invalid or incomplete SDK response")
                return {"message": response, "results": []}
            except BaseException as exc:
                self.close()
                raise HarnessUnavailable("Local harness unavailable; using legacy path") from exc

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sdk, tmp = self._sdk, self._temp
            self._sdk, self._temp = None, None
            try:
                if sdk is not None:
                    # SDK 0.1.5rc1 reaps dsh but leaves Python pipe objects
                    # open (ResourceWarning). Close them after process exit.
                    proc = (getattr(sdk.client, "_proc", None)
                            if self.factory is None else None)
                    try:
                        sdk.close()
                    finally:
                        if proc is not None and proc.poll() is not None:
                            for fd in (proc.stdin, proc.stdout, proc.stderr):
                                if fd is not None and not fd.closed:
                                    fd.close()
            finally:
                if tmp is not None:
                    tmp.cleanup()
