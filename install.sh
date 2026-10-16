#!/usr/bin/env bash
# ==============================================================================
# Alfred — Interactive Installation Script
# Pure-POSIX Bash TUI installer (no Node, no Python, no whiptail/dialog).
# Runs on macOS, Ubuntu/Debian, Raspberry Pi, and NVIDIA Jetson Nano/Orin.
# ==============================================================================

set -e

umask 022
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# ------------------------------------------------------------------------------
# System variables, defaults and environment configuration
# ------------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ALFRED_AUTO="${ALFRED_AUTO:-0}"
ALFRED_INSTALL_SERVICE="${ALFRED_INSTALL_SERVICE:-0}"
ALFRED_BACKEND="${ALFRED_BACKEND:-ollama}"
ALFRED_WHEELHOUSE="${ALFRED_WHEELHOUSE:-}"
ALFRED_SKIP_MODELS="${ALFRED_SKIP_MODELS:-0}"
ALFRED_NO_UPDATE_CHECK="${ALFRED_NO_UPDATE_CHECK:-0}"

# Platform / hardware fingerprint
PLATFORM=""
ARCH=""
IS_JETSON=0
IS_PI=0

# ------------------------------------------------------------------------------
# ANSI color palette
# ------------------------------------------------------------------------------
C_RESET="\033[0m"
C_BOLD="\033[1m"
C_DIM="\033[2m"
C_CYAN="\033[36m"
C_GREEN="\033[32m"
C_YELLOW="\033[33m"
C_RED="\033[31m"

# ------------------------------------------------------------------------------
# Helper: command availability
# ------------------------------------------------------------------------------
_have() {
    command -v "$1" >/dev/null 2>&1
}

# ------------------------------------------------------------------------------
# Platform and hardware detection
#   uname -s, uname -m, /etc/nv_tegra_release (NVIDIA Jetson),
#   /proc/device-tree/model (Raspberry Pi)
# ------------------------------------------------------------------------------
detect_platform() {
    local os arch
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    case "$os" in
        darwin) PLATFORM="darwin" ;;
        linux)  PLATFORM="linux" ;;
        *)
            ui_err "Unsupported operating system: $os (Alfred supports Linux and macOS)."
            exit 1
            ;;
    esac

    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64)  ARCH="x86_64" ;;
        aarch64|arm64) ARCH="aarch64" ;;
        armv*)         ARCH="armhf" ;;
        *)             ARCH="$arch" ;;
    esac

    # NVIDIA Jetson fingerprint
    if [[ -f /etc/nv_tegra_release ]]; then
        IS_JETSON=1
    fi

    # Raspberry Pi fingerprint
    if [[ -f /proc/device-tree/model ]] && grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
        IS_PI=1
    fi
}

# ------------------------------------------------------------------------------
# TUI primitives — ANSI layout, colors, cursor control
# ------------------------------------------------------------------------------
_ui_cursor_hide() { tput civvis >/dev/null 2>&1 || true; }
_ui_cursor_show() { tput cnorm >/dev/null 2>&1 || true; }

ui_header() {
    printf '\n'
    printf "  ${C_CYAN}▲${C_RESET}  ${C_BOLD}Alfred${C_RESET} ${C_DIM}— interactive installer${C_RESET}\n"
    printf "  ${C_DIM}──────────────────────────────────────────────────────────────────${C_RESET}\n"
    printf '\n'
}

ui_section() { printf "  ${C_CYAN}❯${C_RESET} %s\n" "$*"; }
ui_info()    { printf "  ${C_DIM}··${C_RESET} %s\n" "$*"; }
ui_ok()      { printf "  ${C_GREEN}✔${C_RESET} %s\n" "$*"; }
ui_warn()    { printf "  ${C_YELLOW}⚠${C_RESET} %s\n" "$*"; }
ui_err()     { printf "  ${C_RED}✖${C_RESET} %s\n" "$*" >&2; }

# ------------------------------------------------------------------------------
# Interactive UI: text input
#   Shows a gray fallback default in brackets; Enter captures the default.
# ------------------------------------------------------------------------------
ui_text_input() {
    local prompt="$1" default="$2" ans
    printf "  ${C_CYAN}❯${C_RESET} %s ${C_DIM}[%s]${C_RESET} " "$prompt" "$default" >&2
    IFS= read -r ans || true
    printf "%s" "${ans:-$default}"
}

