"""Cross-platform Alfred setup. Imports only Python's standard library."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import copy
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from .ui import Terminal
from .state import InstallerLock, InstallerState, cache_root, fingerprint, log_path, state_root

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = "0.1.5rc1"
DEFAULT_MODEL = "qwen3.5:4b"


def parser():
    p = argparse.ArgumentParser(description="Guided Alfred setup")
    p.add_argument("--auto", action="store_true", help="Non-interactive setup")
    p.add_argument("--plain", action="store_true", help="Use numbered line prompts")
    p.add_argument("--customize", action="store_true", help="Reopen setup choices")
    p.add_argument("--service", action="store_true", help="Install a Linux systemd user service")
    p.add_argument("--backend", choices=("ollama", "lmstudio", "llamacpp", "skip"))
    p.add_argument("--wheelhouse", help="Use verified local Harness wheels; never fetch replacements")
    p.add_argument("--skip-models", action="store_true", help="Skip every model download")
    p.add_argument("--repair", action="store_true", help="Preserve and rebuild a damaged Alfred environment")
    p.add_argument("--no-update-check", action="store_true", help=argparse.SUPPRESS)
    return p


def run(argv, *, cwd=None, env=None, input=None, output=None, check=True):
    """Subprocess helper: argv only, no shell interpolation."""
    return subprocess.run(argv, cwd=cwd, env=env, stdin=input, stdout=output,
                          stderr=subprocess.STDOUT if output else None,
                          check=check, text=output is not None)


def platform_key(system=None, machine=None):
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64",
            "aarch64": "aarch64"}.get(machine)
    os_key = {"linux": "linux", "darwin": "macos", "windows": "windows"}.get(system)
    if not arch or not os_key:
        raise RuntimeError(f"Unsupported platform: {system} {machine}; see installation requirements")
    key = f"{os_key}_{arch}"
    if key not in {"linux_x86_64", "linux_aarch64", "macos_x86_64",
                   "macos_aarch64", "windows_x86_64"}:
        raise RuntimeError(f"No pinned Harness runtime is published for {system} {machine}")
    return key


def load_config(path):
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("config.json must contain a JSON object")
        return value
    with (ROOT / "config.example.json").open(encoding="utf-8") as stream:
        return json.load(stream)


def save_config(path, value):
    """Atomic config update with one recoverable, private backup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    fd, name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def _has(name):
    return shutil.which(name) is not None


def _refresh_windows_path():
    if platform.system() != "Windows":
        return
    import winreg
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        parts.extend(os.path.expandvars(item) for item in value.split(os.pathsep))
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(item for item in parts if item))


def _call(argv, ui, label, *, cwd=None, env=None, log=None, check=True):
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write("$ " + " ".join(map(str, argv)) + "\n")
            stream.flush()
            with ui.progress(label):
                child = subprocess.Popen([str(x) for x in argv], cwd=cwd, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1)
                try:
                    for line in child.stdout:
                        stream.write(line)
                        stream.flush()
                        ui.out.write(line)
                        ui.out.flush()
                    status = child.wait()
                except KeyboardInterrupt:
                    try:
                        child.send_signal(signal.SIGINT)
                    except OSError:
                        child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    raise
                if check and status:
                    raise subprocess.CalledProcessError(status, argv)
                return subprocess.CompletedProcess(argv, status)
    with ui.progress(label):
        return run([str(x) for x in argv], cwd=cwd, env=env, check=check)


def _detect_audio(system):
    if system == "Windows":
        if not _has("ffmpeg"):
            return []
        result = subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true",
                                 "-f", "dshow", "-i", "dummy"],
                                capture_output=True, text=True, timeout=10, check=False)
        import re
        return list(dict.fromkeys(re.findall(r'"([^"\r\n]+)"\s+\(audio\)', result.stderr)))
    if system == "Darwin":
        if not _has("system_profiler"):
            return []
        result = subprocess.run(["system_profiler", "SPAudioDataType"],
                                capture_output=True, text=True, timeout=15, check=False)
        return ["default"] if "Input" in result.stdout else []
    if _has("pactl"):
        result = subprocess.run(["pactl", "-f", "json", "list", "sources"],
                                capture_output=True, text=True, timeout=8, check=False)
        if result.returncode == 0:
            try:
                sources = json.loads(result.stdout)
                return [row["name"] for row in sources
                        if row.get("name") and not row["name"].endswith(".monitor")]
            except (ValueError, TypeError, KeyError):
                pass
    return []


def _detect_backend(ui):
    found = []
    if _has("ollama"):
        found.append("Ollama")
    for url, label in (("http://127.0.0.1:1234/v1/models", "LM Studio"),
                       ("http://127.0.0.1:11434/v1/models", "Ollama server")):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    found.append(label)
        except (OSError, urllib.error.URLError, TimeoutError):
            pass
    if _query_ollama_models():
        found.append("Ollama server")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=1) as response:
            if response.status == 200:
                found.append("llama.cpp server")
    except (OSError, urllib.error.URLError, TimeoutError):
        pass
    return list(dict.fromkeys(found))


