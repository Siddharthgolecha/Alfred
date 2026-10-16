# Installation Simplification Plan

## Top-Level Overview

Alfred currently requires 6 discrete manual steps (system packages, Python env, config, model
caches, optional backend build, verification) that take 1–3 hours and are spread across the
README, two doc pages, and an ad-hoc `scripts/` helper.

**Goal:** collapse everything into a single `install.sh` at the repo root that:

- Runs in two modes: `./install.sh` (interactive guided) and `./install.sh --auto` (CI/headless).
- Defaults to Ollama as the inference backend in both modes; llama.cpp available as `--backend llamacpp`.
- Always enforces SHA-256 checksum verification for the DeepSeek Harness wheels.
- Installs a systemd user-service when `--service` is passed or the user answers yes in interactive mode.
- Self-updates: on every run the script fetches its own SHA from `main` and prompts the user to
  pull if a newer version exists; in `--auto` mode it auto-applies the update and re-execs itself.

---

## Parallelisation Map

Sub-tasks are grouped into waves. Within each wave the sub-tasks are independent of one another
and can be implemented in parallel by separate agents. Later waves depend on earlier ones.

```
Wave 1 (independent foundations)
├── Sub-task 1  — install.sh core scaffold
├── Sub-task 2  — System packages block
└── Sub-task 7  — Systemd service template + block

Wave 2 (depends on Wave 1 being merged)
├── Sub-task 3  — Python venv + pip + Harness verification
└── Sub-task 4  — Config generation block

Wave 3 (depends on Wave 2)
├── Sub-task 5  — Inference backend setup block
└── Sub-task 6  — Model cache download block

Wave 4 (depends on Wave 3)
├── Sub-task 8  — Self-update mechanism
└── Sub-task 9  — README + docs update
```

---

## Sub-Task 1 — `install.sh` core scaffold

**Status:** [ ] pending

### Intent
Create the master `install.sh` at the repo root. This is the single file users clone and run.
It owns argument parsing, OS/arch detection, coloured logging helpers, and the top-level
orchestration loop that calls into helpers defined in every other sub-task. Nothing outside
this file should know about `--auto` or `--service` — they read the exported variables.

### Expected Outcomes
- `install.sh` exists at repo root and is executable (`chmod +x`).
- `./install.sh --help` prints usage summary.
- `--auto` sets `ALFRED_AUTO=1`; `--service` sets `ALFRED_INSTALL_SERVICE=1`;
  `--backend <ollama|llamacpp>` sets `ALFRED_BACKEND`; `--wheelhouse <path>` sets
  `ALFRED_WHEELHOUSE`; `--skip-models` skips Sub-task 6.
- Defaults: `ALFRED_BACKEND=ollama`, `ALFRED_AUTO=0`, `ALFRED_INSTALL_SERVICE=0`.
- `PLATFORM` (`darwin`/`linux`) and `ARCH` (`x86_64`/`aarch64`) detected and exported.
- `log_step`, `log_warn`, `log_error`, `prompt_yn` (respects `ALFRED_AUTO`) helpers defined.
- `set -euo pipefail` throughout; any unhandled exit triggers a red "Installation failed at …" message.
- Ends with a green "Alfred is ready — run: alfred --config config.json" summary.

### Todo List
1. Create `install.sh` with shebang `#!/usr/bin/env bash` and `set -euo pipefail`.
2. Parse `--help`, `--auto`, `--service`, `--backend`, `--wheelhouse`, `--skip-models`.
3. Detect `PLATFORM` and `ARCH`; emit Jetson notice when `ARCH=aarch64`.
4. Define `log_step`, `log_warn`, `log_error`, `prompt_yn` using ANSI colour codes.
5. Define a `trap ERR` handler that prints file/line of failure.
6. Add stub `# TODO sub-task N` call-sites in order for waves 1–4.
7. Print final ready message.

### Relevant Context
- `README.md` lines 26–36 — current Quick Start the scaffold will replace.
- `docs/getting-started/installation.md` — full 6-step procedure the script codifies.

---

## Sub-Task 2 — System packages block

**Status:** [ ] pending  
**Wave:** 1 (independent)