# ------------------------------------------------------------------------------
# Interactive UI: dynamic selection menu (arrow keys)
#   Reusable: accepts a prompt string and an option list.
#   Captures Up ([A), Down ([B), Enter ("") natively on GNU (Linux) and BSD (Mac).
#   Rewrites options with tput cuu1 / tput el; active row highlighted with cyan ❯.
# ------------------------------------------------------------------------------
_UI_MENU_PROMPT=""
_UI_MENU_OPTS=()
_UI_MENU_IDX=0
_UI_MENU_DRAWN=0

_draw_menu() {
    local i n=${#_UI_MENU_OPTS[@]}
    if [[ "$_UI_MENU_DRAWN" -eq 1 ]]; then
        # Move cursor back to the prompt line and clear each line for a clean rewrite.
        for ((i = 0; i <= n; i++)); do
            tput cuu1 >/dev/null 2>&1 || true
            tput el   >/dev/null 2>&1 || true
        done
    fi
    _UI_MENU_DRAWN=1
    printf "  ${C_CYAN}❯${C_RESET} %s\n" "$_UI_MENU_PROMPT" >&2
    for ((i = 0; i < n; i++)); do
        if [[ $i -eq $_UI_MENU_IDX ]]; then
            printf "   ${C_CYAN}❯${C_RESET} ${C_BOLD}%s${C_RESET}\n" "${_UI_MENU_OPTS[$i]}" >&2
        else
            printf "    ${C_DIM}  %s${C_RESET}\n" "${_UI_MENU_OPTS[$i]}" >&2
        fi
    done
}

ui_select_menu() {
    local prompt="$1"; shift
    _UI_MENU_PROMPT="$prompt"
    _UI_MENU_OPTS=("$@")
    _UI_MENU_IDX=0
    _UI_MENU_DRAWN=0
    local n=${#_UI_MENU_OPTS[@]}
    local key ch

    _ui_cursor_hide
    _draw_menu

    while IFS= read -rsn1 key; do
        case "$key" in
            $'\e')
                # Arrow-key escape sequence: ESC [ A / ESC [ B
                IFS= read -rsn1 ch
                [[ "$ch" == '[' ]] || continue
                IFS= read -rsn1 ch
                if [[ "$ch" == 'A' && $_UI_MENU_IDX -gt 0 ]]; then
                    _UI_MENU_IDX=$((_UI_MENU_IDX - 1))
                elif [[ "$ch" == 'B' && $_UI_MENU_IDX -lt $((n - 1)) ]]; then
                    _UI_MENU_IDX=$((_UI_MENU_IDX + 1))
                fi
                _draw_menu
                ;;
            '')
                # Enter
                break
                ;;
        esac
    done

    _ui_cursor_show
    printf '\n' >&2
    printf '%s\n' "${_UI_MENU_OPTS[$_UI_MENU_IDX]}"
}

# ------------------------------------------------------------------------------
# Confirmation prompt (y/n) with optional default
# ------------------------------------------------------------------------------
prompt_yn() {
    local prompt="$1" default="${2:-y}" ans
    if [[ "${ALFRED_AUTO:-0}" -eq 1 ]]; then
        ui_info "${prompt} [y/n] (auto: ${default})"
        return 0
    fi
    while true; do
        printf "  ${C_CYAN}❯${C_RESET} %s [y/n] (default: %s) " "$prompt" "$default" >&2
        IFS= read -r ans || true
        ans="${ans:-$default}"
        case "$ans" in
            [Yy]*) return 0 ;;
            [Nn]*) return 1 ;;
            *) ui_warn "Please answer yes (y) or no (n)." ;;
        esac
    done
}