def _query_model_ids(base_url):
    try:
        request = urllib.request.Request(base_url.rstrip("/") + "/v1/models",
                                         headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            body = json.load(response)
        rows = body.get("data", []) if isinstance(body, dict) else []
        return [str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")]
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return []


def _query_ollama_models():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
            body = json.load(response)
        rows = body.get("models", []) if isinstance(body, dict) else []
        return [str(row["name"]) for row in rows if isinstance(row, dict) and row.get("name")]
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return []


def _options(rows):
    return [(value, label, hint) for value, label, hint in rows]


def _read_env_bool(name, default=False):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


def _install_uv(ui, system, auto=False):
    if _has("uv"):
        return shutil.which("uv")
    ui.warn("uv is needed to create Alfred's isolated Python environment.")
    if not auto and not ui.confirm("Download and install uv for your user?", default=True):
        raise RuntimeError("uv is required. Install it from https://docs.astral.sh/uv/ and retry")
    url = ("https://astral.sh/uv/install.ps1" if system == "Windows"
           else "https://astral.sh/uv/install.sh")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            installer = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError(f"Could not download the uv installer: {exc}") from exc
    if system == "Windows":
        with tempfile.NamedTemporaryFile(prefix="alfred-uv-", suffix=".ps1", delete=False) as stream:
            path = Path(stream.name)
            stream.write(installer)
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(path)], check=True)
        finally:
            path.unlink(missing_ok=True)
    else:
        subprocess.run(["sh"], input=installer, check=True)
    candidates = ([Path.home() / ".local" / "bin" / "uv.exe",
                   Path.home() / ".cargo" / "bin" / "uv.exe"] if system == "Windows" else
                  [Path.home() / ".local" / "bin" / "uv",
                   Path.home() / ".cargo" / "bin" / "uv"])
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("uv")
    if not found:
        raise RuntimeError("uv installed, but is not on PATH. Open a new terminal and retry")
    return found


def _ensure_system_tools(ui, system, settings):
    missing = []
    if system == "Linux":
        if not _has("pactl") and settings.get("audio") != "later":
            missing.append("pulseaudio-utils")
        if settings.get("bluetooth") and not _has("bluetoothctl"):
            missing.append("bluez")
        if missing:
            if not _has("apt-get"):
                raise RuntimeError("Missing " + ", ".join(missing) + ". Install them with your Linux package manager")
            _call(["sudo", "apt-get", "update", "-qq"], ui, "Refreshing package list")
            _call(["sudo", "apt-get", "install", "-y", *missing], ui, "Installing " + ", ".join(missing))
    elif system == "Darwin":
        if not _has("sox") and not _has("ffmpeg") and settings.get("audio") != "later":
            if not _has("brew"):
                raise RuntimeError("Microphone capture needs sox or FFmpeg. Install Homebrew from https://brew.sh, then rerun setup")
            _call(["brew", "install", "sox"], ui, "Installing audio capture")
    elif system == "Windows" and settings.get("audio") != "later" and not _has("ffmpeg"):
        should_install = settings.get("auto") or ui.confirm(
            "Install FFmpeg for microphone capture with winget?", default=True)
        if _has("winget") and should_install:
            _call(["winget", "install", "--id", "Gyan.FFmpeg.Shared", "--exact",
                   "--accept-package-agreements", "--accept-source-agreements"], ui,
                  "Installing FFmpeg")
            _refresh_windows_path()
            if not _has("ffmpeg"):
                raise RuntimeError("FFmpeg was installed. Open a new PowerShell window and rerun setup so Windows can refresh PATH")
        else:
            raise RuntimeError("Voice capture needs FFmpeg with DirectShow. Install it, reopen the terminal, and retry")


def _wheel_records(wheelhouse, target):
    lock_path = ROOT / "coordination" / "deepseek-harness-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    records = [lock["sdk"], lock["runtime"][target]]
    root = Path(wheelhouse).expanduser().resolve()
    verified = []
    for record in records:
        name = record["filename"]
        wheel = root / name
        if not wheel.is_file() or wheel.is_symlink():
            raise RuntimeError(f"Verified wheelhouse is missing {name}; no network fallback was attempted")
        digest = hashlib.sha256()
        with wheel.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != record["sha256"]:
            raise RuntimeError(f"SHA-256 mismatch for {name}; wheel rejected")
        verified.append(wheel)
    return verified