### Intent
Automate OS-level dependency installation so users never have to read the requirements table and
translate it into `apt` or `brew` commands.

### Expected Outcomes
- On Linux, probes each required binary; runs `sudo apt-get install -y` only for missing ones.
  Required: `bluez`, `pulseaudio-utils`, `libasound2-dev`, `python3-venv`.
- On macOS, probes `brew`; warns if missing. Checks `sox`/`ffmpeg`; in interactive mode offers
  to run `brew install sox`; in `--auto` mode runs without confirmation.
- Entire block is a no-op if all binaries already exist on `PATH`.
- Emits a `[SKIP]` log line for each package already present.

### Todo List
1. Write `check_system_deps()` function: array of required binaries, probe each with `command -v`.
2. Write `install_system_deps_linux()`: collect missing packages, run single `apt-get` call.
3. Write `install_system_deps_macos()`: probe `brew`, collect missing formulae, offer/run install.
4. Write `install_system_deps()` dispatcher that calls the correct platform function.
5. Call `install_system_deps` from the Sub-task 1 stub site.

### Relevant Context
- `docs/getting-started/installation.md` lines 22–25 — package list.
- `alfred/constants.py` — audio binary names (`pactl`, `parec`, `say`) that the script should probe.

---

## Sub-Task 3 — Python venv + pip + Harness verification block

**Status:** [ ] pending  
**Wave:** 2 (after Sub-task 1)

### Intent
Create the `.venv`, install core dependencies, and enforce SHA-256 checksum verification of the
DeepSeek Harness wheels before installing them. Integrates the existing `scripts/install_harness.py`.

### Expected Outcomes
- Creates `.venv` if absent; skips silently if present.
- Activates venv within the script's environment (prepends `.venv/bin` to `PATH`).
- Runs `pip install -e .` for core deps.
- Reads `ALFRED_WHEELHOUSE` (or downloads from PyPI into a temp dir) and calls
  `python scripts/install_harness.py --wheelhouse <dir> --platform <ARCH_STR> --verify` first.
  If verification fails the script exits non-zero with a clear error.
  Then calls `--install` on success.
- In interactive mode, asks whether to install the `wakeword` extra.
- In `--auto` mode, skips the `wakeword` extra.

### Todo List
1. Write `setup_venv()`: create `.venv` if missing, prepend `.venv/bin` to `PATH`.
2. Write `install_python_deps()`: `pip install -e .`; handle `wakeword` extra prompt.
3. Write `install_harness()`: resolve wheelhouse (env var, flag, or temp-dir download);
   call `scripts/install_harness.py --verify` then `--install`; map `ARCH` to `linux_x86_64` /
   `linux_aarch64`.
4. Call functions in order from the Sub-task 1 stub site.

### Relevant Context
- `pyproject.toml` — dependency list and optional extras.
- `scripts/install_harness.py` — existing offline-verified harness installer to reuse.
- `coordination/deepseek-harness-lock.json` — hash lock consumed by `install_harness.py`.
- `docs/getting-started/installation.md` lines 29–67.

---

## Sub-Task 4 — Configuration generation block

**Status:** [ ] pending  
**Wave:** 2 (after Sub-task 1, parallel with Sub-task 3)

### Intent
Replace the manual `cp config.example.json config.json` step with a guided copy that patches
the most common values (Bluetooth device name, chosen backend) so the first `--doctor` run is
likely to pass without extra editing.

### Expected Outcomes
- If `config.json` already exists, prints a `[SKIP]` notice and continues.
- Otherwise, copies `config.example.json` → `config.json`.
- In interactive mode, prompts for:
  - Bluetooth device name (optional; left as example default if skipped).
  - Backend choice (Ollama / llama.cpp / skip) — exports `ALFRED_BACKEND` for Sub-task 5.
- Patches values into `config.json` using `python -c` + stdlib `json` — no `jq` dependency.
- Prints a reminder that `config.json` must not be committed.

### Todo List
1. Write `generate_config()`: exists-guard, copy template.
2. Add interactive prompts for Bluetooth name and backend choice; respect `ALFRED_AUTO`.
3. Write `patch_config()` helper: `python -c` snippet that loads, mutates, and writes JSON.
4. Export `ALFRED_BACKEND` for Sub-task 5.
5. Call `generate_config` from the Sub-task 1 stub site.

