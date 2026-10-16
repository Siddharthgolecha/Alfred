# Automations

Reminders and GPU temperature watches are stored in SQLite and run on a deterministic scheduler.
The scheduler does not ask Qwen on each tick. Automations only deliver local notifications. A
GPU watch is read-only and cannot stop services or run commands.

Internals: [Data model](../architecture/data-model.md) and
[Concurrency](../architecture/concurrency.md). Tool arguments: [Tool reference](../reference/tools.md).

## What you can create

| Kind | Tool | Approval | Example request |
|---|---|---|---|
| One-shot reminder, relative | `create_reminder` | Runs immediately | "Remind me in 10 minutes to stretch." |
| One-shot reminder, absolute | `create_reminder_at` | Runs immediately | "Remind me to check the experiment at 8 PM on October 15, 2026, India time." |
| Daily reminder | `create_daily_reminder` | Local approval | "Create a daily reminder for my meeting at 9 AM in Asia/Kolkata." |
| GPU watch | `watch_gpu_temperature` | Local approval | "Watch GPU temperature every 5 minutes and alert above 80 Celsius, cooldown 30 minutes." |
| Recurring status check | `schedule_every` | Local approval | "Check system status every 30 minutes." |
| List | `list_reminders`, `list_schedules`, `list_automations` | Runs immediately | "List my automations." |
| Pause, resume, cancel | `pause_automation`, `resume_automation`, `cancel_automation`, `cancel_reminder`, `cancel_schedule` | Local approval | "Pause automation 3." |

Sensitive changes are staged. Review and run them with `--pending` and `--approve ID`. See
[Approvals](approvals.md). Never approve from a microphone transcript.

## Time rules

- A reminder needs an explicit UTC offset (`+05:30`, `Z`) or an IANA time zone. Alfred does
  **not guess** a zone for a bare clock time.
- `when` must be in the future and within five years. The message is 1 to 240 characters.
- Times are stored as UTC epoch seconds.
- **Daylight saving time:** a wall-clock time that does not exist (spring forward) is skipped.
  For an ambiguous time (fall back) the first occurrence is used.
- A daily reminder that is overdue after a restart fires **once**, then moves to the next
  future occurrence. It does not send one alert for every missed day.

## How GPU watches behave

A watch checks the GPU temperature on its interval and alerts when it reaches the threshold.

1. When the reading is at or above `threshold_c` and the watch is armed, it alerts and disarms.
2. It re-arms after the reading falls at least **3 °C** below the threshold. This hysteresis is
   fixed.
3. `cooldown_minutes` suppresses repeated alerts.
4. If no temperature is available (for example `tegrastats` is missing), the condition is false.
   Alfred never invents a reading.

Ranges: `threshold_c` 20 to 110, `interval_minutes` 1 to 1440, `cooldown_minutes` 1 to 10080.

## Delivery guarantees

Jobs live in `automation_jobs`. A notification is written to `automation_outbox` **before** it is
shown.

| Property | Behaviour |
|---|---|
| Failed delivery | Retried later with bounded exponential backoff. |
| Delivered | Marked delivered and not intentionally sent again. |
| Crash after showing, before acknowledging | The notification can appear twice. |
| Overall | **At-least-once**, not exactly-once. |

A due reminder is shown on the console and, if `runtime.tts` is enabled and `spd-say` exists,
spoken as "Reminder #ID: message".

## Run without a microphone

A disconnected microphone should stop the voice agent without cancelling your timers. The
headless mode needs no microphone, no Cactus models and no Qwen:

```bash
# Long-running scheduler
alfred --config config.json --scheduler-only

# Inspect every automation, including paused and cancelled, without loading models
alfred --config config.json --automations

# Run one scheduler cycle by hand
alfred --config config.json --automation-tick
```

To keep it running in the background, install the scheduler service. See
[Deployment](deployment.md).

:::{tip}
The interactive voice process also runs its own scheduler. SQLite claims prevent the same job
from firing twice under normal concurrency, but for a single durable owner prefer the headless
scheduler service.
:::

## Live transcripts

Whistle prints one `Whistle: ` line in an interactive terminal and corrects only the changed
end of the text as speech arrives. Redirected output, such as the systemd journal, receives only
the **final** transcript. Unconfirmed text never triggers a tool. Alfred does not store audio. If
a service manager captures stdout, it may retain final utterances. Consider that for privacy.

## Hardware validation

Still to be checked on a Jetson: live transcription on a headset, `tegrastats` watch accuracy,
daylight-saving reminders, and recovery after restarts and reconnects.
