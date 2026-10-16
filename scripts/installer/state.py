"""Crash-safe, per-checkout installer checkpoints and process locking."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile


SCHEMA = 1


def state_root() -> Path:
    """Return Alfred's private user state directory on each supported OS."""
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Alfred"
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Alfred"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "alfred"
    return base / "installer"


def cache_root() -> Path:
    """Return Alfred's persistent download cache directory."""
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Alfred" / "cache"
    elif system == "Darwin":
        base = Path.home() / "Library" / "Caches" / "Alfred"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "alfred"
    return base / "installer"


def log_path() -> Path:
    return state_root().parent / "installer.log"


def checkout_id(root: Path) -> str:
    canonical = str(root.resolve()).encode("utf-8", errors="surrogateescape")
    return hashlib.sha256(canonical).hexdigest()[:24]


def fingerprint(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class InstallerState:
    """Persist choices and successful step receipts outside the source checkout."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = state_root() / "checkouts" / checkout_id(self.root) / "state.json"
        self.data = {"schema": SCHEMA, "root": str(self.root), "choices": {}, "steps": {}}
        self.load()

    def load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (isinstance(value, dict) and value.get("schema") == SCHEMA
                    and value.get("root") == str(self.root)
                    and isinstance(value.get("choices"), dict)
                    and isinstance(value.get("steps"), dict)):
                self.data = value
        except (OSError, ValueError, TypeError):
            # A torn or old state file is recoverable: start with no receipts.
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".installer-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)

    def remember_choices(self, choices, *, base_config_hash):
        saved = self.data.get("choices", {})
        self.data["choices"] = {**saved, "values": choices,
                                 "base_config_hash": base_config_hash}
        self.save()

    def saved_choices(self):
        choices = self.data.get("choices", {})
        if isinstance(choices, dict) and isinstance(choices.get("values"), dict):
            return choices
        return None

    def complete(self, name: str, inputs, *, output=None):
        self.data["steps"][name] = {
            "inputs": fingerprint(inputs),
            "output": output,
        }
        self.save()

    def matches(self, name: str, inputs) -> bool:
        receipt = self.data["steps"].get(name)
        return isinstance(receipt, dict) and receipt.get("inputs") == fingerprint(inputs)

    def forget(self, name: str):
        if name in self.data["steps"]:
            del self.data["steps"][name]
            self.save()


class InstallerLock:
    """Hold an OS-managed exclusive lock for one checkout's entire install."""

    def __init__(self, root: Path):
        self.path = state_root() / "checkouts" / checkout_id(root) / "install.lock"
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if os.name == "nt":
            import msvcrt
            self.stream.seek(0)
            if not self.path.stat().st_size:
                self.stream.write(b"\0")
                self.stream.flush()
            self.stream.seek(0)
            try:
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                self.stream.close()
                self.stream = None
                raise RuntimeError("Another Alfred installer is using this checkout") from exc
        else:
            import fcntl
            try:
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self.stream.close()
                self.stream = None
                raise RuntimeError("Another Alfred installer is using this checkout") from exc
        self.stream.seek(0)
        self.stream.truncate()
        self.stream.write((str(os.getpid()) + "\n").encode("ascii"))
        self.stream.flush()
        return self

    def __exit__(self, *_exc):
        if self.stream is None:
            return False
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None
        return False