### Relevant Context
- `config.example.json` — template file.
- `alfred/config.py` — valid config keys and their defaults.
- `docs/getting-started/installation.md` lines 69–81.

---

## Sub-Task 5 — Inference backend setup block

**Status:** [ ] pending  
**Wave:** 3 (after Sub-tasks 3 and 4)

### Intent
Give users a working inference backend without manual research. Ollama is the default easy path;
llama.cpp with CUDA is the advanced/Jetson path.

### Expected Outcomes
- `ALFRED_BACKEND=ollama` (default):
  - Checks if `ollama` is on `PATH`; if not, installs via the official `curl | sh` installer.
  - Runs `ollama pull qwen2.5:3b`.
  - Patches `config.json` with `qwen.backend = "ollama"` and `qwen.url = "http://127.0.0.1:11434"`.
- `ALFRED_BACKEND=llamacpp`:
  - Clones `llama.cpp` into `./llama.cpp` if absent (or `git pull` if present).
  - On `aarch64`, adds `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87`.
  - Builds with `cmake` + `make -j4`.
  - Patches `config.json` `qwen.binary` to the built path.
- `ALFRED_BACKEND=skip`: prints manual instructions, does not modify `config.json`.
- In `--auto` mode, defaults to Ollama without confirmation.

### Todo List
1. Write `setup_ollama()`: probe binary, install if missing, pull model, patch config.
2. Write `setup_llamacpp()`: clone/update repo, run cmake with platform flags, patch config.
3. Write `setup_backend()` dispatcher that reads `ALFRED_BACKEND` and calls the right function.
4. Call `setup_backend` from the Sub-task 1 stub site.

### Relevant Context
- `docs/guides/inference-backends.md` — describes all three backends.
- `docs/getting-started/installation.md` lines 96–108 — llama.cpp cmake flags.
- `alfred/config.py` — `qwen.backend`, `qwen.url`, `qwen.binary` keys.

---

## Sub-Task 6 — Model cache download block

**Status:** [ ] pending  
**Wave:** 3 (after Sub-task 3, parallel with Sub-task 5)

### Intent
Pre-populate the Cactus model cache so Alfred starts up instantly on first run rather than
downloading at launch time.

### Expected Outcomes
- Calls `needle download needle3` and `needle download whistle` inside the activated venv.
- Checks if each model is already cached (`needle list` output or known cache dir) and skips if so.
- Skipped entirely if `--skip-models` flag is passed or `ALFRED_SKIP_MODELS=1` is set (for
  air-gapped environments).
- In interactive mode, asks before downloading and prints estimated sizes.
- In `--auto` mode, downloads without prompting.

### Todo List
1. Write `download_models()`: loop over `(needle3, whistle)`, probe cache, call `needle download`.
2. Add `--skip-models` / `ALFRED_SKIP_MODELS` guard at the top of the function.
3. Add prompt/auto handling.
4. Call `download_models` from the Sub-task 1 stub site.

### Relevant Context
- `docs/getting-started/installation.md` lines 84–92.
- `needle` CLI is available after Sub-task 3 installs `cactus-needle`.

---

## Sub-Task 7 — Systemd service template and installation block

**Status:** [ ] pending  
**Wave:** 1 (independent)

### Intent
Let users register Alfred as a user-level systemd service for auto-start on login/boot. Includes
the template file itself and the script block that instantiates and enables it.

### Expected Outcomes
- `scripts/alfred.service.template` added with `[Unit]`, `[Service]`, `[Install]` sections;
  placeholders: `{{INSTALL_DIR}}`, `{{VENV_PYTHON}}`, `{{USER}}`, `{{CONFIG}}`.
- `install_service()` function: detects `systemd` availability (`systemctl --version`);
  substitutes placeholders; writes to `~/.config/systemd/user/alfred.service`;
  runs `systemctl --user daemon-reload && systemctl --user enable --now alfred.service`.
