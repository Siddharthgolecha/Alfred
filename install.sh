#!/bin/sh
# Alfred's POSIX launcher. The Python UI itself uses only the standard library.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
AUTO=${ALFRED_AUTO:-0}
for arg in "$@"; do
    [ "$arg" = "--auto" ] && AUTO=1
done

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    exec python3 -m scripts.installer.core "$@" 2>/dev/null || {
        exec python -m scripts.installer.core "$@" 2>/dev/null || {
            cat <<'EOF'
Usage: ./install.sh [--auto] [--plain] [--customize] [--service] [--repair]
                    [--backend ollama|lmstudio|llamacpp|skip]
                    [--wheelhouse PATH] [--skip-models]
EOF
            exit 0
        }
    }
fi

if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    exec python3 -m scripts.installer.core "$@"
fi
if command -v python >/dev/null 2>&1 && python -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    exec python -m scripts.installer.core "$@"
fi

if ! command -v uv >/dev/null 2>&1; then
    if [ "$AUTO" = 1 ]; then
        echo "Alfred setup needs Python 3.10+ or uv. Install uv, then rerun ./install.sh --auto." >&2
        exit 2
    fi
    printf 'Python 3.10+ is missing. Download uv and Python 3.12 for your user? [Y/n] '
    IFS= read -r answer || answer=y
    case "$answer" in n|N|no|NO) echo "Install Python 3.10+ and rerun ./install.sh" >&2; exit 2;; esac
    command -v curl >/dev/null 2>&1 || { echo "Install curl, then rerun the installer." >&2; exit 2; }
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
fi
uv python install 3.12
exec uv run --no-project --python 3.12 -- python -m scripts.installer.core "$@"
