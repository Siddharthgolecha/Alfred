#!/usr/bin/env bash
# ==============================================================================
# Alfred Installation Script
# Master orchestration script for setting up Alfred and dependencies.
# ==============================================================================

set -euo pipefail

# ANSI color codes
COLOR_RESET="\033[0m"
COLOR_GREEN_BOLD="\033[1;32m"
COLOR_YELLOW="\033[0;33m"
COLOR_RED="\033[0;31m"
COLOR_CYAN="\033[0;36m"

# ------------------------------------------------------------------------------
# Logging Helpers
# ------------------------------------------------------------------------------

log_step() {
    printf "${COLOR_GREEN_BOLD}[✓]${COLOR_RESET} %s\n" "$*"
}

log_warn() {
    printf "${COLOR_YELLOW}[!]${COLOR_RESET} %s\n" "$*"
}

log_error() {
    printf "${COLOR_RED}[✗]${COLOR_RESET} %s\n" "$*" >&2
}

log_info() {
    printf "${COLOR_CYAN}[·]${COLOR_RESET} %s\n" "$*"
}

prompt_yn() {
    local prompt="$1"
    local default="${2:-y}"

    if [[ "${ALFRED_AUTO}" -eq 1 ]]; then
        log_step "${prompt} [y/n] (auto: y)"
        return 0
    fi

    while true; do
        read -r -p "${prompt} [y/n]: " answer
        case "${answer:-$default}" in
            [Yy]* ) return 0 ;;
            [Nn]* ) return 1 ;;
            * ) echo "Please answer yes (y) or no (n)." ;;
        esac
    done
}

# ------------------------------------------------------------------------------
# Error Trap
# ------------------------------------------------------------------------------

trap 'log_error "Installation failed at line $LINENO"; exit 1' ERR

# ------------------------------------------------------------------------------
# Defaults and Environment Variable Configuration
# ------------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORIGINAL_ARGS=("$@")

export ALFRED_AUTO="${ALFRED_AUTO:-0}"
export ALFRED_INSTALL_SERVICE="${ALFRED_INSTALL_SERVICE:-0}"
export ALFRED_BACKEND="${ALFRED_BACKEND:-ollama}"
export ALFRED_WHEELHOUSE="${ALFRED_WHEELHOUSE:-}"
export ALFRED_SKIP_MODELS="${ALFRED_SKIP_MODELS:-0}"
export ALFRED_NO_UPDATE_CHECK="${ALFRED_NO_UPDATE_CHECK:-0}"

# Tracks whether any user choice differs from the built-in default.
# generate_config writes config.json only when this is 1.
CONFIG_DIRTY=0

# ------------------------------------------------------------------------------
# Help and Usage
# ------------------------------------------------------------------------------

show_help() {
    cat << 'EOF'
Usage: ./install.sh [OPTIONS]

Alfred installation script. Sets up system dependencies, Python environment,
DeepSeek Harness wheels, optional inference backend, model caches, and systemd service.

Options:
  --auto                        Run non-interactively with defaults (CI/headless mode)
  --service                     Install and enable Alfred as a systemd user service
  --backend <ollama|llamacpp|lmstudio|skip>
                                Inference backend to configure (default: ollama)
  --wheelhouse <path>           Directory of pre-downloaded Harness wheels (Linux offline)
  --skip-models                 Skip pre-downloading needle and whistle models
  --no-update-check             Skip checking for updates to install.sh
  --help, -h                    Show this help message and exit

Environment Variables:
  ALFRED_AUTO             Set to 1 for non-interactive mode
  ALFRED_INSTALL_SERVICE  Set to 1 to install systemd service
  ALFRED_BACKEND          Inference backend (default: ollama)
  ALFRED_WHEELHOUSE       Directory containing Harness wheels (Linux only)
  ALFRED_SKIP_MODELS      Set to 1 to skip downloading models
  ALFRED_NO_UPDATE_CHECK  Set to 1 to skip self-update check
EOF
    exit 0
}

# ------------------------------------------------------------------------------
# Argument Parsing
# ------------------------------------------------------------------------------

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help|-h)
                show_help
                ;;
            --auto)
                export ALFRED_AUTO=1
                shift
                ;;
            --service)
                export ALFRED_INSTALL_SERVICE=1
                shift
                ;;
            --backend)
                if [[ -z "${2:-}" ]]; then
                    log_error "--backend requires an argument (ollama, llamacpp, lmstudio, skip)"
                    exit 1
                fi
                export ALFRED_BACKEND="$2"
                shift 2
                ;;
            --wheelhouse)
                if [[ -z "${2:-}" ]]; then
                    log_error "--wheelhouse requires a path argument"
                    exit 1
                fi
                export ALFRED_WHEELHOUSE="$2"
                shift 2
                ;;
            --skip-models)
                export ALFRED_SKIP_MODELS=1
                shift
                ;;
            --no-update-check)
                export ALFRED_NO_UPDATE_CHECK=1
                shift
                ;;
            *)
                log_error "Unknown option: $1"
                echo "Run './install.sh --help' for usage."
                exit 1
                ;;
        esac
    done
}