def _download_verified_wheels(directory, target, ui):
    """Reuse or download only exact pinned wheel bytes, retaining safe partials."""
    lock = json.loads((ROOT / "coordination" / "deepseek-harness-lock.json").read_text())
    verified = []
    for row in (lock["sdk"], lock["runtime"][target]):
        path = directory / row["filename"]
        partial = path.with_suffix(path.suffix + ".part")
        url = row.get("url")
        if not url:
            raise RuntimeError(f"The lock has no pinned download URL for {row['filename']}")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            path.parent.chmod(0o700)
        if partial.is_symlink():
            partial.unlink()

        def digest_file(candidate):
            digest = hashlib.sha256()
            with candidate.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            return digest.hexdigest()

        if path.is_file() and not path.is_symlink():
            if digest_file(path) == row["sha256"]:
                if os.name != "nt":
                    path.chmod(0o600)
                ui.ok("Using cached, verified " + path.name)
                verified.append(path)
                continue
            path.unlink()
        for attempt in range(3):
            offset = partial.stat().st_size if partial.is_file() and not partial.is_symlink() else 0
            headers = {"User-Agent": "Alfred-installer/1"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    status = getattr(response, "status", response.getcode())
                    append = False
                    if offset and status == 206:
                        content_range = response.headers.get("Content-Range", "")
                        if not content_range.startswith(f"bytes {offset}-"):
                            partial.unlink(missing_ok=True)
                            raise RuntimeError("Server returned an invalid Harness download range")
                        append = True
                    elif offset and status == 200:
                        # Range requests are optional; a full response replaces stale partial data.
                        offset = 0
                    elif status != 200:
                        raise RuntimeError(f"Unexpected HTTP status {status} for {path.name}")
                    total_header = response.headers.get("Content-Length", "")
                    total = (int(total_header) + offset) if total_header.isdigit() else None
                    ui.info("Downloading pinned " + path.name +
                            (f" (resuming at {offset // (1024 * 1024)} MiB)" if offset else ""))
                    received = offset
                    mode = "ab" if append else "wb"
                    with partial.open(mode) as out:
                        last_update = 0.0
                        while True:
                            block = response.read(1024 * 1024)
                            if not block:
                                break
                            out.write(block)
                            received += len(block)
                            now = time.monotonic()
                            if now - last_update >= 0.25:
                                ui.download_progress(path.name, received, total)
                                last_update = now
                        out.flush()
                        os.fsync(out.fileno())
                    ui.finish_download()
                if digest_file(partial) != row["sha256"]:
                    partial.unlink(missing_ok=True)
                    raise RuntimeError(f"SHA-256 mismatch for downloaded {path.name}; wheel rejected")
                os.replace(partial, path)
                if os.name != "nt":
                    path.chmod(0o600)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and attempt < 2:
                    partial.unlink(missing_ok=True)
                    continue
                if attempt == 2:
                    raise RuntimeError(f"Could not download pinned Harness wheel {path.name}: {exc}") from exc
                time.sleep(2 ** attempt)
            except (OSError, urllib.error.URLError, http.client.HTTPException, TimeoutError) as exc:
                if attempt == 2:
                    raise RuntimeError(f"Could not download pinned Harness wheel {path.name}: {exc}") from exc
                time.sleep(2 ** attempt)
            except RuntimeError:
                raise
        ui.ok("Verified " + path.name)
        verified.append(path)
    return verified


def _package_snapshot(uv, python, env):
    result = subprocess.run([str(uv), "pip", "list", "--format", "json", "--python", str(python)],
                            capture_output=True, text=True, env=env, check=False, timeout=30)
    if result.returncode:
        return None
    try:
        rows = json.loads(result.stdout)
        return {str(row["name"]).lower().replace("_", "-"): str(row["version"])
                for row in rows if isinstance(row, dict) and "name" in row and "version" in row}
    except (ValueError, TypeError, KeyError):
        return None


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _service(ui, python, root, config=None):
    if platform.system() != "Linux" or not _has("systemctl"):
        ui.warn("Automatic startup is available on Linux with systemd only")
        return False
    if subprocess.run(["systemctl", "--user", "show-environment"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      check=False).returncode != 0:
        ui.warn("No systemd user session is available; Alfred remains installed for manual startup")
        return False
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "alfred.service"
    def quote(value):
        value = str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
        return '"' + value + '"'

    argv = [python, "-m", "alfred"]
    if config is not None:
        argv.extend(("--config", config))
    command = " ".join(quote(item) for item in argv)
    body = ("[Unit]\nDescription=Alfred voice assistant\nAfter=default.target\n\n"
            "[Service]\nType=simple\nWorkingDirectory=" + quote(root) + "\nExecStart=" + command +
            "\nRestart=on-failure\nRestartPreventExitStatus=2\n\n"
            "[Install]\nWantedBy=default.target\n")
    fd, name = tempfile.mkstemp(prefix=".alfred-service-", suffix=".tmp", dir=unit_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, unit)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "alfred.service"], check=True)
    ui.ok("Autostart enabled. Check with: systemctl --user status alfred")
    return True