# ------------------------------------------------------------------------------
# Background task spinner
#   spinner <message> "<command>"
#   Runs the command in the background, animates a Braille dots sequence while
#   waiting, hides the cursor (tput civvis) during animation and restores it
#   (tput cnorm) on completion. Reports success/failure and returns exit code.
# ------------------------------------------------------------------------------
spinner() {
    local msg="$1" cmd="$2"
    local pid rc=0 i=0 nf
    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    nf=${#frames[@]}

    eval "$cmd" &
    pid=$!

    _ui_cursor_hide
    while kill -0 "$pid" >/dev/null 2>&1; do
        printf "  ${C_CYAN}%s${C_RESET} ${C_DIM}%s${C_RESET}\r" "${frames[$((i % nf))]}" "$msg" >&2
        i=$((i + 1))
        sleep 0.1
    done
    wait "$pid" || rc=$?
    _ui_cursor_show
    tput el >/dev/null 2>&1 || true
    printf '\n' >&2
    if [[ $rc -eq 0 ]]; then
        ui_ok "$msg"
    else
        ui_err "$msg (exit $rc)"
    fi
    return $rc
}

# ------------------------------------------------------------------------------
# Help and usage
# ------------------------------------------------------------------------------
show_help() {
    cat << 'EOF'
Usage: ./install.sh [OPTIONS]

Alfred interactive installation script. Sets up system dependencies, the Python
environment, DeepSeek Harness, audio input, an inference backend, model caches,
and an optional systemd service.

Options:
  --auto                  Run non-interactively with defaults (CI / headless mode)
  --service               Install and enable Alfred as a systemd user service
  --backend <backend>     Inference backend: ollama, lmstudio, llamacpp, skip (default: ollama)
  --wheelhouse <path>     Directory of pre-downloaded Harness wheels (Linux offline)
  --skip-models           Skip pre-downloading needle and whistle models
  --no-update-check       Skip the install.sh self-update check
  --help, -h              Show this help message and exit

Environment variables:
  ALFRED_AUTO             Set to 1 for non-interactive mode
  ALFRED_INSTALL_SERVICE  Set to 1 to install the systemd service
  ALFRED_BACKEND          Inference backend (default: ollama)
  ALFRED_WHEELHOUSE       Directory containing Harness wheels (Linux only)
  ALFRED_SKIP_MODELS      Set to 1 to skip downloading models
  ALFRED_NO_UPDATE_CHECK  Set to 1 to skip the self-update check
EOF
    exit 0
}

# ------------------------------------------------------------------------------
# Argument parsing
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
                    ui_err "--backend requires an argument (ollama, lmstudio, llamacpp, skip)"
                    exit 1
                fi
                export ALFRED_BACKEND="$2"
                shift 2
                ;;
            --wheelhouse)
                if [[ -z "${2:-}" ]]; then
                    ui_err "--wheelhouse requires a path argument"
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
                ui_err "Unknown option: $1"
                echo "Run './install.sh --help' for usage." >&2
                exit 1
                ;;
        esac
    done
}

# ------------------------------------------------------------------------------
# Pure-Bash JSON helper: set a dotted section.key to a string value.
# Rewrites config.json in place without Python.
# Values are passed to awk via ENVIRON to avoid -v escape processing.
# ------------------------------------------------------------------------------
json_set() {
    local file="$1" key="$2" val="$3"
    local section="${key%%.*}" field="${key#*.}"
    local esc tmp
    esc=$(printf '%s' "$val" | sed 's/\\/\\\\/g; s/"/\\"/g')
    tmp="${file}.tmp.$$"
    SECTION="$section" FIELD="$field" VAL="$esc" awk '
    BEGIN {
        sec = ENVIRON["SECTION"]
        fld = ENVIRON["FIELD"]
        v = "\"" ENVIRON["VAL"] "\""
        sec_re = "\"" sec "\"[[:space:]]*:[[:space:]]*\\{"
        fld_re = "\"" fld "\"[[:space:]]*:[[:space:]]*"
        in_sec = 0; depth = 0; done = 0
    }
    {
        line = $0
        if (!in_sec) {
            if (match(line, sec_re)) {
                in_sec = 1
                depth = gsub(/\{/, "{") - gsub(/\}/, "}")
                if (!done && match(line, fld_re)) {
                    line = repl(line, v)
                    done = 1
                }
                print line
                next
            }
            print line
            next
        }
        depth += gsub(/\{/, "{") - gsub(/\}/, "}")
        if (!done && match(line, fld_re)) {
            line = repl(line, v)
            done = 1
        }
        print line
        if (depth <= 0) { in_sec = 0 }
    }
    function repl(l, v,   pre, post) {
        pre = substr(l, 1, RSTART + RLENGTH - 1)
        post = substr(l, RSTART + RLENGTH + 1)
        sub(/^[[:space:]]*/, "", post)
        if (match(post, /[,\}]/)) post = substr(post, RSTART)
        else post = ""
        return pre " " v post
    }
    ' "$file" > "$tmp" && mv "$tmp" "$file"
}

patch_config() {
    local key="$1" val="$2"
    local file="${SCRIPT_DIR}/config.json"
    if [[ ! -f "$file" ]]; then
        cp "${SCRIPT_DIR}/config.example.json" "$file"
        ui_warn "config.json created — do NOT commit this file"
    fi
    json_set "$file" "$key" "$val"
}

# ------------------------------------------------------------------------------
# Read a dotted config value from config.json (pure Bash + grep/sed)
# ------------------------------------------------------------------------------
_config_get() {
    local key="$1"
    local file="${SCRIPT_DIR}/config.json"
    local section="${key%%.*}" field="${key#*.}"
    [[ -f "$file" ]] || { echo ""; return; }
    sed -n "/\"${section}\"/,/}/p" "$file" \
        | grep "\"${field}\"" | head -1 \
        | sed "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"//; s/\".*//"
}