# ------------------------------------------------------------------------------
# Platform and Architecture Detection
# ------------------------------------------------------------------------------

detect_platform() {
    local os
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    case "$os" in
        darwin) export PLATFORM="darwin" ;;
        linux)  export PLATFORM="linux" ;;
        *)
            log_error "Unsupported operating system: $os. Alfred supports Linux and macOS."
            exit 1
            ;;
    esac

    local arch
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64)   export ARCH="x86_64" ;;
        aarch64|arm64)  export ARCH="aarch64" ;;
        *)              export ARCH="$arch" ;;
    esac

    log_step "Detected platform: ${PLATFORM} (${ARCH})"

    if [[ "${PLATFORM}" == "linux" && "${ARCH}" == "aarch64" ]]; then
        log_warn "NVIDIA Jetson / aarch64 Linux detected. Ensure JetPack L4T dependencies are available."
    fi
}

# ------------------------------------------------------------------------------
# Self-update
# Compares local git blob SHA of install.sh against GitHub main.
# Never aborts the installation on any failure.
# ------------------------------------------------------------------------------

self_update() {
    if [[ "${ALFRED_NO_UPDATE_CHECK}" == "1" ]]; then
        return 0
    fi

    if ! _check_cmd curl; then
        log_warn "curl not found — skipping update check"
        return 0
    fi

    local remote_info remote_sha local_sha
    remote_info=$(curl -sf --max-time 5 \
        "https://api.github.com/repos/Siddharthgolecha/Alfred/contents/install.sh") || true
    if [[ -z "${remote_info}" ]]; then
        log_warn "Update check failed — continuing with current version"
        return 0
    fi

    remote_sha=$(echo "${remote_info}" | \
        python3 -c "import sys,json; print(json.load(sys.stdin).get('sha',''))" 2>/dev/null || echo "")
    if [[ -z "${remote_sha}" ]]; then
        log_warn "Could not parse update info — continuing"
        return 0
    fi

    local_sha=$(git -C "${SCRIPT_DIR}" hash-object "${SCRIPT_DIR}/install.sh" 2>/dev/null || echo "")
    if [[ -z "${local_sha}" ]]; then
        log_warn "Not a git repository — skipping update check"
        return 0
    fi

    if [[ "${local_sha}" == "${remote_sha}" ]]; then
        return 0
    fi

    log_warn "A newer version of install.sh is available on main."
    if [[ "${ALFRED_AUTO}" -eq 1 ]]; then
        log_step "Auto-updating install.sh..."
    elif ! prompt_yn "Download and apply update now? (re-runs the installer after update)"; then
        log_warn "Continuing with current version. Run: git pull && ./install.sh to update later."
        return 0
    fi

    local tmpfile
    tmpfile=$(mktemp) || { log_error "Could not create temp file"; return 0; }
    if ! curl -fsSL --max-time 30 \
            "https://raw.githubusercontent.com/Siddharthgolecha/Alfred/main/install.sh" \
            -o "${tmpfile}"; then
        log_error "Download failed"
        rm -f "${tmpfile}"
        return 0
    fi
    chmod +x "${tmpfile}"
    if ! cp "${tmpfile}" "${SCRIPT_DIR}/install.sh"; then
        log_error "Could not replace install.sh"
        rm -f "${tmpfile}"
        return 0
    fi
    rm -f "${tmpfile}"

    log_step "install.sh updated. Re-running..."
    export ALFRED_NO_UPDATE_CHECK=1
    exec bash "${SCRIPT_DIR}/install.sh" ${ORIGINAL_ARGS[@]+"${ORIGINAL_ARGS[@]}"}
}

# ------------------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------------------

_check_cmd() {
    command -v "$1" >/dev/null 2>&1
}

# ------------------------------------------------------------------------------
# System Package Installation
# ------------------------------------------------------------------------------

_is_bluez_installed()        { _check_cmd bluetoothctl; }
_is_pulseaudio_installed()   { _check_cmd pactl; }
_is_libasound_installed()    { [[ -f /usr/include/alsa/asoundlib.h ]]; }
_is_python3_venv_installed() { python3 -m venv --help >/dev/null 2>&1; }
_is_git_installed()          { _check_cmd git; }
_is_curl_installed()         { _check_cmd curl; }

