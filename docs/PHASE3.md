# Phase 3 — Persistent local automations and typing-style transcription

Alfred now provides independent, deterministic scheduling in SQLite, without asking Qwen on every timer tick. Phase 3 supplements the original relative reminders and read-only periodic diagnostics from Phases 1/2.

## New automation tools

| Tool | Function | Voice approval |
| --- | --- | --- |
| create_reminder_at | One-off reminder at an ISO-8601 timestamp with explicit UTC offset | Immediate for unambiguous, explicit requests |
| create_daily_reminder | Recurring local HH:MM time with IANA timezone | Local approval |
| watch_gpu_temperature | GPU threshold alert with interval, cooldown, 3 C reset hysteresis | Local approval |
| list_automations | Query active/paused rules | Immediate |
| pause_automation | Suspend a rule | Local approval |
| resume_automation | Resume a paused rule | Local approval |
| cancel_automation | Cancel a rule | Local approval |

This module **only delivers local notifications**. GPU monitoring is read-only; it cannot stop services or run arbitrary commands. Saved Qwen workflows do not bypass Phase 2's approval policy.

### Example requests

- "Remind me to check the experiment at 8 PM on October 15, 2026, India time."
- "Create a daily reminder for my meeting at 9 AM in Asia/Kolkata."
- "Watch GPU temperature every 5 minutes and alert above 80 Celsius; cooldown 30 minutes."
- "List my automations." 
- "Pause automation 3."

Qwen/Needle must supply an explicit time-zone offset or IANA zone. Bare clock times are **not guessed**. The app stores timestamps as UTC epoch seconds. It handles DST by skipping nonexistent spring-forward wall times and using the first occurrence of an ambiguous fall-back time. Missed daily occurrences are coalesced to a single alert after a restart.

### Run without a microphone or LLM

The most important change is **headless scheduling**. A microphone disconnected from the Orin Nano should stop the interactive voice agent, not cancel your already-configured timers.

\`\`\`bash
# Schedules keep working without a mic, native Cactus models, or llama.cpp.
python -m alfred --config config.json --scheduler-only

# List all, including completed/cancelled, without models:
python -m alfred --config config.json --automations

# Run one scheduler tick manually:
python -m alfred --config config.json --automation-tick
\`\`\`

For background persistence, set up the **separate** systemd user service:

\`\`\`bash
cp deployment/alfred-scheduler.service ~/.config/systemd/user/alfred-scheduler.service
systemctl --user daemon-reload
systemctl --user enable --now alfred-scheduler
journalctl --user -u alfred-scheduler -f
\`\`\`

Edit the service's working directory/exec path if Alfred isn't installed in \`~/Alfred\`. To run user units at boot before login, use the operating system's user-session/linger configuration if permitted. Test restart and Bluetooth disconnect separately.

The interactive Alfred process currently also starts its scheduler, but SQLite claims ensure at-most-once scheduling under ordinary concurrent runs. For a single durable owner, prefer the headless scheduler service. The two processes may both query read-only GPU metrics; no LLM is invoked during ticks.

### Reliability and exactness

- Persistent rules live in \`automation_jobs\`. Pending notifications are stored in \`automation_outbox\` before a callback executes.
- On failed delivery, the pending notification retries with bounded exponential backoff. Successful deliveries are ACKed and never intentionally replayed.
- A crash *after* the notification is shown but *before* ACK may repeat it. This is **at-least-once**, not exactly-once delivery.
- A daily reminder overdue after restart runs once, then calculates the next future occurrence; it does not flood one notification for every missed day.
- Watch alerts fire on a rising/high threshold if armed, then disarm. They rearm after cooling at least 3 °C below the configured threshold. Cooldowns suppress rapid retriggering.
- GPU temperature missing (for example when \`tegrastats\` is unavailable) cannot satisfy a condition; no fabricated zero values.
- Legacy \`create_reminder\`, \`list_reminders\`, \`schedule_every\`, \`cancel_schedule\` continue to function; migration is additive.
- **Security:** new recurring rules and pausing/canceling require Phase 2's local CLI approval (\`alfred --pending\`, \`--approve ID\`). Never auto-approve from a microphone transcript.

### Typing-style Whistle terminal

In an interactive terminal, Alfred prints \`Whistle: \` once, then **appends only newly recognized characters** as speech arrives. If a partial hypothesis changes, it removes the changed suffix and types the correction. No artificial typing delay is imposed. The final confirmed transcript ends the line; this is the *only* text allowed to trigger Needle/tools.

In a noninteractive context such as systemd journal or piped stdout, Alfred logs **only the final transcript** to prevent one log entry per streaming update. Audio is not stored; if you enable stdout capture through a service manager, it may retain the final utterances—consider the privacy implications.

### Hardware acceptance

Test on Jetson Orin Nano 8GB with WI-C100 HFP microphone: one-line live transcription, GPU-watch \`tegrastats\` measurements, DST-specific daily reminders, restart and connectivity recovery. CI runs portable offline tests; it cannot establish physical Bluetooth/CUDA availability.
