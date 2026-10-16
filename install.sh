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
COLOR_MUTED="\033[0;90m"

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

export ALFRED_AUTO="${ALFRED_AUTO:-0}"
export ALFRED_INSTALL_SERVICE="${ALFRED_INSTALL_SERVICE:-0}"
export ALFRED_BACKEND="${ALFRED_BACKEND:-ollama}"
export ALFRED_WHEELHOUSE="${ALFRED_WHEELHOUSE:-}"
export ALFRED_SKIP_MODELS="${ALFRED_SKIP_MODELS:-0}"
export ALFRED_NO_UPDATE_CHECK="${ALFRED_NO_UPDATE_CHECK:-0}"

# ------------------------------------------------------------------------------
# Help and Usage
# ------------------------------------------------------------------------------

show_help() {
    cat << 'EOF'
Usage: ./install.sh [OPTIONS]

Alfred installation script. Sets up system dependencies, Python virtual environment,
DeepSeek Harness wheels, optional inference backend, model caches, and systemd service.

Options:
  --auto                  Run non-interactively with defaults (CI/headless mode)
  --service               Install and enable Alfred as a systemd user service
  --backend <ollama|llamacpp>
                          Inference backend to configure (default: ollama)
  --wheelhouse <path>     Directory path to pre-downloaded Harness wheels
  --skip-models           Skip pre-downloading needle and whistle models
  --no-update-check       Skip checking for updates to install.sh
  --help, -h              Show this help message and exit

Environment Variables:
  ALFRED_AUTO             Set to 1 for non-interactive mode
  ALFRED_INSTALL_SERVICE  Set to 1 to install systemd service
  ALFRED_BACKEND          Inference backend (default: ollama)
  ALFRED_WHEELHOUSE       Directory containing Harness wheels
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
                    log_error "--backend requires an argument (ollama or llamacpp)"
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
        darwin)
            export PLATFORM="darwin"
            ;;
        linux)
            export PLATFORM="linux"
            ;;
        *)
            log_error "Unsupported operating system: $os. Alfred supports Linux and macOS."
            exit 1
            ;;
    esac

    local arch
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64)
            export ARCH="x86_64"
            ;;
        aarch64|arm64)
            export ARCH="aarch64"
            ;;
        *)
            export ARCH="$arch"
            ;;
    esac

    log_step "Detected platform: ${PLATFORM} (${ARCH})"

    if [[ "${PLATFORM}" == "linux" && "${ARCH}" == "aarch64" ]]; then
        log_warn "NVIDIA Jetson / aarch64 Linux detected. Ensure JetPack L4T dependencies are available."
    fi
}

# ------------------------------------------------------------------------------
# Stub Functions for Wave Sub-tasks
# ------------------------------------------------------------------------------

# Wave 4 - Sub-task 8: Self-update mechanism
self_update() {
    log_warn "TODO: not yet implemented"
}

# Wave 1 - Sub-task 2: System packages
install_system_deps() {
    log_warn "TODO: not yet implemented"
}

# Wave 2 - Sub-task 3: Python venv setup
setup_venv() {
    log_warn "TODO: not yet implemented"
}

# Wave 2 - Sub-task 3: Python package installation
install_python_deps() {
    log_warn "TODO: not yet implemented"
}

# Wave 2 - Sub-task 3: Harness wheels verification and install
install_harness() {
    log_warn "TODO: not yet implemented"
}

# Wave 2 - Sub-task 4: Config generation
generate_config() {
    log_warn "TODO: not yet implemented"
}

# Wave 3 - Sub-task 5: Inference backend setup
setup_backend() {
    log_warn "TODO: not yet implemented"
}

# Wave 3 - Sub-task 6: Model cache download
download_models() {
    log_warn "TODO: not yet implemented"
}

# Wave 1 - Sub-task 7: Systemd service installation
install_service() {
    log_warn "TODO: not yet implemented"
}

# ------------------------------------------------------------------------------
# Master Orchestration Flow
# ------------------------------------------------------------------------------

main() {
    parse_args "$@"
    detect_platform

    # Wave 4 - Sub-task 8
    self_update

    # Wave 1 - Sub-task 2
    install_system_deps

    # Wave 2 - Sub-task 3
    setup_venv

    # Wave 2 - Sub-task 3
    install_python_deps

    # Wave 2 - Sub-task 3
    install_harness

    # Wave 2 - Sub-task 4
    generate_config

    # Wave 3 - Sub-task 5
    setup_backend

    # Wave 3 - Sub-task 6
    download_models

    # Wave 1 - Sub-task 7
    if [[ "${ALFRED_INSTALL_SERVICE}" -eq 1 ]]; then
        install_service
    fi

    # Completion summary
    printf "\n${COLOR_GREEN_BOLD}Alfred is ready. Run: alfred${COLOR_RESET}\n"
    printf "${COLOR_MUTED}  Tip: no config.json required — Alfred runs on built-in defaults.${COLOR_RESET}\n\n"
}

main "$@"