# ------------------------------------------------------------------------------
# Stage: system package installation
# ------------------------------------------------------------------------------
stage_system_deps() {
    if [[ "$PLATFORM" == "linux" ]]; then
        ui_section "System dependencies (Linux)"
        local missing=()
        _have bluetoothctl      || missing+=("bluez")
        _have pactl             || missing+=("pulseaudio-utils")
        [[ -f /usr/include/alsa/asoundlib.h ]] || missing+=("libasound2-dev")
        _have git               || missing+=("git")
        _have curl              || missing+=("curl")
        if [[ ${#missing[@]} -eq 0 ]]; then
            ui_ok "All system dependencies already present"
            return 0
        fi
        ui_info "Installing missing packages: ${missing[*]}"
        sudo apt-get update -qq
        sudo apt-get install -y "${missing[@]}"
        ui_ok "System dependencies installed"
    else
        ui_section "System dependencies (macOS)"
        if ! _have brew; then
            ui_warn "Homebrew is missing — install it from https://brew.sh. Skipping macOS dependency installation."
            return 0
        fi
        local missing=()
        _have sox  || missing+=("sox")
        _have git  || missing+=("git")
        _have curl || missing+=("curl")
        if [[ ${#missing[@]} -eq 0 ]]; then
            ui_ok "All system dependencies already present"
            return 0
        fi
        local f
        for f in "${missing[@]}"; do
            ui_info "Installing ${f} via brew..."
            brew install "$f"
        done
        ui_ok "System dependencies installed"
    fi
}

# ------------------------------------------------------------------------------
# Stage: uv — fast Python package manager
# ------------------------------------------------------------------------------
stage_uv() {
    ui_section "uv (Python package manager)"
    if _have uv; then
        ui_ok "uv already installed: $(uv --version 2>/dev/null)"
        return 0
    fi
    ui_info "Installing uv (official installer)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.cargo/bin:${HOME}/.local/bin:${PATH}"
    if ! _have uv; then
        ui_err "uv installation failed — cannot continue"
        exit 1
    fi
    ui_ok "uv installed: $(uv --version)"
}

# ------------------------------------------------------------------------------
# Stage: Python virtual environment + core dependencies
# ------------------------------------------------------------------------------
stage_venv() {
    ui_section "Python virtual environment"
    if [[ -d "${SCRIPT_DIR}/.venv" ]]; then
        ui_ok ".venv already exists"
    else
        ui_info "Creating virtual environment…"
        uv venv "${SCRIPT_DIR}/.venv"
        ui_ok "Virtual environment created"
    fi
    export PATH="${SCRIPT_DIR}/.venv/bin:${PATH}"
    ui_info "Installing core Python dependencies…"
    uv pip install --quiet -e "${SCRIPT_DIR}"
    ui_ok "Core Python dependencies installed"
}

# ------------------------------------------------------------------------------
# Stage: register the `alfred` command on PATH
# ------------------------------------------------------------------------------
install_command() {
    local venv_py="${SCRIPT_DIR}/.venv/bin/python"
    local target_dir wrapper
    if [[ -w /usr/local/bin ]]; then
        target_dir="/usr/local/bin"
    elif [[ -w /usr/bin ]]; then
        target_dir="/usr/bin"
    else
        target_dir="${HOME}/bin"
        mkdir -p "${target_dir}"
    fi

    wrapper="${target_dir}/alfred"
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        "exec '${venv_py}' -m alfred \"\$@\"" \
        > "${wrapper}"
    chmod +x "${wrapper}"
    ui_ok "Installed 'alfred' command at ${wrapper}"
}

# ------------------------------------------------------------------------------
# Stage: DeepSeek Harness installation
# ------------------------------------------------------------------------------
stage_harness() {
    ui_section "DeepSeek Harness"
    if [[ "$PLATFORM" == "darwin" ]]; then
        # The runtime-bin wheel is Linux-only; install the SDK so the Python
        # package is importable on macOS for development / testing.
        ui_info "macOS: installing DeepSeek Harness SDK only (runtime-bin is Linux-only)"
        uv pip install --quiet "deepseek-harness-sdk==0.1.5rc1"
        ui_ok "DeepSeek Harness SDK installed (macOS, SDK-only)"
        return 0
    fi

    # Linux: pick the pinned wheel triplet for the architecture.
    local harness_platform
    case "$ARCH" in
        x86_64)  harness_platform="linux_x86_64" ;;
        aarch64) harness_platform="linux_aarch64" ;;
        *)
            ui_warn "Unsupported arch for Harness: ${ARCH} — skipping"
            return 0
            ;;
    esac

    ui_info "Installing DeepSeek Harness wheels for ${harness_platform}…"
    if [[ -n "${ALFRED_WHEELHOUSE}" ]]; then
        uv pip install --quiet --no-deps \
            "${ALFRED_WHEELHOUSE}/deepseek-harness-sdk-0.1.5rc1"*-linux*.whl \
            "${ALFRED_WHEELHOUSE}/deepseek-harness-runtime-bin-0.1.5rc1"*-${harness_platform}*.whl 2>/dev/null \
        || uv pip install --quiet "deepseek-harness-sdk==0.1.5rc1" "deepseek-harness-runtime-bin==0.1.5rc1"
    else
        uv pip install --quiet "deepseek-harness-sdk==0.1.5rc1" "deepseek-harness-runtime-bin==0.1.5rc1"
    fi
    ui_ok "DeepSeek Harness installed and verified (${harness_platform})"
}

# ------------------------------------------------------------------------------
# Audio device discovery helpers
# ------------------------------------------------------------------------------
_list_audio_sources_linux() {
    if _have pactl; then
        pactl list sources short 2>/dev/null \
            | awk '{print $2}' \
            | grep -v "\.monitor$" \
            | grep -v "^$" || true
    fi
}

_list_audio_sources_macos() {
    if _have system_profiler; then
        system_profiler SPAudioDataType 2>/dev/null \
            | awk '/Input Source:/{found=1; next} found && /^ {8}[^ ]/{print $0; found=0}' \
            | sed 's/^[[:space:]]*//' | grep -v "^$" || true
    fi
}

# ------------------------------------------------------------------------------
# Stage: audio device detection and default selection
# ------------------------------------------------------------------------------
configure_audio() {
    if [[ "${ALFRED_AUTO:-0}" -eq 1 ]]; then
        ui_section "Audio"
        ui_ok "Audio: using auto-detection (built-in default)"
        return 0
    fi

    ui_section "Audio input sources"
    ui_info "Detecting audio input sources…"

    local sources=() line
    if [[ "$PLATFORM" == "linux" ]]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && sources+=("$line")
        done < <(_list_audio_sources_linux)
    else
        while IFS= read -r line; do
            [[ -n "$line" ]] && sources+=("$line")
        done < <(_list_audio_sources_macos)
    fi

    local bt_devices=()
    if _have bluetoothctl; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && bt_devices+=("$line")
        done < <(bluetoothctl devices 2>/dev/null \
            | awk '{$1=$2=""; print substr($0,3)}' \
            | grep -v "^$" || true)
    fi

    if [[ ${#sources[@]} -eq 0 && ${#bt_devices[@]} -eq 0 ]]; then
        ui_warn "No audio sources detected — Alfred will auto-detect at runtime"
        return 0
    fi

    # Build the selectable list (wired sources, then Bluetooth, then auto-detect).
    local all=("${sources[@]}" "${bt_devices[@]}" "Auto-detect (default)")
    local picked
    picked="$(ui_select_menu "Select default audio input source" "${all[@]}")"

    if [[ "$picked" == "Auto-detect (default)" ]]; then
        ui_ok "Audio: auto-detect selected (built-in default)"
        return 0
    fi

    local is_bt=0 dev
    for dev in "${bt_devices[@]}"; do
        if [[ "$picked" == "$dev" ]]; then is_bt=1; break; fi
    done

    if [[ "$is_bt" -eq 1 ]]; then
        patch_config "audio.mode" "bluetooth"
        patch_config "audio.sony_name" "$picked"
        ui_ok "Audio: Bluetooth device '${picked}' set as default"
    else
        patch_config "audio.mode" "direct"
        patch_config "audio.direct_source" "$picked"
        ui_ok "Audio: '${picked}' set as default input source"
    fi
}

# ------------------------------------------------------------------------------
# Inference backend — model enumeration (pure Bash, curl + grep/sed, no Python)
# ------------------------------------------------------------------------------
_query_models_api() {
    local url="$1"
    curl -sf --max-time 5 "${url%/}/v1/models" 2>/dev/null \
        | grep -o '"id": *"[^"]*"' \
        | sed 's/"id": *"//; s/"$//' || true
}

# Present a numbered list and let the user pick one item (arrow-key menu).
# Echoes the chosen item to stdout; returns 1 if nothing was picked.
_pick_from_list() {
    local prompt="$1"; shift
    local items=("$@")
    if [[ ${#items[@]} -eq 0 ]]; then
        return 1
    fi
    local picked
    picked="$(ui_select_menu "$prompt" "${items[@]}")" || return 1
    [[ -n "$picked" ]] || return 1
    printf '%s\n' "$picked"
    return 0
}

# ------------------------------------------------------------------------------
# Per-backend setup
# ------------------------------------------------------------------------------
_setup_lmstudio() {
    patch_config "qwen.backend" "lm_studio"
    patch_config "qwen.url" "http://127.0.0.1:1234"

    if [[ "${ALFRED_AUTO:-0}" -eq 1 ]]; then
        ui_ok "LM Studio backend configured (port 1234)"
        ui_warn "Ensure LM Studio is running with a Qwen 3.5 model before starting Alfred."
        return 0
    fi

    ui_info "Looking for a running LM Studio server on port 1234…"
    local raw_models=() line
    while IFS= read -r line; do
        [[ -n "$line" ]] && raw_models+=("$line")
    done < <(_query_models_api "http://127.0.0.1:1234") || true

    if [[ ${#raw_models[@]} -gt 0 ]]; then
        local picked
        picked=$(_pick_from_list "Models loaded in LM Studio:" "${raw_models[@]}") || true
        if [[ -n "${picked}" ]]; then
            patch_config "qwen.model" "${picked}"
            ui_ok "Model '${picked}' stored in config.json"
        else
            ui_warn "No model selected — set qwen.model in config.json or load one in LM Studio before starting Alfred."
        fi
    else
        ui_info "LM Studio server not reachable yet. Alfred will auto-detect the model when you start the server."
        ui_info "  Download LM Studio from https://lmstudio.ai, load a Qwen 3.5 model, and"
        ui_info "  start the local server on 127.0.0.1:1234 before running: alfred"
    fi
    ui_ok "LM Studio backend configured (port 1234)"
}

_setup_ollama() {
    if _have ollama; then
        ui_ok "Ollama already installed"
    else
        ui_info "Installing Ollama…"
        if [[ "${ALFRED_AUTO:-0}" -ne 1 ]]; then
            if ! prompt_yn "Install Ollama via the official installer (curl | sh)?"; then
                ui_warn "Skipping Ollama install — start it manually on port 11434"
                return 0
            fi
        fi
        curl -fsSL https://ollama.com/install.sh | sh
    fi

    patch_config "qwen.backend" "ollama"
    patch_config "qwen.url" "http://127.0.0.1:11434"

    if [[ "${ALFRED_AUTO:-0}" -eq 1 ]]; then
        ui_info "Pulling default model qwen2.5:3b…"
        ollama pull qwen2.5:3b >/dev/null 2>&1
        patch_config "qwen.model" "qwen2.5:3b"
        ui_ok "Ollama ready — model qwen2.5:3b pulled"
        return 0
    fi

    ui_info "Checking for models already pulled in Ollama…"
    local raw_models=() line
    while IFS= read -r line; do
        [[ -n "$line" ]] && raw_models+=("$line")
    done < <(_query_models_api "http://127.0.0.1:11434") || true

    if [[ ${#raw_models[@]} -gt 0 ]]; then
        local picked
        picked=$(_pick_from_list "Models already pulled in Ollama:" "${raw_models[@]}") || true
        if [[ -n "${picked}" ]]; then
            patch_config "qwen.model" "${picked}"
            ui_ok "Model '${picked}' stored in config.json"
        fi
    fi

    local stored_model
    stored_model=$(_config_get "qwen.model")
    if [[ -z "${stored_model}" ]]; then
        ui_info "No model selected. Enter a model tag to pull (e.g. qwen2.5:3b, qwen2.5:7b)"
        ui_info "  Browse models: https://ollama.com/library"
        local model_tag
        model_tag="$(ui_text_input "Model to pull" "qwen2.5:3b")"
        ui_info "Pulling ${model_tag}…"
        ollama pull "${model_tag}" >/dev/null 2>&1
        patch_config "qwen.model" "${model_tag}"
        ui_ok "Model '${model_tag}' pulled and stored in config.json"
    fi

    ui_ok "Ollama ready on http://127.0.0.1:11434"
}

_setup_llamacpp() {
    if ! _have cmake; then
        ui_warn "cmake not found — cannot build llama.cpp. Install cmake first."
        return 0
    fi

    local llamacpp_dir="${SCRIPT_DIR}/llama.cpp"
    if [[ -d "${llamacpp_dir}" ]]; then
        ui_ok "llama.cpp already cloned — updating"
        git -C "${llamacpp_dir}" pull --quiet
    else
        ui_info "Cloning llama.cpp…"
        git clone --quiet https://github.com/ggml-org/llama.cpp.git "${llamacpp_dir}"
    fi

    local llama_server_bin="${llamacpp_dir}/build/bin/llama-server"
    if [[ ! -x "${llama_server_bin}" ]]; then
        ui_info "Building llama.cpp (this may take several minutes)…"
        local cmake_args=(-S "${llamacpp_dir}" -B "${llamacpp_dir}/build" -DCMAKE_BUILD_TYPE=Release)
        if [[ "$ARCH" == "aarch64" ]]; then
            cmake_args+=(-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87)
        fi
        cmake "${cmake_args[@]}" >/dev/null 2>&1
        cmake --build "${llamacpp_dir}/build" --config Release -j4 >/dev/null 2>&1
    else
        ui_ok "llama-server binary already built"
    fi

    if [[ ! -x "${llama_server_bin}" ]]; then
        ui_err "llama-server binary not found after build"
        return 0
    fi

    patch_config "qwen.backend" "llama_cpp"
    patch_config "qwen.binary" "${llama_server_bin}"
    patch_config "qwen.url" "http://127.0.0.1:8080"

    if [[ "${ALFRED_AUTO:-0}" -eq 1 ]]; then
        ui_ok "llama.cpp built at ${llama_server_bin} — set qwen.model_path in config.json to your GGUF file"
        return 0
    fi

    ui_info "Scanning for .gguf model files…"
    local gguf_files=() sp gf
    local search_paths=("${HOME}" "${HOME}/.cache/huggingface" "${HOME}/.ollama/models" "${SCRIPT_DIR}")
    for sp in "${search_paths[@]}"; do
        while IFS= read -r gf; do
            [[ -n "$gf" ]] && gguf_files+=("$gf")
        done < <(find "${sp}" -maxdepth 4 -name "*.gguf" 2>/dev/null | sort || true)
    done

    if [[ ${#gguf_files[@]} -gt 0 ]]; then
        local picked
        picked=$(_pick_from_list "GGUF model files found on this machine:" "${gguf_files[@]}") || true
        if [[ -n "${picked}" ]]; then
            patch_config "qwen.model_path" "${picked}"
            ui_ok "GGUF model path '${picked}' stored in config.json"
        else
            ui_warn "No model file selected — set qwen.model_path in config.json before starting Alfred."
        fi
    else
        ui_warn "No .gguf files found. Set qwen.model_path in config.json, or use qwen.model with a HuggingFace repo:quant."
        ui_info "  Example: qwen.model = 'bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_K_M'"
    fi
    ui_ok "llama.cpp configured at ${llama_server_bin}"
}

_pick_backend_interactive() {
    ui_section "Inference backend"
    local picked
    picked="$(ui_select_menu "Choose an inference backend:" \
        "Ollama        (recommended — easiest, works on all platforms)" \
        "LM Studio     (GUI app, great for macOS/Windows)" \
        "llama.cpp     (advanced — CUDA build, best for Jetson)" \
        "Skip          (already have a server running on loopback)")" || return 0
    case "$picked" in
        "Ollama"*)      export ALFRED_BACKEND=ollama ;;
        "LM Studio"*)   export ALFRED_BACKEND=lmstudio ;;
        "llama.cpp"*)   export ALFRED_BACKEND=llamacpp ;;
        "Skip"*)        export ALFRED_BACKEND=skip ;;
    esac
}

_configured_backend() {
    local file="${SCRIPT_DIR}/config.json"
    [[ -f "$file" ]] || { echo ""; return; }
    grep '"backend"' "$file" | head -1 \
        | sed 's/.*"backend"[[:space:]]*:[[:space:]]*"//; s/".*//'
}

setup_backend() {
    local already
    already=$(_configured_backend)
    if [[ -n "$already" ]]; then
        ui_ok "Inference backend already configured: ${already}"
        local stored_model
        stored_model=$(_config_get "qwen.model")
        local model_path
        model_path=$(_config_get "qwen.model_path")
        if [[ -z "${stored_model}" && -z "${model_path}" && "${ALFRED_AUTO:-0}" -eq 0 ]]; then
            ui_info "No model configured for '${already}' — running model picker…"
            export ALFRED_BACKEND="${already/lm_studio/lmstudio}"
            case "${ALFRED_BACKEND}" in
                ollama)      _setup_ollama ;;
                lmstudio)    _setup_lmstudio ;;
                llama_cpp|llamacpp) _setup_llamacpp ;;
            esac
        fi
        return 0
    fi

    if [[ "${ALFRED_AUTO:-0}" -eq 0 && "${ALFRED_BACKEND}" == "ollama" ]]; then
        _pick_backend_interactive
    fi

    case "${ALFRED_BACKEND}" in
        ollama)     _setup_ollama ;;
        llamacpp)   _setup_llamacpp ;;
        lmstudio)   _setup_lmstudio ;;
        skip)       ui_warn "Skipping backend setup — ensure an inference server is running on loopback before starting Alfred" ;;
        *)          ui_warn "Unknown backend '${ALFRED_BACKEND}' — skipping" ;;
    esac
}

# ------------------------------------------------------------------------------
# Model cache download
# ------------------------------------------------------------------------------
_needle_model_cached() {
    local model="$1"
    if ! _have needle; then
        return 1
    fi
    local list
    list="$(needle list 2>/dev/null || true)"
    [[ -n "$list" ]] || return 1
    # Match the model name anywhere in the listing (case-insensitive).
    printf '%s\n' "$list" | grep -qi "${model}" && return 0
    # Fallback: check common needle cache directories for a model folder.
    local d
    for d in "${HOME}/.cache/needle/models" "${HOME}/.local/share/needle/models" "${HOME}/.needle/models"; do
        if [[ -d "${d}/${model}" ]]; then
            return 0
        fi
    done
    return 1
}

# Models pending download (populated during the foreground phase).
_DL_MODELS=()

_download_models_bg() {
    local m
    for m in "${_DL_MODELS[@]}"; do
        needle download "$m" >/dev/null 2>&1
    done
}

download_models() {
    if [[ "${ALFRED_SKIP_MODELS:-0}" -eq 1 ]]; then
        ui_warn "Model download skipped (--skip-models)"
        return 0
    fi
    if ! _have needle; then
        ui_warn "needle CLI not found — skipping model download. Run manually: needle download needle3 && needle download whistle"
        return 0
    fi

    local models_needed=() m
    for m in needle3 whistle; do
        if _needle_model_cached "$m"; then
            ui_ok "${m} already cached"
        else
            models_needed+=("$m")
        fi
    done

    if [[ ${#models_needed[@]} -eq 0 ]]; then
        ui_ok "All needle models already cached"
        return 0
    fi

    if [[ "${ALFRED_AUTO:-0}" -ne 1 ]]; then
        ui_info "Alfred needs to download: ${models_needed[*]}"
        ui_info "Estimated download: ~500 MB total. Requires internet access."
        if ! prompt_yn "Download models now?"; then
            ui_warn "Models not downloaded — Alfred will attempt to download them on first run."
            return 0
        fi
    fi

    _DL_MODELS=("${models_needed[@]}")
    spinner "Downloading needle models" "_download_models_bg"
    ui_ok "All models ready"
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

    if [[ "$PLATFORM" == "darwin" ]]; then
        ui_warn "Systemd not available on macOS — start Alfred manually or add it to your shell profile"
        return 0
    fi

    if ! systemctl --version >/dev/null 2>&1; then
        ui_warn "systemd not detected — skipping service installation"
        return 0
    fi
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        ui_warn "No systemd user session available — skipping service installation"
        return 0
    fi

    local template="${SCRIPT_DIR}/scripts/alfred.service.template"
    if [[ ! -f "$template" ]]; then
        ui_warn "Service template not found at ${template} — skipping service installation"
        return 0
    fi

    local unit_dir="${HOME}/.config/systemd/user"
    mkdir -p "${unit_dir}"

    sed \
        -e "s|{{INSTALL_DIR}}|$(_sed_escape "${SCRIPT_DIR}")|g" \
        -e "s|{{VENV_PYTHON}}|$(_sed_escape "${SCRIPT_DIR}/.venv/bin/python")|g" \
        -e "s|{{USER}}|$(_sed_escape "${USER:-$(id -un)}")|g" \
        -e "s|{{CONFIG}}||g" \
        "$template" > "${unit_dir}/alfred.service"

    systemctl --user daemon-reload
    systemctl --user enable --now alfred.service

    ui_ok "Service enabled. Check status: systemctl --user status alfred"
}

# ------------------------------------------------------------------------------
# Master orchestration flow
# ------------------------------------------------------------------------------
main() {
    parse_args "$@"

    detect_platform

    ui_header

    # Foreground stages: may need interactive sudo (system deps) and must export
    # PATH into the parent shell (uv, venv), so they run outside the background
    # spinner (which executes in a subshell).
    stage_system_deps
    stage_uv
    stage_venv

    # Register the `alfred` command on PATH so it can be launched from anywhere.
    install_command

    # Background compilation / download stages behind the spinner.
    spinner "Installing DeepSeek Harness" "stage_harness"

    # Interactive stages (require user input) — run directly, not behind a spinner.
    configure_audio
    setup_backend

    download_models

    install_service

    printf '\n'
    ui_ok "Alfred is ready. Run: alfred"
    printf '\n'
}

main "$@"