class Wizard:
    def __init__(self, args, ui):
        self.args, self.ui = args, ui
        self.system = platform.system()
        self.config_path = ROOT / "config.json"
        self.config = load_config(self.config_path)
        self.original_config = copy.deepcopy(self.config)
        self.installed = self.config_path.exists() or (ROOT / ".venv").exists()
        self.settings = {}
        self.state = InstallerState(ROOT)
        self._restore_choices = False
        saved = self.state.saved_choices()
        if saved:
            config_hash = fingerprint(self.config)
            self._restore_choices = config_hash in (
                saved.get("base_config_hash"), saved.get("configured_config_hash"))
            if self._restore_choices:
                self.settings.update(saved["values"])
        self.existing_backends = _detect_backend(ui)
        self.existing_audio = _detect_audio(self.system)

    def _select(self, label, rows, default=0):
        if self.args.auto:
            if default < 0 or default >= len(rows):
                raise RuntimeError(f"--auto needs a value for {label}; use --help or set ALFRED_BACKEND")
            return rows[default][0]
        return self.ui.choose(label, _options(rows), default=default)

    def preflight(self):
        self.ui.section("System check", f"{self.system} {platform.machine()} · Python {platform.python_version()}")
        key = platform_key()
        if sys.version_info < (3, 10):
            raise RuntimeError("Alfred requires Python 3.10 or newer. Install Python 3.12 and rerun setup")
        if self.system == "Linux" and platform.libc_ver()[0] == "glibc":
            try:
                major, minor = map(int, platform.libc_ver()[1].split(".")[:2])
                if (major, minor) < (2, 28):
                    raise RuntimeError("Pinned Harness needs glibc 2.28 or newer. This system may need a newer OS")
            except ValueError:
                pass
        if self.system == "Darwin":
            try:
                version = tuple(int(part) for part in platform.mac_ver()[0].split(".")[:2])
            except ValueError:
                version = ()
            if version and version < (14, 0):
                raise RuntimeError("The pinned DeepSeek Harness runtime requires macOS 14 or newer")
        free = shutil.disk_usage(ROOT).free
        self.ui.info(f"Harness runtime: {key}; free storage: {free // (1024 ** 3)} GiB")
        if free < 1024 ** 3:
            raise RuntimeError("Alfred setup needs at least 1 GiB of free storage; clear space and retry")
        self.settings["runtime_target"] = key

    def choose_audio(self):
        if getattr(self, "quick", False):
            audio = self.config.get("audio", {})
            source = audio.get("direct_source", "")
            mode = audio.get("mode", "auto")
            if source in self.existing_audio:
                self.settings["audio"] = f"device:{self.existing_audio.index(source)}"
            elif mode == "auto":
                self.settings["audio"] = "auto"
            else:
                # Keep a configured but temporarily unavailable device intact.
                self.settings["audio"] = "saved"
            return
        if self.existing_audio:
            self.ui.info("Detected microphones: " + ", ".join(self.existing_audio[:5]))
        if not self.existing_audio and self.system == "Windows" and not _has("ffmpeg"):
            choices = [("install", "Set up voice capture", "Install FFmpeg through winget, if available."),
                       ("later", "Configure later", "Use text mode until a microphone is configured.")]
        else:
            default_label = ("Use the detected default" if self.existing_audio
                             else "Use the system default")
            choices = [("auto", default_label, "Alfred will verify live microphone audio at startup."),
                       ("later", "Configure later", "Continue installation; voice mode may need setup.")]
            choices.extend((f"device:{index}", name, "Use this input for voice capture.")
                           for index, name in enumerate(self.existing_audio))
        old = self.config.get("audio", {}).get("direct_source", "")
        for idx, row in enumerate(choices):
            if old in self.existing_audio and row[0] == f"device:{self.existing_audio.index(old)}":
                default = idx
                break
        else:
            default = 0
        if self.args.auto:
            value = os.environ.get("ALFRED_AUDIO", "auto")
            self.settings["audio"] = "later" if value == "later" else "auto"
            return
        value = self._select("Microphone", choices, default)
        self.settings["audio"] = value
        if value.startswith("device:"):
            source = self.existing_audio[int(value.split(":", 1)[1])]
            self.settings["bluetooth"] = source.startswith("bluez_input.")

    def choose_backend(self):
        backend = self.args.backend or os.environ.get("ALFRED_BACKEND") or self.config.get("qwen", {}).get("backend")
        backend = {"auto": "", "lm_studio": "lmstudio", "llama_cpp": "llamacpp"}.get(backend, backend)
        if self.args.backend:
            self.settings["backend"] = self.args.backend
            return self.settings["backend"]
        if os.environ.get("ALFRED_BACKEND"):
            self.settings["backend"] = backend or "ollama"
            return self.settings["backend"]
        if backend in ("ollama", "lmstudio", "llamacpp", "skip") and self.installed and not self.args.customize:
            self.settings["backend"] = backend
            self.ui.info(f"Keeping saved backend: {backend}")
            return self.settings["backend"]
        rows = [
            ("ollama", "Ollama" + (" · detected" if "Ollama" in self.existing_backends else ""),
             "An easy local server with CPU/GPU acceleration. Alfred will use its loopback API."),
            ("lmstudio", "LM Studio" + (" · detected" if "LM Studio" in self.existing_backends else ""),
             "A desktop app with a visual model picker and a local server."),
            ("llamacpp", "llama.cpp" + (" · detected" if "llama.cpp server" in self.existing_backends else ""),
             "Advanced local GGUF server. Alfred can reuse a running server; a local build needs platform tools."),
            ("skip", "Configure later", "Keep the current model configuration and set up a server later."),
            ("__back__", "Back", "Return to microphone setup."),
        ]
        if self.args.auto:
            if not backend:
                raise RuntimeError("--auto needs a configured backend; pass --backend or set ALFRED_BACKEND")
            if backend not in ("ollama", "lmstudio", "llamacpp", "skip"):
                raise RuntimeError(f"Unknown backend {backend!r}; choose ollama, lmstudio, llamacpp or skip")
            self.settings["backend"] = backend
            return backend
        if not backend:
            if "LM Studio" in self.existing_backends and "Ollama" not in self.existing_backends and "Ollama server" not in self.existing_backends:
                backend = "lmstudio"
            elif "llama.cpp server" in self.existing_backends and "Ollama" not in self.existing_backends and "Ollama server" not in self.existing_backends:
                backend = "llamacpp"
            else:
                backend = "ollama"
        default = next((i for i, row in enumerate(rows) if row[0] == backend), 0)
        self.settings["backend"] = self.ui.choose("Choose your local model server", _options(rows),
                                                   default=default, back=True)
        return self.settings["backend"]

    def choose_model(self):
        backend = self.settings["backend"]
        saved = self.config.get("qwen", {}).get("model", "")
        if backend in ("skip", "llamacpp") or self.settings.get("skip_models"):
            self.settings["model"] = saved
        elif backend == "ollama":
            installed = _query_ollama_models()
            if _has("ollama"):
                result = subprocess.run(["ollama", "list"], capture_output=True, text=True,
                                        timeout=8, check=False)
                if result.returncode == 0:
                    installed = [line.split()[0] for line in result.stdout.splitlines()[1:]
                                 if line.split()]
            installed = list(dict.fromkeys(installed))
            rows = [(f"model:{tag}", tag, "Already available in Ollama.") for tag in installed]
            rows.append(("pull", "Download a model tag", "Choose the model now; the review shows it before download."))
            if self.args.auto:
                self.settings["model"] = saved if saved and "/" not in saved else DEFAULT_MODEL
            elif getattr(self, "quick", False):
                self.settings["model"] = saved if saved and "/" not in saved else DEFAULT_MODEL
            else:
                default = next((i for i, row in enumerate(rows) if row[0] == f"model:{saved}"), len(rows) - 1)
                selected = self.ui.choose("Choose an Ollama model", _options(rows),
                                          default=default, back=True)
                if selected == "__back__":
                    self.settings["model"] = "__back__"
                elif selected == "pull":
                    tag = self.ui.text("Ollama model tag", DEFAULT_MODEL)
                    self.settings["model"] = "__back__" if tag.lower() in ("b", "back") else tag
                else:
                    self.settings["model"] = selected.split(":", 1)[1]
        elif backend == "lmstudio":
            models = _query_model_ids("http://127.0.0.1:1234")
            rows = [(f"model:{model}", model, "Currently served by LM Studio.") for model in models]
            rows.append(("later", "Configure the model later", "Load a model in LM Studio before starting Alfred."))
            if self.args.auto or getattr(self, "quick", False):
                self.settings["model"] = saved
            else:
                selected = self.ui.choose("Choose a model loaded in LM Studio", _options(rows), back=True)
                if selected == "__back__":
                    self.settings["model"] = "__back__"
                elif selected == "later":
                    self.settings["model"] = ""
                else:
                    self.settings["model"] = selected.split(":", 1)[1]
        else:
            self.settings["model"] = saved

    def review(self):
        self.settings["service"] = self.args.service or _read_env_bool("ALFRED_INSTALL_SERVICE")
        self.settings["skip_models"] = self.args.skip_models or _read_env_bool("ALFRED_SKIP_MODELS")
        print("\n  Setup review", file=self.ui.out)
        print(f"    System       {self.system} {platform.machine()}", file=self.ui.out)
        print("    Packages     Alfred runtime + hash-verified Harness; audio tools if needed", file=self.ui.out)
        print(f"    Microphone   {self.settings['audio']}", file=self.ui.out)
        if self.settings.get("bluetooth"):
            print("    Bluetooth    headset microphone", file=self.ui.out)
        print(f"    Model server {self.settings['backend']}", file=self.ui.out)
        if self.settings.get("model"):
            print(f"    Model        {self.settings['model']}", file=self.ui.out)
        backend_model_pull = ("; Ollama model if missing and accepted"
                              if self.settings["backend"] == "ollama" else "")
        pulls = ("none (--skip-models)" if self.settings["skip_models"] else
                 "Needle 3 + Whistle if missing" + backend_model_pull)
        print(f"    Downloads    {pulls}", file=self.ui.out)
        print(f"    Config       {self.config_path} (backup before changes)", file=self.ui.out)
        print(f"    Startup      {'systemd user service' if self.settings['service'] else 'manual'}", file=self.ui.out)
        if self.args.auto:
            return "continue"
        return self.ui.choose("Continue with these settings?", _options([
            ("continue", "Continue", "Install Alfred with the choices shown."),
            ("edit", "Edit choices", "Return to microphone and model server setup."),
            ("cancel", "Cancel", "Leave the installation unchanged."),
        ]), default=0)

    def collect(self):
        self.ui.header("guided setup")
        self.preflight()
        if self._restore_choices and not self.args.customize:
            if self.args.backend:
                self.settings["backend"] = self.args.backend
            self.settings["auto"] = self.args.auto
            self.settings["skip_models"] = (self.settings.get("skip_models", False)
                                             or self.args.skip_models
                                             or _read_env_bool("ALFRED_SKIP_MODELS"))
            self.ui.info("Resuming with the saved setup choices; completed steps will be verified and reused.")
            return True
        if self.installed and not self.args.customize and not self.args.auto:
            flow = self.ui.choose("Welcome back. What would you like to do?", _options([
                ("quick", "Quick setup", "Keep saved choices and repair missing install steps."),
                ("customize", "Customize", "Review microphone and model server choices again."),
            ]), default=0)
            if flow == "customize":
                self.args.customize = True
            else:
                self.quick = True
        self.choose_audio()
        while True:
            backend = self.choose_backend()
            if backend == "__back__":
                self.choose_audio()
                continue
            self.settings["skip_models"] = self.args.skip_models or _read_env_bool("ALFRED_SKIP_MODELS")
            self.choose_model()
            if self.settings.get("model") == "__back__":
                continue
            result = self.review()
            if result == "edit":
                self.args.customize = True
                self.quick = False
                self.choose_audio()
                continue
            if result == "continue":
                self.settings["auto"] = self.args.auto
                self.state.remember_choices(self.settings,
                                            base_config_hash=fingerprint(self.original_config))
            return result == "continue"

    def _configure(self):
        audio = self.config.setdefault("audio", {})
        picked = self.settings["audio"]
        if picked.startswith("device:"):
            index = int(picked.split(":", 1)[1])
            source = self.existing_audio[index]
            if source.startswith("bluez_input."):
                audio["mode"] = "bluetooth"
                address = source.split("bluez_input.", 1)[1].split(".", 1)[0]
                if len(address.split("_")) == 6:
                    audio["bluetooth_address"] = address.replace("_", ":")
            elif self.system == "Windows":
                audio["mode"], audio["direct_source"] = "direct", source
            elif self.system == "Darwin":
                audio["mode"], audio["direct_source"] = "direct", source
            else:
                audio["mode"], audio["direct_source"] = "direct", source
        elif picked == "auto":
            audio["mode"] = "auto"
        qwen = self.config.setdefault("qwen", {})
        backend = self.settings["backend"]
        if backend != "skip":
            previous_backend = qwen.get("backend", "auto")
            defaults = {"ollama": "http://127.0.0.1:11434",
                        "lm_studio": "http://127.0.0.1:1234",
                        "llama_cpp": "http://127.0.0.1:8080"}
            configured_backend = {"lmstudio": "lm_studio", "llamacpp": "llama_cpp"}.get(backend, backend)
            qwen["backend"] = configured_backend
            # A user-selected loopback port is configuration, not a backend default.
            old_url = qwen.get("url", "")
            if not old_url or old_url == defaults.get(previous_backend):
                qwen["url"] = defaults.get(configured_backend, "")
            if self.settings.get("model"):
                qwen["model"] = self.settings["model"]

    def _step(self, name, inputs, check, action):
        receipt = self.state.data["steps"].get(name, {})
        if self.state.matches(name, inputs) and check(receipt.get("output")):
            self.ui.ok(name.replace("_", " ").capitalize() + " already complete; reusing it")
            return receipt.get("output")
        self.state.forget(name)
        output = action()
        if not check(output):
            raise RuntimeError(f"Installation step {name.replace('_', ' ')} did not pass its verification")
        self.state.complete(name, inputs, output=output)
        return output

    def install_service(self, python):
        if not self.settings.get("service"):
            return True
        inputs = {"python": str(python), "root": str(ROOT.resolve()),
                  "config": str(self.config_path.resolve())}

        def ready(output):
            if isinstance(output, dict) and output.get("enabled") is False:
                return True
            return subprocess.run(["systemctl", "--user", "is-enabled", "alfred.service"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  check=False).returncode == 0

        def enable():
            enabled = _service(self.ui, python, ROOT, self.config_path)
            return {"enabled": enabled}

        output = self._step("service", inputs, ready, enable)
        return bool(output and output.get("enabled"))

    def _environment_ready(self, python):
        try:
            result = subprocess.run([str(python), "-c",
                                     "import json,sys; print(json.dumps({'prefix':sys.prefix,'version':list(sys.version_info[:3])}))"],
                                    capture_output=True, text=True, timeout=8, check=False)
            value = json.loads(result.stdout) if result.returncode == 0 else {}
            prefix = Path(value.get("prefix", "")).resolve()
            version = value.get("version", [])
            return (prefix == (ROOT / ".venv").resolve() and len(version) == 3
                    and tuple(version[:2]) == tuple(sys.version_info[:2])
                    and tuple(version[:2]) >= (3, 10))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False

    def _project_ready(self, python):
        probe = ("import importlib.metadata as m,json; "
                 "d=m.distribution('alfred-jetson'); "
                 "print(json.dumps({'version':d.version,'direct':d.read_text('direct_url.json')}))")
        try:
            result = subprocess.run([str(python), "-c", probe], capture_output=True,
                                    text=True, timeout=8, check=False)
            value = json.loads(result.stdout) if result.returncode == 0 else {}
            direct = json.loads(value["direct"]) if value.get("direct") else {}
            return (value.get("version") == "0.1.0"
                    and direct.get("url") == ROOT.resolve().as_uri()
                    and direct.get("dir_info", {}).get("editable") is True)
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            return False

    def _runtime_ready(self, output, current):
        if not isinstance(output, dict) or not isinstance(current, dict):
            return False
        harness_packages = {"deepseek-harness-sdk", "deepseek-harness-runtime-bin"}
        return all(name in harness_packages or current.get(name) == version
                   for name, version in output.items())

    def install(self):
        env_root = ROOT / ".venv"
        python = env_root / ("Scripts/python.exe" if self.system == "Windows" else "bin/python")
        uv = _install_uv(self.ui, self.system, auto=self.args.auto)
        env = os.environ.copy()
        env["PATH"] = str(Path(uv).parent) + os.pathsep + env.get("PATH", "")
        log = log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        _ensure_system_tools(self.ui, self.system, self.settings)
        self.existing_backends = _detect_backend(self.ui)
        self.existing_audio = _detect_audio(self.system)

        if self.args.repair and env_root.exists():
            backup_root = ROOT / ".venv.recovery"
            backup_root.mkdir(exist_ok=True)
            backup = backup_root / ("venv-" + str(time.time_ns()))
            os.replace(env_root, backup)
            self.ui.warn("Preserved the previous Alfred environment at " + str(backup))
            self.state.forget("environment")
            self.state.forget("project")
            self.state.forget("dependencies")
            self.state.forget("harness")

        if env_root.exists() and not self._environment_ready(python):
            raise RuntimeError("The existing .venv is damaged or uses another Python version. "
                               "Rerun with --repair to preserve it and build a matching environment")

        env_inputs = {"python": str(Path(sys.executable).resolve()),
                      "version": list(sys.version_info[:3]), "platform": self.settings["runtime_target"]}
        def create_environment():
            if not self._environment_ready(python):
                _call([uv, "venv", str(env_root), "--python", sys.executable], self.ui,
                      "Creating Alfred's isolated environment", cwd=ROOT, env=env, log=log)
            return {"python": str(python)}

        self._step("environment", env_inputs,
                   lambda _output: self._environment_ready(python), create_environment)

        project_inputs = {"pyproject": _file_hash(ROOT / "pyproject.toml"),
                          "python": str(python), "root": str(ROOT.resolve())}
        self._step("project", project_inputs, lambda _output: self._project_ready(python),
                   lambda: (_call([uv, "pip", "install", "--python", str(python), "--no-deps",
                                   "-e", str(ROOT)], self.ui, "Installing Alfred", cwd=ROOT,
                                  env=env, log=log), {"editable": True})[1])

        dependency_inputs = {"lock": _file_hash(ROOT / "uv.lock"),
                            "project": _file_hash(ROOT / "pyproject.toml"),
                            "python": list(sys.version_info[:3]),
                            "target": self.settings["runtime_target"]}

        def install_dependencies():
            temp_dir = state_root() / "requirements"
            temp_dir.mkdir(parents=True, exist_ok=True)
            requirements = temp_dir / (fingerprint(dependency_inputs) + ".txt")
            try:
                _call([uv, "export", "--frozen", "--no-dev", "--no-emit-project",
                       "--no-emit-package", "deepseek-harness-sdk",
                       "--no-emit-package", "deepseek-harness-runtime-bin",
                       "--format", "requirements.txt", "--output-file", str(requirements)],
                      self.ui, "Preparing locked runtime dependencies", cwd=ROOT, env=env, log=log)
                _call([uv, "pip", "install", "--python", str(python), "--no-deps",
                       "--requirement", str(requirements)], self.ui,
                      "Installing Alfred's locked Python runtime", cwd=ROOT, env=env, log=log)
            finally:
                requirements.unlink(missing_ok=True)
            packages = _package_snapshot(uv, python, env)
            if packages is None:
                raise RuntimeError("Could not verify the installed Python runtime packages")
            return packages

        current_packages = _package_snapshot(uv, python, env)
        dependency_receipt = self.state.data["steps"].get("dependencies", {})
        if not (self.state.matches("dependencies", dependency_inputs)
                and self._runtime_ready(dependency_receipt.get("output"), current_packages)):
            self._step("dependencies", dependency_inputs,
                       lambda output: self._runtime_ready(output, _package_snapshot(uv, python, env)),
                       install_dependencies)

        target = self.settings["runtime_target"]
        wheel_lock = json.loads((ROOT / "coordination" / "deepseek-harness-lock.json").read_text(encoding="utf-8"))
        harness_inputs = {"lock": _file_hash(ROOT / "coordination" / "deepseek-harness-lock.json"),
                          "target": target, "sdk": wheel_lock["sdk"]["sha256"],
                          "runtime": wheel_lock["runtime"][target]["sha256"], "python": str(python)}

        def harness_ready(output):
            packages = _package_snapshot(uv, python, env)
            return (isinstance(output, dict) and packages is not None
                    and packages.get("deepseek-harness-sdk") == RUNTIME
                    and packages.get("deepseek-harness-runtime-bin") == RUNTIME
                    and output.get("verified_wheels") ==
                    [wheel_lock["sdk"]["sha256"], wheel_lock["runtime"][target]["sha256"]])

        def install_harness():
            if self.args.wheelhouse:
                wheels = _wheel_records(self.args.wheelhouse, target)
            else:
                wheel_dir = (cache_root() / "checkouts" / self.state.path.parent.name /
                             "harness" / target / RUNTIME /
                             fingerprint([wheel_lock["sdk"]["sha256"],
                                          wheel_lock["runtime"][target]["sha256"]]))
                wheels = _download_verified_wheels(wheel_dir, target, self.ui)
            _call([uv, "pip", "install", "--python", str(python), "--no-deps",
                   *map(str, wheels)], self.ui, "Installing verified DeepSeek Harness",
                  cwd=ROOT, env=env, log=log)
            return {"verified_wheels": [_file_hash(wheel) for wheel in wheels]}

        self._step("harness", harness_inputs, harness_ready, install_harness)

        self._configure()
        configured_hash = fingerprint(self.config)
        choice_row = self.state.saved_choices() or {}
        self.state.remember_choices(self.settings, base_config_hash=choice_row.get(
            "base_config_hash", fingerprint(self.original_config)))
        self.state.data["choices"]["configured_config_hash"] = configured_hash
        self.state.save()
        if self.config != self.original_config:
            save_config(self.config_path, self.config)

        launcher_inputs = {"python": str(python), "config": str(self.config_path.resolve()),
                           "system": self.system}
        launcher_path = (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" /
                         "Alfred" / "alfred.cmd" if self.system == "Windows" else
                         Path.home() / ".local" / "bin" / "alfred")
        expected_launcher = (f'@echo off\n"{python}" -m alfred --config "{self.config_path}" %*\n'
                             if self.system == "Windows" else None)

        def launcher_ready(_output):
            try:
                content = launcher_path.read_text(encoding="utf-8")
                if self.system == "Windows":
                    return content == expected_launcher
                return (str(python) in content and str(self.config_path.resolve()) in content
                        and os.access(launcher_path, os.X_OK))
            except OSError:
                return False

        self._step("launcher", launcher_inputs, launcher_ready,
                   lambda: (self._install_launcher(python), {"path": str(launcher_path)})[1])

        backend_inputs = {"backend": self.settings["backend"],
                          "url": self.config.get("qwen", {}).get("url", ""),
                          "system": self.system}
        def backend_ready(_output):
            if self.settings["backend"] in ("skip", "lmstudio", "llamacpp"):
                return True
            return _has("ollama") or "Ollama server" in _detect_backend(self.ui)

        self._step("backend", backend_inputs, backend_ready,
                   lambda: (self._backend(uv, python, env, log), {"backend": self.settings["backend"]})[1])

        if not self.settings["skip_models"]:
            self._models(python, log, env)
        return python

    def _install_launcher(self, python):
        if self.system == "Windows":
            bindir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / "Alfred"
            bindir.mkdir(parents=True, exist_ok=True)
            launcher = bindir / "alfred.cmd"
            launcher.write_text(f'@echo off\n"{python}" -m alfred --config "{self.config_path}" %*\n', encoding="utf-8")
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as key:
                try:
                    current, kind = winreg.QueryValueEx(key, "Path")
                except FileNotFoundError:
                    current, kind = "", winreg.REG_EXPAND_SZ
                values = [part for part in current.split(os.pathsep) if part]
                if str(bindir) not in values:
                    winreg.SetValueEx(key, "Path", 0, kind, os.pathsep.join(values + [str(bindir)]))
                    self.ui.warn("Alfred was added to your user PATH. Open a new terminal to use `alfred`.")
                    try:
                        import ctypes
                        from ctypes import wintypes
                        result = wintypes.DWORD()
                        ctypes.windll.user32.SendMessageTimeoutW(
                            0xFFFF, 0x1A, 0, "Environment", 0x0002, 5000,
                            ctypes.byref(result))
                    except (AttributeError, OSError):
                        pass
            return
        bindir = Path.home() / ".local" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        launcher = bindir / "alfred"
        import shlex
        launcher.write_text(
            "#!/bin/sh\nexec " + shlex.quote(str(python)) + " -m alfred --config "
            + shlex.quote(str(self.config_path)) + ' "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
        if str(bindir) not in os.environ.get("PATH", "").split(os.pathsep):
            self.ui.warn(f"Add {bindir} to PATH to run `alfred` from any directory")

    def _backend(self, uv, python, env, log):
        backend = self.settings["backend"]
        if backend in ("skip", "lmstudio", "llamacpp"):
            return
        if self.system == "Windows":
            if "Ollama server" in self.existing_backends:
                return
            if not _has("ollama"):
                if not _has("winget"):
                    self.ui.warn("Install Ollama from https://ollama.com/download/windows, then configure its local API")
                    return
                if self.args.auto or self.ui.confirm("Install Ollama for Windows now?", default=True):
                    _call(["winget", "install", "--id", "Ollama.Ollama", "--exact",
                           "--accept-package-agreements", "--accept-source-agreements"], self.ui,
                          "Installing Ollama", log=log)
                    _refresh_windows_path()
            return
        if self.system == "Darwin":
            if "Ollama server" in self.existing_backends:
                return
            if not _has("ollama"):
                self.ui.info("Install Ollama for macOS from https://ollama.com/download/macOS")
            return
        if "Ollama server" in self.existing_backends:
            return
        if not _has("ollama"):
            if self.args.auto or self.ui.confirm("Install Ollama using its official installer?", default=True):
                try:
                    with urllib.request.urlopen("https://ollama.com/install.sh", timeout=30) as response:
                        installer = response.read()
                except (OSError, urllib.error.URLError) as exc:
                    raise RuntimeError(f"Could not download the Ollama installer: {exc}") from exc
                with self.ui.progress("Installing Ollama"):
                    subprocess.run(["sh"], input=installer, check=True)

    def _models(self, python, log, env):
        needle = Path(python).parent / ("needle.exe" if self.system == "Windows" else "needle")
        if not needle.exists() and shutil.which("needle"):
            needle = Path(shutil.which("needle"))
        for model in ("needle3", "whistle"):
            cached = subprocess.run([str(needle), "list"], capture_output=True, text=True,
                                    timeout=8, check=False)
            import re
            present = (cached.returncode == 0 and
                       re.search(r"(?<![a-z0-9_.-])" + re.escape(model.lower()) +
                                 r"(?![a-z0-9_.-])", cached.stdout.lower()) is not None)
            if present:
                self.state.data.setdefault("choices", {}).setdefault("models", {})[model] = "downloaded"
                self.state.save()
                self.ui.ok(model + " is already cached")
                continue
            decisions = self.state.data.setdefault("choices", {}).setdefault("models", {})
            decision = decisions.get(model)
            if decision not in ("download", "defer"):
                decision = ("download" if self.args.auto or
                            self.ui.confirm(f"Download the {model} model? (~250 MB combined)", default=True)
                            else "defer")
                decisions[model] = decision
                self.state.save()
            if decision == "download":
                _call([str(needle), "download", model], self.ui, "Downloading " + model,
                      cwd=ROOT, env=env, log=log)
                decisions[model] = "downloaded"
                self.state.save()
        if self.settings["backend"] == "ollama" and _has("ollama"):
            tag = self.settings.get("model") or DEFAULT_MODEL
            self.ui.info(f"Ollama model: {tag}")
            listed = subprocess.run(["ollama", "list"], capture_output=True, text=True,
                                    timeout=8, check=False)
            cached = listed.returncode == 0 and any(
                line.split() and line.split()[0] == tag for line in listed.stdout.splitlines()[1:])
            if cached:
                self.ui.ok(tag + " is already available")
                self.state.data.setdefault("choices", {}).setdefault("models", {})["ollama"] = "downloaded"
                self.state.save()
            else:
                decisions = self.state.data.setdefault("choices", {}).setdefault("models", {})
                decision = decisions.get("ollama")
                if decision not in ("download", "defer"):
                    decision = ("download" if self.args.auto or self.ui.confirm(
                        f"Download {tag} with Ollama? Model size depends on the quantization.",
                        default=False) else "defer")
                    decisions["ollama"] = decision
                    self.state.save()
                if decision != "download":
                    return
                _call(["ollama", "pull", tag], self.ui, "Downloading " + tag, log=log)
                self.config["qwen"]["model"] = tag
                self.state.data.setdefault("choices", {}).setdefault("models", {})["ollama"] = "downloaded"
                self.state.data["choices"]["configured_config_hash"] = fingerprint(self.config)
                self.state.save()
                if self.config != self.original_config:
                    save_config(self.config_path, self.config)


def main(argv=None):
    args = parser().parse_args(argv)
    args.auto = args.auto or _read_env_bool("ALFRED_AUTO")
    args.wheelhouse = args.wheelhouse or os.environ.get("ALFRED_WHEELHOUSE")
    ui = Terminal(plain=args.plain or _read_env_bool("ALFRED_PLAIN"))
    old_handler = signal.getsignal(signal.SIGINT)
    try:
        with InstallerLock(ROOT):
            wizard = Wizard(args, ui)
            wizard.settings["auto"] = args.auto
            if not wizard.collect():
                ui.warn("Setup paused before installation. Run the installer again when ready.")
                return 0
            python = wizard.install()
        ui.section("System check", "verifying the installed backend, microphone and Harness")
        doctor = subprocess.run([str(python), "-m", "alfred", "--config",
                                 str(wizard.config_path), "--doctor"], cwd=ROOT, check=False)
        if doctor.returncode == 0:
            if wizard.settings.get("service") and not wizard.install_service(python):
                ui.warn("Alfred is ready, but automatic startup could not be enabled on this system")
            ui.ok("Alfred is ready. Open a new terminal and run: alfred")
            return 0
        ui.warn("Setup complete, but a readiness check needs attention")
        ui.info("Run `alfred --doctor` after setting up a microphone and local model server")
        ui.info("Installer log: " + str(log_path()))
        return 0
    except KeyboardInterrupt:
        ui.warn("Setup interrupted. Completed steps are safe to reuse; rerun the installer to continue.")
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        ui.error(str(exc))
        ui.info("Installer log: " + str(log_path()))
        return 1
    finally:
        signal.signal(signal.SIGINT, old_handler)


if __name__ == "__main__":
    raise SystemExit(main())