- On macOS or non-systemd Linux: prints a note and skips.
- Activated by `ALFRED_INSTALL_SERVICE=1` (set by `--service` flag or interactive `prompt_yn`).
- Prints `systemctl --user status alfred` as the post-install verification command.

### Todo List
1. Create `scripts/alfred.service.template` with the four `{{…}}` placeholders.
2. Write `install_service()`: probe systemd, substitute placeholders using `sed`, enable service.
3. Add `ALFRED_INSTALL_SERVICE` guard and interactive prompt in the top-level orchestrator
   (Sub-task 1 stub site).

### Relevant Context
- `docs/guides/deployment.md` — check for any existing manual service guidance to stay consistent.
- `alfred/app.py` — entry point command (`alfred`) to place in `ExecStart`.

---

## Sub-Task 8 — Self-update mechanism

**Status:** [ ] pending  
**Wave:** 4 (after all implementation sub-tasks are done)

### Intent
`install.sh` should never silently go stale. On every run it fetches the SHA of `install.sh` from
`main` on GitHub, compares it to the local SHA, and tells the user when a newer version exists —
or auto-applies the update in `--auto` mode.

### Expected Outcomes
- At the top of the script (before any installation work), a `self_update()` function runs.
- It curls `https://api.github.com/repos/Siddharthgolecha/Alfred/contents/install.sh` (or the
  raw blob SHA from the git API) and compares to `sha256sum` of the local file.
- If the local file is up-to-date: continues silently.
- If a newer version is available:
  - Interactive: prints a notice and `prompt_yn "Update install.sh now?"`. If yes, downloads
    the new script to a temp file, replaces itself, and re-execs `exec bash "$0" "$@"`.
    If no, continues with the current version and prints a warning.
  - `--auto`: downloads and re-execs automatically without prompting.
- Gracefully degrades (prints a warning and continues) if the GitHub API is unreachable (e.g.
  air-gapped Jetson).
- The update check can be skipped with `--no-update-check` or `ALFRED_NO_UPDATE_CHECK=1`.

### Todo List
1. Write `self_update()` function with GitHub API call (`curl -sf`), local SHA comparison, and
   update/re-exec logic.
2. Add `--no-update-check` / `ALFRED_NO_UPDATE_CHECK` guard.
3. Call `self_update` as the very first action in the script, before `install_system_deps`.
4. Ensure the `--help` output documents `--no-update-check`.

### Relevant Context
- Sub-task 1 scaffold — `self_update()` is called from the orchestration loop defined there.
- GitHub repository: `https://github.com/Siddharthgolecha/Alfred`.

---

## Sub-Task 9 — Update README and installation docs

**Status:** [ ] pending  
**Wave:** 4 (after all implementation sub-tasks are done, parallel with Sub-task 8)

### Intent
Replace the multi-step Quick Start in `README.md` with the new one-liner and restructure
`docs/getting-started/installation.md` so the scripted path comes first; the existing 6-step
procedure becomes a collapsible "Manual installation" fallback for power users.

### Expected Outcomes
- `README.md` Quick Start block (lines 26–36) becomes:
  ```bash
  git clone https://github.com/Siddharthgolecha/Alfred.git
  cd Alfred
  ./install.sh          # guided interactive setup
  # or: ./install.sh --auto        for CI / headless
  # or: ./install.sh --service     to also register a systemd auto-start service
  ```
- The old 5-command manual block is removed from README.
- `docs/getting-started/installation.md` is restructured:
  - New "Scripted installation" section at the top with the one-liner.
  - A table of `install.sh` flags (`--auto`, `--service`, `--backend`, `--no-update-check`, etc.).
  - Existing sections 1–6 moved under a "Manual installation" heading.
- `docs/getting-started/quickstart.md` preamble (line 4) updated to reference `install.sh`
  instead of manual install steps.

### Todo List
1. Edit `README.md` lines 26–36: replace Quick Start block with new one-liner + flag table.
2. Edit `docs/getting-started/installation.md`: insert "Scripted installation" section at top,
   demote existing sections under "Manual installation".
3. Edit `docs/getting-started/quickstart.md` line 4: update preamble reference.
4. Check `docs/getting-started/index.md` for any links that need updating.
