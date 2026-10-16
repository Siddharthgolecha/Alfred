# Deployment

Run Alfred as a systemd **user** service so it starts at login and restarts on failure. Two unit
templates are provided in [`deployment/`](../../deployment/alfred.service).

| Unit | Command | Needs | Purpose |
|---|---|---|---|
| `alfred` | `alfred --config <path>` | Microphone, audio server, Qwen | Voice agent, plus its own scheduler. |
| `alfred-scheduler` | `alfred --config <path> --scheduler-only` | Nothing but the database | Reminders, watches and workflows without a microphone. |

For a single durable owner of timers, run `alfred-scheduler`. See
[Automations](automations.md#run-without-a-microphone).

## Install a unit

The templates assume the checkout is at `~/Alfred` with a virtual environment in `.venv`
(`WorkingDirectory=%h/Alfred`, `ExecStart=%h/Alfred/.venv/bin/alfred --config
%h/Alfred/config.json`). Edit those paths first if yours differ.

```bash
mkdir -p ~/.config/systemd/user
cp deployment/alfred.service ~/.config/systemd/user/alfred.service
systemctl --user daemon-reload
systemctl --user enable --now alfred
journalctl --user -u alfred -f
```

For the scheduler, repeat with `alfred-scheduler.service` and the unit name `alfred-scheduler`.

The audio daemon and BlueZ session must be available to your user. Running `systemd --user`
without a graphical or audio session may not be enough. To start user units at boot before
login, use your system's user-session (linger) configuration, if permitted. Test restart and
Bluetooth disconnects separately.

## What the units set

| Setting | `alfred` | `alfred-scheduler` | Why |
|---|---|---|---|
| `Restart` | `on-failure` | `on-failure` | Recover from crashes. |
| `RestartSec` | 5 | 5 | Delay between restarts. |
| `RestartPreventExitStatus` | `2` | not set | Exit status 2 means no usable microphone. Restarting would only loop. |
| `TimeoutStopSec` | 15 | not set | Time allowed for graceful shutdown. |
| `NEEDLE_TELEMETRY` | `0` | `0` | Disable Needle telemetry. |
| `PYTHONUNBUFFERED` | `1` | `1` | Timely journal output. |

After connecting a missing microphone, run `systemctl --user restart alfred`.

## Stop and shut down

| Method | Effect |
|---|---|
| `Ctrl+C` or `SIGTERM` | Graceful stop. Alfred stops audio capture, in-progress named-task and speech processes, the scheduler and workflow threads, the watchdog, and **only the inference server it started**. |
| `alfred close` | Asks running voice and scheduler instances to stop through their control sockets. |
| `systemctl --user stop alfred` | Stops the service and marks it stopped in systemd. |
| Say "Alfred, close" | Stops the voice agent after wake authorization. |

`alfred close` and a spoken close exit cleanly, so they do not trigger `Restart=on-failure`.
Prefer `systemctl --user stop` when you also want systemd to record the service as stopped.

### Control sockets

`alfred close` uses a Unix socket at `<runtime.logs>/.control/<role>.sock`, where the role is
`voice` or `scheduler`. The socket is mode `0600` in a `0700` directory owned by you. There is no
PID file, port scan or `pkill`. A second instance of the same role refuses to start. Running
`alfred close` again after shutdown is safe. Add `--config` if you use a non-default config path.

### What shutdown does not undo

- Actions you approved, such as `control_service`, are not reverted.
- A server Alfred **adopted** is never stopped.
- The server started by `--ensure-backend` stays running by design.

## Checks before relying on it

1. `alfred --doctor` exits 0.
2. `systemctl --user status alfred` shows the service active.
3. Reboot or restart, then confirm reminders and schedules are still listed
   (`alfred --automations`).

Never run the service as root. Continuous listening is a choice you make. Use it only where
recording is appropriate. Alfred does not store raw audio, and its database logs the transcript
**length**, not the content.