_install_system_deps_linux() {
    local missing_pkgs=()

    _is_bluez_installed       && log_step "[SKIP] bluez already present"        || missing_pkgs+=("bluez")
    _is_pulseaudio_installed  && log_step "[SKIP] pulseaudio-utils already present" || missing_pkgs+=("pulseaudio-utils")
    _is_libasound_installed   && log_step "[SKIP] libasound2-dev already present"   || missing_pkgs+=("libasound2-dev")
    _is_python3_venv_installed && log_step "[SKIP] python3-venv already present"    || missing_pkgs+=("python3-venv")
    _is_git_installed         && log_step "[SKIP] git already present"              || missing_pkgs+=("git")
    _is_curl_installed        && log_step "[SKIP] curl already present"             || missing_pkgs+=("curl")

    if [[ ${#missing_pkgs[@]} -eq 0 ]]; then
        log_step "All system dependencies already installed"
        return 0
    fi

    log_step "Installing missing system packages: ${missing_pkgs[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y "${missing_pkgs[@]}"
}

_install_system_deps_macos() {
    if ! _check_cmd brew; then
        log_warn "Homebrew is missing. Install it from https://brew.sh. Skipping macOS dependency installation."
        return 0
    fi

    local missing_formulae=()
    _check_cmd sox  && log_step "[SKIP] sox already present"  || missing_formulae+=("sox")
    _check_cmd git  && log_step "[SKIP] git already present"  || missing_formulae+=("git")
    _check_cmd curl && log_step "[SKIP] curl already present" || missing_formulae+=("curl")

    if [[ ${#missing_formulae[@]} -eq 0 ]]; then
        log_step "All system dependencies already installed"
        return 0
    fi

    for formula in "${missing_formulae[@]}"; do
        if [[ "${ALFRED_AUTO}" -eq 1 ]] || prompt_yn "Install ${formula} via brew?"; then
            log_step "Installing ${formula} via brew..."
            brew install "${formula}"
        else
            log_warn "Skipped installation of ${formula}"
        fi
    done
}

install_system_deps() {
    if [[ "${PLATFORM}" == "linux" ]]; then
        _install_system_deps_linux
    else
        _install_system_deps_macos
    fi
}

# ------------------------------------------------------------------------------
# uv — fast Python package manager (replaces pip for venv + installs)
# ------------------------------------------------------------------------------

_ensure_uv() {
    if _check_cmd uv; then
        log_step "[SKIP] uv already installed"
        return 0
    fi
    log_step "Installing uv (fast Python package manager)..."
    # Official uv installer — works on Linux and macOS
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer places uv in ~/.cargo/bin or ~/.local/bin; add both to PATH
    export PATH="${HOME}/.cargo/bin:${HOME}/.local/bin:${PATH}"
    if ! _check_cmd uv; then
        log_error "uv installation failed — cannot continue"
        exit 1
    fi
    log_step "uv installed: $(uv --version)"
}

# ------------------------------------------------------------------------------
# Python virtual environment and dependencies
# ------------------------------------------------------------------------------

setup_venv() {
    _ensure_uv
    if [[ -d "${SCRIPT_DIR}/.venv" ]]; then
        log_step "[SKIP] .venv already exists"
    else
        uv venv "${SCRIPT_DIR}/.venv"
        log_step "Python virtual environment created"
    fi
    export PATH="${SCRIPT_DIR}/.venv/bin:${PATH}"
    log_step "Python virtual environment ready"
}

install_python_deps() {
    # uv pip install — significantly faster than plain pip
    uv pip install --quiet -e "${SCRIPT_DIR}"
    log_step "Core Python dependencies installed"

    # Check if openwakeword is already installed before asking
    if python3 -c "import openwakeword" 2>/dev/null; then
        log_step "[SKIP] wakeword extra already installed"
    elif [[ "${ALFRED_AUTO}" -eq 0 ]] && prompt_yn "Install wake-word extra (openwakeword + numpy)?"; then
        uv pip install --quiet -e "${SCRIPT_DIR}[wakeword]"
        log_step "Wake-word extra installed"
    else
        log_step "[SKIP] wakeword extra skipped"
    fi
}

# ------------------------------------------------------------------------------
# DeepSeek Harness installation
#
# Linux: always enforces SHA-256 verification via scripts/install_harness.py
#        before installing the pinned wheels.
# macOS: the runtime-bin wheel is Linux-only; install only the SDK wheel via
#        uv pip so the Python import works on macOS for development/testing.
# ------------------------------------------------------------------------------

HARNESS_TMPDIR=""
_cleanup_harness_tmpdir() {
    if [[ -n "${HARNESS_TMPDIR}" ]]; then
        rm -rf "${HARNESS_TMPDIR}"
    fi
}

install_harness() {
    # Check if the SDK is already installed at the correct version
    if python3 -c "import importlib.metadata; importlib.metadata.version('deepseek-harness-sdk')" 2>/dev/null | grep -q "0.1.5"; then
        log_step "[SKIP] DeepSeek Harness SDK already installed"
        return 0
    fi

    if [[ "${PLATFORM}" == "darwin" ]]; then
        # The runtime-bin wheel is Linux-only.  Install only the SDK so the
        # Python package is importable (needed for --doctor, --text, etc.).
        log_info "macOS: installing DeepSeek Harness SDK only (runtime-bin is Linux-only)"
        uv pip install --quiet "deepseek-harness-sdk==0.1.5rc1"
        log_step "DeepSeek Harness SDK installed (macOS, SDK-only)"
        return 0
    fi

    # ---- Linux: full verified install ----------------------------------------
    local harness_platform
    case "${ARCH}" in
        x86_64)  harness_platform="linux_x86_64" ;;
        aarch64) harness_platform="linux_aarch64" ;;
        *)
            log_warn "Unsupported arch for Harness: ${ARCH} — skipping"
            return 0
            ;;
    esac

    local wheelhouse="${ALFRED_WHEELHOUSE}"
    if [[ -z "${wheelhouse}" ]]; then
        HARNESS_TMPDIR="$(mktemp -d)"
        trap _cleanup_harness_tmpdir EXIT
        wheelhouse="${HARNESS_TMPDIR}"
        log_step "Downloading pinned Harness wheels for ${harness_platform}..."
        uv pip download --quiet --dest "${wheelhouse}" --no-deps \
            "deepseek-harness-sdk==0.1.5rc1" \
            "deepseek-harness-runtime-bin==0.1.5rc1"
    fi

    if ! python "${SCRIPT_DIR}/scripts/install_harness.py" \
            --wheelhouse "${wheelhouse}" --platform "${harness_platform}" --verify; then
        log_error "Harness wheel verification failed — aborting."
        exit 1
    fi

    python "${SCRIPT_DIR}/scripts/install_harness.py" \
        --wheelhouse "${wheelhouse}" --platform "${harness_platform}" --install
    log_step "DeepSeek Harness installed and verified"
}

# ------------------------------------------------------------------------------
# config.json — write-on-change helper
#
# patch_config <dot.notation.key> <value>
#   Writes config.json (copied from config.example.json if absent) and patches
#   the key.  Marks CONFIG_DIRTY=1 so generate_config knows to keep the file.
# ------------------------------------------------------------------------------

patch_config() {
    local config_file="${SCRIPT_DIR}/config.json"
    # Create from template if this is the first patch
    if [[ ! -f "${config_file}" ]]; then
        cp "${SCRIPT_DIR}/config.example.json" "${config_file}"
        log_warn "config.json created — do NOT commit this file"
    fi
    python3 - "${config_file}" "$1" "$2" <<'PYEOF'
import json, sys
path, key, value = sys.argv[1:4]
with open(path, encoding="utf-8") as f:
    cfg = json.load(f)
parts = key.split(".")
obj = cfg
for part in parts[:-1]:
    obj = obj[part]
obj[parts[-1]] = value
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PYEOF
    CONFIG_DIRTY=1
}

# ------------------------------------------------------------------------------
# Audio device detection and default selection
#
# Alfred defaults to audio.mode="auto" which probes all available sources.
# This step discovers connected audio devices and lets the user pick a default
# rather than asking for a device name by hand.
# ------------------------------------------------------------------------------

_list_audio_sources_linux() {
    # Returns newline-separated list of source names visible to PulseAudio/PipeWire
    if _check_cmd pactl; then
        pactl list sources short 2>/dev/null \
            | awk '{print $2}' \
            | grep -v "\.monitor$" \
            | grep -v "^$" || true
    fi
}

_list_audio_sources_macos() {
    # Returns newline-separated list of input device names
    if _check_cmd system_profiler; then
        system_profiler SPAudioDataType 2>/dev/null \
            | awk '/Input Source:/{found=1; next} found && /^ {8}[^ ]/{print $0; found=0}' \
            | sed 's/^[[:space:]]*//' | grep -v "^$" || true
    fi
}

configure_audio() {
    # In --auto mode: leave audio.mode=auto, let Alfred detect at runtime
    if [[ "${ALFRED_AUTO}" -eq 1 ]]; then
        log_step "Audio: using auto-detection (built-in default)"
        return 0
    fi

    log_info "Detecting audio input sources..."
    local sources=()
    local line
    if [[ "${PLATFORM}" == "linux" ]]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && sources+=("$line")
        done < <(_list_audio_sources_linux)
    else
        while IFS= read -r line; do
            [[ -n "$line" ]] && sources+=("$line")
        done < <(_list_audio_sources_macos)
    fi

    # Also add Bluetooth as an option if bluez is available
    local bt_devices=()
    if _check_cmd bluetoothctl; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && bt_devices+=("$line")
        done < <(bluetoothctl devices 2>/dev/null | awk '{$1=$2=""; print substr($0,3)}' | grep -v "^$" || true)
    fi

    if [[ ${#sources[@]} -eq 0 && ${#bt_devices[@]} -eq 0 ]]; then
        log_warn "No audio sources detected — Alfred will auto-detect at runtime"
        return 0
    fi

    echo ""
    echo "  Available audio input sources:"
    local idx=1
    for src in "${sources[@]}"; do
        printf "    %d) %s\n" "$idx" "$src"
        idx=$((idx + 1))
    done
    if [[ ${#bt_devices[@]} -gt 0 ]]; then
        echo "  Bluetooth devices (pair first, then select here):"
        for dev in "${bt_devices[@]}"; do
            printf "    %d) %s  [Bluetooth]\n" "$idx" "$dev"
            idx=$((idx + 1))
        done
    fi
    printf "    %d) Let Alfred auto-detect (default)\n" "$idx"
    echo ""

    local choice
    read -r -p "  Select default audio source [${idx}]: " choice || true
    choice="${choice:-${idx}}"  # default = auto-detect

    # If user chose auto-detect (last option) — nothing to write
    if [[ "${choice}" -eq "${idx}" ]] 2>/dev/null; then
        log_step "Audio: auto-detect selected (built-in default)"
        return 0
    fi

    # Validate choice is in range
    if ! [[ "${choice}" =~ ^[0-9]+$ ]] || [[ "${choice}" -lt 1 ]] || [[ "${choice}" -ge "${idx}" ]]; then
        log_warn "Invalid choice — using auto-detect"
        return 0
    fi

    local all_sources=("${sources[@]}" "${bt_devices[@]}")
    local selected="${all_sources[$((choice - 1))]}"

    # Determine if chosen source is Bluetooth
    local is_bt=0
    local src_idx=1
    for src in "${sources[@]}"; do
        if [[ "${src_idx}" -eq "${choice}" ]]; then is_bt=0; break; fi
        src_idx=$((src_idx + 1))
    done
    if [[ "${choice}" -gt "${#sources[@]}" ]]; then
        is_bt=1
    fi

    if [[ "${is_bt}" -eq 1 ]]; then
        # Bluetooth: set mode=bluetooth, sony_name to the device name
        patch_config "audio.mode" "bluetooth"
        patch_config "audio.sony_name" "${selected}"
        log_step "Audio: Bluetooth device '${selected}' set as default"
    else
        # Wired/USB/built-in: set mode=direct, direct_source to the source name
        patch_config "audio.mode" "direct"
        patch_config "audio.direct_source" "${selected}"
        log_step "Audio: '${selected}' set as default input source"
    fi
}

# ------------------------------------------------------------------------------
# Inference backend — model enumeration helpers
# ------------------------------------------------------------------------------

# Query /v1/models from a running server. Returns newline-separated model IDs.
# Usage: _query_models_api <url>
_query_models_api() {
    local url="$1"
    python3 - "${url}" <<'PYEOF' 2>/dev/null || true
import sys
try:
    from urllib.request import urlopen
    from urllib.error import URLError
    import json
    url = sys.argv[1].rstrip("/") + "/v1/models"
    with urlopen(url, timeout=3) as r:
        data = json.loads(r.read())
    for item in data.get("data", []):
        if item.get("id"):
            print(item["id"])
except Exception:
    pass
PYEOF
}

# Scan a directory for .gguf files and print their paths.
_find_gguf_files() {
    local search_dir="$1"
    if [[ -d "${search_dir}" ]]; then
        find "${search_dir}" -maxdepth 4 -name "*.gguf" 2>/dev/null | sort || true
    fi
}

# Present a numbered list and let the user pick one item.
# Usage: _pick_from_list "prompt" item1 item2 ...
# Prints the chosen item to stdout; returns 1 if nothing was picked.
_pick_from_list() {
    local prompt="$1"
    shift
    local items=("$@")
    if [[ ${#items[@]} -eq 0 ]]; then
        return 1
    fi
    echo ""
    echo "  ${prompt}"
    local i=1
    for item in "${items[@]}"; do
        printf "    %d) %s\n" "$i" "$item"
        i=$((i + 1))
    done
    printf "    %d) Skip / enter manually later\n" "$i"
    echo ""
    local choice
    read -r -p "  Choice [1]: " choice || true
    choice="${choice:-1}"
    if ! [[ "${choice}" =~ ^[0-9]+$ ]] || [[ "${choice}" -lt 1 ]] || [[ "${choice}" -gt "${i}" ]]; then
        log_warn "Invalid choice — skipping model selection"
        return 1
    fi
    if [[ "${choice}" -eq "$i" ]]; then
        return 1  # user chose "skip"
    fi
    echo "${items[$((choice - 1))]}"
    return 0
}

# ------------------------------------------------------------------------------
# Inference backend — per-backend setup
# ------------------------------------------------------------------------------

_setup_lmstudio() {
    patch_config "qwen.backend" "lm_studio"
    patch_config "qwen.url" "http://127.0.0.1:1234"

    if [[ "${ALFRED_AUTO}" -eq 1 ]]; then
        log_step "LM Studio backend configured (port 1234)"
        log_warn "Ensure LM Studio is running with a Qwen 3.5 model before starting Alfred."
        return 0
    fi

    # Try to enumerate models from a running LM Studio server
    log_info "Looking for a running LM Studio server on port 1234..."
    local raw_models=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && raw_models+=("$line")
    done < <(_query_models_api "http://127.0.0.1:1234")

    if [[ ${#raw_models[@]} -gt 0 ]]; then
        local picked
        picked=$(_pick_from_list "Models loaded in LM Studio:" "${raw_models[@]}") || true
        if [[ -n "${picked}" ]]; then
            patch_config "qwen.model" "${picked}"
            log_step "Model '${picked}' stored in config.json"
        else
            log_warn "No model selected — set qwen.model in config.json or load one in LM Studio before starting Alfred."
        fi
    else
        log_info "LM Studio server not reachable yet. Alfred will auto-detect the model when you start the server."
        log_info "  Download LM Studio from https://lmstudio.ai, load a Qwen 3.5 model, and"
        log_info "  start the local server on 127.0.0.1:1234 before running: alfred"
    fi

    # Alfred's ModelSupervisor will call `lms server start` and `lms load <model>` at runtime.
    log_step "LM Studio backend configured (port 1234)"
}

_setup_ollama() {
    if _check_cmd ollama; then
        log_step "[SKIP] Ollama already installed"
    else
        log_step "Installing Ollama..."
        if [[ "${ALFRED_AUTO}" -ne 1 ]]; then
            if ! prompt_yn "Install Ollama via the official installer (curl | sh)?"; then
                log_warn "Skipping Ollama install — start it manually on port 11434"
                return 0
            fi
        fi
        curl -fsSL https://ollama.com/install.sh | sh
    fi

    patch_config "qwen.backend" "ollama"
    patch_config "qwen.url" "http://127.0.0.1:11434"

    if [[ "${ALFRED_AUTO}" -eq 1 ]]; then
        # In auto mode always pull a sensible default
        ollama pull qwen2.5:3b
        patch_config "qwen.model" "qwen2.5:3b"
        log_step "Ollama ready — model qwen2.5:3b pulled"
        return 0
    fi

    # Enumerate already-pulled models
    log_info "Checking for models already pulled in Ollama..."
    local raw_models=()
    # Start ollama briefly to query it, if it isn't already running
    local ollama_was_running=0
    if _check_cmd ollama && ollama ps &>/dev/null; then
        ollama_was_running=1
    fi

    while IFS= read -r line; do
        [[ -n "$line" ]] && raw_models+=("$line")
    done < <(_query_models_api "http://127.0.0.1:11434")

    if [[ ${#raw_models[@]} -gt 0 ]]; then
        local picked
        picked=$(_pick_from_list "Models already pulled in Ollama:" "${raw_models[@]}") || true
        if [[ -n "${picked}" ]]; then
            patch_config "qwen.model" "${picked}"
            log_step "Model '${picked}' stored in config.json"
        fi
    fi

    # If no model selected yet, offer to pull one
    local stored_model
    stored_model=$(python3 -c "
import json,sys
try:
    cfg=json.load(open('${SCRIPT_DIR}/config.json'))
    print(cfg.get('qwen',{}).get('model',''))
except: pass
" 2>/dev/null || echo "")

    if [[ -z "${stored_model}" ]]; then
        echo ""
        log_info "No model selected. Enter a model tag to pull (e.g. qwen2.5:3b, qwen2.5:7b)"
        log_info "  Browse models: https://ollama.com/library"
        local model_tag
        read -r -p "  Model to pull [qwen2.5:3b]: " model_tag || true
        model_tag="${model_tag:-qwen2.5:3b}"
        ollama pull "${model_tag}"
        patch_config "qwen.model" "${model_tag}"
        log_step "Model '${model_tag}' pulled and stored in config.json"
    fi

    log_step "Ollama ready on http://127.0.0.1:11434"
    # Alfred's ModelSupervisor starts Ollama automatically at runtime via `ollama serve`.
}

_setup_llamacpp() {
    if ! _check_cmd cmake; then
        log_warn "cmake not found — cannot build llama.cpp. Install cmake first."
        return 0
    fi

    local llamacpp_dir="${SCRIPT_DIR}/llama.cpp"
    if [[ -d "${llamacpp_dir}" ]]; then
        log_step "[SKIP] llama.cpp already cloned — updating"
        git -C "${llamacpp_dir}" pull --quiet
    else
        git clone --quiet https://github.com/ggml-org/llama.cpp.git "${llamacpp_dir}"
    fi

    # Check if the binary already exists and is up to date
    local llama_server_bin="${llamacpp_dir}/build/bin/llama-server"
    if [[ ! -x "${llama_server_bin}" ]]; then
        log_step "Building llama.cpp (this may take several minutes)..."
        local cmake_args=(-S "${llamacpp_dir}" -B "${llamacpp_dir}/build" -DCMAKE_BUILD_TYPE=Release)
        if [[ "${ARCH}" == "aarch64" ]]; then
            cmake_args+=(-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87)
        fi
        cmake "${cmake_args[@]}"
        cmake --build "${llamacpp_dir}/build" --config Release -j4
    else
        log_step "[SKIP] llama-server binary already built"
    fi

    if [[ ! -x "${llama_server_bin}" ]]; then
        log_error "llama-server binary not found after build"
        return 0
    fi

    patch_config "qwen.backend" "llama_cpp"
    patch_config "qwen.binary" "${llama_server_bin}"
    patch_config "qwen.url" "http://127.0.0.1:8080"

    if [[ "${ALFRED_AUTO}" -eq 1 ]]; then
        log_step "llama.cpp built at ${llama_server_bin} — set qwen.model_path in config.json to your GGUF file"
        return 0
    fi

    # Let user pick a GGUF file from common locations
    log_info "Scanning for .gguf model files..."
    local gguf_files=()
    local search_paths=("${HOME}" "${HOME}/.cache/huggingface" "${HOME}/.ollama/models" "${SCRIPT_DIR}")
    for sp in "${search_paths[@]}"; do
        while IFS= read -r gf; do
            [[ -n "$gf" ]] && gguf_files+=("$gf")
        done < <(_find_gguf_files "${sp}")
    done

    if [[ ${#gguf_files[@]} -gt 0 ]]; then
        local picked
        picked=$(_pick_from_list "GGUF model files found on this machine:" "${gguf_files[@]}") || true
        if [[ -n "${picked}" ]]; then
            patch_config "qwen.model_path" "${picked}"
            log_step "GGUF model path '${picked}' stored in config.json"
        else
            log_warn "No model file selected — set qwen.model_path in config.json before starting Alfred."
        fi
    else
        log_warn "No .gguf files found. Set qwen.model_path in config.json, or use qwen.model with a HuggingFace repo:quant."
        log_info "  Example: patch qwen.model = 'bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_K_M'"
    fi

    log_step "llama.cpp configured at ${llama_server_bin}"
    # Alfred's ModelSupervisor starts llama-server automatically at runtime.
}

# Interactive backend picker — only runs in interactive mode when --backend was
# not explicitly passed via CLI.
_pick_backend_interactive() {
    echo ""
    echo "  Choose an inference backend:"
    echo "    1) Ollama        (recommended — easiest, works on all platforms)"
    echo "    2) LM Studio     (GUI app, great for macOS/Windows)"
    echo "    3) llama.cpp     (advanced — CUDA build, best for Jetson)"
    echo "    4) Skip          (already have a server running on loopback)"
    echo ""
    local choice
    read -r -p "  Choice [1]: " choice || true
    case "${choice:-1}" in
        1) export ALFRED_BACKEND=ollama ;;
        2) export ALFRED_BACKEND=lmstudio ;;
        3) export ALFRED_BACKEND=llamacpp ;;
        4) export ALFRED_BACKEND=skip ;;
        *) export ALFRED_BACKEND=ollama ;;
    esac
}

# Check whether the backend is already configured in config.json.
# Returns the backend name if already set, empty string otherwise.
_configured_backend() {
    if [[ ! -f "${SCRIPT_DIR}/config.json" ]]; then
        echo ""
        return
    fi
    python3 -c "
import json, sys
try:
    cfg = json.load(open('${SCRIPT_DIR}/config.json'))
    b = cfg.get('qwen', {}).get('backend', '')
    if b and b != 'auto':
        print(b)
except: pass
" 2>/dev/null || echo ""
}

setup_backend() {
    # If already configured, skip the picker but still offer model selection
    local already
    already=$(_configured_backend)
    if [[ -n "${already}" ]]; then
        log_step "[SKIP] Inference backend already configured: ${already}"
        # Still offer model enumeration if in interactive mode and no model set
        local stored_model
        stored_model=$(python3 -c "
import json,sys
try:
    cfg=json.load(open('${SCRIPT_DIR}/config.json'))
    print(cfg.get('qwen',{}).get('model','') or cfg.get('qwen',{}).get('model_path',''))
except: pass
" 2>/dev/null || echo "")
        if [[ -z "${stored_model}" && "${ALFRED_AUTO}" -eq 0 ]]; then
            log_info "No model configured for '${already}' — running model picker..."
            export ALFRED_BACKEND="${already/lm_studio/lmstudio}"
            case "${ALFRED_BACKEND}" in
                ollama)   _setup_ollama ;;
                lmstudio) _setup_lmstudio ;;
                llama_cpp|llamacpp) _setup_llamacpp ;;
            esac
        fi
        return 0
    fi

    # In interactive mode with default, let the user choose
    if [[ "${ALFRED_AUTO}" -eq 0 && "${ALFRED_BACKEND}" == "ollama" ]]; then
        _pick_backend_interactive
    fi

    case "${ALFRED_BACKEND}" in
        ollama)    _setup_ollama ;;
        llamacpp)  _setup_llamacpp ;;
        lmstudio)  _setup_lmstudio ;;
        skip)      log_warn "Skipping backend setup — ensure an inference server is running on loopback before starting Alfred" ;;
        *)         log_warn "Unknown backend '${ALFRED_BACKEND}' — skipping" ;;
    esac
}

# ------------------------------------------------------------------------------
# Model cache download
# ------------------------------------------------------------------------------

# Check if a needle model is already cached by probing `needle list`.
_needle_model_cached() {
    local model="$1"
    needle list 2>/dev/null | grep -qi "^${model}" || \
    needle list 2>/dev/null | grep -qi "${model}" || \
    return 1
}

download_models() {
    if [[ "${ALFRED_SKIP_MODELS:-0}" -eq 1 ]]; then
        log_warn "[SKIP] Model download skipped (--skip-models)"
        return 0
    fi

    if ! _check_cmd needle; then
        log_warn "needle CLI not found — skipping model download. Run manually: needle download needle3 && needle download whistle"
        return 0
    fi

    # Check which models still need downloading
    local models_needed=()
    for model in needle3 whistle; do
        if _needle_model_cached "${model}"; then
            log_step "[SKIP] ${model} already cached"
        else
            models_needed+=("${model}")
        fi
    done

    if [[ ${#models_needed[@]} -eq 0 ]]; then
        log_step "All needle models already cached"
        return 0
    fi

    if [[ "${ALFRED_AUTO}" -ne 1 ]]; then
        echo ""
        log_info "Alfred needs to download: ${models_needed[*]}"
        log_info "Estimated download: ~500 MB total. Requires internet access."
        if ! prompt_yn "Download models now?"; then
            log_warn "Models not downloaded — Alfred will attempt to download them on first run."
            return 0
        fi
    fi

    for model in "${models_needed[@]}"; do
        log_step "Downloading ${model}..."
        needle download "${model}"
        log_step "${model} downloaded"
    done

    log_step "All models ready"
}

# ------------------------------------------------------------------------------
# Systemd service installation
# ------------------------------------------------------------------------------

_sed_escape() {
    printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

install_service() {
    if [[ "${ALFRED_INSTALL_SERVICE}" -ne 1 && "${ALFRED_AUTO}" -ne 1 ]]; then
        if prompt_yn "Register Alfred as a systemd user service (auto-start on login)?" "n"; then
            ALFRED_INSTALL_SERVICE=1
        fi
    fi
    [[ "${ALFRED_INSTALL_SERVICE}" -eq 1 ]] || return 0

    if [[ "${PLATFORM}" == "darwin" ]]; then
        log_warn "Systemd not available on macOS — start Alfred manually or add it to your shell profile"
        return 0
    fi

    if ! systemctl --version >/dev/null 2>&1; then
        log_warn "systemd not detected — skipping service installation"
        return 0
    fi

    if ! systemctl --user show-environment >/dev/null 2>&1; then
        log_warn "No systemd user session available — skipping service installation"
        return 0
    fi

    local template="${SCRIPT_DIR}/scripts/alfred.service.template"
    if [[ ! -f "${template}" ]]; then
        log_warn "Service template not found at ${template} — skipping service installation"
        return 0
    fi

    local unit_dir="${HOME}/.config/systemd/user"
    mkdir -p "${unit_dir}"

    sed \
        -e "s|{{INSTALL_DIR}}|$(_sed_escape "${SCRIPT_DIR}")|g" \
        -e "s|{{VENV_PYTHON}}|$(_sed_escape "${SCRIPT_DIR}/.venv/bin/python")|g" \
        -e "s|{{USER}}|$(_sed_escape "${USER:-$(id -un)}")|g" \
        -e "s|{{CONFIG}}||g" \
        "${template}" > "${unit_dir}/alfred.service"

    systemctl --user daemon-reload
    systemctl --user enable --now alfred.service

    log_step "Service enabled. Check status: systemctl --user status alfred"
}

# ------------------------------------------------------------------------------
# Master Orchestration Flow
# ------------------------------------------------------------------------------

main() {
    parse_args "$@"
    detect_platform

    # Self-update check (Wave 4 - Sub-task 8) — runs before everything else
    self_update

    # System packages (Wave 1 - Sub-task 2)
    install_system_deps

    # Python environment via uv (Wave 2 - Sub-task 3)
    setup_venv
    install_harness
    install_python_deps

    # Audio device detection and default selection
    configure_audio

    # Inference backend (Wave 3 - Sub-task 5)
    setup_backend

    # Model cache (Wave 3 - Sub-task 6)
    download_models

    # Systemd service (Wave 1 - Sub-task 7)
    install_service

    # Completion
    printf "\n${COLOR_GREEN_BOLD}Alfred is ready. Run: alfred${COLOR_RESET}\n\n"
}

main "$@"
